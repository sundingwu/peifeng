"""规划协议：没有直接回答这条路，澄清只允许一次，之后必须给计划或说做不了。"""
from __future__ import annotations

import json
import unittest

from backend.agents.mentor import parse_planning_result
from backend.orchestration.orchestrate import SessionState, TaskOrchestrator, TaskState

CLARIFY = {"mode": "clarify", "clarify": {"questions": [{"question": "要什么图？", "options": []}]}}
PLAN = {
    "mode": "plan",
    "requirement": "画一张柱状图",
    "output_name": "chart.png",
    "steps": [{"worker": "visualizer", "goal": "画柱状图", "expected_output": "chart.png"}],
}


class Clarifier:
    """收集问题，固定回答：测试只关心问了几次、问了什么。"""

    def __init__(self):
        self.asked: list[list[dict]] = []

    def ask_clarification(self, questions):
        self.asked.append(list(questions))
        return ["按默认" for _ in questions]


def build_orchestrator(responses, requests=None):
    """只装配 request_plan 需要的字段：节点调度用队列模拟。"""
    runner = object.__new__(TaskOrchestrator)
    runner.task = TaskState.start(1, "画一张柱状图", "plan_and_execute")
    runner.session = SessionState()
    runner.check_cancel = lambda: None
    runner.clarifier = Clarifier()
    runner.workers = {"visualizer": object()}
    runner.listener = None
    queue = list(responses)

    def dispatch(node, request):
        if requests is not None:
            requests.append(request)
        return (queue.pop(0), [])

    runner.dispatch_node = dispatch
    return runner


class PlanningProtocolTests(unittest.TestCase):
    def test_direct_mode_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "plan / clarify / unable"):
            parse_planning_result(json.dumps({"mode": "direct", "answer": "你好"}))

    def test_short_answer_must_be_a_plan(self):
        result = parse_planning_result(json.dumps(PLAN))
        self.assertEqual(result.mode, "plan")
        self.assertEqual(result.plan.steps[0].worker, "visualizer")

    def test_single_clarification_then_plan(self):
        runner = build_orchestrator(
            [parse_planning_result(json.dumps(CLARIFY)), parse_planning_result(json.dumps(PLAN))]
        )
        result = runner.request_plan()
        self.assertEqual(result.mode, "plan")
        self.assertEqual(len(runner.clarifier.asked), 1)
        self.assertTrue(any("要什么图" in item for item in runner.task.user_decisions))

    def test_unknown_worker_is_sent_back_for_replanning(self):
        bad = parse_planning_result(json.dumps({
            "mode": "plan",
            "requirement": "画一张柱状图",
            "output_name": "chart.png",
            "steps": [{"worker": "chart", "goal": "画柱状图", "expected_output": "chart.png"}],
        }))
        requests: list = []
        runner = build_orchestrator([bad, parse_planning_result(json.dumps(PLAN))], requests)
        result = runner.request_plan()
        self.assertEqual(result.mode, "plan")
        self.assertEqual(result.plan.steps[0].worker, "visualizer")
        # 退回给模型的是"上一步错在哪"，不是悄悄换一个默认 worker
        self.assertIn("chart", requests[1].plan_feedback)
        self.assertIn("visualizer", requests[1].plan_feedback)

    def test_missing_worker_becomes_unable_after_retries(self):
        bad = parse_planning_result(json.dumps({
            "mode": "plan",
            "requirement": "画一张柱状图",
            "steps": [{"goal": "画柱状图"}],
        }))
        runner = build_orchestrator([bad, bad, bad])
        result = runner.request_plan()
        self.assertEqual(result.mode, "unable")
        self.assertIn("没有指定 worker", result.reason)

    def test_second_clarification_becomes_unable(self):
        clarify = parse_planning_result(json.dumps(CLARIFY))
        runner = build_orchestrator([clarify, clarify])
        result = runner.request_plan()
        self.assertEqual(result.mode, "unable")
        self.assertIn("要什么图", result.reason)
        self.assertEqual(len(runner.clarifier.asked), 1)

    def test_unable_finishes_without_plan(self):
        unable = parse_planning_result(json.dumps({"mode": "unable", "reason": "缺 PDF 原件"}))
        runner = build_orchestrator([unable])
        result = runner.request_plan()
        self.assertEqual(result.mode, "unable")
        self.assertEqual(result.reason, "缺 PDF 原件")


if __name__ == "__main__":
    unittest.main()
