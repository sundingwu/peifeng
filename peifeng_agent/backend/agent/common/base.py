"""Agent 基类：装配自己的能力（目录即边界）、运行上下文、事件协议与工具循环。
"""
from __future__ import annotations

import json
import logging
import traceback
from collections import Counter
from copy import copy
from typing import Any, Callable, List, Optional

from peifeng_agent.backend.agent.common.context import CHARS_PER_TOKEN, MessageContext
from peifeng_agent.backend.agent.common.llm import LLMProvider, ResponseMessage
from peifeng_agent.backend.agent.common.rules import COMMON_RULES
from peifeng_agent.backend.agent.common.skillbox import SkillBox
from peifeng_agent.backend.agent.common.toolbox import ToolBox
from peifeng_agent.backend.utils.contracts import AgentRequest, Listener, ToolCall, ToolRunner
from peifeng_agent.backend.utils.exceptions import CancelledError
from peifeng_agent.backend.utils.loader import (
    SKILL_DIR,
    agent_home,
    load_agent_skills,
    load_agent_tools,
    load_prompt,
)
from peifeng_agent.backend.utils.preferences import preferences_segment
from peifeng_agent.backend.utils.tokens import estimate_tokens

logger = logging.getLogger(__name__)

#: 轮次用尽后的收尾指令：关掉工具，逼模型用已有信息给结论（而不是白跑一趟整步作废）
FINAL_ROUND_HINT = (
    "【收尾】工具轮次已用尽，本轮不要再调用任何工具，也不要输出任何工具调用标记"
    "（例如 <|DSML|function_calls>、```tool 之类）。"
    "请基于上面已经获得的信息，只输出一个 JSON 对象："
    "任务已达成则 status=completed；只完成一部分则 status=partial，并在 limitations 里写清还缺什么；"
    "确实无法给出可用结果时才用 needs_replan（任务会就此停止，问题记进会话记录）。"
)

TOOL_FAILURE_HINT = (
    "【失败提示】上一次工具调用失败。请依据实际错误选择合适做法：可等待后重试、"
    "调整参数、更换方法或换用来源；确实需要用户提供信息或决定时输出 needs_input 状态；"
    "继续执行必须改变计划目标或方法已穷尽时输出 needs_replan 状态。每次调用都带来新信息："
    "同参重复无新产出时，改为调整参数、换方法或换来源。"
)

#: 输出撞到长度上限时的收尾指令：内容没写完，JSON 也不完整——让它把长内容改走文件
TRUNCATED_HINT = (
    "【上一条输出被长度上限截断】内容没有写完，结构化结果也就不完整。"
    "请按输出协议**重新给出完整结果**：长正文用 write / docx 等工具写进文件，"
    "JSON 里 content 只放摘要与文件路径（控制在 2000 字以内），不要重复已被截断的那份内容。"
)

#: 同一工具 + 同一参数在**一个回合内**累计出现几次就判定为"原地打转"
MAX_REPEAT_CALLS = 3
#: 同一路径在一个回合内最多读几次：模型会"逐行读"（offset 每次 +1），签名各不不同，
#: 上面的完全匹配抓不到，但它本质上仍是原地打转（实测 12 行的文件读了 13 次）
READ_SAME_PATH_LIMIT = 5
#: 同一工具在一个回合内最多调几次：兜底防"渐进式试探"（切片范围每次变、命令每次微调）
SAME_TOOL_LIMIT = 30

#: 模型窗口未知时的兜底（与页面「上下文占用」的显示口径一致）
DEFAULT_CONTEXT_LIMIT = 1_048_576

#: 一个回合内最多看几张图：实测会出现"裁剪局部 → 看图 → 再裁剪"十几轮的循环，
#: 每轮两次往返，而第二轮起看到的东西不再增加
VISION_CALL_LIMIT = 5

#: 原地打转时的收尾指令：点明"结果不会变"，并给出拿新信息的正向做法
REPEAT_HINT = (
    "【同一调用已重复 {times} 次】这几个调用用的是完全相同的工具与参数，返回的结果也就完全相同。"
    "本轮先不要再调用工具，请依据已拿到的信息给出终态 JSON："
    "任务已达成则 status=completed；只完成一部分则 status=partial，并在 limitations 里写清缺口。"
    "如果确实还差信息，就在 limitations 里写明下一步该变的那个变量"
    "（换检索关键词、换分页区间、换数据来源、换工具），下一轮照着新的调用继续。"
)


