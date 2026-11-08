"""能力管理：查看角色/工具/技能，并从页面创建与删除。

**为什么要有它**：能力靠目录决定，好处是可审计，代价是"加个技能得自己去翻文件系统"。
这里把同一套目录约定包成页面能用的动作：查看、新建worker、上传工具与技能、删除。

**安全底线**（三条，改这块前先读）：

1. 所有写入路径都必须落在 ``agent_dir`` 之内：名字只允许 ``[a-z0-9_]``，再加 ``resolve_inside``
   二次校验——页面传来的字符串一律不可信。
2. 上传的工具/技能先写到临时位置**解析一遍**，通过校验才落盘：坏文件不能进目录（否则
   下次启动整个角色装配失败）。
3. ``planner`` / ``critic`` 是流程必需的核心角色，不允许删除。
"""
from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from peifeng_agent.backend.utils.loader import (
    WORKER_DEF_DIR,
    SKILL_DIR,
    TOOL_DIR,
    load_worker_specs,
    parse_skill,
    parse_tool,
    split_frontmatter,
)

#: 名字白名单：worker名、工具名、技能名都走这一套（防路径穿越与奇怪字符）
NAME_PATTERN = re.compile(r"^[a-z0-9_]{1,40}$")

#: 删除保护：这两个角色缺了流程就跑不起来
CORE_ROLES = ("planner", "critic")

#: worker共享工具目录（相对 ``agent_dir``）
SHARED_TOOLS = f"{WORKER_DEF_DIR}/common/{TOOL_DIR}"

#: 新建worker时生成的 prompt 骨架：只放"必须自己写"的部分，通用纪律由 COMMON_RULES 提供
EXECUTOR_PROMPT_TEMPLATE = """# Identity（身份）

{description}

# Instructions（指令）

1. **边界**：只做本角色职责范围内的事，越界的需求在 limitations 里说明并交回。
2. **交付**：产物落盘并写进 files（相对 <项目目录>）；summary 一句话写清方法与结果。

# Output Format（输出格式）

只输出一个 JSON 对象（不要解释、不要 Markdown 围栏、不要任何工具调用标记），字段全部保留：

```json
{{
  "status": "completed | partial | needs_input | needs_replan",
  "content": "可直接交付或复用的结果正文",
  "summary": "做法与目的：用什么方法达成了什么目的、产出在哪",
  "handoff": "给评审的交接说明：产出在哪、关键证据在哪个文件/哪一段、建议抽查哪几处",
  "question": "需要用户补充时填，否则空字符串",
  "files": ["成品路径"],
  "sources": ["采用的来源"],
  "limitations": ["已知限制或未完成项"]
}}
```
"""


def check_name(value: str, label: str) -> str:
    """校验名字白名单；不合法直接报错（不静默修正——页面要能告诉用户为什么不行）。"""
    name = str(value or "").strip()
    if not NAME_PATTERN.match(name):
        raise ValueError(f"{label}只能用英文小写、数字与下划线（1~40 字符），当前：{value!r}")
    return name


def role_dir(agent_dir: Path, agent: str) -> Path:
    """角色的根目录：worker在 ``executor/<name>``，核心角色在 ``agent/<name>``。"""
    name = check_name(agent, "角色名")
    base = Path(agent_dir)
    if name in CORE_ROLES:
        return base / name
    return base / WORKER_DEF_DIR / name


def safe_inside(root: Path, relative: str) -> Path:
    """把相对路径拼到 ``root`` 下并确认没跑出去；越界直接报错。"""
    target = (Path(root) / relative).resolve()
    base = Path(root).resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"路径越界：{relative}")
    return target


def list_capabilities(agent_dir: Path) -> list[dict]:
    """列出所有角色及其工具、技能（含来源路径与是否共享），供页面展示。

    worker带上 ``spec.py`` 里的职责描述——页面要显示"这个worker是干什么的"，
    否则用户只能看到一堆名字。共享工具仍以 ``shared=True`` 返回，由页面决定要不要展示。
    """
    base = Path(agent_dir)
    shared = base / SHARED_TOOLS
    descriptions = {
        spec["name"]: str(spec.get("description") or "").strip() for spec in load_worker_specs(base)[0]
    }
    roles: list[dict] = []

    def collect(role: str, kind: str, folder: Path) -> dict:
        tools: list[dict] = []
        for root in (folder / TOOL_DIR, shared if kind == "executor" else None):
            if root is None or not root.is_dir():
                continue
            for path in sorted(root.glob("*.py")):
                if path.name.startswith("_"):
                    continue
                try:
                    info = parse_tool(path)
                except Exception as error:  # noqa: BLE001 - 坏文件也要显示出来
                    tools.append(
                        {"name": path.stem, "description": f"（解析失败：{error}）", "path": str(path), "shared": False}
                    )
                    continue
                if info is not None:
                    tools.append(
                        {
                            "name": info["name"],
                            "description": info.get("description", ""),
                            "path": str(path),
                            "shared": root == shared,
                        }
                    )
        skills: list[dict] = []
        skill_root = folder / SKILL_DIR
        if skill_root.is_dir():
            for path in sorted(skill_root.glob("*/SKILL.md")):
                try:
                    info = parse_skill(path)
                except Exception as error:  # noqa: BLE001
                    skills.append({"name": path.parent.name, "description": f"（解析失败：{error}）", "path": str(path)})
                    continue
                skills.append({"name": info["name"], "description": info.get("description", ""), "path": str(path)})
        return {
            "name": role,
            "kind": kind,
            "description": descriptions.get(role, ""),
            "path": str(folder),
            "exists": folder.is_dir() and (folder / "prompt.md").is_file(),
            "deletable": kind == "executor",
            "tools": tools,
            "skills": skills,
        }

    for role in CORE_ROLES:
        roles.append(collect(role, "core", base / role))
    workers = base / WORKER_DEF_DIR
    if workers.is_dir():
        for folder in sorted(path for path in workers.iterdir() if (path / "spec.py").is_file()):
            roles.append(collect(folder.name, "executor", folder))
    return roles


