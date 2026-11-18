"""文档技能迁移的真实路径：批注/修订、表格保护、PDF表单与CLI回执。"""
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from openpyxl import load_workbook
from pypdf import PdfReader

from backend.utils.skills import load_skill_script
from backend.utils.skill_runtime import atomic_output, file_digest


class DocumentSkillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.word=load_skill_script('writer','document-formatting','create_word')
        cls.xml=load_skill_script('writer','document-formatting','edit_word_xml')
        cls.sheet=load_skill_script('writer','spreadsheets','edit_workbook')
        cls.pdf=load_skill_script('writer','pdf-documents','process_pdf')

    def test_word_comment_has_visible_anchor_and_invalid_anchor_preserves_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            self.word.process({'file':'source.docx','blocks':[{'type':'paragraph','text':'输入数据已保留。'}]},root)
            before=(root/'source.docx').read_bytes()
            self.word.process({'action':'comment','path':'source.docx','file':'commented.docx','paragraph':1,'text':'补充采集日期','author':'研究者'},root)
            with zipfile.ZipFile(root/'commented.docx') as archive:
                document=archive.read('word/document.xml').decode('utf-8')
                comments=archive.read('word/comments.xml').decode('utf-8')
                self.assertIn('commentRangeStart',document)
                self.assertIn('commentRangeEnd',document)
                self.assertIn('commentReference',document)
                self.assertIn('补充采集日期',comments)
            with self.assertRaises(ValueError):
                self.word.process({'action':'comment','path':'source.docx','file':'bad.docx','paragraph':99,'text':'不可见批注'},root)
            self.assertFalse((root/'bad.docx').exists())
            self.assertEqual((root/'source.docx').read_bytes(),before)

    def test_word_accepts_insertions_deletions_and_deleted_paragraph_marks(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);document=Document()
            paragraph=document.add_paragraph()
            for kind,value in [('del','旧内容'),('ins','新内容')]:
                change=OxmlElement('w:'+kind);change.set(qn('w:id'),'1' if kind=='del' else '2');change.set(qn('w:author'),'研究者');change.set(qn('w:date'),'2026-11-16T00:00:00Z')
                run=OxmlElement('w:r');text=OxmlElement('w:delText' if kind=='del' else 'w:t');text.text=value;run.append(text);change.append(run);paragraph._p.append(change)
            paragraph=document.add_paragraph('前段')
            rpr=OxmlElement('w:rPr');mark=OxmlElement('w:del');mark.set(qn('w:id'),'3');mark.set(qn('w:author'),'研究者');mark.set(qn('w:date'),'2026-11-16T00:00:00Z');rpr.append(mark);paragraph._p.get_or_add_pPr().append(rpr)
            document.add_paragraph('后段');document.save(root/'tracked.docx')
            before=(root/'tracked.docx').read_bytes()
            self.word.process({'action':'accept_changes','path':'tracked.docx','file':'clean.docx'},root)
            self.assertEqual([p.text for p in Document(root/'clean.docx').paragraphs],['新内容','前段后段'])
            self.assertEqual((root/'tracked.docx').read_bytes(),before)
            with zipfile.ZipFile(root/'clean.docx') as archive:
                self.assertNotIn(b'<w:del ',archive.read('word/document.xml'))

    def test_word_append_keeps_semantics_and_honors_explicit_format(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            self.word.process({'file':'report.docx','blocks':[{'type':'heading','level':2,'text':'研究方法'}]},root)
            self.word.process({'file':'report.docx','append':True,'blocks':[{'type':'paragraph','text':'正文不应进入标题导航'}]},root)
            paragraphs=Document(root/'report.docx').paragraphs
            self.assertEqual(paragraphs[0].style.name,'Heading 2')
            self.assertEqual(paragraphs[1].style.name,'Normal')
            self.word.process({'file':'report.docx','append':True,'style':{'styles':{'body':{'size':12}}},'blocks':[{'type':'paragraph','text':'明确格式优先'}]},root)
            self.assertEqual(Document(root/'report.docx').paragraphs[-1].runs[0].font.size.pt,12)

    def test_safe_unpack_rejects_escape_and_pack_roundtrip_keeps_content(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with zipfile.ZipFile(root/'malicious.docx','w') as archive:archive.writestr('../outside.txt','bad')
            with self.assertRaises(ValueError):self.word.process({'action':'unpack','path':'malicious.docx','directory':'bad'},root)
            self.assertFalse((root/'bad').exists())
            self.word.process({'file':'source.docx','blocks':[{'type':'paragraph','text':'正文未改'}]},root)
            self.word.process({'action':'unpack','path':'source.docx','directory':'unpacked'},root)
            self.word.process({'action':'pack','path':'unpacked','file':'packed.docx'},root)
            self.assertEqual(Document(root/'packed.docx').paragraphs[0].text,'正文未改')
            from unittest.mock import patch
            original=Path.is_symlink
            def simulated_link(path):
                return path==root/'unpacked/word/document.xml' or original(path)
            with patch.object(Path,'is_symlink',simulated_link):
                with self.assertRaisesRegex(ValueError,'链接'):
                    self.word.process({'action':'pack','path':'unpacked','file':'linked.docx'},root)
            self.assertFalse((root/'linked.docx').exists())


    def test_workbook_edit_preserves_formula_and_reports_uncalculated_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            result=self.sheet.process({'file':'data.xlsx','sheets':[{'name':'实验','header':['输入','复算'],'rows':[[2,'=A2*2']],'number_formats':{'B':'0.00'}}]},root)
            self.assertEqual(result['status'],'not_computed')
            self.assertEqual(result['not_computed'],['实验!B2'])
            before=(root/'data.xlsx').read_bytes()
            self.sheet.process({'action':'edit','path':'data.xlsx','file':'edited.xlsx','patch':[{'sheet':'实验','cell':'A2','value':3}]},root)
            book=load_workbook(root/'edited.xlsx');self.assertEqual(book['实验']['B2'].value,'=A2*2');self.assertEqual(book['实验']['B2'].number_format,'0.00');book.close()
            self.assertEqual((root/'data.xlsx').read_bytes(),before)
            with self.assertRaises(ValueError):self.sheet.process({'action':'recalculate','path':'data.xlsx'},root)

    def test_workbook_errors_and_shapes_cannot_silently_replace_valid_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            self.sheet.process({'file':'data.xlsx','sheets':[{'name':'输入','rows':[[1]]}]},root)
            before=(root/'data.xlsx').read_bytes()
            with self.assertRaisesRegex(ValueError,'错误单元格'):
                self.sheet.process({'file':'data.xlsx','overwrite':True,'sheets':[{'name':'输入','rows':[['#REF!']]}]},root)
            self.assertEqual((root/'data.xlsx').read_bytes(),before)
            with zipfile.ZipFile(root/'data.xlsx','a') as archive:archive.writestr('xl/drawings/drawing1.xml','<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"><xdr:sp/></xdr:wsDr>')
            before=(root/'data.xlsx').read_bytes()
            with self.assertRaisesRegex(ValueError,'形状'):
                self.sheet.process({'action':'edit','path':'data.xlsx','file':'out.xlsx','patch':[{'sheet':'输入','cell':'A1','value':2}]},root)
            self.assertEqual((root/'data.xlsx').read_bytes(),before);self.assertFalse((root/'out.xlsx').exists())

    def test_pdf_merge_selection_rotation_and_rendering(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for i in [1,2]:self.pdf.process({'file':f'{i}.pdf','text':f'中文结果 {i}'},root)
            self.pdf.process({'action':'merge','paths':['1.pdf','2.pdf'],'file':'merged.pdf'},root)
            self.assertEqual(len(PdfReader(root/'merged.pdf').pages),2)
            self.pdf.process({'action':'select','path':'merged.pdf','pages':[2],'file':'selected.pdf'},root)
            self.assertIn('2',PdfReader(root/'selected.pdf').pages[0].extract_text())
            self.pdf.process({'action':'rotate','path':'selected.pdf','degrees':90,'file':'rotated.pdf'},root)
            self.assertEqual(PdfReader(root/'rotated.pdf').pages[0].rotation,90)
            with self.assertRaises(ValueError):self.pdf.process({'action':'split','path':'merged.pdf','pages':[],'file':'bad.pdf'},root)
            self.assertFalse((root/'bad.pdf').exists())
            self.pdf.process({'action':'watermark','path':'selected.pdf','watermark':'1.pdf','file':'marked.pdf'},root)
            self.assertIn('1',PdfReader(root/'marked.pdf').pages[0].extract_text())

            result=self.pdf.process({'action':'render','path':'1.pdf','width':600},root)
            self.assertEqual(len(result['files']),1);self.assertTrue(Path(result['files'][0]).is_file())

    def test_pdf_form_info_fill_and_invalid_field_protection(self):
        from reportlab.pdfgen import canvas
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);c=canvas.Canvas(str(root/'form.pdf'));c.drawString(50,750,'Participant')
            c.acroForm.textfield(name='participant',x=50,y=700,width=200,height=24);c.save()
            info=self.pdf.process({'action':'form_info','path':'form.pdf'},root)
            self.assertTrue(info['fillable']);field=info['fields'][0]
            fields=root/'fields.json';fields.write_text(json.dumps([{'field_id':field['field_id'],'page':field['page'],'value':'Researcher'}]),encoding='utf-8')
            self.pdf.process({'action':'fill_form','path':'form.pdf','fields':'fields.json','file':'filled.pdf'},root)
            self.assertEqual(PdfReader(root/'filled.pdf').get_fields()['participant']['/V'],'Researcher')
            before=(root/'filled.pdf').read_bytes();fields.write_text(json.dumps([{'field_id':'missing','page':1,'value':'bad'}]),encoding='utf-8')
            with self.assertRaises(RuntimeError):self.pdf.process({'action':'fill_form','path':'form.pdf','fields':'fields.json','file':'filled.pdf','overwrite':True},root)
            self.assertEqual((root/'filled.pdf').read_bytes(),before)

    def test_pdf_previews_include_form_values_and_annotation_appearances(self):
        from reportlab.pdfgen import canvas
        from PIL import Image, ImageChops
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);c=canvas.Canvas(str(root/'source.pdf'))
            c.drawString(50,760,'Form preview test');c.acroForm.textfield(name='name',x=50,y=690,width=220,height=26);c.save()
            (root/'values.json').write_text(json.dumps([{'field_id':'name','page':1,'value':'Visible value'}]),encoding='utf-8')
            self.pdf.process({'action':'fill_form','path':'source.pdf','fields':'values.json','file':'filled.pdf'},root)
            original=self.pdf.process({'action':'render','path':'source.pdf','width':600},root)['files'][0]
            filled=self.pdf.process({'action':'render','path':'filled.pdf','width':600},root)['files'][0]
            with Image.open(original) as a, Image.open(filled) as b:
                self.assertIsNotNone(ImageChops.difference(a.convert('RGB'),b.convert('RGB')).getbbox())
            (root/'annotations.json').write_text(json.dumps({'pages':[{'page_number':1,'image_width':600,'image_height':850}],
                'form_fields':[{'page_number':1,'entry_bounding_box':[80,250,420,300],'entry_text':{'text':'Visible annotation','font_size':14}}]}),encoding='utf-8')
            self.pdf.process({'action':'annotate','path':'source.pdf','fields':'annotations.json','file':'annotated.pdf'},root)
            annotation=PdfReader(root/'annotated.pdf').pages[0]['/Annots'][-1].get_object()
            self.assertEqual(annotation['/Subtype'],'/FreeText');self.assertIn('/N',annotation['/AP'])
            annotated=self.pdf.process({'action':'render','path':'annotated.pdf','width':600},root)['files'][0]
            with Image.open(original) as a, Image.open(annotated) as b:
                self.assertIsNotNone(ImageChops.difference(a.convert('RGB'),b.convert('RGB')).getbbox())

    def test_atomic_publish_detects_conflicts_and_retains_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);path=root/'original.txt';path.write_text('original',encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError,'目标文件'):
                with atomic_output(path) as candidate:
                    candidate.write_text('new',encoding='utf-8');path.write_text('external edit',encoding='utf-8')
            self.assertEqual(path.read_text(encoding='utf-8'),'external edit')
            with atomic_output(path) as candidate:candidate.write_text('accepted',encoding='utf-8')
            self.assertTrue(any(p.read_text(encoding='utf-8')=='external edit' for p in (root/'internal').rglob('original.txt')))

    def test_real_bash_batch_reports_successful_files_on_partial_failure(self):
        from config import Settings
        from backend.orchestration.orchestrate import build_registry
        from backend.agents.base import ToolCall
        from backend.agents.worker import produced_files
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            payload={'targets':[{'file':'good.docx','blocks':[{'type':'paragraph','text':'成功项'}]}, {'file':'bad.docx','blocks':[{'type':'title','text':''}]}]}
            (root/'params.json').write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8')
            registry=build_registry(Settings(),object());self.assertNotIn('document',registry.nodes['writer'].toolbox.names)
            receipt,failed=registry.nodes['writer'].toolbox.runner(workspace=root)('run_code',{'targets':[{"file_path": "skill://document-formatting/scripts/create_word.py", "args": ["--input", "params.json", "--base-dir", "."]}]})
            self.assertTrue(failed);self.assertTrue((root/'good.docx').exists());self.assertFalse((root/'bad.docx').exists())
            files=produced_files([ToolCall(name='run_code',result=receipt,is_error=True)])
            self.assertIn(str(root/'good.docx'),files)


if __name__=='__main__':unittest.main()
