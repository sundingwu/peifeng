import json
import unittest

from backend.agents.base import (
    BUDGET_HINT,
    ExecutionResult,
    Plan,
    PlanStep,
    ResponseMessage,
    ReviewRequest,
    ReviewResult,
    StageReview,
    StageReviewRequest,
)
from backend.agents.base import BaseAgent
from backend.agents.mentor import parse_stage_review
from backend.orchestration.orchestrate import DEFAULT_EXTRA_BUDGET, TaskOrchestrator, TaskState


class _BudgetProbe(BaseAgent):
    """探针：模型每次都要调用工具，收尾调用（工具已关）时给出阶段性结果。"""

    name = "probe"

    def __init__(self, quota):
        super().__init__(llm=None, tool_defs=[{"type": "function", "function": {"name": "read"}}],
                         tool_runner=lambda name, arguments: ("ok", False), max_tool_rounds=20)
        self.quota = quota
        self.rounds = 0
        # 工具定义生效在 context 上（complete 读的是它）
        self.set_fixed_layer(tool_defs=self.tool_defs)

    def complete(self, *, interruptible: bool = True) -> ResponseMessage:
        self.rounds += 1
        if not self.context.tool_defs:
            return ResponseMessage(content=json.dumps(
                {"status": "partial", "summary": "已读一半", "limitations": ["还差后半段"]}, ensure_ascii=False))
        return ResponseMessage(content="继续读", tool_calls=[
            {"id": f"c{self.rounds}", "name": "read", "arguments": {"path": "a.txt"}, "type": "function"}])


def _parse_partial(text, records):
    data = json.loads(text)
    return ExecutionResult(status=data["status"], summary=data["summary"], tool_calls=list(records))


