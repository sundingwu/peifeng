"""验证办公能力独立于旧引擎，并在禁用子进程时真实编辑文件。"""
import ast
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from docx import Document
from pypdf import PdfReader
from backend.utils.skills import load_skill_script
from backend.utils.text import content_lines

ROOT = Path(__file__).resolve().parents[1]


class OfficeInternalizationTests(unittest.TestCase):
    def test_no_distribution_or_subprocess_adapter(self):
        for name in ['document_skills', 'ppt_master']:
            self.assertFalse((ROOT/'backend/vendor'/name).exists())
        self.assertFalse((ROOT/'backend/utils/document_runtime.py').exists())
        self.assertFalse((ROOT/'runtime/Lib/site-packages/md_to_word').exists())
        for skill in ['document-formatting', 'spreadsheets', 'pdf-documents', 'presentation-slides']:
            for script in (ROOT/'backend/skills'/skill/'scripts').rglob('*.py'):
                tree = ast.parse(script.read_text(encoding='utf-8-sig'))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        self.assertFalse(any(n.name == 'subprocess' for n in node.names), str(script))
                    if isinstance(node, ast.ImportFrom):
                        self.assertNotEqual(node.module, 'subprocess', str(script))

    def test_template_files_unchanged(self):
        baseline = {'group-meeting-template.pptx': 'a6ec8e0f49a74983ed0a5f817f9f624ec04a68c566325442873e57a419a79f20',
                    'project-defense-template.pptx': '479c02b63be16957290c79bab93b670851995c4b8e3b5318bc732d722b0acd2f'}
        for name in baseline:
            path = ROOT/'backend/skills/presentation-slides/assets'/name
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), baseline[name])

    def test_guarded_execution_needs_only_the_assigned_office_skill(self):
        from backend.utils import paths
        from backend.tools.run_code import run_one
        previous = paths.current()
        cases = [('document-formatting', 'create_word', {'action':'create','file':'new.docx','blocks':[{'type':'paragraph','text':'研究记录'}]}, 'new.docx'),
                 ('spreadsheets', 'edit_workbook', {'action':'create','file':'new.xlsx','sheets':[{'name':'数据','headers':['编号'],'rows':[[1]]}]}, 'new.xlsx'),
                 ('pdf-documents', 'process_pdf', {'action':'create','file':'new.pdf','text':'Verified evidence'}, 'new.pdf'),
                 ('presentation-slides', 'create_presentation', {'action':'prepare','mode':'group','workspace':'slides'}, 'slides/authoring-svg-flat/slide_01.svg')]
        try:
            for skill, entry, payload, expected in cases:
                with self.subTest(skill=skill), tempfile.TemporaryDirectory() as folder:
                    base = Path(folder)
                    paths.bind(paths.Roots(base, skills=[skill]))
                    (base/'input.json').write_text(json.dumps(payload), encoding='utf-8')
                    run_one(f'skill://{skill}/scripts/{entry}.py', ['--input', 'input.json', '--base-dir', '.'], timeout=120, base_dir=base)
                    self.assertTrue((base/expected).exists())
                    followup = None
                    if skill == 'document-formatting':
                        followup = {'action':'comment','path':'new.docx','file':'edited.docx', 'paragraph':1,'text':'核验记录'}
                        expected = 'edited.docx'
                    elif skill == 'presentation-slides':
                        followup = {'action':'build','workspace':'slides','output':'edited.pptx','expected_pages':3}
                        expected = 'edited.pptx'
                    if followup:
                        (base/'input.json').write_text(json.dumps(followup), encoding='utf-8')
                        run_one(f'skill://{skill}/scripts/{entry}.py', ['--input', 'input.json', '--base-dir', '.'], timeout=120, base_dir=base)
                        self.assertTrue((base/expected).is_file())
                    if skill == 'document-formatting':
                        (base/'report.md').write_text('# 研究进展\n\n已完成方法核验。', encoding='utf-8')
                        (base/'input.json').write_text(json.dumps({'action':'markdown','path':'report.md',
                                                                  'file':'report.docx','preset':'zh-academic'}), encoding='utf-8')
                        run_one(f'skill://{skill}/scripts/{entry}.py', ['--input', 'input.json', '--base-dir', '.'], timeout=120, base_dir=base)
                        self.assertTrue((base/'report.docx').is_file())
        finally:
            paths.bind(previous)

    def test_word_comment_and_pdf_annotation_without_child_processes(self):
        word = load_skill_script('writer', 'document-formatting', 'create_word')
        pdf = load_skill_script('writer', 'pdf-documents', 'process_pdf')
        with tempfile.TemporaryDirectory() as folder, patch('subprocess.Popen', side_effect=AssertionError('应直接调用技能函数')):
            base = Path(folder)
            source = Document()
            source.add_paragraph('保留原始研究记录')
            source.save(base/'source.docx')
            result = word.process({'action':'comment', 'path':'source.docx', 'file':'commented.docx',
                                   'paragraph':1, 'text':'补充日期'}, base)
            self.assertEqual(result['status'], 'pass')
            self.assertIn('保留原始研究记录', '\n'.join(content_lines(base/'commented.docx')))
            pdf.process({'action':'create', 'file':'source.pdf', 'text':'Scientific evidence'}, base)
            fields = {'pages':[{'page_number':1,'pdf_width':595,'pdf_height':842}],
                      'form_fields':[{'page_number':1,'entry_bounding_box':[50,50,200,80],
                                      'entry_text':{'text':'Verified'}}]}
            (base/'fields.json').write_text(json.dumps(fields), encoding='utf-8')
            pdf.process({'action':'annotate', 'path':'source.pdf', 'fields':'fields.json', 'file':'annotated.pdf'}, base)
            self.assertEqual(len(PdfReader(base/'annotated.pdf').pages[0]['/Annots']), 1)


if __name__ == '__main__':
    unittest.main()
