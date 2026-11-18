import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from backend.agents.base import ExecutionResult, PlanStep, TaskDetail, TaskStep
from backend.utils.paths import Roots
from backend.memory.store import DetailStore, SummaryStore
from backend.tools.read import read


class SingleSessionLogTests(unittest.TestCase):
    def test_accepted_summary_is_persisted_before_next_step(self):
        from backend.agents.base import FinalAnswer, FinalRequest, Plan, ReviewResult
        from backend.orchestration.orchestrate import TaskOrchestrator
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            summaries = SummaryStore(root)
            session = summaries.create('session')
            records = DetailStore(root)
            runner = TaskOrchestrator(object(), {'study': object()}, None,
                                      summary_store=summaries, detail_store=records)
            runner.session.reset(session.meta)
            observed = []
            def execute(node, request):
                if node == 'mentor':
                    return FinalAnswer(content='done') if isinstance(request, FinalRequest) else ReviewResult(approved=True)
                if request.step.goal == 'second':
                    log = json.loads((root / 'session/summary.json').read_text(encoding='utf-8'))
                    observed.append(log['records']['1']['steps'][0]['summary'])
                return ExecutionResult(status='completed', summary='accepted marker')
            runner.run_node = execute
            result = runner.run_task('two steps', plan=Plan(steps=[
                PlanStep(worker='study', goal='first'), PlanStep(worker='study', goal='second')]))
            self.assertEqual(result.status, 'completed')
            self.assertEqual(observed, ['accepted marker'])

    def test_summary_updates_preserve_parallel_full_records_and_field_read(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            summaries = SummaryStore(root)
            session = summaries.create('session')
            records = DetailStore(root)
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda index: records.save_output('session', 1, index,
                    ExecutionResult(status='completed', summary=f'output-{index}', content=f'evidence-{index}')), range(4)))
            records.save('session', TaskDetail(task_seq=1, steps=[TaskStep(index=i, goal='step') for i in range(4)]))
            summaries.save(session)
            target = root / 'session/summary.json'
            payload = json.loads(target.read_text(encoding='utf-8'))
            self.assertEqual(len(payload['records']['1']['outputs']), 4)
            self.assertEqual(list((root / 'session').iterdir()), [target])
            result = read([{'path': 'summary.json', 'session': True,
                            'json_pointer': '/records/1/outputs/2/content'}],
                          roots=Roots(root, session_dir=root / 'session'))
            self.assertIn('evidence-2', result)
            self.assertNotIn('evidence-1', result)
            with self.assertRaises(ValueError):
                read([{'path': 'summary.json', 'session': True, 'json_pointer': '/records/missing'}],
                     roots=Roots(root, session_dir=root / 'session'))

    def test_replan_replaces_steps_and_drops_removed_ones(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            records = DetailStore(root)
            records.save_output('session', 2, 0, ExecutionResult(status='completed', summary='new'))
            records.save('session', TaskDetail(task_seq=2, steps=[TaskStep(index=0, goal='step'), TaskStep(index=1, goal='step')]))
            records.save('session', TaskDetail(task_seq=2, steps=[TaskStep(index=0, goal='step')]))
            self.assertIsNone(records.load_step('session', 2, 1))

    def test_corrupt_log_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / 'session/summary.json'
            target.parent.mkdir()
            target.write_text('{broken', encoding='utf-8')
            with self.assertRaises(ValueError):
                DetailStore(root).save_output('session', 1, 0, ExecutionResult(status='completed', summary='new'))
            self.assertEqual(target.read_text(encoding='utf-8'), '{broken')

    def test_worker_request_has_no_automatic_prior_step_history(self):
        from backend.orchestration.orchestrate import TaskState, build_request
        state = TaskState.start(1, 'research task', 'plan')
        request = build_request('step', state, step=PlanStep(goal='Read source.csv, write result.csv',
                                     expected_output='Check input hash and result values'), index=0, feedback='')
        self.assertEqual(request.previous_results, '')
        self.assertEqual(request.plan_outline, '')
        self.assertIn('source.csv', request.step.goal)

    def test_json_field_paging_and_project_summary_do_not_change_channel(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'summary.json').write_text(json.dumps({'values': ['first', 'second', 'third']}), encoding='utf-8')
            result = read([{'path': 'summary.json', 'json_pointer': '/values', 'offset': 2, 'limit': 1}],
                          roots=Roots(root, session_dir=root / 'other-session'))
            self.assertIn('first', result)
            self.assertNotIn('second', result)
            self.assertIn('offset=3', result)

