"""能力资源加载：按 agent 认领自己目录下的工具、技能与提示词。

**目录即归属**：一个 agent 的全部能力都在它自己的目录里——

- 固定角色：``agent/<角色>/{prompt.md, tool/, skill/}``
- worker：``agent/executor/<名字>/{spec.py, prompt.md, tool/, skill/}``

本模块只回答"某个 agent 的目录里有什么"；挑与不挑由 agent 在启动装配时自己决定
（``BaseAgent.assemble``），装配层不替它选。工具与技能文件导出 ``information`` 元数据、
以及同名可调用对象；下划线开头的文件跳过。单个坏条目只记入错误列表，不影响其余条目。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Callable, Optional

#: 条目解析器：接收条目路径，返回条目数据；返回 None 表示跳过该条目。
EntryParser = Callable[[Path], Any]

#: agent 目录内的子目录 / 文件名
TOOL_DIR = "tool"
#: worker共享工具目录（相对 ``agent_dir``）：``executor/common/tool``，只有worker加载
SHARED_TOOL_DIR = "common/tool"
SKILL_DIR = "skill"
PROMPT_FILE = "prompt.md"
SPEC_FILE = "spec.py"

#: ``agent/`` 下存放worker定义的目录（每个子目录一份 ``spec.py``）
WORKER_DEF_DIR = "executor"


def agent_home(agent_dir, agent: str, is_worker: bool = False) -> Path:
    """该 agent 自己的目录：固定角色 ``<agent_dir>/<角色>``，worker ``<agent_dir>/executor/<名字>``。"""
    root = Path(agent_dir)
    return root / WORKER_DEF_DIR / agent if is_worker else root / agent


def discover_entries(root, pattern: str, parse: EntryParser) -> tuple[list, list[str]]:
    """扫描 ``root`` 下匹配 ``pattern`` 的条目并逐项解析。

    返回 ``(条目列表, 错误列表)``；``root`` 不存在时返回空列表与一条说明。
    """
    base = Path(root)
    if not base.is_dir():
        return [], [f"目录不存在：{base}"]
    entries: list = []
    errors: list[str] = []
    for path in sorted(base.glob(pattern)):
        try:
            entry = parse(path)
        except Exception as error:  # noqa: BLE001 - 单个坏条目不应中断整体加载
            errors.append(f"{path}: {type(error).__name__}: {error}")
            continue
        if entry is not None:
            entries.append(entry)
    return entries, errors


def load_agent_tools(agent_dir, agent: str, is_worker: bool = False) -> tuple[list, list[str]]:
    """加载**一个 agent 能用的工具**：自己的 ``tool/`` + worker共享目录。

    归属完全由目录决定，不读任何元数据：

    - ``<自己的目录>/tool/``：该角色自己的工具（固定角色的 ``read`` 就在这里，各自一份）；
    - ``<agent_dir>/executor/common/tool/``：**所有worker**共用的工具（``read`` / ``write`` /
      ``move`` / ``delete`` / ``bash``），只有worker加载。

    所以 Planner / Critic 天然拿不到 ``write`` / ``bash``：共享目录只对worker生效。
    没有 ``tool/`` 目录是正常的（只靠技能与提示词干活）；共享目录不存在时跳过。
    """
    folders = [agent_home(agent_dir, agent, is_worker) / TOOL_DIR]
    if is_worker:
        folders.append(Path(agent_dir) / WORKER_DEF_DIR / SHARED_TOOL_DIR)
    entries: list = []
    errors: list[str] = []
    seen: dict[str, str] = {}
    for folder in folders:
        if not folder.is_dir():
            continue
        found, problems = discover_entries(folder, "*.py", parse_tool)
        errors.extend(problems)
        for tool in found:
            if tool["name"] in seen:
                errors.append(
                    f"{agent}：{tool['name']} 在 {seen[tool['name']]} 与 {folder} 各有一份，"
                    "公共工具只应放在共享目录里"
                )
                continue
            seen[tool["name"]] = str(folder)
            entries.append(tool)
    entries.sort(key=lambda tool: tool["name"])  # 清单顺序稳定，模型看到的工具表不随目录顺序变
    return entries, errors


def load_agent_skills(agent_dir, agent: str, is_worker: bool = False) -> tuple[list, list[str]]:
    """加载**一个 agent 自己目录下的技能**（``<自己的目录>/skill/*/SKILL.md``）。

    技能名必须与目录名一致：模型是按「``skill/<技能名>/SKILL.md``」拼路径去 ``read`` 的
    （见 ``build_environment``），两者不一致就等于给了一条读不到的路径。
    """
    folder = agent_home(agent_dir, agent, is_worker) / SKILL_DIR
    if not folder.is_dir():
        return [], []
    entries, errors = discover_entries(folder, "*/SKILL.md", parse_skill)
    for entry in entries:
        entry["agent"] = agent
        folder_name = Path(entry["path"]).parent.name
        if entry["name"] != folder_name:
            errors.append(
                f"{agent}：技能 {entry['name']} 的目录名是 {folder_name}，两者必须一致"
                "（模型按 <技能名>/SKILL.md 拼路径读取）"
            )
    return entries, errors


