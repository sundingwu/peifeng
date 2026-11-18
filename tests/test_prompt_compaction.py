"""精简后仍能找到专业规则，阶段协议不再污染所有常驻上下文。"""
import json
from pathlib import Path
import tempfile
import unittest

from backend.agents.base import PlanningRequest, ReviewRequest, PlanStep, ExecutionResult
from backend.agents.mentor import parse_planning_result, parse_review_result
from backend.orchestration.orchestrate import build_registry
from config import Settings


class PromptCompactionTests(unittest.TestCase):
    def setUp(self):
        self.registry=build_registry(Settings(),object())
        self.assertFalse(self.registry.errors)

    def test_resident_context_keeps_contracts_without_domain_recipes(self):
        for role,node in self.registry.nodes.items():
            with self.subTest(role=role):
                self.assertLess(len(node.system_prompt)+len(node.system_tail),3500)
                self.assertIn('白名单',node.system_prompt)
                self.assertIn('【技能读取】',node.system_tail)
                self.assertNotIn('ROUND_HALF_UP',node.system_prompt)
                self.assertNotIn('source_sha256',node.system_prompt)
                self.assertNotIn('只回复审核JSON',node.system_prompt)
        self.assertEqual(set(self.registry.nodes['mentor'].toolbox.names),{'read'})
        self.assertNotIn('脚本用run_code', self.registry.nodes['mentor'].system_tail)
        self.assertIn('具备对应skill的Worker', self.registry.nodes['mentor'].system_tail)

    def test_deferred_rules_are_reachable_with_each_roles_actual_permissions(self):
        cases=[
            ('engineer','research-coding','methods.md','工程实现与验证'),
            ('engineer','data-analysis','methods.md','数据交付与科学解释'),
            ('researcher','literature-search','methods.md','检索停止与来源范围'),
            ('researcher','research-design','methods.md','证据分析与交接'),
            ('researcher','paper-reading','methods.md','来源解读与教学交付'),
            ('writer','paper-writing','methods.md','证据边界与精简修改'),
            ('writer','document-formatting','operations.md','结构核验与版本'),
            ('mentor','research-planning','methods.md','自包含派发与调整'),
            ('mentor','research-review','methods.md','交付物核验与最小返修'),
            ('mentor','research-review','inference-boundaries.md','未读取字段'),
        ]
        with tempfile.TemporaryDirectory() as folder:
            for role,skill,file,heading in cases:
                with self.subTest(role=role,skill=skill):
                    runner=self.registry.nodes[role].toolbox.runner(workspace=folder)
                    body,failed=runner('read',{'targets':[{'skill':skill,'path':'references/'+file,'grep':heading}]})
                    self.assertFalse(failed,body)
                    self.assertIn(heading,body)
                    home=Path('backend/skills')/skill
                    entry=(home/'SKILL.md').read_text(encoding='utf-8')
                    self.assertIn(file,entry)

    def test_mentor_phase_contracts_remain_parseable_and_separate(self):
        mentor=self.registry.nodes['mentor']
        planning=mentor.build_planning_request(PlanningRequest(task='核对资料并交付报告'))
        examples=[json.loads(line) for line in planning.splitlines() if line.startswith('{')]
        self.assertEqual([parse_planning_result(json.dumps(row)).mode for row in examples],['plan','clarify','unable'])
        self.assertNotIn('只回复审核JSON',planning)
        request=ReviewRequest(task='交付报告',step=PlanStep(worker='writer',goal='制作报告'),result=ExecutionResult(status='completed',summary='报告已生成'))
        review=mentor.build_request(request)
        example=json.loads(review.split('只回复审核JSON：',1)[1].splitlines()[0])
        self.assertTrue(parse_review_result(json.dumps(example)).approved)
        self.assertNotIn('"mode":"clarify"',review)
        self.assertNotIn('"mode":"plan"',mentor.system_prompt)


if __name__=='__main__':unittest.main()
