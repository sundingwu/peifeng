"""Offline compatibility tests for the fused research capabilities."""
from document_cli_support import run_document, run_figure
import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.orchestration.orchestrate import split_frontmatter
from backend.orchestration.orchestrate import build_registry
from config import Settings


class MigrationTests(unittest.TestCase):
    def test_axes_cleanup_does_not_override_skill_grid_style(self):
        from backend.utils import charts as chart_kit
        figure,axes = chart_kit.plt.subplots()
        try:
            chart_kit.clean_axes(axes,spec=chart_kit.resolve_style({'grid_linestyle':':','grid_linewidth':1.7}))
            self.assertEqual(axes.get_ygridlines()[0].get_linestyle(),':')
            self.assertEqual(axes.get_ygridlines()[0].get_linewidth(),1.7)
        finally:
            chart_kit.plt.close(figure)

    def test_shared_style_preserves_blue_white_defaults_and_explicit_overrides(self):
        from backend.utils.charts import resolve_style, PALETTE
        self.assertEqual(resolve_style(None)['palette'],list(PALETTE))
        source = {'palette':['#900000'],'text':'#123456','tick_size':12,'font_size':15}
        custom = resolve_style(source)
        self.assertEqual(custom['value_size'],12)
        self.assertEqual(custom['caption_size'],12)
        self.assertEqual(custom['error_color'],'#123456')
        self.assertEqual(custom['node_font_size'],15)
        custom['palette'].append('#000000')
        self.assertEqual(source['palette'],['#900000'])
        self.assertEqual(resolve_style(None)['palette'],list(PALETTE))

    def test_diagram_style_controls_font_colors_and_strokes(self):
        from backend.utils.skills import load_skill_script
        draw_target = load_skill_script("visualizer","research-figures","draw_figure").draw_target
        from xml.etree import ElementTree
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            draw_target('flow.svg',[{'id':'a','label':'Input'},{'id':'b','label':'Output'}],[{'from':'a','to':'b'}],
                style={'node_fill':'#ffeecc','node_border':'#993333','text':'#445566','edge_color':'#aa00ff','edge_width':3,'node_radius':0,'node_font_size':20},base_dir=root)
            xml = ElementTree.parse(root/'flow.svg').getroot()
            boxes = [node for node in xml.findall('{http://www.w3.org/2000/svg}rect') if node.get('x')]
            self.assertEqual(boxes[0].get('fill'),'#ffeecc')
            self.assertEqual(boxes[0].get('rx'),'0')
            self.assertEqual(xml.find('{http://www.w3.org/2000/svg}polyline').get('stroke'),'#aa00ff')
            text = xml.find('{http://www.w3.org/2000/svg}text')
            self.assertEqual(text.get('fill'),'#445566')
            self.assertEqual(float(text.get('font-size')),20)

    def test_native_bar_tool_exports_counts_caption_and_editable_svg_transactionally(self):
        from xml.etree import ElementTree
        from PIL import Image
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'result.svg').write_text('previous vector',encoding='utf-8')
            runner = build_registry(Settings(),object()).nodes['visualizer'].toolbox.runner(workspace=root,session_dir=root/'session')
            target = {'kind':'bar','output':'result.png','svg_output':'result.svg','categories':['task A','task B'],
                'series':[{'name':'Baseline','values':[7,10],'errors':[2,2],'counts':[3,3]},
                          {'name':'Candidate','values':[5,8.5],'errors':[1.4,2.5],'counts':[2,3]}],
                'show_values':True,'ylabel':'errors_per_task (lower is better)',
                'style':{'palette':['#9b2226','#005f73'],'value_size':11,'caption_size':9,'error_color':'#aa00ff','error_linewidth':2.5,'error_capsize':6},
                'caption':'教学演示，非真实 benchmark；误差棒为样本标准差 ddof=1；各组样本数不齐。'}
            receipt, failed = run_figure(runner,root,{'targets':[target]})
            self.assertFalse(failed,receipt)
            with Image.open(root/'result.png') as picture:picture.verify()
            xml = ElementTree.parse(root/'result.svg')
            text = ' '.join(xml.getroot().itertext())
            self.assertIn('n=2',text)
            self.assertIn('ddof=1',text)
            raw = (root/'result.svg').read_text(encoding='utf-8')
            self.assertIn('#9b2226',raw)
            self.assertIn('#005f73',raw)
            self.assertIn('#aa00ff',raw)
            self.assertIn('stroke-width: 2.5',raw)
            self.assertTrue(xml.findall('.//{http://www.w3.org/2000/svg}text'))
            self.assertIn('可编辑SVG',receipt)

    def test_native_chart_rejects_fractional_or_misaligned_sample_counts(self):
        from backend.utils.charts import clean_series
        for counts in [[1.5,2],[1],[-1,2],[True,2]]:
            with self.assertRaises(RuntimeError):
                clean_series([{'name':'data','values':[1,2],'counts':counts}])

    def test_order_checker_selects_scalar_arrays_inside_object_arrays(self):
        from backend.utils.skills import load_skill_script
        checker = load_skill_script('engineer','data-analysis','check_order')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'issues.json').write_text('{"gaps":[{"only":["1","3"]}]}',encoding='utf-8')
            self.assertEqual(json.loads(checker.check_order({'path':'issues.json','array':'gaps.0.only'},root))['records'],2)
            with self.assertRaisesRegex(ValueError,'下标无效'):
                checker.check_order({'path':'issues.json','array':'gaps.3.only'},root)

    def test_svg_structure_reports_text_nodes_without_a_vision_call(self):
        from backend.tools.read import read
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'figure.svg'
            path.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="144pt" height="72pt" viewBox="0 0 144 72"><text x="1" y="20">Label</text><path d="M 0 0 L 1 1"/></svg>',encoding='utf-8')
            vision = unittest.mock.Mock(side_effect=AssertionError('No vision for structure'))
            text = read([{'path':str(path),'view':'structure'}],base_dir=path.parent,vision=vision)
            self.assertIn('width=144pt',text)
            self.assertIn('文本元素：1',text)
            self.assertIn('路径：1',text)
            vision.assert_not_called()

    def test_word_export_rejects_source_data_changes_before_replacing_output(self):
        from backend.utils.skills import load_skill_script
        from backend.utils.skills import load_skill_script
        markdown_docx = load_skill_script('writer','document-formatting','create_word').markdown_docx
        from word_markdown import export
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root/'data.csv';data.write_text('method,seed,score\nC,1,1\nB,1,1\n',encoding='utf-8')
            load_skill_script('engineer','data-analysis','paired_evidence').paired_evidence({'path':'data.csv','output':'facts.json','keys':['seed'],'candidate':'C','controls':['B']},root)
            (root/'report.md').write_text('# Report\n\nLimited interpretation.',encoding='utf-8')
            output = root/'report.docx';output.write_bytes(b'previous output')
            def change_after_export(*args,**kwargs):
                result = export(*args,**kwargs)
                data.write_text('method,seed,score\nC,1,2\nB,1,1\n',encoding='utf-8')
                return result
            with patch('word_markdown.export',side_effect=change_after_export):
                with self.assertRaisesRegex(ValueError,'事实来源已变化'):
                    markdown_docx([{'path':'report.md','file':'report.docx','facts':'facts.json','preset':'en-nature'}],root)
            self.assertEqual(output.read_bytes(),b'previous output')
            self.assertFalse(list(root.glob('.peifeng-facts-*')))

    def test_reading_past_eof_is_empty_success_but_invalid_offsets_still_fail(self):
        from backend.tools.read import page
        selected, message = page(['one line'],2,10)
        self.assertEqual(selected,[])
        self.assertIn('已到末尾',message)
        with self.assertRaises(RuntimeError):page(['one line'],0,10)

    def test_observation_audit_requires_explicit_absent_status_declaration(self):
        from backend.utils.skills import load_skill_script
        module = load_skill_script('engineer','data-analysis','audit_observations')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'input.csv').write_text('method,seed,score\nC,1,2\nB,1,2\n',encoding='utf-8')
            target = {'inputs':[{'path':'input.csv'}],'keys':['seed'],'candidate':'C','controls':['B']}
            with self.assertRaisesRegex(ValueError,'status:null'):
                module.audit_observations(target,root)
            result = module.audit_observations(dict(target,status=None),root)
            self.assertEqual(result['status_information'],'not_provided')
            self.assertEqual(result['counts']['accepted_rows'],2)
            self.assertEqual(json.loads((root/'issues.json').read_text())['failed_keys'],[])

    def test_observation_audit_uses_configured_names_and_preserves_tiny_differences(self):
        import csv
        from backend.utils.skills import load_skill_script
        module = load_skill_script('engineer','data-analysis','audit_observations')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'input.csv').write_text('dataset,region,treatment,trial,errors,state\nd1,east,base,s1,2,valid\nd1,east,new,s1,2.000000000001,valid\nd1,east,new,s2,1,valid\nd1,east,base,s2,,failed\nd1,east,new,s2,1.0,valid\n',encoding='utf-8')
            result = module.audit_observations({'inputs':[{'path':'input.csv'}],'group_by':['dataset','region'],'method':'treatment','keys':['trial'],'value':'errors','status':'state','accepted_status':'valid','candidate':'new','controls':['base']},root)
            self.assertEqual(result['counts'],{'input_rows':5,'exact_duplicate_rows':1,'accepted_rows':3})
            issues = json.loads((root/'issues.json').read_text())
            self.assertEqual(issues['pairing_gaps'][0]['candidate_only'],['s2'])
            with (root/'paired.csv').open(newline='') as stream:
                pairs = list(csv.DictReader(stream))
            self.assertGreater(float(pairs[0]['difference']),0)
            self.assertLess(float(pairs[0]['difference']),1e-10)

    def test_observation_audit_rejects_unknown_columns_before_writing(self):
        from backend.utils.skills import load_skill_script
        module = load_skill_script('engineer','data-analysis','audit_observations')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'input.csv').write_text('method,seed,score,status,extra\nC,1,1,ok,x\n',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'表头与声明不一致'):
                module.audit_observations({'inputs':[{'path':'input.csv'}],'keys':['seed'],'candidate':'C','controls':['B']},root)
            self.assertFalse((root/'cleaned.csv').exists())

    def test_fact_bound_write_works_through_real_transaction_and_child_process(self):
        from backend.utils.skills import load_skill_script
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder);(root/'internal').mkdir()
            data = root/'data.csv';data.write_text('method,seed,score\nC,1,1\nB,1,1\n',encoding='utf-8')
            load_skill_script('engineer','data-analysis','paired_evidence').paired_evidence({'path':'data.csv','output':'internal/facts.json','keys':['seed'],'candidate':'C','controls':['B']},root)
            original = data.read_bytes()
            runner = build_registry(Settings(),object()).nodes['researcher'].toolbox.runner(workspace=root,session_dir=root/'session')
            output, failed = runner('write',{'targets':[{'path':'audit.md','content':'# Audit\n\nLimited interpretation.','facts':'internal/facts.json'}]})
            self.assertFalse(failed,output)
            self.assertIn('零值1个',(root/'audit.md').read_text(encoding='utf-8'))
            self.assertEqual(data.read_bytes(),original)

    def test_fact_transaction_rejects_source_change_before_commit(self):
        from backend.utils.skills import load_skill_script
        from backend.utils.files import run_staged_transaction as run_staged
        from backend.tools.write import write
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder);(root/'internal').mkdir()
            data = root/'data.csv';data.write_text('method,seed,score\nC,1,1\nB,1,1\n',encoding='utf-8')
            load_skill_script('engineer','data-analysis','paired_evidence').paired_evidence({'path':'data.csv','output':'internal/facts.json','keys':['seed'],'candidate':'C','controls':['B']},root)
            report = root/'audit.md';report.write_text('Previous valid report',encoding='utf-8')
            def change_after_execute(function,payload,stopped,vision):
                result = function(**payload)
                data.write_text('method,seed,score\nC,1,2\nB,1,1\n',encoding='utf-8')
                return result
            with self.assertRaisesRegex(ValueError,'事实来源已变化'):
                run_staged(write,{'targets':[{'path':'audit.md','content':'# Audit','facts':'internal/facts.json'}],'base_dir':root},None,execute=change_after_execute)
            self.assertEqual(report.read_text(encoding='utf-8'),'Previous valid report')

    def test_fact_binding_appends_a_complete_section_without_placeholders(self):
        from backend.utils.skills import load_skill_script
        from backend.tools.write import write_target
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'data.csv').write_text('method,seed,score\nC,1,1\nB,1,1\n',encoding='utf-8')
            load_skill_script('engineer','data-analysis','paired_evidence').paired_evidence({'path':'data.csv','output':'facts.json','keys':['seed'],'candidate':'C','controls':['B']},root)
            write_target('audit.md',content='# Audit\n\nInterpretation remains limited.',facts='facts.json',base_dir=root)
            text = (root/'audit.md').read_text(encoding='utf-8')
            self.assertIn('## 配对结果',text)
            self.assertIn('负值0个、零值1个、正值0个',text)

    def test_fact_binding_rejects_inserting_a_comparison_in_another_claim(self):
        from backend.utils.skills import load_skill_script
        from backend.tools.write import write_target
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'data.csv').write_text('method,seed,score\nC,1,1\nB,1,1\n',encoding='utf-8')
            load_skill_script('engineer','data-analysis','paired_evidence').paired_evidence({'path':'data.csv','output':'facts.json','keys':['seed'],'candidate':'C','controls':['B']},root)
            with self.assertRaisesRegex(ValueError,'独立成段'):
                write_target('audit.md',content='train intersection = {{fact:comparison_1}}',facts='facts.json',base_dir=root)
            self.assertFalse((root/'audit.md').exists())

    def test_task_local_fact_reference_survives_absent_model_handoff(self):
        from backend.utils.skills import load_skill_script
        from backend.agents.worker import fact_handoff
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder);(root/'internal').mkdir()
            data = root/'data.csv';data.write_text('method,seed,score\nC,1,1\nB,1,1\n',encoding='utf-8')
            load_skill_script('engineer','data-analysis','paired_evidence').paired_evidence({'path':'data.csv','output':'internal/facts.json','keys':['seed'],'candidate':'C','controls':['B']},root)
            text = fact_handoff(str(root))
            self.assertIn('internal/facts.json',text)
            self.assertIn('comparison_1',text)
            data.write_text('method,seed,score\nC,1,2\nB,1,1\n',encoding='utf-8')
            self.assertIn('事实未通过复算',fact_handoff(str(root)))

    def test_replanning_retains_artifact_references_without_old_tool_bodies(self):
        from backend.orchestration.orchestrate import TaskOrchestrator, TaskState, build_completed_prompt
        from backend.agents.base import Plan, PlanStep, ExecutionResult, ToolCall
        runner = TaskOrchestrator(object(),{'engineer':object(),'writer':object()},None)
        runner.task = TaskState.start(1,'Goal','plan')
        plan = Plan(steps=[PlanStep(goal='Compute',worker='engineer')])
        runner.task.set_plan(plan)
        result = ExecutionResult(status='partial',files=['cleaned.csv','internal/facts.json'],tool_calls=[ToolCall(name='run_code',result='PRIVATE_OLD_TOOL_BODY')])
        runner.task.record_attempt(0,result)
        runner.update_remaining_plan(plan,0,Plan(steps=[PlanStep(goal='Finish report',worker='writer')]))
        prompt = build_completed_prompt(runner.task)
        self.assertIn('internal/facts.json',prompt)
        self.assertIn('未等同当前目标已验收',prompt)
        self.assertNotIn('PRIVATE_OLD_TOOL_BODY',prompt)

    def test_paired_facts_preserve_zero_differences_and_missing_side(self):
        from backend.utils.skills import load_skill_script
        module = load_skill_script('engineer', 'data-analysis', 'paired_evidence')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'data.csv').write_text('task,method,seed,score\nT,C,2,3\nT,B,3,9\nT,C,1,1\nT,B,1,1\nT,B,2,4\n', encoding='utf-8')
            target = {'path':'data.csv','output':'facts.json','group_by':['task'],'keys':['seed'],'candidate':'C','controls':['B']}
            module.paired_evidence(target, root)
            payload = json.loads((root/'facts.json').read_text(encoding='utf-8'))
            row = payload['comparisons'][0]
            self.assertEqual(row['difference_counts'], {'negative':1,'zero':1,'positive':0})
            self.assertEqual(row['candidate_only_keys'], [])
            self.assertEqual(row['control_only_keys'], [{'seed':'3'}])
            self.assertEqual(row['paired_count'], row['candidate_count'])
            self.assertEqual(row['mean_difference'], -.5)
            self.assertIn('B独有键（C无对应观测）', payload['facts']['comparison_1'])
            self.assertIn('零值1个', payload['facts']['comparison_1'])
            original = (root/'facts.json').read_bytes()
            with (root/'data.csv').open('a', encoding='utf-8') as stream:
                stream.write('T,C,1,1\n')
            with self.assertRaisesRegex(ValueError, '重复'):
                module.paired_evidence(target, root)
            self.assertEqual((root/'facts.json').read_bytes(), original)

    def test_fact_binding_exports_exact_content_and_rejects_stale_sources(self):
        from backend.utils.skills import load_skill_script
        from backend.utils.skills import load_skill_script
        markdown_docx = load_skill_script('writer','document-formatting','create_word').markdown_docx
        from docx import Document
        from PIL import Image
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'internal').mkdir()
            (root/'data.csv').write_text('method,seed,score\nC,1,2\nB,1,2\n',encoding='utf-8')
            module = load_skill_script('engineer','data-analysis','paired_evidence')
            module.paired_evidence({'path':'data.csv','output':'internal/facts.json','keys':['seed'],'candidate':'C','controls':['B']},root)
            Image.new('RGB',(120,60),'blue').save(root/'image.png')
            source = root/'internal/report.md'
            source.write_text('# Report\n\n{{fact:comparison_1}}\n\n![](../image.png)\n\n%% Image Caption Figure 1 Observations\n',encoding='utf-8')
            original = source.read_bytes()
            target = {'path':'internal/report.md','file':'report.docx','facts':'internal/facts.json','preset':'en-nature'}
            receipt = json.loads(markdown_docx([target],root))
            self.assertEqual(receipt['facts_used'],['comparison_1'])
            self.assertEqual(receipt['source_sha256'],hashlib.sha256(original).hexdigest())
            doc = Document(root/'report.docx')
            text = '\n'.join(p.text for p in doc.paragraphs)
            self.assertIn('负值0个、零值1个、正值0个', text)
            self.assertNotIn('{{fact:', text)
            self.assertEqual(len(doc.inline_shapes),1)
            self.assertEqual(source.read_bytes(),original)
            self.assertFalse(list(source.parent.glob('.peifeng-facts-*')))
            saved = (root/'report.docx').read_bytes()
            evidence = root/'internal/facts.json'
            data = json.loads(evidence.read_text(encoding='utf-8'))
            data['facts']['comparison_1'] = 'Incorrectly claims all differences are negative.'
            evidence.write_text(json.dumps(data), encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'事实与来源复算不一致'):
                markdown_docx([target],root)
            module.paired_evidence({'path':'data.csv','output':'internal/facts.json','keys':['seed'],'candidate':'C','controls':['B']},root)
            (root/'data.csv').write_text('method,seed,score\nC,1,3\nB,1,2\n',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'事实来源已变化'):
                markdown_docx([target],root)
            self.assertEqual((root/'report.docx').read_bytes(),saved)

    def test_fact_binding_rejects_unknown_or_unbound_placeholders(self):
        from backend.utils.skills import load_skill_script
        from backend.utils.skills import load_skill_script
        markdown_docx = load_skill_script('writer','document-formatting','create_word').markdown_docx
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'data.csv').write_text('method,seed,score\nC,1,2\nB,1,2\n',encoding='utf-8')
            load_skill_script('engineer','data-analysis','paired_evidence').paired_evidence({'path':'data.csv','output':'facts.json','keys':['seed'],'candidate':'C','controls':['B']},root)
            (root/'report.md').write_text('# Report\n\n{{fact:missing}}\n',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'不存在的事实ID'):
                markdown_docx([{'path':'report.md','file':'report.docx','facts':'facts.json'}],root)
            with self.assertRaisesRegex(ValueError,'必须提供facts'):
                markdown_docx([{'path':'report.md','file':'report.docx'}],root)
            self.assertFalse((root/'report.docx').exists())

    def test_markdown_write_binds_recomputed_facts_before_touching_output(self):
        from backend.utils.skills import load_skill_script
        from backend.tools.write import write_target
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'data.csv').write_text('method,seed,score\nC,1,1\nB,1,1\n',encoding='utf-8')
            load_skill_script('engineer','data-analysis','paired_evidence').paired_evidence({'path':'data.csv','output':'facts.json','keys':['seed'],'candidate':'C','controls':['B']},root)
            receipt = write_target('audit.md',content='# Audit\n\n{{fact:comparison_1}}',facts='facts.json',base_dir=root)
            self.assertIn('来源复算并绑定事实',receipt)
            self.assertIn('零值1个',(root/'audit.md').read_text(encoding='utf-8'))
            saved = (root/'audit.md').read_bytes()
            with self.assertRaisesRegex(ValueError,'不存在的事实ID'):
                write_target('audit.md',content='{{fact:missing}}',facts='facts.json',base_dir=root)
            self.assertEqual((root/'audit.md').read_bytes(),saved)
            with self.assertRaisesRegex(ValueError,'整体写入'):
                write_target('audit.md',content='{{fact:comparison_1}}',append=True,facts='facts.json',base_dir=root)

    def test_order_checker_supports_scalar_id_arrays_without_object_keys(self):
        from backend.utils.skills import load_skill_script
        checker = load_skill_script('engineer','data-analysis','check_order')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root/'evidence.json'
            path.write_text(json.dumps({'ids':['q1','q3'],'empty':[]}),encoding='utf-8')
            self.assertEqual(json.loads(checker.check_order({'path':path.name,'array':'ids'},root))['records'],2)
            self.assertEqual(json.loads(checker.check_order({'path':path.name,'array':'empty','keys':[]},root))['records'],0)
            with self.assertRaisesRegex(ValueError,'省略keys'):
                checker.check_order({'path':path.name,'array':'ids','keys':['id']},root)
            path.write_text('{"ids":["q3","q1"]}',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'排序不符'):
                checker.check_order({'path':path.name,'array':'ids'},root)

    def test_review_accepts_step_array_updates_without_relaxing_findings(self):
        from backend.agents.mentor import parse_review_result
        from backend.utils.errors import ReviewError
        payload={'approved':True,'goal_check':'user changed the remaining deliverable','comment':'keep completed data','plan_update':[{'worker':'writer','goal':'Write rules.md'}]}
        result=parse_review_result(json.dumps(payload))
        self.assertEqual(result.plan_update.steps[0].goal,'Write rules.md')
        payload['plan_update']=[]
        with self.assertRaises(ReviewError): parse_review_result(json.dumps(payload))

    def test_yaml_multiline_and_malformed_metadata(self):
        meta, body = split_frontmatter('---\nname: sample\ndescription: >-\n  first\n  second\nmetadata:\n  status: active\n---\nbody')
        self.assertEqual(meta['description'], 'first second')
        self.assertEqual(meta['metadata'], {'status':'active'})
        self.assertEqual(body, 'body')
        with self.assertRaises(ValueError):
            split_frontmatter('---\n- invalid\n---')

    def test_roles_and_active_skills(self):
        registry=build_registry(Settings(),object())
        self.assertEqual(set(registry.nodes),{'mentor','researcher','engineer','visualizer','writer'})
        self.assertFalse(registry.errors)
        self.assertEqual(registry.nodes['mentor'].toolbox.names,['read'])
        self.assertNotIn('write',registry.nodes['mentor'].toolbox.names)
        from backend.agents.base import PlanStep
        self.assertEqual(PlanStep.model_validate({'worker':'designer','goal':'chart'}).worker,'visualizer')
        self.assertEqual({name for agent in registry.nodes.values() for name in agent.toolbox.names},{'read','write','run_code','delete','move'})

    def test_assembly_does_not_scan_lazy_reference_trees(self):
        with patch.object(Path,'rglob',side_effect=AssertionError('Do not scan reference trees during assembly')):
            registry=build_registry(Settings(),object())
        self.assertFalse(registry.errors)

    def test_skill_references_and_scripts_are_local_and_resolvable(self):
        """技能自包含：SKILL.md 里列出的 references/scripts 必须在该技能目录内且真实存在。"""
        root=Path(__file__).resolve().parents[1]/'backend'/'skills'
        checked=0
        for home in sorted(root.iterdir()):
            skill=home/'SKILL.md'
            if not skill.is_file():
                continue
            for line in skill.read_text(encoding='utf-8').splitlines():
                match=re.match(r'- ((?:references|scripts)/[^\s：:]+)',line.strip())
                if not match:
                    continue
                relative=match.group(1)
                checked+=1
                target=(home/relative).resolve()
                self.assertTrue(target.is_relative_to(home.resolve()),f'{home.name}: {relative}')
                self.assertTrue(target.is_file(),f'{home.name}: {relative}')
        self.assertGreater(checked,0,'技能入口应提供可解析的按需资源')
        # 文件数量不是覆盖质量；精简后继续验证所有技能入口均可装配。
        self.assertEqual(len([home for home in root.iterdir() if (home/'SKILL.md').is_file()]),20)

    def test_python_only_execution_and_failure_receipt(self):
        from backend.tools.run_code import run_one
        from backend.utils.skills import experiment_target
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'mul.py').write_text('print(6*7)\n',encoding='utf-8')
            self.assertIn('42',run_one('mul.py',[],10,root))
            (root/'mul2.py').write_text('print(6*7)\nprint(7*7)\n',encoding='utf-8')
            self.assertIn('42\n49',run_one('mul2.py',[],10,root).replace('\r\n','\n'))
            with self.assertRaisesRegex(RuntimeError,r'必须是 \.py'):
                run_one('inline.pyx',[],10,root)
            with self.assertRaises(RuntimeError):
                run_one('echo.bat',[],10,root)
            (root/'fail.py').write_text('raise RuntimeError("expected")',encoding='utf-8')
            with self.assertRaises(RuntimeError):
                experiment_target({'path':'fail.py','output':'receipt.json'},root)
            self.assertEqual(json.loads((root/'receipt.json').read_text(encoding='utf-8'))['status'],'failed')

    def test_skill_cli_preserves_successes_and_signals_partial_failure(self):
        import subprocess,sys
        from backend.utils.skills import load_skill_script
        module=load_skill_script('engineer','data-analysis','analyze_statistics')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            path=root/'input.json'
            path.write_text(json.dumps({'targets':[{'values':[]},{'values':[1,2,3],'output':'good.json'}]}),encoding='utf-8')
            result=subprocess.run([sys.executable,module.__file__,'--input',str(path),'--base-dir',str(root)],capture_output=True,text=True,encoding='utf-8')
            self.assertEqual(result.returncode,1,result.stderr)
            report=json.loads(result.stdout)
            self.assertEqual([row['status'] for row in report['results']],['failed','completed'])
            self.assertEqual(json.loads((root/'good.json').read_text(encoding='utf-8'))['mean'],2)

    def test_single_target_envelope_repair_is_explicit_and_never_overrides(self):
        registry=build_registry(Settings(),object())
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            runner=registry.nodes['engineer'].toolbox.runner(workspace=root)
            payload={'targets':[{'content':'verified'}],'path':'a.txt'}
            receipt,failed=runner('write',payload)
            self.assertFalse(failed,receipt)
            self.assertIn('参数兼容',receipt)
            self.assertEqual((root/'a.txt').read_text(),'verified')
            self.assertNotIn('path',payload['targets'][0])
            receipt,failed=runner('write',{'targets':[{'path':'a.txt','content':'wrong'}],'path':'b.txt'})
            self.assertTrue(failed)
            self.assertEqual((root/'a.txt').read_text(),'verified')
            self.assertFalse((root/'b.txt').exists())

    def test_diagram_adapts_height_to_long_labels_without_clipping(self):
        import xml.etree.ElementTree as ET
        from backend.utils.skills import load_skill_script
        draw_target = load_skill_script("visualizer","research-figures","draw_figure").draw_target
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'diagram.svg'
            draw_target(str(path),[{'id':'a','label':'科研证据'*25}],[{'from':'a','to':'a'}])
            drawing=ET.parse(path).getroot()
            boxes=[node for node in drawing.findall('{http://www.w3.org/2000/svg}rect') if node.get('x')]
            box=boxes[0]
            top=float(box.get('y')); bottom=top+float(box.get('height'))
            self.assertGreater(float(box.get('height')),80)
            self.assertTrue(all(top<float(node.get('y'))<bottom for node in drawing.findall('{http://www.w3.org/2000/svg}text')))

    def test_statistics_known_values_and_invalid_data(self):
        from backend.utils.skills import statistics_target
        result=json.loads(statistics_target({'values':[1,2,3]}))
        self.assertEqual(result['mean'],2)
        self.assertEqual(result['sample_sd'],1)
        result=json.loads(statistics_target({'operation':'meta','values':[1,3],'variances':[1,1]}))
        self.assertEqual(result['pooled_effect'],2)
        for values in [[True,2],[1,float('nan')],[]]:
            with self.assertRaises(ValueError): statistics_target({'values':values})

    def test_pdf_generation_extraction_and_python_rendering(self):
        from backend.utils.skills import load_skill_script
        pdf = load_skill_script('writer','pdf-documents','process_pdf').pdf
        from backend.utils.skills import paper_target
        from backend.utils.skills import load_skill_script
        render_pdf = load_skill_script('writer','pdf-documents','process_pdf').render_pdf
        render_word = load_skill_script('writer','pdf-documents','process_pdf').render_word
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            pdf([{'file':'paper.pdf','text':'Evidence 42\n中文科研内容'}],root)
            result=json.loads(paper_target({'path':'paper.pdf'},root))
            self.assertIn('Evidence 42',result['pages'][0]['text'])
            images=render_pdf(root/'paper.pdf',root,600,[])
            self.assertTrue(Path(images[0]).is_file())
            with self.assertRaises(RuntimeError): render_word(root/'x.docx',root/'x.pdf')

    def test_bibliography_roundtrip_deduplicates_and_preserves_unicode(self):
        from backend.utils.skills import load_skill_script
        module=load_skill_script('researcher','citation-management','references')
        literature,load_records=module.literature,module.load_records
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'in.bib').write_text('@article{a,title={中文 Paper},doi={10.1/example},year={2020}}\n@article{b,title={Copy},doi={10.1/example}}',encoding='utf-8')
            literature([{'operation':'deduplicate','path':'in.bib','output':'out.ris'}],root)
            rows=load_records(root/'out.ris')
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['title'],'中文 Paper')

    def test_mentor_review_has_no_second_model_call(self):
        from backend.agents.mentor import MentorAgent
        import inspect
        self.assertNotIn('verify_rejection(',inspect.getsource(MentorAgent.execute_role))

    def test_registered_tools_work_through_process_and_transaction_boundary(self):
        from backend.utils.skills import load_skill_script
        registry=build_registry(Settings(),object())
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            load_skill_script('engineer','data-analysis','analyze_statistics')
            (root/'input.json').write_text(json.dumps({'values':[1,2,3],'output':'stats.json'}),encoding='utf-8')
            receipt,failed=registry.nodes['engineer'].toolbox.runner(workspace=root)('run_code',{'targets':[{'file_path':'skill://data-analysis/scripts/analyze_statistics.py','args':['--input','input.json','--base-dir','.']}]})
            self.assertFalse(failed,receipt)
            receipt,failed=run_document(registry.nodes['writer'].toolbox.runner(workspace=root,session_dir=root/'internal'),root,{'targets':[{'kind':'pdf','file':'paper.pdf','text':'Evidence 42 中文科研内容'}]})
            self.assertFalse(failed,receipt)
            receipt,failed=registry.nodes['researcher'].toolbox.runner(workspace=root)('read',{'targets':[{'path':'paper.pdf','pages':[1]}]})
            self.assertFalse(failed,receipt)
            self.assertIn('Evidence 42',receipt)
            self.assertIn('[page 1]',receipt)
            self.assertEqual(json.loads((root/'stats.json').read_text(encoding='utf-8'))['mean'],2)

    def test_screening_rejects_conflicts_and_requires_exclusion_locators(self):
        from backend.utils.skills import load_skill_script
        summarize=load_skill_script('researcher','literature-review','screening').summarize
        record={'id':'a','stage':'full_text','decision':'exclude','reason':'wrong population'}
        with self.assertRaises(ValueError): summarize([record])
        report=summarize([dict(record,locator='paper.pdf/page 3')])
        self.assertEqual(report['counts']['full_text']['exclude'],1)
        self.assertEqual(report['review_mode'],'single_review_with_rechecks')

    def test_nbib_reuses_migrated_converter(self):
        from backend.utils.skills import load_skill_script
        load_records=load_skill_script('researcher','citation-management','references').load_records
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'refs.nbib'
            path.write_text('PMID- 123\nTI  - Test title\nFAU - Smith, John\nDP  - 2020\nLID - 10.1/test [doi]\n',encoding='utf-8')
            rows=load_records(path)
            self.assertEqual(rows[0]['title'],'Test title')
            self.assertEqual(rows[0]['doi'],'10.1/test')

    def test_doi_script_run_path_executes_the_lookup(self):
        from backend.utils.skills import load_skill_script
        module=load_skill_script('researcher','paper-download','doi')
        record={'doi':'10.example/test','title':['A source'],'source_url':'https://example.org'}
        with patch.object(module,'lookup',return_value=[record]) as lookup:
            result=json.loads(module.run([{'doi':'10.example/test'}]))
        self.assertEqual(result,[record])
        lookup.assert_called_once_with({'doi':'10.example/test'})

    def test_tool_argument_parser_preserves_code_and_rejects_nested_recovery(self):
        from backend.agents.base import parse_arguments
        content='pattern = ",}"\nprint("verified")'
        payload={'targets':[{'path':'program.py','content':content}]}
        self.assertEqual(parse_arguments(json.dumps(payload)),payload)
        self.assertEqual(parse_arguments('{"targets":[{"path":"a","content":"x",}],}'),{'targets':[{'path':'a','content':'x'}]})
        broken='{"targets": BROKEN, "nested": {"path":"wrong.txt","content":"must not execute"}}'
        self.assertEqual(parse_arguments(broken),{'_raw':broken})

    def test_svg_preview_supports_physical_units_without_modifying_source(self):
        from backend.utils.text import image_source
        from PIL import Image
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'physical.svg'
            source = '<svg xmlns="http://www.w3.org/2000/svg" width="144pt" height="72pt" viewBox="0 0 144 72"><rect width="144" height="72" fill="red"/></svg>'
            path.write_text(source, encoding='utf-8')
            with Image.open(image_source(path)) as image:
                self.assertEqual(image.size, (1800, 900))
                self.assertEqual(image.convert('RGB').getpixel((900, 450)), (255, 0, 0))
            self.assertEqual(path.read_text(encoding='utf-8'), source)

    def test_advisory_without_criterion_does_not_invent_blocking_evidence(self):
        from backend.agents.mentor import parse_review_result
        from backend.utils.errors import ReviewError
        finding = {'severity': 'advisory', 'location': 'report.docx', 'evidence': 'Long title', 'correction': 'Shorten title'}
        payload = {'approved': True, 'goal_check': 'unchanged', 'findings': [finding]}
        result = parse_review_result(json.dumps(payload))
        self.assertEqual(result.findings[0].criterion, '建议项：未指定验收依据')
        finding['severity'] = 'blocking'
        payload['approved'] = False
        with self.assertRaises(ReviewError):
            parse_review_result(json.dumps(payload))

    def test_completed_approved_review_can_explicitly_have_no_remaining_steps(self):
        from backend.agents.mentor import parse_review_result
        payload = {'approved': True, 'complete': True, 'goal_check': 'All goals verified', 'plan_update': []}
        result = parse_review_result(json.dumps(payload))
        self.assertTrue(result.complete)
        self.assertIsNone(result.plan_update)

    def test_tool_arguments_accept_only_complete_outer_object_with_extra_closers(self):
        from backend.agents.base import parse_arguments
        value = {'targets': [{'path': 'note.md', 'content': 'quoted } ] text'}]}
        self.assertEqual(parse_arguments(json.dumps(value) + '}'), value)
        for suffix in [' {"path":"other"}', ' malicious', ',']:
            raw = json.dumps(value) + suffix
            self.assertEqual(parse_arguments(raw), {'_raw': raw})

    def test_grouping_script_preserves_counts_precision_and_input(self):
        import csv
        from backend.utils.skills import load_skill_script
        module=load_skill_script('engineer','data-analysis','summarize_table')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'data.csv'
            original='class,score\nB,70\nA,80\nA,90\nB,90\n'
            source.write_text(original,encoding='utf-8')
            result=json.loads(module.run([{'path':'data.csv','group':'class','value':'score','metrics':['count','mean'],'precision':2,'output':'summary.csv'}],root))
            self.assertEqual(result['observations'],4)
            with (root/'summary.csv').open(encoding='utf-8',newline='') as stream:
                self.assertEqual(list(csv.reader(stream)),[['class','count','mean'],['A','2','85.00'],['B','2','80.00']])
            self.assertEqual(source.read_text(encoding='utf-8'),original)
            with self.assertRaises(ValueError): module.run([{'path':'data.csv','group':'class','value':'score','output':'data.csv'}],root)

    def test_original_markdown_engine_exports_content_and_native_equations(self):
        from backend.utils.skills import load_skill_script
        markdown_docx = load_skill_script('writer','document-formatting','create_word').markdown_docx
        from docx import Document
        from zipfile import ZipFile
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'paper.md').write_text('# Research report\n\nA verified result is 42.\n\n## Methods\n\nThe equation is $x^2$.\n',encoding='utf-8')
            receipt=markdown_docx([{'path':'paper.md','file':'paper.docx','preset':'en-nature'}],root)
            self.assertEqual(json.loads(receipt)['status'],'pass')
            self.assertIn('42','\n'.join(p.text for p in Document(root/'paper.docx').paragraphs))
            with ZipFile(root/'paper.docx') as archive:
                self.assertIn(b'<m:oMath',archive.read('word/document.xml'))

    def test_record_view_count_excludes_header_and_empty_rows_without_deduplicating(self):
        from backend.tools.read import record_lines
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'data.csv'
            path.write_text('id,value\na,1\n,\na,1\n', encoding='utf-8')
            lines = record_lines(path)
            self.assertIn('数据记录数：2', lines[0])
            self.assertEqual(len(lines), 3)

    def test_order_checker_rejects_unsorted_json_and_preserves_input(self):
        from backend.utils.skills import load_skill_script
        checker = load_skill_script('engineer', 'data-analysis', 'check_order')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'output.json'
            source.write_text(json.dumps({'records':[{'method':'Z'},{'method':'A'}]}), encoding='utf-8')
            original = source.read_bytes()
            target = {'path':'output.json','array':'records','keys':['method']}
            with self.assertRaisesRegex(ValueError, '排序不符'):
                checker.check_order(target, root)
            self.assertEqual(source.read_bytes(), original)
            source.write_text(json.dumps({'records':[{'method':'A'},{'method':'Z'}]}), encoding='utf-8')
            self.assertEqual(json.loads(checker.check_order(target, root))['status'], 'pass')
            source.write_text('{"records":[]}', encoding='utf-8')
            self.assertEqual(json.loads(checker.check_order(target, root))['records'], 0)

    def test_markdown_report_resolves_relative_images_and_native_caption(self):
        from backend.utils.skills import load_skill_script
        markdown_docx = load_skill_script('writer','document-formatting','create_word').markdown_docx
        from PIL import Image
        from docx import Document
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'internal').mkdir()
            Image.new('RGB', (120, 60), 'blue').save(root / 'result.png')
            source = root / 'internal' / 'report.md'
            source.write_text('# Report\n\nVerified result.\n\n![](../result.png)\n\n%% Image Caption Figure 1 Verified observations\n', encoding='utf-8')
            result = json.loads(markdown_docx([{'path':'internal/report.md','file':'report.docx','preset':'en-nature'}], root))
            self.assertEqual(result['status'], 'pass')
            document = Document(root / 'report.docx')
            self.assertEqual(len(document.inline_shapes), 1)
            self.assertIn('Figure 1', '\n'.join(p.text for p in document.paragraphs))

    def test_public_search_has_no_keyed_backend(self):
        from backend.utils.skills import load_skill_script
        module=load_skill_script('researcher','literature-search','research_web')
        with patch.object(module,'search_public',return_value=[{'title':'source','url':'https://python.org'}]) as public:
            result=module.process({'action':'search','query':'topic'},Path('.'))
            self.assertIn('source',result['evidence'])
            public.assert_called_once_with('topic',5)
        with patch.object(module,'retrieve',return_value={'content':'public text','source_level':'public_page_text'}) as public:
            self.assertIn('public text',module.process({'action':'fetch','url':'https://python.org'},Path('.'))['evidence'])
            public.assert_called_once_with('https://python.org')

    def test_original_auditors_are_read_only_and_keep_unchecked_states(self):
        from backend.utils.skills import audit_target
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'paper.md').write_text('# Research\n\nAn equation $x^2$.\n',encoding='utf-8')
            original=(root/'paper.md').read_bytes()
            result=json.loads(audit_target({'operation':'reader_math','path':'paper.md'},root))
            self.assertIn(result['status'],{'pass','warn','fail'})
            report=json.loads(audit_target({'operation':'submission','path':'.'},root))
            self.assertIn('checks',report)
            self.assertIn(report['verifier_exit_code'],{0,1,3})
            self.assertEqual((root/'paper.md').read_bytes(),original)
            self.assertEqual([path.name for path in root.iterdir()],['paper.md'])


if __name__=='__main__':
    unittest.main()