Parser = Callable[[str, List[ToolCall]], Any]


ExhaustedHandler = Callable[[List[ToolCall], str, List[str]], Any]


def call_signature(record: ToolCall) -> str:
    """一次调用的指纹：工具名 + 规范化参数（参数顺序不同也算同一次调用）。"""
    return json.dumps([record.name, record.arguments], ensure_ascii=False, sort_keys=True)


def drift_hint(batch: List[ToolCall], path_reads: "Counter[str]", tool_calls: "Counter[str]") -> str:
    """检测"渐进式试探"：参数一直在变，但语义上是原地打转。

    完全匹配的重复检测（:func:`count_repeats`）抓不到这种——``read(offset=1)`` →
    ``read(offset=2)`` → … 签名全不同，却是同一个文件读了十几次。返回一句提醒（空串 = 不用提醒），
    由调用方追加到上下文；**不终止回合**，因为直接掐断会毙掉正常的大批量处理。
    """
    for record in batch:
        if record.name == "read":
            target = str(record.arguments.get("path") or "").strip()
            if target:
                path_reads[target] += 1
        tool_calls[record.name] += 1
    for target, times in path_reads.items():
        if times == READ_SAME_PATH_LIMIT + 1:
            name = target.replace("\\", "/").rsplit("/", 1)[-1]
            return (
                f"【提醒】你已经对这个文件读了 {times} 次（{name}）。"
                "一次读完比逐段读更快：不传 offset/limit 就是全文；"
                "要找内容用 read(grep=\"关键词\")。别再用 offset 一点点挪了。"
            )
    for name, times in tool_calls.items():
        if times == SAME_TOOL_LIMIT + 1:
            return (
                f"【提醒】本回合你已经调了 {times} 次 {name}。"
                "参数一直在变但结果没进展的话，换工具或换方法；已经拿到的先落盘再继续。"
            )
    return ""


def count_repeats(seen: "Counter[str]", batch: List[ToolCall]) -> tuple[Optional[str], int]:
    """把本批调用计入 ``seen``，返回当前**累计最多**的那个签名与它的次数。

    按**累计**而不是"连续"计数：真实运行里同一串 `bash cd ... && python ...` 会隔几次别的
    调用再出现一次（一次回合内重复 13 次），严格连续检测抓不到。
    """
    for record in batch:
        seen[call_signature(record)] += 1
    if not seen:
        return None, 0
    signature, times = seen.most_common(1)[0]
    return signature, times


_SEGMENT_JOIN = "\n\n"


