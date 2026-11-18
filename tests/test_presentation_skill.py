"""PPT Master接入：原模板、对象保留、编辑隔离、失败原子性与真实工具调用。"""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from xml.etree import ElementTree as ET
import zipfile

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.util import Inches
from PIL import Image
from backend.utils.skills import load_skill_script

SVG = 'http://www.w3.org/2000/svg'
ET.register_namespace('', SVG)


class PresentationSkillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.builder = load_skill_script('writer', 'presentation-slides', 'create_presentation')
        cls.checker = load_skill_script('writer', 'presentation-slides', 'check_presentation')

    def test_default_templates_roundtrip_preserves_native_backgrounds_and_fonts(self):
        for mode, source, count in [('defense', self.builder.DEFAULT_TEMPLATE, 11), ('group', self.builder.GROUP_TEMPLATE, 3)]:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                before = hashlib.sha256(source.read_bytes()).hexdigest()
                self.builder.prepare({'mode': mode, 'workspace': 'edit'}, root)
                result = self.builder.build({'workspace': 'edit', 'output': 'deck.pptx',
                    'expected_pages': count, 'preserve_motion': True}, root)
                self.assertEqual(result['pages'], count)
                self.assertEqual(result['check']['errors'], [])
                self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
                with zipfile.ZipFile(source) as a, zipfile.ZipFile(root/'deck.pptx') as b:
                    for name in a.namelist():
                        if not name.endswith('/') and name.startswith(('ppt/slideMasters/', 'ppt/slideLayouts/', 'ppt/theme/', 'ppt/media/', 'ppt/fonts/')):
                            self.assertEqual(a.read(name), b.read(name), name)

    def test_repeated_pages_editable_text_and_independent_notes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.builder.prepare({'workspace': 'edit'}, root)
            edit = root/'edit'
            for number in [1, 2]:
                tree = ET.parse(edit/'authoring-svg-flat/slide_04.svg')
                group = next(n for n in tree.getroot().iter() if n.get('data-pptx-source-ref') == 'slide:31')
                text = next(n for n in group.iter() if n.tag == '{'+SVG+'}text')
                for node in list(text): text.remove(node)
                text.text = f'章节{number}'
                tree.write(edit/f'authoring-svg-flat/page_{number}.svg', encoding='utf-8')
                (edit/'notes').mkdir(exist_ok=True)
                (edit/f'notes/page_{number}.md').write_text(f'第{number}份独立讲稿', encoding='utf-8')
            (edit/'page_plan.json').write_text(json.dumps({'schema': 'ppt-master.roundtrip-page-plan.v1',
                'pages': [{'source_slide':4, 'svg':f'page_{i}.svg'} for i in [1,2]]}), encoding='utf-8')
            result = self.builder.build({'workspace':'edit','output':'edited.pptx','expected_pages':2}, root)
            self.assertEqual(result['check']['errors'], [])
            book = Presentation(root/'edited.pptx')
            for i, slide in enumerate(book.slides, 1):
                self.assertIn(f'章节{i}', '\n'.join(s.text for s in slide.shapes if s.has_text_frame))
                self.assertIn(f'第{i}份独立讲稿', slide.notes_slide.notes_text_frame.text)
                self.assertNotIn(f'第{3-i}份独立讲稿', slide.notes_slide.notes_text_frame.text)

    def test_user_template_keeps_native_chart_table_image_and_canvas(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            Image.new('RGB', (80,120), 'blue').save(root/'image.png')
            book = Presentation(); book.slide_width=Inches(10); book.slide_height=Inches(7.5)
            slide=book.slides.add_slide(book.slide_layouts[6])
            slide.shapes.add_picture(str(root/'image.png'), Inches(.2), Inches(.2), height=Inches(1))
            data=CategoryChartData();data.categories=['A','B'];data.add_series('实测',[1,2])
            slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(2), Inches(4), Inches(3), data)
            table=slide.shapes.add_table(2,2,Inches(6),Inches(2),Inches(3),Inches(2)).table
            table.cell(0,0).text='条件';table.cell(1,0).text='保持原值'
            book.save(root/'source.pptx');before=(root/'source.pptx').read_bytes()
            self.builder.prepare({'workspace':'edit','template':'source.pptx'}, root)
            with self.assertRaises(ValueError):
                self.builder.build({'workspace':'edit','output':'source.pptx','overwrite':True},root)
            self.builder.build({'workspace':'edit','output':'out.pptx','preserve_motion':True}, root)
            output=Presentation(root/'out.pptx')
            self.assertEqual(output.slide_width, Inches(10))
            self.assertEqual(sum(bool(getattr(s,'has_chart',False)) for s in output.slides[0].shapes),1)
            self.assertEqual(sum(bool(getattr(s,'has_table',False)) for s in output.slides[0].shapes),1)
            self.assertEqual((root/'source.pptx').read_bytes(),before)

    def test_generation_quality_gate_and_failure_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);svg_dir=root/'design/svg_output';svg_dir.mkdir(parents=True)
            source=svg_dir/'01.svg'
            source.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720" font-family="Microsoft YaHei" font-size="28" lang="zh-CN"><rect x="0" y="0" width="1280" height="720" fill="#FFFFFF"/><text x="90" y="100" fill="#063772">可编辑的新页面</text></svg>',encoding='utf-8')
            plan={'workspace':'design','route':'generate','output':'out.pptx','expected_pages':1}
            result=self.builder.build(plan,root)
            self.assertEqual(result['check']['errors'],[])
            self.assertTrue(any(s.has_text_frame and '可编辑的新页面' in s.text for s in Presentation(root/'out.pptx').slides[0].shapes))
            before=(root/'out.pptx').read_bytes()
            source.write_text('<svg broken>',encoding='utf-8')
            with self.assertRaises(RuntimeError):self.builder.build(plan|{'overwrite':True},root)
            self.assertEqual((root/'out.pptx').read_bytes(),before)

    def test_path_and_source_protection_and_page_count(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with self.assertRaises(ValueError):self.builder.prepare({'workspace':'../outside'},root)
            self.builder.prepare({'workspace':'edit','mode':'group'},root)
            with self.assertRaises(ValueError):self.builder.prepare({'workspace':'edit'},root)
            with self.assertRaises(ValueError):self.builder.build({'workspace':'edit','output':'edit/sources/source.pptx','overwrite':True},root)
            target=root/'out.pptx';target.write_bytes(b'existing')
            with self.assertRaises(RuntimeError):self.builder.build({'workspace':'edit','output':'out.pptx','overwrite':True,'expected_pages':9},root)
            self.assertEqual(target.read_bytes(),b'existing')
            result=self.builder.copy_guide({'topic':'shapes'},root)
            self.assertTrue(Path(result['guide']).is_file())
            with self.assertRaises(ValueError):self.builder.copy_guide({'topic':'../LICENSE'},root)

    def test_offline_pages_uses_engine_and_preserves_supplied_words(self):
        from backend.scripts.offline import run_ppt_pages
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'pages.md').write_text('# 校准研究\n## 实验设计\n- 保留原始输入\n- 固定条件进行复核',encoding='utf-8')
            result=run_ppt_pages({'source':'pages.md'},root)
            book=Presentation(result.files[0]);self.assertEqual(len(book.slides),2)
            text='\n'.join(s.text for slide in book.slides for s in slide.shapes if s.has_text_frame)
            for expected in ['校准研究','实验设计','保留原始输入','固定条件进行复核']:self.assertIn(expected,text)

    def test_document_ppt_creation_removed_and_read_remains_available(self):
        from backend.utils.office import _describe
        self.assertFalse(Path('backend/tools/document.py').exists())
        self.assertIn('3 页',_describe(self.builder.GROUP_TEMPLATE))

    def test_long_template_title_is_blocked_before_publishing(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            self.builder.prepare({'workspace':'edit'},root)
            path=root/'edit/authoring-svg-flat/slide_04.svg'
            tree=ET.parse(path)
            group=next(n for n in tree.getroot().iter() if n.get('data-pptx-source-ref')=='slide:31')
            text=next(n for n in group.iter() if n.tag=='{'+SVG+'}text')
            for child in list(text):text.remove(child)
            text.text='这个长标题不能硬塞进原模板的窄章节标题框'
            tree.write(path,encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError,'owning frame'):
                self.builder.build({'workspace':'edit','output':'bad.pptx'},root)
            self.assertFalse((root/'bad.pptx').exists())

    def test_writer_bash_can_call_adapter_without_vendor_tool_access(self):
        from backend.tools.run_code import run_one
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'inspect.json').write_text(json.dumps({'action':'inspect','mode':'group','report':'index.json'}),encoding='utf-8')
            receipt=run_one('skill://presentation-slides/scripts/create_presentation.py',['--input','inspect.json','--base-dir','.'],timeout=60,base_dir=root)
            self.assertTrue((root/'index.json').is_file(),receipt)
            self.assertEqual(len(json.loads((root/'index.json').read_text(encoding='utf-8'))['pages']),3)
            (root/'prepare.json').write_text(json.dumps({'action':'prepare','mode':'group','workspace':'edit','report':'import.json'}),encoding='utf-8')
            run_one('skill://presentation-slides/scripts/create_presentation.py',['--input','prepare.json','--base-dir','.'],timeout=120,base_dir=root)
            (root/'build.json').write_text(json.dumps({'workspace':'edit','output':'real-tool.pptx','expected_pages':3,'report':'result.json'}),encoding='utf-8')
            run_one('skill://presentation-slides/scripts/create_presentation.py',['--input','build.json','--base-dir','.'],timeout=120,base_dir=root)
            self.assertEqual(len(Presentation(root/'real-tool.pptx').slides),3)
            receipt=json.loads((root/'result.json').read_text(encoding='utf-8'))
            self.assertEqual(receipt['check']['errors'],[])
            self.assertEqual(receipt['check']['report']['file']['path'],str(root/'real-tool.pptx'))



if __name__ == '__main__':unittest.main()
