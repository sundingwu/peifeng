"""装配：启动时把「谁」装好，之后由编排层统一调度。

- build_registry：扫worker注册表（``agents/*.py`` 里导出 ``SPEC`` 的模块），构造 1 个 Planner、
  1 个 Critic 与**每个worker一个实例**，然后**让每个 agent 自己装配自己**（``BaseAgent.assemble``
  认领 ``tools/``、``skills/``、``prompts/`` 下属于自己的部分）。
  装配只做一次，产物常驻：编排层拿到的是"装好的节点表"，按名字派发即可。
- build_environment：当前项目的运行环境说明（每次任务由编排层注入，不进 system 固定部分）。
- copy_artifacts：任务完成后把声明的成果文件复制到项目工作文件夹（同名不覆盖）。

本模块只做构造与交付，不参与流程判断，也不替 agent 挑工具/技能（那是 agent 自己的事）。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from peifeng_agent.backend.agent.common.llm import OpenAIProvider
from peifeng_agent.backend.agent.critic.agent import CriticAgent
from peifeng_agent.backend.agent.executor.agent import ExecutorAgent
from peifeng_agent.backend.agent.planner.agent import PlannerAgent
from peifeng_agent.backend.utils.contracts import ARTIFACT_SUFFIXES, CORE_ROLES, AgentNode, TaskResult
from peifeng_agent.backend.utils.loader import load_worker_specs
from peifeng_agent.config import Settings


@dataclass
class AgentRegistry:
    """启动时装配好的全部角色：编排层按名字统一调度。"""

    #: 角色名 → 已装配的 agent（planner / critic / 各worker）
    nodes: dict[str, AgentNode]
    #: worker定义（注册表顺序：name / description / body / path），给 Planner 做分派依据
    specs: list[dict] = field(default_factory=list)
    #: 装配期错误（缺文件、坏条目…），由应用层推给页面
    errors: list[str] = field(default_factory=list)

    @property
    def executor_names(self) -> list[str]:
        return [spec["name"] for spec in self.specs]

    #: 回退时的优先顺序：coding 最通用（能跑脚本、读写文件），其次文件管理、文档、图表
    DEFAULT_PRIORITY = ("coding", "file_manager", "office_docs", "chart", "web_search")

    @property
    def default_executor(self) -> str:
        """默认worker：计划里 worker 为空或未知时回退到它。

        **不能取"目录扫描的第一个"**——那个顺序由文件名决定，实测第一个是 ``chart``：
        于是任何写错的 worker 名都会落到"只会画图"的角色上，任务直接做不了。
        按能力从宽到窄排：coding（通用）→ file_manager → office_docs → chart → web_search。
        """
        available = self.executor_names
        if not available:
            return ""
        for name in self.DEFAULT_PRIORITY:
            if name in available:
                return name
        return available[0]


def build_registry(settings: Settings, provider: Any = None) -> AgentRegistry:
    """启动时装配一次：读注册表 → 构造各角色 → 让每个 agent 各自认领目录并装配自己。"""
    if provider is None:
        provider = OpenAIProvider(
            settings.model,
            settings.base_url,
            api_key=settings.api_key,
            timeout=settings.llm_timeout,
            stream=settings.stream,
        )
    specs, errors = load_worker_specs(settings.agent_dir)
    nodes: dict[str, AgentNode] = {}

    planner = PlannerAgent(
        provider,
        temperature=settings.temperature,
        max_tool_rounds=settings.planner_rounds,
        max_steps=settings.plan_max_steps,
    )
    # 可用worker清单是装配产物（启动时固定）：进 planner 的 system 固定层，不再每个规划请求重复
    planner.extra_system = build_executor_index(specs) + plan_limits(settings.plan_max_steps)
    errors.extend(assemble_agent(planner, settings))
    nodes["planner"] = planner

    for spec in specs:
        name = spec["name"]
        if name in CORE_ROLES:
            errors.append(f"worker名与固定角色冲突：{name}（已跳过）")
            continue
        agent = ExecutorAgent(
            provider,
            name=name,
            description=str(spec.get("description") or ""),
            temperature=settings.temperature,
            max_tool_rounds=settings.max_tool_rounds,
        )
        errors.extend(assemble_agent(agent, settings))
        nodes[name] = agent

    critic = CriticAgent(provider, temperature=settings.temperature, max_tool_rounds=settings.critic_rounds)
    errors.extend(assemble_agent(critic, settings))
    nodes["critic"] = critic

    return AgentRegistry(nodes=nodes, specs=[spec for spec in specs if spec["name"] in nodes], errors=errors)


def build_executor_index(specs: list) -> str:
    """可用worker清单（名字 + 职责）：装配时拼进 Planner 的 system 固定层。

    worker注册表在启动时就固定了，所以这份清单是**稳定信息**——放进 system 而不是每个
    规划请求里重复携带。
    """
    lines = [f"- {spec['name']}：{spec.get('description')}" for spec in specs]
    if not lines:
        return ""
    return "【可用worker】\n" + "\n".join(lines) + (
        "\n为每个步骤指定 worker：从上面挑一个最匹配的worker名；技能只能从该worker自己的技能清单里选"
    )


def plan_limits(max_steps: int) -> str:
    """计划步数上限 + 合并 / 并行 / 串行的选择判据（拼进 Planner 的 system 固定层）。

    步数是**成本的第一杠杆**：每步固定 2 次完整模型往返（执行 + 独立评审）；同一步里塞多少活
    决定**耗时**（一次生成越长越慢）。判据后附一组实测数据——讲数据比讲道理更容易照做。
    """
    return (
        f"\n【计划步数】最多 {max_steps} 步。每步 = 2 次完整模型往返（执行 + 独立评审）。按三条选：\n"
        "\n**A. 合并成 1 步（默认）**：同类交付物、每份都短（合计 < 1200 字）、彼此无依赖。"
        "worker会用 `write(files=[...])` 一次写完。\n"
        "**B. 拆并行（同 `group` > 0，必须相邻）**：满足任一 → 子任务 ≥ 3 个且每个都要实质产出；"
        "合计输出预计 > 1200 字；需要不同能力（文档 + 图表 + 检索）。同组同时执行、**整组只评审一次**。\n"
        "**C. 只能串行**：后一步必须看到前一步的产出才能决定怎么做（先清点再按结果归档、先合并再成册）。\n"
        "\n实测（4 份 300 字文档）：合并 1 步 = 5 轮 23 秒；4 步并行 = 11 轮 14 秒；4 步串行 = 39 轮 107 秒。"
        "\n所以不要为了\"条理清晰\"把同类小任务拆成串行多步，也不要写\"核对 / 验收 / 再检查一遍\"这类步骤"
        "（评审每步都做）。"
    )


#: 装配产物的进程内缓存：键是"角色名 + 是否worker"。
#: 实测装配一次要 1 秒（读 40+ 工具文件、解析 schema、拼 prompt），而这些内容**跨会话完全相同**；
#: 缓存后新会话只花几十毫秒。缓存里存的是不可变字符串，agent 实例仍然每会话新建（上下文有状态）。
_ASSEMBLY_CACHE: dict[str, dict] = {}


def assemble_agent(agent, settings: Settings) -> list[str]:
    """让一个 agent 装配自己（装配过就复用缓存），并把错误标上角色名。

    缓存只省"读文件 + 拼字符串"这段；`context`（对话）永远新建——那是每会话独立的状态。
    配置目录（agent_dir / data_dir）变了就作废，避免改了技能或偏好还吃旧缓存。
    """
    from peifeng_agent.backend.agent.common.context import MessageContext
    from peifeng_agent.backend.agent.common.skillbox import SkillBox
    from peifeng_agent.backend.agent.common.toolbox import ToolBox

    stamp = f"{settings.agent_dir}|{settings.data_dir}"
    key = f"{agent.name}:{int(agent.is_worker)}"
    cached = _ASSEMBLY_CACHE.get(key)
    if cached is not None and cached["stamp"] == stamp:
        agent.tools = cached["tools"]
        agent.skills = cached["skills"]
        agent.tool_defs = cached["tool_defs"]
        agent.system_prompt = cached["system_prompt"]
        agent.toolbox = ToolBox(agent.tools)
        agent.skillbox = SkillBox(agent.skills)
        agent.context = MessageContext(system_prompt=agent.system_prompt, suffix=agent.context.suffix)
        return []

    try:
        errors = [f"{agent.name}：{message}" for message in agent.assemble(settings)]
    except Exception as error:  # noqa: BLE001 - 单个 agent 装配失败不应影响其它 agent
        return [f"{agent.name}：装配失败 {type(error).__name__}: {error}"]
    _ASSEMBLY_CACHE[key] = {
        "stamp": stamp,
        "tools": agent.tools,
        "skills": agent.skills,
        "tool_defs": agent.tool_defs,
        "system_prompt": agent.system_prompt,
    }
    return errors


#: 会话数据文件的形状说明在「通用规则」里（agent/common/rules.py），这里不再重复


def build_environment(settings: Settings, workspace: Path, session_dir: Path, work_dir: Path) -> str:
    """当前项目的运行环境：**只有会随项目/会话变化的事实**（路径 + 平台）。

    "怎么读、怎么接续"这类规则在 ``agent/common/rules.py`` 的「通用规则」里（system 固定层，
    装配一次）；这里只回答"东西在哪"。把不变量前移也让前缀缓存能命中更多 token。
    ``<项目目录>`` 是用户真正的工作文件夹：回答「目录下有什么」、读写产出文件都以它为准；
    ``<中间目录>`` 只放运行期间的脚本与临时文件，不交给用户。
    """
    platform_hint = (
        "Windows（命令用 cmd 语法：dir/type/cd /d；ls/pwd/mkdir -p 的等价能力分别用 dir、cd、mkdir）"
        if os.name == "nt"
        else "Linux/macOS"
    )
    return "\n".join(
        [
            "<运行环境>",
            f"<运行平台>：{platform_hint}",
            f"<项目目录>：{workspace}（用户的工作文件夹：产出文件写这里，用户也在这里取文件；"
            "用户问「目录下有什么」「当前目录」时指的就是它）",
            f"<中间目录>：{work_dir}（运行期间的脚本与临时文件，不交给用户）",
            f"<会话目录>：{session_dir}（summary.json 与 detail/ 明细目录所在处）",
            "</运行环境>",
        ]
    )


def copy_artifacts(final: TaskResult, work_dir: Optional[Path], target_dir: Path) -> list[Path]:
    """把各步骤声明的成果文件复制到交付目录，返回实际复制的文件。

    未初始化会话（work_dir 为空）或任务非 completed 时不交付；相对路径按交付目录解析
    （不依赖进程工作目录）；同名文件已存在时跳过，复制失败只跳过（文件仍保留在会话目录）。

    **保留相对路径**：产物本来就在工作区内的（``peifeng-test/a.csv``、``internal/笔记.md``）
    一律跳过——之前按文件名平铺复制，会把子目录里的产物另外复制一份到工作区根目录，
    也会把 ``internal/`` 里的过程笔记当成交付物丢到用户桌面上。
    """
    copied: list[Path] = []
    if final.status != "completed" or not work_dir:
        return copied
    root = Path(target_dir).resolve()
    for result in final.step_results:
        for name in result.files or []:
            path = Path(name)
            if not path.is_absolute():
                path = Path(target_dir) / path
            if not path.is_file():
                continue
            if path.suffix.lower() not in ARTIFACT_SUFFIXES:
                continue  # 只交付文档类产出，别把脚本、临时文件也复制过去
            try:
                # 按"相对工作区的原路径"还原；已在工作区内的就等于自己 → 跳过
                relative = path.resolve().relative_to(root)
            except (OSError, ValueError):
                relative = Path(path.name)  # 工作区外的产物：按文件名放到交付目录
            destination = Path(target_dir) / relative
            if path.resolve() == destination.resolve() or destination.exists():
                continue
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
            except OSError:
                continue
            copied.append(destination)
    return copied