class BaseAgent:
    """无状态 Agent 基类：启动时自装配 + 事件协议 + 运行上下文 + 回合（单轮或工具循环）。"""

    name: str = "agent"
    #: 是否是worker（决定目录位置：``agent/<角色>/`` 还是 ``agent/executor/<名字>/``）
    is_worker: bool = False
    #: 装配层注入的额外固定段（例如 planner 的「可用worker」清单）：由 :meth:`assemble` 拼进 system
    extra_system: str = ""

    def __init__(
        self,
        llm: LLMProvider,
        system_prompt: str = "",
        temperature: float = 0.2,
        suffix: str = "",
        tool_defs: Optional[List[dict]] = None,
        tool_runner: Optional[ToolRunner] = None,
        max_tool_rounds: int = 12,
    ) -> None:
        self.llm = llm
        self.temperature = temperature
        self.system_prompt = system_prompt or ""
        self.context = MessageContext(system_prompt=self.system_prompt, suffix=suffix)
        self.listeners: List[Listener] = []
        #: 本角色可用的工具定义与执行入口（:meth:`assemble` 装好后由工具箱提供；
        #: 也可以直接注入，测试与一次性脚本用得上）
        self.tool_defs = list(tool_defs or [])
        self.tool_runner = tool_runner
        self.max_tool_rounds = max_tool_rounds
        #: 模型上下文窗口（token）：装配时由 settings 覆盖；0 表示未知、不做自动压缩
        self.context_limit = 0
        #: 占用达到该比例就压缩会话层（0 表示关闭）
        self.compress_ratio = 0.0
        #: 固定层字符数的缓存（见 :meth:`_fixed_layer_chars`）
        self._fixed_chars: Optional[int] = None
        #: 本回合已经看了几张图（见 :data:`VISION_CALL_LIMIT`）
        self.vision_calls = 0
        #: 装配产物：自己认领的工具、技能与两个箱子（未装配时为空）
        self.tools: List[dict] = []
        self.skills: List[dict] = []
        self.toolbox: Optional[ToolBox] = None
        self.skillbox: Optional[SkillBox] = None
        #: 本轮取消检查（:meth:`execute` 按请求注入；None 表示不检查）
        self._stop_check: Optional[Callable[[], bool]] = None

    # ---------------------------------------------------------------- 装配
    def assemble(self, settings) -> List[str]:
        """认领自己的目录并完成装配（启动时一次），返回加载错误。

        **目录即归属**（见 :func:`load_agent_tools`）：每个 agent 只扫自己目录下的
        ``tool/`` 与 ``skill/``，不读任何元数据——所以 Planner / Critic 的目录里没有
        写与执行类工具，它们天然拿不到。

        顺序：工具 → 技能 → 提示词 → system prompt → 工具箱 / 技能箱。
        system prompt 只放**稳定信息**（角色规范 + 通用规则 + 工具清单 + 技能清单）；项目路径等
        **可变信息**由编排层每轮注入（:attr:`MessageContext.suffix`）。
        """
        errors: List[str] = []
        self.tools, tool_errors = load_agent_tools(settings.agent_dir, self.name, is_worker=self.is_worker)
        errors.extend(tool_errors)
        self.skills, skill_errors = load_agent_skills(
            settings.agent_dir, self.name, is_worker=self.is_worker
        )
        errors.extend(skill_errors)
        prompt, prompt_errors = self.own_prompt(settings)
        errors.extend(prompt_errors)

        self.toolbox = ToolBox(self.tools, output_limit=getattr(settings, "tool_output_limit", None))
        self.skillbox = SkillBox(self.skills)
        self.tool_defs = self.toolbox.schemas()
        # 上下文水位：窗口未配时用与页面显示一致的兜底值（deepseek-flash 的 1M）
        self.context_limit = int(settings.model_context_limit or DEFAULT_CONTEXT_LIMIT)
        self.compress_ratio = float(getattr(settings, "context_compress_ratio", 0.0) or 0.0)
        self.system_prompt = _SEGMENT_JOIN.join(
            segment
            for segment in (
                prompt,
                COMMON_RULES,
                preferences_segment(settings.data_dir),
                self.extra_system,
                self.toolbox.index(),
                self.skill_hint(settings),
                self.skillbox.index(self.skill_title),
            )
            if segment
        )
        self.context = MessageContext(system_prompt=self.system_prompt, suffix=self.context.suffix)
        return errors

    def skill_hint(self, settings) -> str:
        """本角色的技能目录**绝对路径**：直接给出，省得模型自己拼（真实运行里它会拼错）。"""
        if not self.skills:
            return ""
        folder = agent_home(settings.agent_dir, self.name, self.is_worker) / SKILL_DIR
        return f"【技能目录】{folder}（技能正文与 references/、assets/ 都在这里，按技能名进子目录）"

    def own_prompt(self, settings) -> tuple[str, List[str]]:
        """自己的提示词：``<自己的目录>/prompt.md``（三个角色同一来源，无需子类覆盖）。"""
        return load_prompt(settings.agent_dir, self.name, is_worker=self.is_worker)

    @property
    def skill_title(self) -> str:
        """技能清单在 system 里的标题（子类可按角色改写）。"""
        return "本角色可用技能"

    # ---------------------------------------------------------------- 运行
    def execute(self, request: AgentRequest) -> Any:
        """在独立运行上下文中执行一次调用；失败以异常冒泡，由编排层决定处置。

        每次调用都按**本轮运行环境**重建上下文：环境说明挂在 system 末尾，工具的路径
        按当前项目绑定（共享实例因此可以跨项目复用）。
        """
        # 副本做这一次请求（本类的实例长期复用，上下文有状态）
        clone = copy(self)
        clone.context = MessageContext(system_prompt=self.system_prompt, suffix=request.environment)
        clone._stop_check = request.stop_check
        clone.tool_runner = clone.runner_for(request)
        try:
            return clone.execute_role(request)
        finally:
            clone.emit("context_checkpoint", agent=clone.name, messages=clone.context.conversation[1:])

    def should_stop(self) -> bool:
        """用户是否已要求中止：工具循环在每个边界上看它，命中就收尾。"""
        check = self._stop_check
        if check is None:
            return False
        try:
            return bool(check())
        except Exception:  # noqa: BLE001 - 取消检查本身出错不该影响执行
            return False

    def runner_for(self, request: AgentRequest) -> Optional[ToolRunner]:
        """按本轮运行环境绑定工具路径；未装配工具箱时沿用注入的 runner。"""
        if self.toolbox is None:
            return self.tool_runner
        return self.toolbox.runner(
            workspace=request.workspace,
            session_dir=request.session_dir,
            vision=self.describe_image,
            stopped=self.should_stop,
        )

    def describe_image(self, image_path, question: str, max_side: int = 1400) -> str:
        """看图：把问题转给模型，返回文字回答（工具 image_describe 经注入调用它）。

        签名要与工具的调用口径一致（path / question / max_side）：少一个参数就会
        每次调用都报 TypeError，看图能力等于没有。
        """
        if self.llm is None:
            raise RuntimeError("当前没有可用的模型，无法看图")
        return self.llm.describe_image(image_path, question, max_side)

    def execute_role(self, request: AgentRequest) -> Any:
        raise NotImplementedError

    def subscribe(self, listener: Listener) -> None:
        self.listeners.append(listener)

    def clear_listeners(self) -> None:
        """清空事件订阅者。

        agent 装配一次、长期复用，而编排器每次切项目/会话都会重建——重建时先清空，
        否则监听器会一路累积（旧编排器的队列没人读，事件却还在往里写）。
        """
        self.listeners.clear()

    def emit(self, kind: str, **data: Any) -> None:
        for listener in self.listeners:
            try:
                listener(kind, data)
            except Exception:  # noqa: BLE001 - 单个监听器失败不影响执行
                logger.debug("事件监听器处理 %s 失败", kind, exc_info=True)

    def reset(self) -> None:
        self.context.reset(system_prompt=self.system_prompt)

    def set_fixed_layer(self, tool_defs: Optional[List[dict]] = None) -> None:
        self.context.set_fixed_layer(tool_defs=tool_defs)

    def complete(self, *, interruptible: bool = True) -> ResponseMessage:
        """发一次模型调用。

        ``interruptible=True``（默认）：用户点停止时，流式接收在每个分片之间就地停下
        （返回已收到的部分）。轮次耗尽的 ``finalize_round`` 可关闭中断检查；
        用户中断的 ``finalize_stopped`` 只使用本地记录，不再请求模型。

        **上下文占用两次数**：发请求前先报一个字符估算（界面等待期间也有数），
        拿到响应后若端点给了 ``usage.prompt_tokens``，就用真实值覆盖——那是账单口径，
        比任何估算都准；端点不返回 ``usage`` 时才停在估算上（``source="estimate"``）。
        """
        messages = self.context.to_llm_messages()
        encoded = json.dumps({"messages": messages, "tools": self.context.tool_defs}, ensure_ascii=False)
        characters = len(encoded)
        self.report_usage(characters=characters, tokens=estimate_tokens(encoded), source="estimate")
        response = self.llm.complete(
            messages,
            tools=list(self.context.tool_defs),
            temperature=self.temperature,
            events=self.forward_stream_events,
            should_stop=self.should_stop if interruptible else None,
        )
        usage = response.usage or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        if prompt_tokens > 0:
            self.report_usage(characters=characters, tokens=prompt_tokens, source="api", usage=usage)
        return response

    def report_usage(self, *, characters: int, tokens: int, source: str, usage: Optional[dict] = None) -> None:
        """上报一次上下文占用（``source=api`` 为端点真实用量，``estimate`` 为字符估算）。"""
        data: dict[str, Any] = {
            "agent": self.name,
            "characters": characters,
            "estimated_tokens": tokens,
            "source": source,
        }
        if usage:
            data.update(usage)  # prompt_tokens / completion_tokens / total_tokens
        self.emit("context_usage", **data)

    def forward_stream_events(self, kind: str, data: dict) -> None:
        if kind == "text_delta":
            self.emit("text_delta", agent=self.name, delta=str(data.get("delta") or ""))

    # ---------------------------------------------------------------- 回合
    def _fixed_layer_chars(self) -> int:
        """固定层（system 提示 + 工具 schema）的**字节数**：装配后不变，算一次就缓存。"""
        if self._fixed_chars is None:
            tools = json.dumps(self.tool_defs, ensure_ascii=False) if self.tool_defs else ""
            self._fixed_chars = (
                len((self.system_prompt or "").encode("utf-8"))
                + len((self.context.suffix or "").encode("utf-8"))
                + len(tools.encode("utf-8"))
            )
        return self._fixed_chars

    def maybe_compress(self) -> None:
        """上下文逼近模型窗口时压缩会话层（占用达到 ``compress_ratio`` 触发）。

        只压**会话层**：system 固定层（角色提示/通用规则/工具 schema）与环境说明保持原样——
        它们决定行为，压掉会让模型跑偏。压缩交给 :meth:`MessageContext.compress`：
        先把单条超长的工具结果截成头尾，仍超预算就成组丢弃最旧的往返。
        """
        if not self.context_limit or not self.compress_ratio:
            return
        # 用"字符数 ÷ 3"估算，不走 JSON 序列化：每轮都序列化整个对话在长任务里是纯开销，
        # 而 80% 的阈值本身有缓冲，这点精度差无关紧要
        used = (self.context.size() + self._fixed_layer_chars()) // CHARS_PER_TOKEN
        if used < self.context_limit * self.compress_ratio:
            return
        before = self.context.size()
        # 目标压到当前一半：成组丢弃按回合进行，实际结果只会更小
        if not self.context.compress(max(1, before // 2)):
            return
        self.emit(
            "context_compressed",
            agent=self.name,
            before_chars=before,
            after_chars=self.context.size(),
            used_tokens=used,
            limit=self.context_limit,
            ratio=self.compress_ratio,
        )

    def run_turn(
        self,
        parse: Parser,
        *,
        retry_hint: str,
        error_cls: type[Exception],
        exhausted: Optional[ExhaustedHandler] = None,
        summarize_on_stop: bool = False,
    ) -> Any:
        """运行一次回合并解析结果（只对流式增量与工具调用发事件）。

        ``summarize_on_stop``：用户中止时是否让模型交代"已经做了什么"。worker需要
        （它的中途成果对用户有交接价值）；Planner / Critic 不需要，直接抛取消。
        """
        records: List[ToolCall] = []
        current_goal = ""
        #: 本回合看图次数（工具循环里自增，超上限就拒绝，见 VISION_CALL_LIMIT）
        self.vision_calls = 0
        #: 本回合内每种"工具 + 参数"出现多少次：同一调用反复出现不会带来新信息
        seen_calls: Counter[str] = Counter()
        #: 本回合内每个路径读了几次、每个工具调了几次：抓"渐进式试探"（参数变、语义不变）
        path_reads: Counter[str] = Counter()
        tool_calls: Counter[str] = Counter()
        rounds = self.max_tool_rounds if (self.tool_defs or self.tool_runner) else 1
        for round_index in range(rounds):
            if self.should_stop():
                return self.finalize_stopped(parse, records, summarize_on_stop)
            # 每轮开始前先看上下文水位：逼近模型窗口就压缩，避免请求直接被拒
            self.maybe_compress()
            # 轮次事件：前端可显示"第 N/M 轮"，也是统计真实往返次数的唯一口径
            self.emit("llm_round", agent=self.name, round=round_index + 1, total=rounds)
            response = self.complete()
            if self.should_stop():
                # 本次调用被用户中断（流式就地停下）：不再解析半截输出，重新要一份交代
                return self.finalize_stopped(parse, records, summarize_on_stop)
            if response.parsed["stop_reason"] == "error":
                if summarize_on_stop:
                    result = self.finalize_stopped(parse, records, True)
                    result.status = "needs_replan"
                    result.limitations.append(f"LLM 调用失败：{response.parsed['error_text'] or '未知错误'}")
                    return result
                raise error_cls(f"LLM 调用失败：{response.parsed['error_text'] or '未知错误'}")
            content = response.parsed["content"]
            self.context.add("assistant", content, tool_calls=response.parsed["tool_calls"] or None)
            if response.parsed["stop_reason"] == "length":
                # 撞到输出上限：JSON 必然不完整（工具参数也可能被截断），不能当正常输出去解析，
                # 更不能执行半截工具调用——直接要一次"精简重出"，让模型把长内容改走文件
                return self.finalize_round(
                    parse,
                    records,
                    error_cls,
                    exhausted,
                    "上一条输出被长度上限截断，已要求精简重出。",
                    ["单次输出超过长度上限。"],
                    hint=TRUNCATED_HINT,
                )
            if not response.parsed["has_tool_calls"]:
                return self.parse_with_retry(content, parse, retry_hint, error_cls, records)
            if content.strip():
                current_goal = content.strip()
            batch, any_failed = self.run_tool_calls(response.parsed["tool_calls"], round_index, current_goal)
            records.extend(batch)
            if self.should_stop():
                return self.finalize_stopped(parse, records, summarize_on_stop)
            repeated_signature, repeat_times = count_repeats(seen_calls, batch)
            if repeat_times >= MAX_REPEAT_CALLS:
                # 原地打转：同参数反复调用的结果不会变，换多少轮都是同一份，直接要结论
                return self.finalize_round(
                    parse,
                    records,
                    error_cls,
                    exhausted,
                    f"同一调用重复 {repeat_times} 次，当前回合已终止。",
                    ["同参数重复调用得不到新信息。"],
                    hint=REPEAT_HINT.format(times=repeat_times),
                )
            # 渐进式试探也拦一下：参数一直在变（offset +1、切片范围改），但语义上是原地打转。
            # 这两条不终止回合，而是提醒换方法——直接掐断可能毙掉正常的大批量处理。
            drift = drift_hint(batch, path_reads, tool_calls)
            if drift:
                self.context.add("user", drift)
            if any_failed:
                self.context.add("user", TOOL_FAILURE_HINT)
        return self.finalize_round(
            parse,
            records,
            error_cls,
            exhausted,
            f"工具调用超过 {rounds} 轮，当前回合已终止。",
            ["需要缩短执行链或重新规划。"],
        )

    def run_tool_calls(self, calls: List[dict], round_index: int, goal: str) -> tuple[List[ToolCall], bool]:
        """按模型给出的顺序**逐个**执行本轮调用；上下文与事件都留在本线程。

        **并发不在这里做**：需要一次处理多个目标的工具（read / search_web / fetch_web /
        bash / image_describe / delete / move）由它们自己的批量参数在内部并发执行。
        调度层保持顺序执行——顺序确定、取消及时（每个调用前检查）、失败隔离简单；
        跨会话的并发由会话层负责（每个会话一个执行单元）。

        代价是"跨工具"的并行没了（一次响应里 read 与 search_web 会依次跑），
        但这只是每轮几百毫秒，与一次模型往返相比可以忽略。
        """
        records: List[ToolCall] = []
        context_start = len(self.context.conversation)
        call_ids = [str(call.get("id") or f"call_{round_index}_{index}") for index, call in enumerate(calls)]

        def arguments_for(index):
            arguments = calls[index].get("arguments")
            return arguments if isinstance(arguments, dict) else {}

        def checkpoint():
            """把已完成调用的结果写回对话（后到的调用还没结果，先不占位）。"""
            self.context.conversation[context_start:] = [
                {"role": "tool", "content": record.result or "（无输出）", "tool_call_id": call_ids[index]}
                for index, record in enumerate(records)
            ]
            self.emit("context_checkpoint", agent=self.name, messages=self.context.conversation[1:])

        def finish(index, output, is_error, *, started=True):
            name = str(calls[index].get("name", ""))
            records.append(ToolCall(name=name, arguments=arguments_for(index), result=output or "",
                                    is_error=is_error, goal=goal, round=round_index))
            if started:
                hint = output.strip().splitlines()[0] if is_error and output.strip() else ""
                self.emit("tool_execution_end", agent=self.name, tool=name, tool_call_id=call_ids[index],
                          is_error=is_error, error_hint=hint)
            checkpoint()

        for index in range(len(calls)):
            if self.should_stop():
                finish(index, "工具调用已取消；未启动工具", True, started=False)
                continue
            name = str(calls[index].get("name", ""))
            self.emit("tool_execution_start", agent=self.name, tool=name,
                      arguments=arguments_for(index), tool_call_id=call_ids[index])
            try:
                output, is_error = self.run_tool(name, arguments_for(index))
            except Exception:  # noqa: BLE001 - 单个调用失败不该中断其余调用
                output, is_error = traceback.format_exc(), True
            finish(index, output, is_error)
        return records, any(record.is_error for record in records)

    def run_tool(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        if self.tool_runner is None:
            return f"错误：工具执行器未配置，无法调用 {name}", True
        # 看图硬上限：实测会出现"裁剪局部 → 看图 → 再裁剪"十几轮的循环，每轮两次往返，
        # 而第二轮起看到的东西不再增加。放在这里而不是循环里，任何调用路径都拦得住
        if name == "image_describe":
            self.vision_calls += 1
            if self.vision_calls > VISION_CALL_LIMIT:
                return (
                    f"错误：本步看图已达上限（{VISION_CALL_LIMIT} 次）。"
                    "不要再裁剪/放大反复看——按已经看到的判断并交付，评审会再看一遍",
                    True,
                )
        try:
            return self.tool_runner(name, arguments)
        except Exception as error:  # noqa: BLE001
            return f"错误：工具 {name} 执行异常：{error}\n{traceback.format_exc()}", True

    def parse_with_retry(
        self,
        content: str,
        parse: Parser,
        retry_hint: str,
        error_cls: type[Exception],
        records: List[ToolCall],
    ) -> Any:
        """解析终态输出；失败时按 retry_hint 让模型重试一次。"""
        try:
            return parse(content, records)
        except Exception:  # noqa: BLE001 - 解析失败一律当作"输出不合格"，用重试提示再问一次
            self.context.add("user", retry_hint)
            retry = self.complete()
            if retry.parsed["stop_reason"] == "error":
                raise error_cls(retry.parsed["error_text"])
            self.context.add("assistant", retry.parsed["content"], tool_calls=retry.parsed["tool_calls"] or None)
            try:
                return parse(retry.parsed["content"], records)
            except error_cls:
                raise
            except Exception as error:
                raise error_cls(str(error)) from error

    def finalize_round(
        self,
        parse: Parser,
        records: List[ToolCall],
        error_cls: type[Exception],
        exhausted: Optional[ExhaustedHandler],
        summary: str,
        limitations: List[str],
        hint: str = FINAL_ROUND_HINT,
    ) -> Any:
        """收尾：关掉工具，让模型用已有信息给出终态（轮次用尽、输出被截断都走这里）。

        **为什么要这一步**：直接掐断会让"读了 20 轮但还没总结"的任务整步作废。给一次不带
        工具的收尾调用，绝大多数情况能拿到可交付的结论（至少是 partial + 缺什么），
        比整步作废有用得多。
        ``hint`` 决定这次收尾说什么（轮次用尽 / 输出被截断），收尾调用本身失败时，
        再退回各角色原来的 ``exhausted`` 处理。
        """
        saved_tool_defs = self.context.tool_defs
        self.context.set_fixed_layer(tool_defs=[])
        self.context.add("user", hint)
        try:
            # 收尾调用不可中断：它正是在"已经停下"之后要产出结论的那一次
            response = self.complete(interruptible=False)
        except Exception:  # noqa: BLE001 - 收尾调用失败不该盖住原始原因
            return self.finish_exhausted(exhausted, records, error_cls, summary, limitations)
        finally:
            self.context.set_fixed_layer(tool_defs=saved_tool_defs)
        if response.parsed["stop_reason"] == "error" or response.parsed["has_tool_calls"]:
            return self.finish_exhausted(exhausted, records, error_cls, summary, limitations)
        try:
            return parse(response.parsed["content"], records)
        except Exception:  # noqa: BLE001 - 收尾输出不合格时同样退回兜底
            return self.finish_exhausted(exhausted, records, error_cls, summary, limitations)

    def finalize_stopped(self, parse: Parser, records: List[ToolCall], summarize: bool) -> Any:
        """中断时直接从工具记录构建结果；规划和评审阶段抛取消。

        不请求模型总结，断网时仍能保留已经返回的工具结果。
        """
        if not summarize:
            raise CancelledError("用户中止了任务")
        content = "任务已中断。以下为实际返回的工具记录；未返回的操作不能视为完成。\n" + "\n".join(
            json.dumps(record.model_dump(), ensure_ascii=False) for record in records
        )
        return parse(json.dumps({
            "status": "partial", "summary": "任务中断，已保留工具操作记录",
            "content": content, "limitations": ["尚未完成的操作需结合新需求重新规划"],
        }, ensure_ascii=False), records)

    @staticmethod
    def finish_exhausted(
        exhausted: Optional[ExhaustedHandler],
        records: List[ToolCall],
        error_cls: type[Exception],
        summary: str,
        limitations: List[str],
    ) -> Any:
        """轮次耗尽的收尾：有回调则交给角色决定，否则按该角色的错误类型抛出。"""
        if exhausted is not None:
            return exhausted(records, summary, limitations)
        raise error_cls(summary)