def load_prompt(agent_dir, agent: str, is_worker: bool = False) -> tuple[str, list[str]]:
    """加载**一个 agent 自己的提示词**（``<自己的目录>/prompt.md``）。

    提示词与工具、技能同属该 agent 的目录：新增或改写一个worker的角色说明，只动它自己的
    目录，不碰代码。
    """
    path = agent_home(agent_dir, agent, is_worker) / PROMPT_FILE
    if not path.is_file():
        return "", [f"提示词文件缺失：{path}"]
    return path.read_text(encoding="utf-8").strip(), []


def load_worker_specs(agent_dir) -> tuple[list[dict], list[str]]:
    """加载worker注册表：扫描 ``agent/executor/*/spec.py``，**凡是导出 ``SPEC`` 的就是一个worker**。

    ``SPEC`` 给 ``name``（缺省用目录名）与 ``description``（给 Planner 的分派依据）。
    worker的角色实现是公用的 ``agent/executor/agent.py``，所以新增worker不用写代码。
    """
    root = Path(agent_dir) / WORKER_DEF_DIR
    if not root.is_dir():
        return [], [f"目录不存在：{root}"]
    specs: list[dict] = []
    errors: list[str] = []
    for path in sorted(root.glob(f"*/{SPEC_FILE}")):
        try:
            module = load_module(path)
        except Exception as error:  # noqa: BLE001 - 单个坏模块只记一条
            errors.append(f"{path}: {type(error).__name__}: {error}")
            continue
        spec = getattr(module, "SPEC", None)
        if not isinstance(spec, dict):
            continue  # 不是worker定义模块（内部辅助），跳过
        # 能力按目录定位（prompt.md / tool/ / skill/ 都在同目录），所以worker名必须是目录名
        name = path.parent.name
        declared = str(spec.get("name") or "").strip()
        if declared and declared != name:
            errors.append(f"{path}: SPEC 的 name「{declared}」与目录名「{name}」不一致，按目录名注册")
        specs.append(
            {
                "name": name,
                "description": str(spec.get("description") or "").strip(),
                "path": str(path.resolve()),
            }
        )
    return specs, errors


def split_frontmatter(text: str) -> tuple[dict, str]:
    """解析 SKILL.md：开头 ``---`` 块为元数据，其余为正文；失败返回空 dict + 全文。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict = {}
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return meta, "\n".join(lines[index + 1:]).strip()
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    return meta, ""


def parse_tool(path: Path) -> Optional[dict]:
    """解析一个工具文件；下划线开头的文件视为内部模块并跳过。

    可见范围由目录决定（见 :func:`load_agent_tools`），元数据里不再声明 ``audience``。
    """
    if path.name.startswith("_"):
        return None
    module = load_module(path)
    information = getattr(module, "information", None)
    if not isinstance(information, dict):
        raise ValueError("未导出 information 元数据")
    name = str(information.get("name") or "").strip()
    function = getattr(module, name, None)
    if not name or not callable(function):
        raise ValueError("未导出与 information['name'] 同名的函数")
    return {
        "name": name,
        "description": str(information.get("description") or ""),
        "parameters": information.get("parameters") or {"type": "object", "properties": {}},
        "func": function,
        "source": str(path.resolve()),
    }


def parse_skill(path: Path) -> dict:
    """解析一个 SKILL.md：name 必填；角色由调用方按目录赋值。"""
    meta, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    name = str(meta.get("name") or "").strip()
    if not name:
        raise ValueError("SKILL.md frontmatter 缺少 name")
    return {
        "name": name,
        "description": str(meta.get("description") or "").strip(),
        "path": str(path.resolve()),
    }


#: 已加载工具模块的缓存：键为 (路径, mtime)，避免每次切会话都重新执行顶层代码
_MODULE_CACHE: dict = {}


def load_module(path: Path):
    """按文件路径动态加载工具模块。

    模块名取 ``tools/`` 之后的相对路径：``common/read.py`` → ``peifeng_agent_tool_common_read``，
    ``executor/common/write.py`` → ``peifeng_agent_tool_executor_common_write``。只带 parent.name
    不够——``common/`` 在两层里都出现，会让同名文件互相覆盖。
    按 (路径, mtime) 缓存，文件改了才重新加载。
    """
    module_name = f"peifeng_agent_tool_{'_'.join(relative_slug(path))}"
    try:
        stamp = path.stat().st_mtime
    except OSError:
        stamp = 0.0
    cached = _MODULE_CACHE.get((str(path), stamp))
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载工具：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    _MODULE_CACHE[(str(path), stamp)] = module
    return module


def relative_slug(path: Path) -> list[str]:
    """``agent/`` 之后的路径片段（去掉扩展名），用来拼唯一模块名。

    ``agent/executor/coding/tool/read.py`` → ``executor_coding_tool_read``：带上角色与
    worker名，``read`` 这类同名文件在多个目录下并存时不会互相覆盖。
    """
    parts = list(path.with_suffix("").parts)
    for index in range(len(parts) - 1, -1, -1):
        if parts[index] == "agent":
            parts = parts[index + 1:]
            break
    else:
        parts = parts[-2:]
    return [part for part in parts if part not in {"", ".", "..", "\\"}]