class ToolBudgetTests(unittest.TestCase):
    def test_plan_step_budget_defaults_to_unlimited(self):
        self.assertEqual(PlanStep(goal="x").tool_budget, 0)

    def test_turn_stops_when_budget_reached_and_flags_result(self):
        probe = _BudgetProbe(quota=3)
        result = probe.run_turn(parse=_parse_partial, retry_hint="", error_cls=RuntimeError,
                                exhausted=None, summarize_on_stop=True, tool_budget=3)
        self.assertTrue(probe.budget_exhausted)
        self.assertEqual(result.status, "partial")
        self.assertEqual(len(result.tool_calls), 3)
        self.assertLess(probe.rounds, 20)

    def test_no_flag_without_budget(self):
        probe = _BudgetProbe(quota=0)
        result = probe.run_turn(parse=_parse_partial, retry_hint="", error_cls=RuntimeError,
                                exhausted=None, summarize_on_stop=True, tool_budget=0)
        self.assertFalse(result.budget_exhausted)

    def test_parse_stage_review_actions(self):
        self.assertEqual(parse_stage_review('{"action": "stop", "comment": "够了"}').action, "stop")
        cont = parse_stage_review('{"action": "continue", "hint": "改读 b.txt", "extra_budget": 5}')
        self.assertEqual((cont.hint, cont.extra_budget), ("改读 b.txt", 5))
        adjust = parse_stage_review(json.dumps(
            {"action": "adjust", "plan_update": {"steps": [{"goal": "换路子", "worker": "engineer"}]}},
            ensure_ascii=False))
        self.assertIsNotNone(adjust.plan_update)
        self.assertEqual(adjust.plan_update.steps[0].goal, "换路子")

    def test_parse_stage_review_rejects_inconsistent_payload(self):
        with self.assertRaisesRegex(Exception, "plan_update"):
            parse_stage_review('{"action": "adjust"}')
        with self.assertRaisesRegex(Exception, "action"):
            parse_stage_review('{"action": "maybe"}')
        with self.assertRaisesRegex(Exception, "只有 action=adjust"):
            parse_stage_review(json.dumps(
                {"action": "continue", "plan_update": {"steps": [{"goal": "x", "worker": "engineer"}]}},
                ensure_ascii=False))

    def test_execute_step_continues_with_extra_budget_after_stage_review(self):
        """配额用尽 -> Mentor 说继续并追加配额 -> 这一步用新配额再跑一次。"""

        class Worker:
            def __init__(self):
                self.budgets = []

            def clear_step(self, *_args):
                pass

            def execute(self, request):
                self.budgets.append(request.step.tool_budget)
                if len(self.budgets) == 1:
                    return ExecutionResult(status="partial", summary="读了一半", budget_exhausted=True)
                return ExecutionResult(status="completed", summary="读完了")

        worker = Worker()
        runner = TaskOrchestrator(object(), {"engineer": worker}, None)
        runner.runtime = {"workspace": ""}
        runner.task = TaskState.start(1, "read", "plan")
        runner.task.set_plan(Plan(steps=[PlanStep(goal="read", worker="engineer", tool_budget=3)]))
        stages = iter([(StageReview(action="continue", hint="接着读 b.txt", extra_budget=4), [])])

        real_dispatch = runner.dispatch_node

        def fake_dispatch(node, request):
            if isinstance(request, StageReviewRequest):
                return next(stages)
            if isinstance(request, ReviewRequest):
                return ReviewResult(approved=True, goal_check="ok", comment="通过"), []
            return real_dispatch(node, request)

        runner.dispatch_node = fake_dispatch
        result, review, _failures, interrupted = runner.run_step(runner.task.plan.steps[0], 0, 1)
        self.assertEqual(worker.budgets, [3, 7])
        self.assertFalse(interrupted)
        self.assertTrue(review.approved)
        self.assertEqual(result.status, "completed")

    def test_execute_step_adjust_replaces_remaining_plan(self):
        class Worker:
            def __init__(self):
                self.goals = []

            def clear_step(self, *_args):
                pass

            def execute(self, request):
                self.goals.append(request.step.goal)
                if len(self.goals) == 1:
                    return ExecutionResult(status="partial", summary="路走不通", budget_exhausted=True)
                return ExecutionResult(status="completed", summary="换路子做完")

        worker = Worker()
        runner = TaskOrchestrator(object(), {"engineer": worker}, None)
        runner.runtime = {"workspace": ""}
        runner.task = TaskState.start(1, "read", "plan")
        runner.task.set_plan(Plan(steps=[
            PlanStep(goal="老路子", worker="engineer", tool_budget=2),
            PlanStep(goal="收尾", worker="engineer"),
        ]))
        stages = iter([(StageReview(action="adjust", plan_update=Plan(steps=[
            PlanStep(goal="新路子", worker="engineer", tool_budget=5)])), [])])

        real_dispatch = runner.dispatch_node

        def fake_dispatch(node, request):
            if isinstance(request, StageReviewRequest):
                return next(stages)
            if isinstance(request, ReviewRequest):
                return ReviewResult(approved=True, goal_check="ok", comment="通过"), []
            return real_dispatch(node, request)

        runner.dispatch_node = fake_dispatch
        result, _review, _failures, interrupted = runner.run_step(runner.task.plan.steps[0], 0, 2)
        self.assertFalse(interrupted)
        self.assertEqual(worker.goals, ["老路子", "新路子"])
        self.assertEqual(runner.task.plan.steps[0].goal, "新路子")
        self.assertEqual(result.summary, "换路子做完")

    def test_stage_review_stop_marks_step_interrupted(self):
        class Worker:
            def clear_step(self, *_args):
                pass

            def execute(self, _request):
                return ExecutionResult(status="partial", summary="卡住了", budget_exhausted=True)

        runner = TaskOrchestrator(object(), {"engineer": Worker()}, None)
        runner.runtime = {"workspace": ""}
        runner.task = TaskState.start(1, "read", "plan")
        runner.task.set_plan(Plan(steps=[PlanStep(goal="read", worker="engineer", tool_budget=2)]))
        def fake_dispatch(_node, request):
            if isinstance(request, StageReviewRequest):
                return StageReview(action="stop", comment="方向错了"), []
            return ExecutionResult(status="partial", summary="卡住了", budget_exhausted=True), []

        runner.dispatch_node = fake_dispatch
        _result, review, _failures, interrupted = runner.run_step(runner.task.plan.steps[0], 0, 1)
        self.assertTrue(interrupted)
        self.assertFalse(review.approved)
        self.assertIn("方向错了", review.comment)

    def test_default_extra_budget_is_positive(self):
        self.assertGreater(DEFAULT_EXTRA_BUDGET, 0)
        self.assertIn("配额用尽", BUDGET_HINT)
