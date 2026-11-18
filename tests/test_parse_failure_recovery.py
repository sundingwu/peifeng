"""参数残缺直接执行、报错即结果；解析失败要留痕。"""
import json
import tempfile
import unittest
from pathlib import Path

from backend.agents.base import (
    BaseAgent,
    ExecutionResult,
    Plan,
    PlanStep,
    ToolBox,
    ToolCall,
)
from backend.orchestration.orchestrate import TaskOrchestrator, TaskState
from backend.utils.errors import ExecutionError


class TruncatedArgumentsTests(unittest.TestCase):
    def test_truncated_arguments_are_returned_as_a_result(self):
        """参数 JSON 没写完：不拦截、不做特殊处理，直接给一条带解析位置的错误回执。"""
        with tempfile.TemporaryDirectory() as folder:
            toolbox = ToolBox([{
                "name": "write",
                "description": "写文件",
                "parameters": {"type": "object", "properties": {"targets": {"type": "array"}}},
                "run": lambda targets=None: "ok",
            }])
            body, failed = toolbox.run("write", {"_raw": '{"targets":[{"path":"a.py","content":"x"'}, {})
            self.assertTrue(failed)
            self.assertIn("不是合法的完整JSON", body)
            self.assertIn("原文未执行", body)
            self.assertIn("解析位置", body)

    def test_one_missing_bracket_is_still_repaired(self):
        """一个括号的兜底保留：同一处错位能补回来，参数照常可用。"""
        from backend.agents.base import parse_arguments
        expected = {"targets": [{"path": "a.md", "content": "x"}]}
        raw = json.dumps(expected, ensure_ascii=False)
        broken = raw[:-2] + raw[-1]   # 丢一个 ] 换来一个 }
        self.assertEqual(parse_arguments(broken), expected)

    def test_unrepairable_arguments_keep_raw(self):
        """补不了就保持原文：不猜内容、不造残缺对象。"""
        from backend.agents.base import parse_arguments
        for unsafe in ('{"targets":[{"path":"a.py","content":"unfinished',
                       '{"targets":[{"path":"a.py","content":"x"'):
            self.assertEqual(parse_arguments(unsafe), {"_raw": unsafe})


class ParseFailureRecoveryTests(unittest.TestCase):
    def test_parse_failure_records_the_work_already_done(self):
        """解析失败但工具已经写过文件：这一步要记成 partial，不能留一片空白。"""
        class Worker:
            def __init__(self, workspace):
                self.workspace = workspace

            def clear_step(self, *_args):
                pass

            def execute(self, _request):
                (Path(self.workspace) / "out.md").write_text("done", encoding="utf-8")
                error = ExecutionError("Worker 输出无法解析：status Input should be 'completed'")
                error.records = [ToolCall(name="write",
                                          arguments={"targets": [{"path": "out.md", "content": "done"}]},
                                          result="已写入 out.md", is_error=False)]
                raise error

        with tempfile.TemporaryDirectory() as folder:
            runner = TaskOrchestrator(object(), {"engineer": Worker(folder)}, None)
            runner.runtime = {"workspace": folder}
            runner.task = TaskState.start(1, "写文件", "plan")
            runner.task.set_plan(Plan(steps=[PlanStep(goal="写文件", worker="engineer")]))

            result, review, _failures, interrupted = runner.run_step(runner.task.plan.steps[0], 0, 1)

            self.assertTrue(interrupted)
            self.assertFalse(review.approved)
            self.assertEqual(result.status, "partial")
            self.assertEqual(len(result.tool_calls), 1)
            self.assertIn("out.md", result.files)
            self.assertEqual(len(runner.task.steps[0].attempts), 3)

    def test_parse_failure_without_records_explains_itself(self):
        class Worker:
            def clear_step(self, *_args):
                pass

            def execute(self, _request):
                raise ExecutionError("Worker 输出无法解析：缺少 summary")

        runner = TaskOrchestrator(object(), {"engineer": Worker()}, None)
        runner.runtime = {"workspace": ""}
        runner.task = TaskState.start(1, "写文件", "plan")
        runner.task.set_plan(Plan(steps=[PlanStep(goal="写文件", worker="engineer")]))
        result, _review, _failures, interrupted = runner.run_step(runner.task.plan.steps[0], 0, 1)
        self.assertTrue(interrupted)
        self.assertEqual(result.status, "partial")
        self.assertIn("无法解析", result.summary + "；".join(result.limitations))


if __name__ == "__main__":
    unittest.main()
