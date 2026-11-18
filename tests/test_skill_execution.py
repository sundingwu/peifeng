"""常用skill脚本通过真实run_code执行，检查白名单没有破坏数据与文档链路。"""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from backend.orchestration.orchestrate import build_registry
from backend.tools.run_code import run_one
from backend.utils import paths
from config import Settings


class SkillExecutionTests(unittest.TestCase):
    def test_every_registered_skill_has_readable_entry_and_compilable_scripts(self):
        registry=build_registry(Settings(),object())
        names=set()
        with tempfile.TemporaryDirectory() as folder:
            for node in registry.nodes.values():
                runner=node.toolbox.runner(workspace=folder)
                for skill in node.skills:
                    name=skill['name'];names.add(name)
                    text,failed=runner('read',{'targets':[{'skill':name}]})
                    self.assertFalse(failed,text)
                    home=paths.SKILLS_DIR/name
                    for script in home.glob('scripts/*.py'):
                        compile(script.read_text(encoding='utf-8-sig'),str(script),'exec')
        self.assertEqual(len(names),20)
        vendor=paths.SKILLS_DIR.parent/'vendor/nature_figure'
        manifest=json.loads((vendor/'UPSTREAM.json').read_text(encoding='utf-8'))
        for relative,digest in manifest['files'].items():
            self.assertEqual(hashlib.sha256((vendor/relative).read_bytes()).hexdigest(),digest)

    def test_guarded_skill_workflow(self):
        registry=build_registry(Settings(),object())
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'experiment.py').write_text("from pathlib import Path\nPath('actual.txt').write_text('actual run')\nprint(2+3)",encoding='utf-8')
            (root/'screen.json').write_text(json.dumps([{'id':'p1','stage':'title_abstract','decision':'include'}]),encoding='utf-8')
            (root/'records.json').write_text(json.dumps([{'title':'Example','doi':'10.example/test'}]),encoding='utf-8')
            cases=[
                ('engineer','data-analysis','analyze_statistics',{'values':[1,2,3],'output':'stats.json'}),
                ('engineer','experiments','experiment',{'path':'experiment.py','output':'experiment.json'}),
                ('researcher','literature-review','screening',{'path':'screen.json','output':'screened.json'}),
                ('researcher','citation-management','references',{'operation':'convert','path':'records.json','output':'refs.bib'}),
                ('writer','document-formatting','create_word',{'file':'report.docx','blocks':[{'type':'paragraph','text':'Verified result 5.'}]}),
                ('writer','spreadsheets','edit_workbook',{'file':'data.xlsx','sheets':[{'name':'Results','rows':[['x'],[5]]}]}),
                ('writer','pdf-documents','process_pdf',{'file':'report.pdf','text':'Verified result 5.'}),
                ('researcher','paper-reading','extract_pdf',{'path':'report.pdf','output':'paper.json'}),
                ('mentor','research-review','audit',{'path':'report.pdf','operation':'fingerprint'}),
                ('mentor','research-review','audit',{'path':'.','operation':'submission'}),
            ]
            for role,skill,module,target in cases:
                with self.subTest(skill=skill,module=module,target=target):
                    (root/'input.json').write_text(json.dumps({'targets':[target]}),encoding='utf-8')
                    node=registry.nodes[role]
                    token=paths.bind(paths.Roots(root,skills=[s['name'] for s in node.skills],access=node.registry['access']))
                    try:
                        output=run_one(f'skill://{skill}/scripts/{module}.py',['--input','input.json','--base-dir','.'],90,root)
                        self.assertIn('completed',output)
                    finally:
                        paths.bind(None)
            self.assertEqual(json.loads((root/'stats.json').read_text())['mean'],2)
            self.assertEqual((root/'actual.txt').read_text(),'actual run')
            self.assertEqual(json.loads((root/'experiment.json').read_text())['status'],'completed')
            self.assertIn('Verified result 5.',(root/'paper.json').read_text())


if __name__=='__main__':unittest.main()
