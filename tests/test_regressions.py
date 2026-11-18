"""Offline regression checks for tool contracts and evidence preservation."""
from document_cli_support import run_document, run_figure
import json
import re
import tempfile
import unittest
import shutil
import hashlib
from pathlib import Path

from config import Settings
from backend.orchestration.orchestrate import build_registry
from backend.agents.base import MessageContext
from backend.agents.worker import produced_files
from backend.agents.base import ToolCall
from backend.tools.write import write
from backend.tools.move import move
from backend.tools.delete import delete
from backend.utils.web import run_targets


class RegressionTests(unittest.TestCase):
    def test_final_answer_rejects_review_envelope_and_retries(self):
        from types import SimpleNamespace
        from backend.agents.mentor import parse_final_answer
        from backend.agents.base import BaseAgent
        from backend.utils.errors import PlanningError
        review = '{"approved":true,"goal_check":"done","complete":true}'
        with self.assertRaises(ValueError):
            parse_final_answer(review)
        with self.assertRaises(ValueError):
            parse_final_answer('```json\n' + review + '\n```')
        self.assertEqual(parse_final_answer('{"owner":"project-A"}').content,
                         '{"owner":"project-A"}')
        context = MessageContext()
        fake = SimpleNamespace(context=context, complete=lambda: SimpleNamespace(parsed={
            'stop_reason': 'stop', 'content': 'A班2人，中位数85.00。', 'tool_calls': []}))
        answer = BaseAgent.parse_with_retry(fake, review, lambda text, records: parse_final_answer(text),
                                           '请直接回答用户', PlanningError, [])
        self.assertEqual(answer.content, 'A班2人，中位数85.00。')

    def test_same_named_sessions_are_isolated_by_project(self):
        from backend.server.server import EventStream
        stream = EventStream()
        stream.set_project('project-a')
        stream.set_session('会话1')
        stream.publish('task_received', {'requirement': 'A task'})
        stream.request_input({'kind': 'clarify'}, session='会话1')
        _, subscriber = stream.subscribe(session='会话1', project='project-a')
        stream.set_project('project-b')
        stream.publish('task_received', {'requirement': 'B task'})
        self.assertTrue(subscriber.empty())
        self.assertEqual([event['data']['requirement'] for event in stream.trace()
                          if event['kind'] == 'task_received'], ['B task'])
        self.assertEqual(stream.pending('会话1'), [])
        replay, _ = stream.subscribe(session='会话1', project='project-b')
        self.assertTrue(all(event['project'] == 'project-b' for event in replay))
        stream.set_project('project-a')
        self.assertEqual(len(stream.pending('会话1')), 1)

    def test_all_continuous_summary_fields_respect_requested_precision(self):
        from backend.utils.skills import load_skill_script
        module = load_skill_script('engineer', 'data-analysis', 'summarize_table')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'input.csv').write_text('class,score\nA,80\nA,90\n', encoding='utf-8')
            module.run([{'path': 'input.csv', 'group': 'class', 'value': 'score',
                                    'metrics': ['count', 'median', 'min', 'max', 'sample_stddev'],
                                    'precision': 2, 'output': 'summary.csv'}], base_dir=root)
            import csv
            with (root / 'summary.csv').open(encoding='utf-8') as stream:
                row = list(csv.DictReader(stream))[0]
            self.assertEqual(row, {'class': 'A', 'count': '2', 'median': '85.00',
                                   'min': '80.00', 'max': '90.00', 'sample_stddev': '7.07'})

    def test_artifact_delivery_preserves_paths_and_uses_runner_workspace(self):
        from types import SimpleNamespace
        from backend.server.server import AppEntry
        from backend.agents.base import TaskResult, ExecutionResult
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder) / 'running-project'
            viewed = Path(folder) / 'viewed-project'
            (project / 'nested').mkdir(parents=True)
            viewed.mkdir()
            artifact = project / 'nested/result.md'
            artifact.write_text('running project result', encoding='utf-8')
            (project / 'result.md').write_text('different root result', encoding='utf-8')
            self.assertEqual(AppEntry.artifact_paths([str(artifact), 'result.md', '../outside.md'], project),
                             ['nested/result.md', 'result.md'])
            app = AppEntry.__new__(AppEntry)
            app.workspace = viewed
            published = []
            app.events = SimpleNamespace(publish_for=lambda *args: published.append(args))
            runner = SimpleNamespace(title='test', work_dir=project / 'internal',
                                     orchestrator=SimpleNamespace(runtime={'workspace': str(project)}))
            final = TaskResult(status='completed', content='done',
                               step_results=[ExecutionResult(status='completed', files=[str(artifact)])])
            app.deliver(runner, final)
            self.assertEqual(published[0][2]['files'], ['nested/result.md'])
            self.assertEqual(list(viewed.iterdir()), [])

    def test_task_finalization_preserves_reproduction_and_evidence_files(self):
        from backend.orchestration.orchestrate import TaskOrchestrator
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            paths = ['internal/reproduce_helper.py', 'internal/recipe.json',
                     'internal/run.log', 'internal/spill/peifeng-output-context-evidence.txt',
                     'internal/tmp/disposable.txt']
            for name in paths:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('evidence', encoding='utf-8')
            orchestrator = TaskOrchestrator.__new__(TaskOrchestrator)
            orchestrator.runtime = {'workspace': str(root)}
            orchestrator.started_at = 0
            removed = orchestrator.clean_task_files()
            self.assertEqual(removed, ['internal/tmp/disposable.txt'])
            self.assertTrue(all((root / name).is_file() for name in paths[:-1]))

    def test_spill_archives_remain_readable_and_survive_cleanup(self):
        from backend.tools.read import read
        from backend.tools.delete import remove
        from backend.agents.base import spill_output
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            spill_output('read', 'prefix ' * 1200 + 'CRITICAL_EVIDENCE' + ' suffix' * 1200, workspace=root)
            archives = list((root / 'internal/spill').glob('peifeng-output-*.txt'))
            self.assertTrue(archives)
            receipt = read(targets=[{'path': str(archives[0]), 'grep': 'CRITICAL_EVIDENCE'}], base_dir=root)
            self.assertIn('CRITICAL_EVIDENCE', receipt)
            with self.assertRaisesRegex(RuntimeError, '系统证据归档'):
                remove('internal', True, False, root)
            self.assertTrue(archives[0].is_file())
            (root / 'internal/temporary.txt').write_text('disposable', encoding='utf-8')
            remove('internal/temporary.txt', False, False, root)
            self.assertFalse((root / 'internal/temporary.txt').exists())

    @unittest.skipUnless(__import__('os').name == 'nt', 'Windows TLS initialization')
    def test_tool_subprocess_can_initialize_tls(self):
        import subprocess
        import sys
        from backend.tools.run_code import runner_env
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run(
                [sys.executable, '-c', 'import ssl; ssl.create_default_context()'],
                env=runner_env(folder), capture_output=True, text=True, timeout=20,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_final_role_tools_and_skills(self):
        registry = build_registry(Settings(), object())
        self.assertEqual(set(registry.nodes), {'mentor', 'researcher', 'engineer', 'visualizer', 'writer'})
        self.assertEqual(registry.nodes['mentor'].toolbox.names, ['read'])
        expected = {'researcher': set(), 'engineer': set(), 'visualizer': set(), 'writer': set()}
        shared = {'read', 'write', 'run_code', 'delete', 'move'}
        for name, tools in expected.items():
            self.assertEqual(set(registry.nodes[name].toolbox.names), tools | shared)
        self.assertTrue(all(node.skills for node in registry.nodes.values()))
        self.assertFalse(registry.errors)
        # worker 清单只留名字 + 职责：技能与工具由各角色自己装配
        index = registry.nodes['mentor'].extra_system
        researcher_section = index.split('- researcher：', 1)[1].split('\n- ', 1)[0]
        self.assertIn('阅读论文', researcher_section)
        self.assertNotIn('citation-management', researcher_section)
        # 工具与技能在同一段，技能读取说明在固定层最后
        system = registry.nodes['researcher'].system_prompt
        self.assertIn('【可用工具与技能】', system)
        self.assertIn('citation-management', system)
        self.assertNotIn('【技能读取】', system)
        self.assertIn('【技能读取】', registry.nodes['researcher'].system_tail)
        # 通用规则并进了角色提示词，不再是独立段
        self.assertIn('## 通用规则', system)
        self.assertNotIn('<通用规则>', system)

    def test_mentor_rechecks_current_scope_without_inheriting_rejected_plan(self):
        from backend.agents.base import ReviewRequest, ReviewResult, ReviewFinding, ExecutionResult, PlanStep
        mentor = build_registry(Settings(), object()).nodes['mentor']
        old = ReviewResult(approved=False, goal_check='unchanged', findings=[ReviewFinding(severity='blocking', criterion='invented coverage', location='report', evidence='OLD_ERROR_REQUIREMENT', correction='add every group')])
        request = ReviewRequest(task='Check existing claims only', step=PlanStep(worker='writer', goal='Correct content'), result=ExecutionResult(status='completed', summary='Current file ready', files=['report.docx']), previous_review=old)
        text = mentor.build_request(request)
        self.assertIn('Check existing claims only', text)
        self.assertIn('report.docx', text)
        self.assertNotIn('OLD_ERROR_REQUIREMENT', text)
        self.assertNotIn('add every group', text)

    def test_record_read_preserves_source_keys_physical_rows_and_sheet(self):
        from backend.tools.read import read
        from openpyxl import Workbook
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'part.csv').write_text('course,run,value\nchemistry,B,8\n', encoding='utf-8')
            workbook = Workbook()
            workbook.active.title = 'audit'
            workbook.active.append(['course', 'run', 'value'])
            workbook.active.append(['chemistry', 'C', 6])
            workbook.save(root / 'part.xlsx')
            text = read([{'path': 'part.csv', 'view': 'records'}, {'path': 'part.xlsx', 'view': 'records'}], base_dir=root)
            self.assertIn('"run": "B", "value": "8"', text)
            self.assertIn('"run": "C", "value": 6', text)
            self.assertEqual(text.count('"row": 2'), 2)
            self.assertIn('audit', text)
            self.assertEqual(text.count('SHA256'), 2)

    def test_bash_uses_application_python_and_research_metadata_is_scoped(self):
        import sys
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            runner = build_registry(Settings(), object()).nodes['researcher'].toolbox.runner(workspace=folder)
            runner('write', {'targets': [{'path': 'which.py', 'content': 'import sys\nprint(sys.executable)\n'}]})
            text, failed = runner('run_code', {'targets': [{"file_path": "which.py"}]})
            self.assertFalse(failed, text)
            self.assertIn(str(Path(sys.executable)), text)
        record = {'doi': '10.example/test', 'title': ['Title'], 'authors': ['Author'], 'published': [[2020, 1, 2]], 'source_url': 'https://example.org', 'retrieved_at': '2026-11-11'}
        from backend.utils.skills import load_skill_script
        module=load_skill_script('researcher','paper-download','doi')
        with patch.object(module,'lookup',return_value=[record]):
            output=json.loads(module.literature([{'doi':'10.example/test'}]))
        self.assertIn('Title',json.dumps(output))
        with self.assertRaises(ValueError):
            load_skill_script('researcher','literature-search','research_web').process({'doi':'10.example/test'},Path('.'))

    def test_fetch_preserves_readable_lines_without_json_string_escaping(self):
        from unittest.mock import patch
        from backend.utils.skills import load_skill_script
        module=load_skill_script('researcher','literature-search','research_web')
        with patch.object(module, 'retrieve', return_value={'content':'Title\nAuthors\nYear','source_level':'public_page_text'}):
            result=module.process({'action':'fetch','url':'https://python.org'},Path('.'))
        self.assertIn('Title\nAuthors\nYear',result['evidence'].replace('\\n','\n'))

    def test_document_render_records_real_source_and_compiles_sibling_resources(self):
        from pypdf import PdfWriter
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pdf = PdfWriter()
            pdf.add_blank_page(width=300, height=200)
            with (root / 'sample.pdf').open('wb') as stream:
                pdf.write(stream)
            runner = build_registry(Settings(), object()).nodes['writer'].toolbox.runner(workspace=root, session_dir=root / 'internal')
            result, failed = run_document(runner, root, {'targets': [{'kind': 'render', 'path': 'sample.pdf'}]})
            self.assertFalse(failed, result)
            record = json.loads(next((root / 'internal/render').glob('*/render.json')).read_text(encoding='utf-8'))
            self.assertEqual(Path(record['source']), root / 'sample.pdf')
            self.assertEqual(record['source_sha256'], hashlib.sha256((root / 'sample.pdf').read_bytes()).hexdigest())
            (root / 'body.tex').write_text('Sibling resource works.', encoding='utf-8')
            (root / 'main.tex').write_text(r'\documentclass{article}\begin{document}\input{body}\end{document}', encoding='utf-8')
            result, failed = run_document(runner, root, {'targets': [{'kind': 'latex', 'path': 'main.tex'}]})
            self.assertTrue(failed, result)
            self.assertFalse((root / 'main.pdf').exists())

    def test_document_and_plot_batch_entrypoints_preserve_individual_results(self):
        from docx import Document
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            registry = build_registry(Settings(), object())
            writer = registry.nodes['writer'].toolbox.runner(workspace=root)
            result, failed = run_document(writer, root, {'targets': [{'kind': 'docx', 'file': 'a.docx', 'blocks': [{'type': 'paragraph', 'text': 'first'}]}, {'kind': 'docx', 'file': 'b.docx', 'blocks': [{'type': 'paragraph', 'text': 'second'}]}]})
            self.assertFalse(failed, result)
            self.assertEqual(Document(root / 'a.docx').paragraphs[0].text, 'first')
            self.assertEqual(Document(root / 'b.docx').paragraphs[0].text, 'second')
            original = (root / 'a.docx').read_bytes()
            result, failed = run_document(writer, root, {'targets': [{'kind': 'docx', 'file': 'a.docx', 'overwrite': True, 'blocks': [{'type': 'title', 'text': ''}]}]})
            self.assertTrue(failed, result)
            self.assertEqual((root / 'a.docx').read_bytes(), original)
            visualizer = registry.nodes['visualizer'].toolbox.runner(workspace=root)
            result, failed = run_figure(visualizer, root, {'targets': [{'kind': 'bar', 'output': 'bar.png', 'categories': ['A', 'B'], 'series': [{'name': 'score', 'values': [2, 4]}]}, {'kind': 'line', 'output': 'line.png', 'categories': ['A', 'B'], 'series': [{'name': 'score', 'values': [2, 4]}]}]})
            self.assertFalse(failed, result)
            self.assertTrue((root / 'bar.png').is_file())
            self.assertTrue((root / 'line.png').is_file())

    def test_mentor_reads_current_office_content_structure_and_version(self):
        from docx import Document
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            document = Document()
            document.add_paragraph('retrieval has 9 records')
            document.save(root / 'report.docx')
            runner = build_registry(Settings(), object()).nodes['mentor'].toolbox.runner(workspace=root)
            text, failed = runner('read', {'targets': [{'path': 'report.docx'}, {'path': 'report.docx', 'view': 'structure'}]})
            self.assertFalse(failed, text)
            self.assertIn('retrieval has 9 records', text)
            self.assertIn(hashlib.sha256((root / 'report.docx').read_bytes()).hexdigest(), text)
            text, failed = runner('run_code', {'targets': [{"file_path": "echo", "args": ["unexpected"]}]})
            self.assertTrue(failed)
            self.assertIn('未知工具 run_code', text)

    def test_mentor_sees_prior_task_summaries_without_old_worker_details(self):
        from backend.orchestration.orchestrate import TaskOrchestrator
        from backend.agents.base import PlanningRequest, PlanningResult, Plan, PlanStep, ExecutionResult, ReviewResult
        runner = TaskOrchestrator(object(), {'study': object()}, None)
        histories, worker_requests = [], []
        def execute(node, request):
            if isinstance(request, PlanningRequest):
                histories.append(request.prior_summary)
                return PlanningResult(mode='plan', plan=Plan(requirement=request.task, steps=[PlanStep(goal='study', worker='study')]))
            if node == 'study':
                worker_requests.append(request)
                return ExecutionResult(status='completed', summary='ACCEPTED_TASK_SUMMARY', content='PRIVATE_WORKER_DETAIL')
            return ReviewResult(approved=True, goal_check='all done', complete=True)
        runner.run_node = execute
        runner.run_task('first task')
        runner.run_task('second task')
        self.assertIn('first task', histories[1])
        self.assertIn('ACCEPTED_TASK_SUMMARY', histories[1])
        self.assertNotIn('PRIVATE_WORKER_DETAIL', histories[1])
        self.assertNotEqual(worker_requests[0].context_id, worker_requests[1].context_id)
        self.assertFalse(worker_requests[1].continuation)
        self.assertEqual(runner.task.plan_history, [])

    def test_summary_keeps_detail_index_without_tool_bodies(self):
        from backend.orchestration.orchestrate import TaskState, SessionState, build_task_summary, build_session_summary
        from backend.agents.base import Plan, PlanStep, ExecutionResult
        from backend.memory.store import DetailStore
        state = TaskState.start(1, 'original request', 'plan')
        state.set_plan(Plan(steps=[PlanStep(goal='task', worker='engineer')]))
        state.accept_step(0, ExecutionResult(status='completed', summary='short accepted summary'), artifact='PRIVATE_ARTIFACT_BODY')
        state.status = 'failed'
        state.plan_history.append({'replaced': 'OLD_FAILED_ATTEMPT_DETAIL'})
        summary = build_task_summary(state)
        summary.interruption_log = [{'role': 'tool', 'content': 'PRIVATE_TOOL_BODY'}]
        session = SessionState(tasks=[summary])
        text = build_session_summary(session)
        self.assertIn('short accepted summary', text)
        self.assertIn('summary.json#/records/1', text)
        self.assertNotIn('PRIVATE_ARTIFACT_BODY', text)
        self.assertNotIn('PRIVATE_TOOL_BODY', text)
        self.assertEqual(summary.steps[0].deliverable, '')
        with tempfile.TemporaryDirectory() as folder:
            store = DetailStore(Path(folder))
            detail = state.to_detail()
            detail.interruption_log = summary.interruption_log
            store.save('session', detail)
            archived = json.loads((store.folder_for('session') / 'summary.json').read_text(encoding='utf-8'))['records']['1']
            self.assertIn('OLD_FAILED_ATTEMPT_DETAIL', json.dumps(archived))
            self.assertIn('PRIVATE_TOOL_BODY', json.dumps(archived))

    def test_student_rework_retains_context_and_new_steps_are_fresh(self):
        from unittest.mock import patch
        from backend.agents.worker import WorkerAgent
        from backend.agents.base import ResponseMessage
        from backend.agents.base import ExecutionRequest, PlanStep
        worker = WorkerAgent(object(), name='engineer')
        worker.tool_runner = lambda name, arguments: ('written', False)
        captured = []
        def complete(agent, **kwargs):
            captured.append(json.dumps(agent.context.to_llm_messages(), ensure_ascii=False))
            if len(captured) % 2:
                return ResponseMessage(tool_calls=[{'id': str(len(captured)), 'name': 'write', 'arguments': {'targets': [{'path': 'out.txt', 'content': 'done'}]}}])
            return ResponseMessage(content='{"status":"completed","summary":"done","files":["out.txt"]}')
        with patch.object(WorkerAgent, 'complete', complete):
            first = ExecutionRequest(context_id='task1:step0', step=PlanStep(goal='FIRST_PRIVATE_STEP'))
            worker.execute(first)
            worker.execute(first.model_copy(update={'continuation': True, 'feedback': 'FIX_ONE_SENTENCE'}))
            self.assertIn('FIRST_PRIVATE_STEP', captured[2])
            self.assertIn('FIX_ONE_SENTENCE', captured[2])
            worker.execute(ExecutionRequest(context_id='task1:step1', step=PlanStep(goal='SECOND_STEP')))
            self.assertNotIn('FIRST_PRIVATE_STEP', captured[4])
            self.assertNotIn('FIX_ONE_SENTENCE', captured[4])
            worker.clear_step('', 'task1:step0')
            self.assertNotIn(':task1:step0', worker.step_contexts)

    def test_mentor_receives_summaries_and_can_replace_remaining_plan(self):
        from backend.orchestration.orchestrate import TaskOrchestrator
        from backend.agents.base import Plan, PlanStep, ExecutionResult, ReviewResult, ReviewRequest
        from backend.agents.mentor import MentorAgent
        runner = TaskOrchestrator(object(), {'study': object()}, None)
        executed, reviews = [], []
        def execute(node, request):
            if node == 'study':
                executed.append(request.step.goal)
                return ExecutionResult(status='completed', summary='accepted ' + request.step.goal)
            reviews.append(request)
            if len(reviews) == 1:
                return ReviewResult(approved=True, goal_check='change remaining route', plan_update=Plan(steps=[PlanStep(goal='NEW_ROUTE', worker='study')]))
            return ReviewResult(approved=True, goal_check='all requirements fulfilled', complete=True)
        runner.run_node = execute
        result = runner.run_task('user goal', plan=Plan(steps=[PlanStep(goal='FIRST', worker='study'), PlanStep(goal='OLD_ROUTE', worker='study')]))
        self.assertEqual(result.status, 'completed')
        self.assertEqual(executed, ['FIRST', 'NEW_ROUTE'])
        self.assertIn('accepted FIRST', reviews[1].completed_summary)
        self.assertIn('NEW_ROUTE', reviews[1].plan_outline)
        self.assertTrue(runner.task.plan_history)
        self.assertIn('OLD_ROUTE', json.dumps(runner.task.plan_history))
        request = ReviewRequest(task='goal', prior_summary='OLD_TASK_SUMMARY', completed_summary='ACCEPTED_STEP_SUMMARY', artifact='PRIVATE_DETAIL_NOT_AUTO_INCLUDED', step=PlanStep(goal='review'), result=ExecutionResult(status='completed', summary='ready'))
        text = MentorAgent(object()).build_request(request)
        self.assertIn('OLD_TASK_SUMMARY', text)
        self.assertIn('ACCEPTED_STEP_SUMMARY', text)
        self.assertNotIn('PRIVATE_DETAIL_NOT_AUTO_INCLUDED', text)

    def test_mentor_can_replan_a_blocked_step_without_accepting_it(self):
        from backend.orchestration.orchestrate import TaskOrchestrator
        from backend.agents.base import Plan, PlanStep, ExecutionResult, ReviewResult
        runner = TaskOrchestrator(object(), {'study': object()}, None)
        def execute(node, request):
            if node == 'study':
                return ExecutionResult(status='partial', summary='network unavailable', limitations=['offline']) if request.step.goal == 'ONLINE' else ExecutionResult(status='completed', summary='local material used')
            if request.result.status == 'partial':
                return ReviewResult(approved=False, goal_check='switch to local input', plan_update=Plan(steps=[PlanStep(goal='LOCAL', worker='study')]))
            return ReviewResult(approved=True, goal_check='done', complete=True)
        runner.run_node = execute
        result = runner.run_task('study', plan=Plan(steps=[PlanStep(goal='ONLINE', worker='study')]))
        self.assertEqual(result.status, 'completed')
        self.assertEqual(len(runner.task.step_results), 1)
        self.assertEqual(runner.task.steps[0].goal, 'LOCAL')
        self.assertEqual(runner.task.plan_history[0]['replaced_steps'][0]['attempts'][0]['result']['status'], 'partial')

    def test_review_requires_evidence_and_consistent_decision(self):
        from backend.agents.mentor import parse_review_result
        from backend.utils.errors import ReviewError
        finding = {'severity': 'blocking', 'criterion': 'source accuracy', 'location': 'report.docx/paragraph 2', 'evidence': 'claim contradicts raw.csv row 3', 'correction': 'fix only paragraph 2'}
        for data in [{'approved': False, 'comment': 'looks wrong'}, {'approved': True, 'findings': [finding]}, {'approved': False, 'findings': [finding | {'evidence': ''}]}]:
            with self.assertRaises(ReviewError):
                parse_review_result(json.dumps(data))
        result = parse_review_result(json.dumps({'approved': False, 'goal_check': 'on track', 'findings': [finding]}))
        self.assertEqual(result.findings[0].location, finding['location'])
        self.assertTrue(parse_review_result('{"approved":true,"goal_check":"on track","findings":[]}').approved)

    def test_rework_contains_only_current_blockers(self):
        from backend.agents.base import ReviewResult, ExecutionResult
        from backend.orchestration.orchestrate import build_rework_prompt
        finding = {'severity': 'blocking', 'criterion': 'source accuracy', 'location': 'report.docx/paragraph 2', 'evidence': 'current mismatch', 'correction': 'replace the false sentence'}
        review = ReviewResult(approved=False, comment='short', findings=[finding, finding | {'severity': 'advisory', 'correction': 'add decoration'}])
        text = build_rework_prompt(review, ExecutionResult(status='completed', summary='ready'), [ReviewResult(approved=False, comment='obsolete instruction'), review])
        self.assertIn('replace the false sentence', text)
        self.assertNotIn('add decoration', text)
        self.assertNotIn('obsolete instruction', text)

    def test_round_budget_reminder_allows_verification_then_final_result(self):
        from backend.agents.worker import WorkerAgent
        from backend.agents.base import ResponseMessage
        agent = WorkerAgent(object(), name='engineer')
        agent.max_tool_rounds = 5
        agent.tool_runner = lambda name, arguments: ('verified', False)
        seen = []
        def complete(**kwargs):
            seen.append(list(agent.context.conversation))
            if len(seen) == 4:
                return ResponseMessage(content='{"status":"completed"}')
            return ResponseMessage(tool_calls=[{'id': str(len(seen)), 'name': 'run_code', 'arguments': {'targets': [{'file_path': str(len(seen))}]}}])
        agent.complete = complete
        result = agent.run_turn(lambda text, records: (json.loads(text), records), retry_hint='', error_cls=RuntimeError)
        self.assertEqual(result[0]['status'], 'completed')
        self.assertEqual(len(result[1]), 3)
        self.assertFalse(any('剩余3轮' in str(message) for message in seen[1]))
        self.assertTrue(any('剩余3轮' in str(message) for message in seen[2]))

    def test_run_one_only_accepts_py_script_paths(self):
        from backend.tools.run_code import run_one
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / 'two.py'
            script.write_text('print(1)\nprint(2)\n', encoding='utf-8')
            output = run_one('two.py', [], 10, folder)
            self.assertEqual(output.replace('\r\n', '\n').strip(), '1\n2')
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, r'必须是 \.py'):
                run_one('inline.pyx', [], 10, folder)
            # 命令链不再被解析：整串就是一个不存在的文件名
            with self.assertRaisesRegex(RuntimeError, '不存在'):
                run_one('a.py && b.py', [], 10, folder)
            with self.assertRaisesRegex(RuntimeError, '不存在'):
                run_one('missing.py', [], 10, folder)

    def test_word_block_schema_distinguishes_headings_from_style_names(self):
        from backend.utils.skills import load_skill_script
        from docx import Document
        word=load_skill_script('writer','document-formatting','create_word')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            word.process({'file':'heading.docx','blocks':[{'type':'heading','level':2,'text':'研究方法'}]},root)
            self.assertEqual(Document(root/'heading.docx').paragraphs[0].style.name,'Heading 2')
            with self.assertRaises(RuntimeError):word.process({'file':'invalid.docx','blocks':[{'type':'heading1','text':'无效类型'}]},root)

    def test_office_inspection_does_not_label_changed_source_as_stable(self):
        from unittest.mock import patch
        import importlib
        inspect = importlib.import_module('backend.utils.office')
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'source.docx'
            source.write_bytes(b'original')
            def changing_inspection(path):
                path.write_bytes(b'changed')
                return 'old structure'
            with patch.object(inspect, '_describe', side_effect=changing_inspection):
                with self.assertRaisesRegex(RuntimeError, '结构检查期间改变'):
                    inspect.office_inspect([{'path': str(source)}])

    def test_presentation_page_contract_preserves_existing_file(self):
        from backend.utils.skills import load_skill_script
        builder=load_skill_script('writer','presentation-slides','create_presentation')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            shutil.copy2('tests/fixtures/academic-template.pptx',root/'template.pptx')
            builder.prepare({'template':'template.pptx','workspace':'edit'},root)
            plan={'workspace':'edit','output':'project.pptx','expected_pages':5}
            builder.build(plan,root)
            original=(root/'project.pptx').read_bytes()
            with self.assertRaisesRegex(RuntimeError,'实际页数5不等于要求6'):
                builder.build(plan|{'overwrite':True,'expected_pages':6},root)
            self.assertEqual((root/'project.pptx').read_bytes(),original)

    def test_render_records_source_version_and_rejects_changed_source(self):
        from unittest.mock import patch
        import importlib
        from backend.utils.skills import load_skill_script
        render = load_skill_script('writer','pdf-documents','process_pdf')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source.pptx'
            source.write_bytes(b'original')
            def fake_render(path, output, width):
                image = output / 'slide1.png'
                image.write_bytes(b'fake image for version-only test')
                return [str(image)]
            with patch.object(render, 'render_ppt', side_effect=fake_render):
                receipt = render.render_pages([{'path': str(source)}], session_dir=root)
            digest = hashlib.sha256(b'original').hexdigest()
            self.assertIn(digest, receipt)
            manifests = list(root.rglob('render.json'))
            self.assertEqual(len(manifests), 1)
            self.assertEqual(json.loads(manifests[0].read_text())['source_sha256'], digest)
            def changing_render(path, output, width):
                images = fake_render(path, output, width)
                path.write_bytes(b'changed')
                return images
            with patch.object(render, 'render_ppt', side_effect=changing_render):
                with self.assertRaisesRegex(RuntimeError, '渲染期间改变'):
                    render.render_pages([{'path': str(source)}], session_dir=root)
            self.assertEqual(len(list(root.rglob('render.json'))), 1)

    def test_word_blank_paragraph_and_inspection_contract(self):
        from backend.utils.skills import load_skill_script
        docx = load_skill_script('writer','document-formatting','create_word').docx
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            docx([{'file': 'notice.docx', 'blocks': [{'type': 'paragraph', 'text': ''}, {'type': 'paragraph', 'text': '通知正文'}]}], base_dir=root)
            receipt = docx([{'file': 'notice.docx', 'inspect': True}], base_dir=root)
            self.assertIn('通知正文', receipt)
            self.assertIn(hashlib.sha256((root / 'notice.docx').read_bytes()).hexdigest(), receipt)
            with self.assertRaisesRegex(RuntimeError, '缺少 text'):
                docx([{'file': 'bad.docx', 'blocks': [{'type': 'title', 'text': ''}]}], base_dir=root)

    def test_office_script_parameter_is_rejected(self):
        from backend.utils.skills import load_skill_script
        docx = load_skill_script('writer','document-formatting','create_word').docx
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            docx([{'file':'project.docx','blocks':[{'type':'paragraph','text':'保留原文'}]}],base_dir=root)
            original=(root/'project.docx').read_bytes()
            runner=build_registry(Settings(),object()).nodes['writer'].toolbox.runner(workspace=str(root))
            receipt,failed=run_document(runner, root,{'targets':[{'kind':'docx','file':'project.docx','script':"raise RuntimeError('不得执行')"}]})
            self.assertTrue(failed)
            self.assertIn('script 参数已停用',receipt)
            self.assertEqual((root/'project.docx').read_bytes(),original)

    def test_word_rejects_margins_that_consume_page(self):
        from backend.utils.skills import load_skill_script
        docx = load_skill_script('writer','document-formatting','create_word').docx
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = {'file': 'notice.docx', 'blocks': [{'type': 'paragraph', 'text': '通知正文'}], 'style': {'page': {'top': 2.54, 'bottom': 2.54, 'left': 3, 'right': 3}}}
            docx([target], base_dir=root)
            original = hashlib.sha256((root / 'notice.docx').read_bytes()).hexdigest()
            for page in [{'top': 72, 'bottom': 72}, {'left': float('nan')}, {'left': -1}]:
                with self.assertRaisesRegex(RuntimeError, '厘米'):
                    docx([target | {'overwrite': True, 'style': {'page': page}}], base_dir=root)
                self.assertEqual(hashlib.sha256((root / 'notice.docx').read_bytes()).hexdigest(), original)

    def test_template_description_accepts_file_without_changing_input(self):
        from backend.utils.skills import load_skill_script
        builder=load_skill_script('writer','presentation-slides','create_presentation')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'template.pptx'
            shutil.copy2('tests/fixtures/academic-template.pptx',source)
            original=source.read_bytes()
            report=builder.inspect_template(source)
            self.assertEqual(len(report['pages']),5)
            self.assertEqual(report['pages'][0]['template_page'],1)
            self.assertEqual(source.read_bytes(),original)
            self.assertEqual({p.name for p in root.iterdir()},{'template.pptx'})

    def test_planner_has_no_tools_and_never_truncates_deliverables(self):
        from backend.agents.mentor import parse_planning_result
        registry = build_registry(Settings(), object())
        self.assertEqual(registry.errors, [])
        self.assertEqual(set(registry.nodes) - set(registry.worker_names), {'mentor'})
        self.assertEqual(registry.nodes['mentor'].toolbox.names, ['read'])
        plan = {'mode': 'plan', 'steps': [{'worker': 'researcher', 'goal': '核查输入'}, {'worker': 'writer', 'goal': '交付最终论文'}]}
        with self.assertRaisesRegex(ValueError, '保留全部交付'):
            parse_planning_result(json.dumps(plan), max_steps=1)
        self.assertEqual(len(parse_planning_result(json.dumps(plan), max_steps=2).plan.steps), 2)

    def test_workflow_preserves_midstep_corrections(self):
        from backend.orchestration.orchestrate import TaskOrchestrator
        from backend.agents.base import FinalAnswer, FinalRequest, Plan, PlanStep, ExecutionResult, ReviewResult
        from backend.agents.mentor import MentorAgent
        runner = TaskOrchestrator(object(), {'study': object()}, None)
        requests = []
        def execute(node, request):
            requests.append((node, request))
            if node == 'study':
                if len(requests) == 1:
                    runner.submit_input('改为三道练习题，保留独立答案区')
                return ExecutionResult(status='completed', summary='学习笔记已生成')
            if isinstance(request, FinalRequest):
                return FinalAnswer(content='已按你的补充改成三道练习题，每题下面留了独立答案区。')
            self.assertEqual(request.user_decisions, ['改为三道练习题，保留独立答案区'])
            self.assertIn('三道练习题', MentorAgent(object()).build_request(request))
            return ReviewResult(approved=True)
        runner.run_node = execute
        result = runner.run_task('生成学习笔记', plan=Plan(requirement='生成学习笔记', steps=[PlanStep(goal='写笔记', worker='study')]))
        self.assertEqual(result.status, 'completed')
        self.assertEqual([node for node, _ in requests], ['study', 'study', 'mentor', 'mentor'])
        # 终态正文来自收尾回答，而不是最后一步的 summary
        self.assertIn('三道练习题', result.content)
        self.assertNotEqual(result.content, '学习笔记已生成')
        self.assertEqual(requests[1][1].user_decisions, runner.task.user_decisions)
        from backend.agents.worker import WorkerAgent
        self.assertIn('三道练习题', WorkerAgent(object()).build_request(requests[1][1]))
        self.assertEqual(len(runner.task.user_decisions), 1)

    def test_final_answer_is_written_by_mentor_not_last_step_summary(self):
        from backend.orchestration.orchestrate import TaskOrchestrator
        from backend.agents.base import FinalAnswer, FinalRequest, Plan, PlanStep, ExecutionResult, ReviewResult
        runner = TaskOrchestrator(object(), {'study': object()}, None)
        seen = []
        def execute(node, request):
            if node != 'mentor':
                return ExecutionResult(status='completed', summary='已完成 paper-analysis.md', files=['paper-analysis.md'])
            if isinstance(request, FinalRequest):
                seen.append(request)
                return FinalAnswer(content='核心贡献是 X：实验 Y 上比基线高 3.2 个点。')
            return ReviewResult(approved=True)
        runner.run_node = execute
        result = runner.run_task(
            '这篇论文的核心贡献是什么',
            plan=Plan(requirement='这篇论文的核心贡献是什么', steps=[PlanStep(goal='精读论文', worker='study', expected_output='paper-analysis.md')]),
        )
        self.assertEqual(result.status, 'completed')
        # 终态正文是围绕问题的回答，而不是最后一步的"已完成 xxx.md"
        self.assertEqual(result.content, '核心贡献是 X：实验 Y 上比基线高 3.2 个点。')
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].task, '这篇论文的核心贡献是什么')
        self.assertIn('paper-analysis.md', seen[0].files)
        self.assertIn('精读论文', seen[0].plan_outline)

    def test_final_answer_falls_back_to_summary_when_mentor_fails(self):
        from backend.orchestration.orchestrate import TaskOrchestrator
        from backend.agents.base import FinalRequest, Plan, PlanStep, ExecutionResult, ReviewResult
        runner = TaskOrchestrator(object(), {'study': object()}, None)
        def execute(node, request):
            if node != 'mentor':
                return ExecutionResult(status='completed', summary='已完成笔记', files=['notes.md'])
            if isinstance(request, FinalRequest):
                raise RuntimeError('模型不可用')
            return ReviewResult(approved=True)
        runner.run_node = execute
        result = runner.run_task(
            '写一份笔记',
            plan=Plan(requirement='写一份笔记', steps=[PlanStep(goal='写笔记', worker='study', expected_output='notes.md')]),
        )
        # 收尾失败不该把任务判成失败：退回最后一步的摘要
        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.content, '已完成笔记')

    def test_rework_keeps_new_input_and_separate_evidence(self):
        from backend.orchestration.orchestrate import TaskOrchestrator
        from backend.agents.base import FinalAnswer, FinalRequest, Plan, PlanStep, ExecutionResult, ReviewResult
        runner = TaskOrchestrator(object(), {'a': object(), 'b': object()}, None)
        counts = {'a': 0, 'b': 0, 'mentor': 0}
        reviews = 0
        def execute(node, request):
            nonlocal reviews
            counts[node] += 1
            if node != 'mentor':
                return ExecutionResult(status='completed', summary=node, files=[node + '.txt'])
            if isinstance(request, FinalRequest):
                return FinalAnswer(content='两个文件都写好了，数据来源也补上了。')
            reviews += 1
            if reviews == 1:
                runner.submit_input('两个文件均补充数据来源')
            else:
                self.assertEqual(request.user_decisions, ['两个文件均补充数据来源'])
            return ReviewResult(approved=True, file_path=request.step.worker + '.txt', file_content='仅属于 ' + request.step.worker + ' 的正文')
        runner.run_node = execute
        plan = Plan(requirement='生成两个文件', steps=[PlanStep(goal=name, worker=name, expected_output=name + '.txt') for name in ['a', 'b']])
        result = runner.run_task('生成两个文件', plan=plan)
        self.assertEqual(result.status, 'completed')
        # 第一步：执行 → 评审期间收到补充要求 → 带着新要求返修一次；第二步各执行与评审一次
        self.assertEqual(counts, {'a': 2, 'b': 1, 'mentor': 4})
        self.assertIn('数据来源', result.content)
        self.assertEqual([step.files for step in runner.task.steps], [['a.txt'], ['b.txt']])
        self.assertEqual([len(step.attempts) for step in runner.task.steps], [2, 1])

    def test_step_failure_retains_attempts_and_files(self):
        from backend.orchestration.orchestrate import TaskOrchestrator
        from backend.agents.base import Plan, PlanStep, ExecutionResult, ReviewResult
        runner = TaskOrchestrator(object(), {'a': object(), 'b': object()}, None, step_max_retries=1)
        runner.run_node = lambda node, request: ReviewResult(approved=False, comment='缺少来源') if node == 'mentor' else ExecutionResult(status='completed', files=[node + '.txt'])
        result = runner.run_task('生成文件', plan=Plan(requirement='生成文件', steps=[PlanStep(goal=name, worker=name) for name in ['a', 'b']]))
        self.assertEqual(result.status, 'failed')
        # 第一步失败即停：它的产出要保住，后面还没跑的那步不产生"产出"
        self.assertEqual(set(runner.task.files), {'a.txt'})
        self.assertEqual(runner.task.steps[0].status, 'failed')
        self.assertEqual(len(runner.task.steps[0].attempts), 1)
        self.assertEqual(runner.task.steps[1].status, 'pending')

    def test_replanning_refreshes_new_requirements(self):
        from backend.orchestration.orchestrate import TaskOrchestrator
        from backend.agents.base import FinalAnswer, FinalRequest, Plan, PlanStep, ExecutionResult, ReviewResult, PlanningResult
        runner = TaskOrchestrator(object(), {'study': object()}, None)
        planning_requests = []
        def execute(node, request):
            if node == 'mentor' and isinstance(request, FinalRequest):
                return FinalAnswer(content='已使用本地讲义完成学习')
            if node == 'mentor' and hasattr(request, 'clarifications'):
                planning_requests.append(request)
                if len(planning_requests) == 1:
                    runner.submit_input('改用已有本地讲义，不访问网络')
                return PlanningResult(mode='plan', plan=Plan(steps=[PlanStep(goal='读取本地讲义', worker='study')]))
            if node == 'mentor':
                self.assertEqual(request.user_decisions, ['改用已有本地讲义，不访问网络'])
                if request.result.status == 'partial':
                    # 做不到的步骤不自己下结论：Mentor 收到缺口后自行决定改剩余计划
                    return ReviewResult(approved=False, goal_check='改用本地材料', plan_update=Plan(steps=[PlanStep(goal='读取本地讲义', worker='study')]))
                return ReviewResult(approved=True, goal_check='done', complete=True)
            if request.step.goal == '检索讲义':
                return ExecutionResult(status='partial', summary='网络不可用', limitations=['网络不可用'])
            return ExecutionResult(status='completed', summary='已使用本地讲义')
        runner.run_node = execute
        # 不给现成计划：走真实的"规划 → 收到补充 → 重新规划 → 执行评审"
        result = runner.run_task('讲义学习')
        self.assertEqual(result.status, 'completed')
        self.assertEqual(len(planning_requests), 2)
        self.assertIn('不访问网络', planning_requests[1].clarifications)

    def test_diagram_long_labels_and_self_loops(self):
        import xml.etree.ElementTree as xml
        from backend.utils.skills import load_skill_script
        diagram = load_skill_script("visualizer","research-figures","draw_figure").diagram
        with tempfile.TemporaryDirectory() as folder:
            for direction in ['LR', 'TB']:
                diagram([{'output': direction + '.svg', 'nodes': [{'id': 'a', 'label': 'a' * 36}], 'edges': [{'from': 'a', 'to': 'a'}], 'direction': direction}], base_dir=folder)
                drawing = xml.parse(Path(folder) / (direction + '.svg')).getroot()
                labels = drawing.findall('{http://www.w3.org/2000/svg}text')
                self.assertEqual(''.join(label.text for label in labels), 'a' * 36)
                self.assertTrue(all(len(label.text) <= 18 for label in labels))
                edge = drawing.find('{http://www.w3.org/2000/svg}polyline')
                points = [tuple(map(float, point.split(','))) for point in edge.attrib['points'].split()]
                self.assertGreater(len(set(points)), 2)
                self.assertTrue(all(0 <= x <= float(drawing.attrib['width']) and 0 <= y <= float(drawing.attrib['height']) for x, y in points))

    def test_registry_and_cached_settings(self):
        settings = Settings(tool_output_limit=1234)
        for _ in range(2):
            registry = build_registry(settings, object())
            self.assertEqual(registry.errors, [])
            self.assertEqual(set(registry.worker_names), {'researcher', 'engineer', 'visualizer', 'writer'})
            self.assertTrue({'read', 'write', 'run_code'} <= set(registry.nodes['researcher'].toolbox.names))
            for node in registry.nodes.values():
                self.assertEqual(node.toolbox.output_limit, 1234)
                for tool in node.tools:
                    self.assertEqual(set(tool['parameters']['properties']), {'targets'})

    def test_batch_file_operations(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write([{'path': 'a.txt', 'content': 'a'}, {'path': 'b.txt', 'content': 'b'}], base_dir=root)
            move([{'action': 'copy', 'path': 'a.txt', 'target': 'c.txt'}, {'action': 'rename', 'path': 'b.txt', 'target': 'd.txt'}], base_dir=root)
            delete([{'path': 'c.txt', 'dry_run': True}, {'path': 'd.txt'}], base_dir=root)
            self.assertTrue((root / 'c.txt').is_file())
            self.assertFalse((root / 'd.txt').exists())
            write([{'path': 'a.txt', 'content': '1', 'append': True}, {'path': str(root / 'a.txt'), 'content': '2', 'append': True}], base_dir=root)
            self.assertEqual((root / 'a.txt').read_text(), 'a12')

    def test_partial_failure_and_order(self):
        def execute(target):
            if target['value'] == 2:
                raise ValueError('expected failure')
            return str(target['value'])
        result = run_targets(execute, [{'value': 1}, {'value': 2}, {'value': 3}])
        self.assertLess(result.index('expected failure'), result.rindex('3'))
        self.assertIn('错误', result)

    def test_batch_artifacts(self):
        calls = [ToolCall(name='write', arguments={'targets': [{'path': 'a.txt'}, {'path': 'b.txt'}]}), ToolCall(name='run_code', result=json.dumps({'status':'completed','files':['c.docx']}))]
        self.assertEqual(produced_files(calls), ['a.txt', 'b.txt', 'c.docx'])

    def test_batch_vision_ipc(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ['a.png', 'b.png']:
                Image.new('RGB', (40, 40), 'white').save(root / name)
            registry = build_registry(Settings(), object())
            def describe(path, question, side):
                return Path(path).name + ' ' + question
            runner = registry.nodes['visualizer'].toolbox.runner(workspace=root, vision=describe)
            output, failed = runner('read', {'targets': [{'path': 'a.png', 'question': 'first'}, {'path': 'b.png', 'question': 'second'}]})
            self.assertFalse(failed, output)
            self.assertIn('a.png first', output)
            self.assertIn('b.png second', output)
            self.assertNotIn('错误', output)

    def test_worker_sees_current_scope(self):
        from backend.orchestration.orchestrate import TaskState, build_request
        from backend.agents.base import Plan, PlanStep
        state = TaskState.start(1, 'Make a chart and presentation', 'plan_and_execute')
        steps = [PlanStep(worker='visualizer', goal='Produce chart', expected_output='chart.png'), PlanStep(worker='writer', goal='Produce presentation', expected_output='report.pptx')]
        state.set_plan(Plan(requirement=state.requirement, output_name='chart.png, report.pptx', steps=steps))
        request = build_request('step', state, step=steps[0], index=0, feedback='')
        self.assertIn('Produce chart', request.step.goal)
        self.assertEqual(request.plan_outline, '')
        self.assertNotIn('Produce presentation', request.plan_outline)
        self.assertNotIn('report.pptx', request.plan_outline)

    def test_large_picture_fits_template(self):
        from PIL import Image
        from pptx import Presentation
        from backend.utils.skills import load_skill_script
        builder=load_skill_script('writer','presentation-slides','create_presentation')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);design=root/'design';(design/'svg_output').mkdir(parents=True);(design/'images').mkdir()
            Image.new('RGB',(800,1200),'white').save(design/'images/figure.png')
            (design/'svg_output/01.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720" font-family="Microsoft YaHei" font-size="24" lang="zh-CN"><image href="../images/figure.png" x="140" y="160" width="500" height="440" preserveAspectRatio="xMidYMid meet"/><text x="140" y="650" fill="#23354D">Caption</text></svg>',encoding='utf-8')
            builder.build({'workspace':'design','route':'generate','output':'test.pptx'},root)
            book=Presentation(root/'test.pptx');slide=book.slides[0]
            picture=next(s for s in slide.shapes if s.shape_type==13)
            caption=next(s for s in slide.shapes if s.has_text_frame and s.text=='Caption')
            self.assertLess(picture.top+picture.height,caption.top)
            self.assertLess(picture.left+picture.width,book.slide_width)
            self.assertAlmostEqual(picture.width/picture.height,800/1200,places=5)

    def test_mentor_phase_specific_finalization(self):
        from backend.agents.mentor import MentorAgent
        from backend.agents.base import ResponseMessage
        from backend.agents.base import PlanningRequest, ReviewRequest, PlanStep, ExecutionResult
        agent = MentorAgent(object())
        seen = []
        def complete(**kwargs):
            seen.append(agent.final_hint)
            # 短问答也必须是计划：没有"直接回答"这条路径
            plan = '{"mode":"plan","requirement":"回答","steps":[{"worker":"researcher","goal":"直接回答，无需工具","expected_output":"一句话结论"}]}'
            return ResponseMessage(content=plan if 'mode' in agent.final_hint else '{"approved":true,"goal_check":"on track","findings":[]}')
        agent.complete = complete
        planning = agent.execute_role(PlanningRequest(task='explain'))
        self.assertEqual(planning.mode, 'plan')
        self.assertEqual(planning.plan.steps[0].worker, 'researcher')
        self.assertTrue(agent.execute_role(ReviewRequest(task='explain', step=PlanStep(goal='explain'), result=ExecutionResult(status='completed'))).approved)
        self.assertIn('mode', seen[0])
        self.assertIn('goal_check', seen[1])

    def test_chart_uncertainty_and_invalid_values(self):
        from backend.utils.skills import load_skill_script
        bar_chart = load_skill_script("visualizer","research-figures","draw_figure").bar_chart
        from backend.utils.skills import load_skill_script
        line_chart = load_skill_script("visualizer","research-figures","draw_figure").line_chart
        from backend.utils.charts import numbers
        with self.assertRaises(RuntimeError):
            numbers([float('nan')], 'values')
        with tempfile.TemporaryDirectory() as folder:
            target = {'output': 'bar.png', 'categories': ['A', 'B'], 'series': [{'name': 'mean', 'values': [1, 2], 'errors': [.1, .2]}], 'show_values': True}
            bar_chart([target], base_dir=folder)
            line_chart([{key: value for key, value in target.items() if key != 'show_values'} | {'output': 'line.png'}], base_dir=folder)
            self.assertGreater((Path(folder) / 'bar.png').stat().st_size, 1000)
            self.assertGreater((Path(folder) / 'line.png').stat().st_size, 1000)

    def test_tex_compilation_is_not_registered(self):
        registry=build_registry(Settings(),object())
        self.assertNotIn('document',registry.nodes['writer'].toolbox.names)
        from backend.utils.skills import load_skill_script
        process=load_skill_script('writer','pdf-documents','process_pdf').process
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):process({'action':'latex','path':'main.tex'},Path(folder))


    def test_skill_names_support_standard_hyphens(self):
        from backend.orchestration.orchestrate import check_name, delete_skill
        self.assertEqual(check_name('literature-review', 'skill'), 'literature-review')
        self.assertEqual(check_name('legacy_name', 'skill'), 'legacy_name')
        with self.assertRaises(ValueError):
            check_name('../escape', 'skill')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'agents').mkdir()
            (root / 'agents' / 'agents.yaml').write_text(
                'researcher:\n  kind: worker\n  impl: worker\n  description: test\n  prompt: researcher.md\n'
                '  access: read-write\n  tools: []\n  skills: [literature-review]\n', encoding='utf-8')
            (root / 'prompts').mkdir()
            (root / 'prompts' / 'researcher.md').write_text('# test', encoding='utf-8')
            skill = root / 'skills' / 'literature-review'
            skill.mkdir(parents=True)
            (skill / 'SKILL.md').write_text('---\nname: literature-review\ndescription: test\n---', encoding='utf-8')
            delete_skill(root, 'researcher', 'literature-review')
            self.assertFalse(skill.exists())

    def test_transaction_entrypoint(self):
        with tempfile.TemporaryDirectory() as folder:
            registry = build_registry(Settings(), object())
            runner = registry.nodes['engineer'].toolbox.runner(workspace=folder)
            result, failed = runner('write', {'targets': [{'path': 'a.txt', 'content': 'original'}, {'path': 'b.txt', 'content': 'second'}]})
            self.assertFalse(failed, result)
            self.assertEqual((Path(folder) / 'a.txt').read_text(), 'original')
            runner('write', {'targets': [{'path': 'rename.py', 'content': "from pathlib import Path\nPath('a.txt').rename('c.txt')\n"}]})
            result, failed = runner('run_code', {'targets': [{"file_path": "rename.py"}]})
            self.assertFalse(failed, result)
            self.assertTrue((Path(folder) / 'c.txt').is_file())
            self.assertFalse((Path(folder) / 'a.txt').exists())


if __name__ == '__main__':
    unittest.main()
