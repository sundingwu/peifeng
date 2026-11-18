"""注册表行为：谁声明了哪项能力、未声明的不生效、只读角色拿不到写工具。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from backend.orchestration.orchestrate import delete_tool, upload_skill, upload_tool
from backend.orchestration.orchestrate import load_registry, load_tools, unlisted

TOOL_SOURCE = '''"""示例工具：只为注册表测试服务。"""
information = {
    "name": "read",
    "description": "示例工具",
    "parameters": {"type": "object", "properties": {"targets": {"type": "array"}}, "required": ["targets"]},
}


def read(targets, base_dir=None):
    return "ok"
'''


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.root = Path(self._folder.name)
        for name in ("agents", "tools", "skills", "prompts"):
            (self.root / name).mkdir()
        (self.root / "prompts" / "demo.md").write_text("# demo\n", encoding="utf-8")
        self.registry: dict = {}

    def tearDown(self):
        self._folder.cleanup()

    def write_registry(self, tools=None, skills=None, access="read-write"):
        """整份注册表就一个 agents.yaml：这里维护内存副本，落盘时整体写入。"""
        self.registry["demo"] = {
            "kind": "worker",
            "impl": "worker",
            "description": "测试角色",
            "prompt": "demo.md",
            "access": access,
            "tools": list(tools or []),
            "skills": list(skills or []),
        }
        self.flush()

    def add_role(self, role: str, skills=None):
        current = yaml.safe_load((self.root / "agents" / "agents.yaml").read_text(encoding="utf-8")) or {}
        current[role] = {
            "kind": "worker",
            "impl": "worker",
            "description": "另一个角色",
            "prompt": "demo.md",
            "access": "read-write",
            "tools": [],
            "skills": list(skills or []),
        }
        self.registry = current
        self.flush()

    def flush(self):
        (self.root / "agents" / "agents.yaml").write_text(
            yaml.safe_dump(self.registry, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )

    def write_tool(self, name: str, body: str = TOOL_SOURCE):
        (self.root / "tools" / f"{name}.py").write_text(body.replace('"read"', f'"{name}"'), encoding="utf-8")

    def test_listed_but_missing_tool_is_error(self):
        self.write_registry(tools=["ghost"])
        entry, errors = load_registry(self.root, "demo")
        self.assertIsNotNone(entry)
        self.assertTrue(any("ghost" in item for item in errors), errors)

    def test_unlisted_tool_is_not_loaded(self):
        self.write_registry(tools=["read"])
        self.write_tool("read")
        self.write_tool("spare")
        entry, errors = load_registry(self.root, "demo")
        self.assertEqual(errors, [])
        tools, tool_errors = load_tools(self.root, entry)
        self.assertEqual(tool_errors, [])
        self.assertEqual([tool["name"] for tool in tools], ["read"])
        notes = unlisted(self.root, entry)
        self.assertTrue(any("spare" in note for note in notes), notes)

    def test_read_only_role_cannot_declare_write_tool(self):
        self.write_registry(tools=["write"], access="read-only")
        self.write_tool("write")
        entry, errors = load_registry(self.root, "demo")
        self.assertIsNotNone(entry)
        self.assertTrue(any("read-only" in item for item in errors), errors)

    def test_upload_registers_and_delete_unregisters(self):
        self.write_registry(tools=[])
        uploaded = upload_tool(self.root, "demo", TOOL_SOURCE, "read.py")
        self.assertEqual(uploaded["name"], "read")
        entry, errors = load_registry(self.root, "demo")
        self.assertEqual(errors, [])
        self.assertIn("read", [str(item) for item in entry["tools"]])
        tools, _ = load_tools(self.root, entry)
        self.assertEqual([tool["name"] for tool in tools], ["read"])

        removed = delete_tool(self.root, "demo", "read")
        self.assertTrue(removed["deleted_file"])
        self.assertFalse((self.root / "tools" / "read.py").exists())
        entry, _ = load_registry(self.root, "demo")
        self.assertEqual([str(item) for item in entry["tools"]], [])

    def test_upload_skill_registers_and_keeps_shared_file(self):
        self.write_registry(tools=[], skills=[])
        skill_body = "---\nname: my-skill\ndescription: 示例技能\n---\n正文\n"
        upload_skill(self.root, "demo", skill_body, "my-skill")
        self.add_role("other", skills=["my-skill"])
        from backend.orchestration.orchestrate import delete_skill

        removed = delete_skill(self.root, "demo", "my-skill")
        self.assertFalse(removed["deleted_dir"])
        self.assertEqual(removed["shared_with"], ["other"])
        self.assertTrue((self.root / "skills" / "my-skill" / "SKILL.md").is_file())

    def test_skill_list_shows_chinese_titles_and_keeps_custom_skill_fallback(self):
        from backend.orchestration.orchestrate import list_all
        self.write_registry()
        upload_skill(self.root, "demo", "---\nname: figures\ntitle: 科研绘图\ndescription: 图表与流程图\n---\n正文\n", "figures")
        upload_skill(self.root, "demo", "---\nname: custom\ndescription: 自定义\n---\n正文\n", "custom")
        skills = {row["name"]: row for row in list_all(self.root)["skills"]}
        self.assertEqual(skills["figures"]["title"], "科研绘图")
        self.assertEqual(skills["custom"]["title"], "custom")

    def test_loading_skill_script_does_not_create_bytecode_directory(self):
        import sys
        from unittest.mock import patch
        from backend.orchestration.orchestrate import load_module
        script = self.root / "skills" / "demo" / "scripts" / "probe.py"
        script.parent.mkdir(parents=True)
        script.write_text("value = 42\n", encoding="utf-8")
        with patch.object(sys, "dont_write_bytecode", False):
            self.assertEqual(load_module(script).value, 42)
        self.assertFalse((script.parent / "__pycache__").exists())


if __name__ == "__main__":
    unittest.main()
