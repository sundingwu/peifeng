"""验证精简后的资源闭包与新增可计算能力，不把模板数量当能力。"""
import json
from pathlib import Path
import re
import tempfile
import unittest

from reportlab.pdfgen.canvas import Canvas

from backend.orchestration.orchestrate import build_registry
from backend.utils.skills import load_skill_script
from config import Settings


class SkillFusionTests(unittest.TestCase):
    def test_local_reference_graph_has_no_broken_or_unreachable_files(self):
        root=Path(__file__).resolve().parents[1]/'backend/skills'
        for home in root.iterdir():
            if not (home/'SKILL.md').is_file():continue
            seen=set();pending=[home/'SKILL.md']
            while pending:
                path=pending.pop().resolve()
                if path in seen:continue
                seen.add(path)
                text=path.read_text(encoding='utf-8')
                text=re.sub(r'```.*?```','',text,flags=re.S)
                text=re.sub(r'`[^`]+`','',text)
                relative_paths=re.findall(r'(?<![\w/-])(?:references|scripts)/[A-Za-z0-9_.-]+',text)
                for relative in relative_paths:
                    target=(home/relative).resolve()
                    self.assertTrue(target.is_relative_to(home.resolve()))
                    self.assertTrue(target.is_file(),(home.name,relative))
                    if target.suffix=='.md':pending.append(target)
            # 模板和schema不要求正文读取；所有人读参考必须从入口可达。
            for reference in home.glob('references/*.md'):
                self.assertIn(reference.resolve(),seen,(home.name,reference.name))

    def test_screening_agreement_and_prevalence_degeneracy(self):
        module=load_skill_script('researcher','literature-review','screening')
        make=lambda identifier,decision:{'id':identifier,'stage':'title_abstract','decision':decision,
                                        **({'reason':'outside scope'} if decision=='exclude' else {})}
        actual=[make('a','include'),make('b','uncertain'),make('c','exclude'),make('d','exclude'),make('extra','include')]
        reference=[make('a','include'),make('b','exclude'),make('c','include'),make('d','exclude')]
        result=module.compare_decisions(actual,reference)
        stage=result['stages']['title_abstract']
        self.assertEqual(stage['n'],4)
        self.assertEqual(stage['agreement'],.5)
        self.assertEqual(stage['kappa'],0)
        self.assertEqual(stage['pabak'],0)
        self.assertEqual(result['actual_only'],[{'id':'extra','stage':'title_abstract'}])
        degenerate=module.compare_decisions([make('a','include')],[make('a','uncertain')])['stages']['title_abstract']
        self.assertEqual(degenerate['agreement'],1)
        self.assertIsNone(degenerate['kappa'])
        self.assertIsNone(module.compare_decisions([],[])['stages']['title_abstract']['agreement'])

    def test_partial_source_bundle_and_unchecked_inventory_are_honest(self):
        extractor=load_skill_script('researcher','paper-reading','extract_pdf')
        auditor=load_skill_script('mentor','research-review','audit_paper_card')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);pdf=Canvas(str(root/'paper.pdf'))
            pdf.drawString(50,700,'First page');pdf.showPage()
            pdf.drawString(50,700,'Second page');pdf.save()
            bundle=json.loads(extractor.paper_target({'path':'paper.pdf','pages':[2],'source_bundle':True},root))
            self.assertEqual(bundle['page_count'],2)
            self.assertEqual(bundle['pages'][0]['pdf_page'],2)
            self.assertEqual(bundle['level'],'extracted_partial_text')
            self.assertFalse(bundle['full_scope'])
            self.assertEqual(bundle['inventory_status'],'not_checked')
            card='\n'.join(['> locator: page-grounded']+[f'> field{i}: supplied' for i in range(6)])+'\n'
            card+='\n'.join(f'## {i:02d} Section\n[Paper: PDF p.2]\n' for i in range(1,17))
            report=auditor.audit(card,bundle,'page-grounded')
            item=next(row for row in report['findings'] if row['code']=='source_inventory_unavailable')
            self.assertEqual(item['level'],'warning')
            self.assertFalse(any(row['code']=='figures_coverage' and row['level']=='pass' for row in report['findings']))
            for pages in [[],[1,1],[True],[3]]:
                with self.subTest(pages=pages),self.assertRaises(ValueError):
                    extractor.paper_target({'path':'paper.pdf','pages':pages},root)

    def test_new_screening_option_runs_through_registered_python_boundary(self):
        registry=build_registry(Settings(),object())
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            rows=[{'id':'p1','stage':'title_abstract','decision':'uncertain'}]
            for name in ['actual','human']:(root/(name+'.json')).write_text(json.dumps(rows),encoding='utf-8')
            (root/'input.json').write_text(json.dumps({'path':'actual.json','reference':'human.json','output':'result.json'}),encoding='utf-8')
            runner=registry.nodes['researcher'].toolbox.runner(workspace=root)
            text,failed=runner('run_code',{'targets':[{"file_path": "skill://literature-review/scripts/screening.py", "args": ["--input", "input.json", "--base-dir", "."]}]})
            self.assertFalse(failed,text)
            report=json.loads((root/'result.json').read_text(encoding='utf-8'))
            self.assertEqual(report['comparison']['stages']['title_abstract']['n'],1)


if __name__=='__main__':unittest.main()
