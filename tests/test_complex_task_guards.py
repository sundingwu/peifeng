import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from backend.agents.base import ExecutionResult, Plan, PlanStep
from backend.orchestration.orchestrate import TaskOrchestrator, TaskState


class ComplexTaskGuardTests(unittest.TestCase):
    def test_delivery_list_excludes_nonexistent_cancelled_file(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'rules.md').write_text('new', encoding='utf-8')
            runner = TaskOrchestrator(object(), {'engineer': object()}, None)
            runner.runtime = {'workspace': folder}
            runner.task.step_results = [ExecutionResult(status='completed',
                summary='done', files=['rules.md', 'summary.md', '../outside.md'])]
            self.assertEqual(runner.collect_files(), ['rules.md'])

    def test_changed_user_delivery_supersedes_original_filename(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'rules.md').write_text('new rules', encoding='utf-8')
            runner = TaskOrchestrator(object(), {'engineer': object()}, None)
            runner.runtime = {'workspace': folder}
            runner.task = TaskState.start(1, 'summary', 'plan')
            runner.task.set_plan(Plan(output_name='summary.md', steps=[
                PlanStep(goal='new rules', worker='engineer', expected_output='rules.md')]))
            runner.task.user_decisions.append('不再生成summary.md，改成rules.md')
            self.assertEqual(runner.finalize_task('rules ready').status, 'completed')

    def test_delivery_label_parentheses_are_not_part_of_filename(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'cleaned.csv').write_text('value\n1\n', encoding='utf-8')
            runner = TaskOrchestrator(object(), {'engineer': object()}, None)
            runner.runtime = {'workspace': folder}
            runner.task = TaskState.start(1, 'data', 'plan')
            runner.task.output_name = '实验日志审查交付包（cleaned.csv）'
            self.assertEqual(runner.finalize_task('ready').status, 'completed')

    def test_local_repair_retains_unfinished_figure_and_document(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'data.csv').write_text('value\n1\n', encoding='utf-8')
            runner = TaskOrchestrator(object(), {'engineer': object()}, None)
            runner.runtime = {'workspace': folder}
            runner.task = TaskState.start(1, 'data and report', 'plan')
            plan = Plan(steps=[PlanStep(worker='engineer', goal='data', expected_output='data.csv'),
                               PlanStep(worker='engineer', goal='figure', expected_output='result.png'),
                               PlanStep(worker='engineer', goal='report', expected_output='report.docx')])
            runner.task.set_plan(plan)
            repair = Plan(steps=[PlanStep(worker='engineer', goal='repair evidence', expected_output='evidence.md')])
            merged = runner.update_remaining_plan(plan, 0, repair)
            self.assertEqual([step.expected_output for step in merged.steps],
                             ['evidence.md', 'result.png', 'report.docx'])

    def test_missing_declared_delivery_cannot_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            runner = TaskOrchestrator(object(), {'engineer': object()}, None)
            runner.runtime = {'workspace': folder}
            runner.task = TaskState.start(1, 'write report', 'plan')
            runner.task.output_name = 'report.docx'
            result = runner.finalize_task('report is ready')
            self.assertEqual(result.status, 'failed')
            self.assertIn('report.docx', result.content)
            self.assertNotIn('report is ready', result.content)

    def test_parallel_same_worker_checkpoints_preserve_each_context(self):
        runner = TaskOrchestrator(object(), {'engineer': object()}, None)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda index: runner.notify('context_checkpoint', {
                'agent': 'engineer', 'context_id': f'1:{index}',
                'messages': [{'role': 'tool', 'content': f'proof-{index}'}]}), range(2)))
        self.assertEqual({item['context_id'] for item in runner.interruption_log}, {'1:0', '1:1'})
