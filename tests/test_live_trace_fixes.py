"""来自真实任务轨迹的回归：部分失败、DOI 注册源及错误状态传播。"""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from backend.orchestration.orchestrate import build_registry
from backend.utils.skills import load_skill_script
from config import Settings


class LiveTraceFixTests(unittest.TestCase):
    def test_delivery_check_cannot_accept_files_outside_the_workspace(self):
        from backend.orchestration.orchestrate import missing_step_outputs
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);(base/'workspace').mkdir()
            (base/'outside.pdf').write_bytes(b'external evidence')
            self.assertEqual(missing_step_outputs(base/'workspace','../outside.pdf',['../outside.pdf']),['../outside.pdf'])

    def test_error_hint_is_not_confused_by_nullable_json_fields(self):
        from backend.utils.errors import tool_error_hint
        self.assertEqual(tool_error_hint('{"results":null}\n错误：真实失败'),'错误：真实失败')

    def test_worker_continue_preserves_tool_budget_and_existing_records(self):
        from backend.agents.base import BaseAgent,ResponseMessage,ExecutionResult
        class Probe(BaseAgent):
            is_worker=True
            def __init__(self):
                super().__init__(llm=None,tool_defs=[{'type':'function','function':{'name':'read'}}],
                                 tool_runner=lambda name,args:('existing evidence',False),max_tool_rounds=4)
                self.responses=iter([ResponseMessage(content='{"status":"continue"}'),
                    ResponseMessage(content='',tool_calls=[{'id':'read1','name':'read','arguments':{'targets':[{'path':'evidence.md'}]}}]),
                    ResponseMessage(content='{"status":"completed","summary":"checked"}')])
                self.set_fixed_layer(tool_defs=self.tool_defs)
            def complete(self,**kwargs):return next(self.responses)
        probe=Probe()
        result=probe.run_turn(lambda text,records:ExecutionResult(**json.loads(text),tool_calls=records),
                              retry_hint='retry',error_cls=RuntimeError)
        self.assertEqual(result.status,'completed')
        self.assertEqual(len(result.tool_calls),1)

    def test_final_delivery_short_names_accept_unique_real_subdirectory_files(self):
        from backend.orchestration.orchestrate import TaskOrchestrator,TaskState
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);(base/'deliverables').mkdir()
            (base/'deliverables/短论文.md').write_text('实际正文',encoding='utf-8')
            runner=TaskOrchestrator(object(),{'writer':object()},None)
            runner.runtime={'workspace':folder}
            runner.task=TaskState.start(1,'交付短论文','plan')
            runner.task.output_name='短论文.md'
            self.assertEqual(runner.finalize_task('真实交付').status,'completed')

    def test_only_one_missing_final_container_can_be_completed(self):
        from backend.agents.base import parse_arguments
        expected={'targets':[{'path':'compose.json','json_data':{'pages':[{'title':'字面字符 ] } 不改动'}]}}]}
        raw=json.dumps(expected,ensure_ascii=False)
        # 丢失targets数组末尾括号；容器后没有新字段或源码，补齐位置唯一。
        broken=raw[:-2]+raw[-1]
        recovered=parse_arguments(broken)
        self.assertEqual(recovered,expected)
        self.assertIn('字段和值未改动',recovered.normalization_note)
        for unsafe in ['{"targets": BROKEN, "nested": {"path":"other","content":"x"}}',
                       '{"targets":[{"path":"a.py","content":"unfinished',
                       '{"targets":[{"path":"a.py","content":"x"',
                       '{"targets":[{"path":"a","json_data":{"a":1}},"other":2}']:
            self.assertEqual(parse_arguments(unsafe),{'_raw':unsafe})
    def test_continue_is_progress_and_major_is_never_auto_approved(self):
        from backend.agents.worker import parse_result
        from backend.agents.mentor import parse_review_result
        from backend.utils.errors import ReviewError
        result=parse_result(json.dumps({'status':'continue','summary':'只生成了草稿'}),[])
        self.assertEqual(result.status,'partial')
        finding={'severity':'major','criterion':'实际交付','location':'report.docx','evidence':'文件缺失','correction':'生成文件'}
        review=parse_review_result(json.dumps({'approved':False,'goal_check':'文件未生成','findings':[finding]}))
        self.assertFalse(review.approved)
        self.assertEqual(review.findings[0].severity,'blocking')
        with self.assertRaises(ReviewError):
            parse_review_result(json.dumps({'approved':True,'goal_check':'矛盾','findings':[finding]}))

    def test_unexpected_task_exception_is_persisted_as_failed(self):
        from backend.orchestration.orchestrate import TaskOrchestrator
        from backend.memory.store import SummaryStore,DetailStore
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder)
            store=SummaryStore(base/'sessions')
            session=store.create('probe')
            runner=TaskOrchestrator(object(),{'engineer':object()},None,summary_store=store,detail_store=DetailStore(base/'sessions'))
            runner.bind_session(session)
            runner.runtime={'workspace':str(base/'workspace')}
            (base/'workspace').mkdir()
            def fail(_):raise RuntimeError('real protocol failure')
            with patch.dict(runner._WORKFLOWS,{'failure_probe':fail}):
                with self.assertRaisesRegex(RuntimeError,'real protocol failure'):
                    runner.run_task('probe',workflow='failure_probe')
            payload=json.loads((base/'sessions/probe/summary.json').read_text(encoding='utf-8'))
            self.assertEqual(payload['records']['1']['status'],'failed')
            self.assertEqual(payload['tasks'][-1]['status'],'failed')

    def test_run_code_receipt_exposes_the_real_exit_status(self):
        registry=build_registry(Settings(),object())
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder)
            (base/'ok.py').write_text("print('real output')",encoding='utf-8')
            (base/'bad.py').write_text('raise SystemExit(7)',encoding='utf-8')
            runner=registry.nodes['engineer'].toolbox.runner(workspace=base)
            text,failed=runner('run_code',{'targets':[{"file_path": "ok.py"}]})
            self.assertFalse(failed,text)
            self.assertIn('exit_code=0',text)
            self.assertIn('real output',text)
            text,failed=runner('run_code',{'targets':[{"file_path": "bad.py"}]})
            self.assertTrue(failed,text)
            self.assertIn('退出码 7',text)

    def test_move_and_delete_rejection_have_error_status(self):
        registry=build_registry(Settings(),object())
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder)
            (base/'nonempty').mkdir()
            (base/'nonempty/keep.txt').write_text('keep',encoding='utf-8')
            runner=registry.nodes['engineer'].toolbox.runner(workspace=base)
            for name,args in [('move',{'action':'move','path':'missing.txt','target':'out.txt'}),
                              ('delete',{'path':'nonempty'})]:
                text,failed=runner(name,{'targets':[args]})
                self.assertTrue(failed,text)
            self.assertEqual((base/'nonempty/keep.txt').read_text(encoding='utf-8'),'keep')

    def test_approval_requires_actual_step_deliveries(self):
        from backend.orchestration.orchestrate import missing_step_outputs
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder)
            (base/'deliverables').mkdir()
            (base/'deliverables/summary.pdf').write_bytes(b'actual bytes')
            self.assertEqual(missing_step_outputs(base,'summary.pdf、report.docx',['deliverables/summary.pdf']),['report.docx'])
            self.assertEqual(missing_step_outputs(base,'deliverables/summary.pdf',[]),[])
            self.assertEqual(missing_step_outputs(base,'a/summary.pdf',['deliverables/summary.pdf']),['a/summary.pdf'])
            (base/'analysis').mkdir()
            (base/'analysis/reading-card.md').write_text('input evidence',encoding='utf-8')
            self.assertEqual(missing_step_outputs(base,'与 reading-card.md 实际文本一致；输出 audit.json',[]),['audit.json'])
            (base/'analysis/mini-review.md').write_text('existing review',encoding='utf-8')
            self.assertEqual(missing_step_outputs(base,'与 reading-card.md/mini-review.md 一致',[]),[])
            self.assertEqual(missing_step_outputs(base,'reading-card.md/unknown.md',[]),['unknown.md'])

    def test_compose_blank_template_pages_preserves_native_resources_and_notes(self):
        import zipfile
        from pptx import Presentation
        builder=load_skill_script('writer','presentation-slides','create_presentation')
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder)
            builder.prepare({'workspace':'edit','mode':'group'},base)
            pages=[{'title':f'研究问题 {i}','bullets':['真实结果来自已核验数据','独立样本，结论限于本数据集'],
                    'source':'analysis/results.json','notes':f'第{i}页真实讲稿'} for i in range(1,7)]
            from PIL import Image
            Image.new('RGB',(320,180),'#2F5597').save(base/'chart.png')
            pages[3]['image']='chart.png'
            pages[4]['bullets']=['核验真实数据']*10
            result=builder.compose_pages({'workspace':'edit','source_slide':2,'pages':pages},base)
            self.assertEqual(result['pages'],6)
            self.assertEqual(result['layout'][4]['body_size'],24)
            builder.build({'workspace':'edit','output':'deck.pptx','expected_pages':6},base)
            deck=Presentation(base/'deck.pptx')
            self.assertEqual(len(deck.slides),6)
            for i,slide in enumerate(deck.slides,1):
                self.assertIn(f'研究问题 {i}','\n'.join(shape.text for shape in builder.walk(slide.shapes) if shape.has_text_frame))
                self.assertIn(f'第{i}页真实讲稿',slide.notes_slide.notes_text_frame.text)
            self.assertTrue(any(shape.shape_type==13 for shape in builder.walk(deck.slides[3].shapes)))
            with zipfile.ZipFile(builder.GROUP_TEMPLATE) as original,zipfile.ZipFile(base/'deck.pptx') as output:
                for name in original.namelist():
                    if name.startswith(('ppt/theme/','ppt/slideMasters/','ppt/slideLayouts/','ppt/media/')):
                        self.assertEqual(original.read(name),output.read(name),name)

    def test_compose_rejects_existing_content_and_dense_text_before_writing(self):
        builder=load_skill_script('writer','presentation-slides','create_presentation')
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder)
            builder.prepare({'workspace':'edit','mode':'group'},base)
            page={'title':'研究问题','bullets':['过密文本'*300],'notes':'讲稿'}
            with self.assertRaisesRegex(ValueError,'内容过密'):
                builder.compose_pages({'workspace':'edit','source_slide':2,'pages':[page]},base)
            self.assertFalse((base/'edit/authoring-svg-flat/composed_001.svg').exists())
            with self.assertRaisesRegex(ValueError,'无页面级对象'):
                builder.compose_pages({'workspace':'edit','source_slide':1,'pages':[page]},base)

    def test_repair_plan_does_not_duplicate_unqualified_deliveries(self):
        from backend.orchestration.orchestrate import TaskOrchestrator
        from backend.agents.base import Plan, PlanStep
        with tempfile.TemporaryDirectory() as folder:
            runner=TaskOrchestrator(object(), {'writer':object()}, None)
            runner.runtime={'workspace':folder}
            old=Plan(steps=[PlanStep(worker='writer',goal='write report',expected_output='report.docx、summary.pdf、group-meeting.pptx')])
            repair=Plan(steps=[PlanStep(worker='writer',goal='finish report',expected_output='deliverables/report.docx、deliverables/summary.pdf、deliverables/group-meeting.pptx')])
            result=runner.update_remaining_plan(old,0,repair)
            self.assertEqual(len(result.steps),1)

    def test_distinct_delivery_directories_are_not_merged(self):
        from backend.orchestration.orchestrate import output_is_mentioned
        self.assertFalse(output_is_mentioned('a/report.docx',{'b/report.docx'}))
        self.assertFalse(output_is_mentioned('report.docx',{'a/report.docx','b/report.docx'}))

    def test_batch_read_failure_keeps_success_and_marks_error(self):
        registry = build_registry(Settings(), object())
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder)
            (base/'present.txt').write_text('real evidence',encoding='utf-8')
            runner=registry.nodes['mentor'].toolbox.runner(workspace=base)
            text, failed=runner('read',{'targets':[{'path':'present.txt'},{'path':'missing.txt'}]})
            self.assertTrue(failed,text)
            self.assertIn('real evidence',text)
            self.assertIn('missing.txt',text)
            text, failed=runner('read',{'targets':[{'path':'present.txt'}]})
            self.assertFalse(failed,text)

    def test_partial_write_commits_success_and_reports_failure(self):
        registry = build_registry(Settings(), object())
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder)
            runner=registry.nodes['engineer'].toolbox.runner(workspace=base)
            text, failed=runner('write',{'targets':[{'path':'present.txt','content':'committed'},
                                                   {'path':'missing.txt','edits':[{'oldText':'absent','newText':'new'}]}]})
            self.assertTrue(failed,text)
            self.assertEqual((base/'present.txt').read_text(encoding='utf-8'),'committed')
            self.assertFalse((base/'missing.txt').exists())

    def test_datacite_fallback_preserves_doi_and_reading_scope(self):
        module=load_skill_script('researcher','paper-download','doi')
        payload={'data':{'attributes':{'doi':'10.example/dataset','titles':[{'title':'Real dataset record'}],
                    'creators':[{'name':'Example Author'}], 'publicationYear':2024,
                    'publisher':'Repository','url':'https://example.org/dataset',
                    'types':{'resourceTypeGeneral':'Dataset'}}}}
        calls=[]
        def respond(request, **kwargs):
            calls.append(request.full_url)
            if 'crossref' in request.full_url:
                raise HTTPError(request.full_url,404,'Not Found',{},None)
            return io.BytesIO(json.dumps(payload).encode('utf-8'))
        with patch.object(module,'urlopen',side_effect=respond):
            record=module.lookup({'doi':'10.example/dataset'})[0]
        self.assertEqual(record['registry'],'DataCite')
        self.assertEqual(record['doi'],'10.example/dataset')
        self.assertEqual(record['source_level'],'metadata_only')
        self.assertEqual(len(calls),2)

    def test_rate_limit_is_not_misreported_as_missing_doi(self):
        module=load_skill_script('researcher','paper-download','doi')
        error=HTTPError('https://api.crossref.org',429,'Too Many Requests',{},None)
        with patch.object(module,'urlopen',side_effect=error) as request:
            with self.assertRaises(HTTPError):module.lookup({'doi':'10.example/dataset'})
        self.assertEqual(request.call_count,1)


if __name__=='__main__':unittest.main()