def create_executor(agent_dir: Path, name: str, description: str, prompt: str = "") -> dict:
    """新建一个worker：``spec.py`` + ``prompt.md``（不生成工具，工具靠上传或共享目录）。"""
    role = check_name(name, "worker名")
    if role in CORE_ROLES:
        raise ValueError(f"{role} 是核心角色名，不能用作worker")
    summary = str(description or "").strip()
    if not summary:
        raise ValueError("请填写职责描述（Planner 靠它决定把任务派给谁）")
    target = safe_inside(Path(agent_dir) / WORKER_DEF_DIR, role)
    if target.exists():
        raise ValueError(f"worker {role} 已存在")
    target.mkdir(parents=True)
    (target / "spec.py").write_text(
        '"""worker定义：目录名即worker名，SPEC 给 Planner 分派依据。"""\n\n'
        "SPEC = {\n"
        f'    "name": "{role}",\n'
        f'    "description": {summary!r},\n'
        "}\n",
        encoding="utf-8",
    )
    body = str(prompt or "").strip() or EXECUTOR_PROMPT_TEMPLATE.format(description=summary)
    (target / "prompt.md").write_text(body + "\n", encoding="utf-8")
    return {"name": role, "path": str(target)}


def delete_executor(agent_dir: Path, name: str) -> dict:
    """删除一个worker目录（核心角色与不存在的角色都拒绝）。"""
    role = check_name(name, "worker名")
    if role in CORE_ROLES:
        raise ValueError(f"{role} 是核心角色，不能删除")
    target = safe_inside(Path(agent_dir) / WORKER_DEF_DIR, role)
    if not target.is_dir():
        raise ValueError(f"worker {role} 不存在")
    shutil.rmtree(target)
    return {"name": role}


def upload_tool(agent_dir: Path, agent: str, content: str, filename: str = "") -> dict:
    """上传一个工具 .py：**先解析校验**再落盘，坏文件不进目录。

    校验三件事：名字合规、``information`` 元数据齐全、导出了与工具同名的函数。
    """
    role = check_name(agent, "角色名")
    stem = Path(str(filename or "").strip()).stem
    name = check_name(stem, "工具名（文件名）")
    body = str(content or "")
    if not body.strip():
        raise ValueError("文件内容为空")
    with tempfile.TemporaryDirectory() as folder:
        probe = Path(folder) / f"{name}.py"
        probe.write_text(body, encoding="utf-8")
        try:
            info = parse_tool(probe)
        except Exception as error:  # noqa: BLE001 - 统一成"上传被拒"的提示
            raise ValueError(f"这个文件不是合法工具：{error}") from error
    if info is None:
        raise ValueError("文件被当成内部模块（下划线开头），换个文件名")
    if info["name"] != name:
        raise ValueError(f"工具名「{info['name']}」与文件名「{name}」不一致：模型按文件名找工具，两者必须相同")
    target = safe_inside(role_dir(agent_dir, role) / TOOL_DIR, f"{name}.py")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return {"agent": role, "name": name, "path": str(target)}


def delete_tool(agent_dir: Path, agent: str, name: str) -> dict:
    """删除角色自己目录下的工具（共享工具不在这个入口删）。"""
    role = check_name(agent, "角色名")
    tool = check_name(name, "工具名")
    target = safe_inside(role_dir(agent_dir, role) / TOOL_DIR, f"{tool}.py")
    if not target.is_file():
        raise ValueError(f"{role} 名下没有工具 {tool}")
    target.unlink()
    return {"agent": role, "name": tool}


def upload_skill(agent_dir: Path, agent: str, content: str, name: str = "") -> dict:
    """上传一个技能：``<角色>/skill/<name>/SKILL.md``，frontmatter 的 name 必须与目录名一致。"""
    role = check_name(agent, "角色名")
    skill = check_name(name, "技能名（目录名）")
    body = str(content or "")
    meta, _ = split_frontmatter(body)
    declared = str(meta.get("name") or "").strip()
    if not declared:
        raise ValueError("SKILL.md 开头需要一个 frontmatter 块，至少写 name: <技能名>")
    if declared != skill:
        raise ValueError(f"frontmatter 的 name「{declared}」与技能目录名「{skill}」不一致")
    target = safe_inside(role_dir(agent_dir, role) / SKILL_DIR, f"{skill}/SKILL.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body if body.endswith("\n") else body + "\n", encoding="utf-8")
    return {"agent": role, "name": skill, "path": str(target)}


def delete_skill(agent_dir: Path, agent: str, name: str) -> dict:
    role = check_name(agent, "角色名")
    skill = check_name(name, "技能名")
    target = safe_inside(role_dir(agent_dir, role) / SKILL_DIR, skill)
    if not (target / "SKILL.md").is_file():
        raise ValueError(f"{role} 名下没有技能 {skill}")
    shutil.rmtree(target)
    return {"agent": role, "name": skill}


def resolve_role_folder(agent_dir: Path, agent: str) -> Optional[Path]:
    """角色的目录；不存在返回 ``None``（页面用它决定要不要显示"尚未创建"）。"""
    folder = role_dir(agent_dir, agent)
    return folder if folder.is_dir() else None
