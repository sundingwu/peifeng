"""技能箱：agent 装配出的技能集合。

与 :class:`~peifeng_agent.backend.agent.common.toolbox.ToolBox` 对称，但职责窄得多：
**只持有"我有哪些技能"（名字 + 一句描述）并产出 system 里的清单段**，不负责执行。
技能正文由模型用 ``read`` 直接读技能文件（路径见运行环境里的 ``<能力目录>`` 说明）——这样
system 里只有索引，上下文不被正文撑大。
"""
from __future__ import annotations

from typing import List, Optional


class SkillBox:
    """某个 agent 认领到的技能（``skill/<名字>/SKILL.md`` 的 frontmatter）。"""

    def __init__(self, skills: List[dict]) -> None:
        self.skills: List[dict] = list(skills)
        self._by_name = {str(skill.get("name")): skill for skill in self.skills}

    def __bool__(self) -> bool:
        return bool(self.skills)

    def __len__(self) -> int:
        return len(self.skills)

    @property
    def names(self) -> List[str]:
        """技能名（按目录顺序），用于诊断与测试。"""
        return [str(skill.get("name")) for skill in self.skills]

    def get(self, name: str) -> Optional[dict]:
        """按名字取技能条目（只含元数据，不含正文）。"""
        return self._by_name.get((name or "").strip())

    def index(self, title: str = "本角色可用技能") -> str:
        """system 里的技能清单段：只给名字与一句描述，够用来"选"。"""
        lines = [
            f"- {skill.get('name')}：{skill.get('description')}"
            for skill in self.skills
            if skill.get("name") and skill.get("description")
        ]
        if not lines:
            return ""
        return f"\n\n【{title}】\n" + "\n".join(lines)
