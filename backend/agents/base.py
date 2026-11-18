"""角色基座：数据模型 + LLM 调用 + 分层上下文 + 工具循环。

一个文件四块：
- ``models``：编排层与角色之间的全部数据模型（Plan / ExecutionResult / ReviewResult / 任务记录…）
- ``llm``：统一消息模型 + OpenAI 兼容 Provider（Chat Completions，流式与图片）
- ``jsonout``：模型输出的 JSON 容错（去围栏、修尾逗号、补全截断）
- ``BaseAgent``：按注册表装配、分层上下文与压缩、工具回合、收尾与中断
"""

from __future__ import annotations

from pathlib import Path
import inspect
import json
import logging
import traceback
from copy import copy
from typing import Any, Callable, List, Optional
from backend.utils.errors import CancelledError
from backend.utils.text import estimate_tokens
import threading
import time
from abc import ABC, abstractmethod
from pydantic import BaseModel, Field
from backend.utils.text import image_source
from typing import Any, Callable, List, Literal, Optional, Protocol
from pydantic import BaseModel, Field, model_validator
import re
from typing import Any


# ---------------------------------------------------------------- models
# ---------------------------------------------------------------- 节点调用

#: 事件监听器：接收 (事件名, 事件数据)。
Listener = Callable[[str, dict[str, Any]], None]

#: 固定角色（各一个）：规划者与评审者。worker是多个，由 ``workers/`` 注册表在启动时注册。
CORE_ROLES: tuple[str, ...] = ("mentor",)

#: 交付成果时认可的文档扩展名（避免把脚本、临时文件算成交付物）
ARTIFACT_SUFFIXES = (".docx", ".xlsx", ".pptx", ".pdf", ".md", ".txt", ".csv", ".png", ".jpg", ".jpeg", ".svg", ".tex", ".bib")


class AgentRequest(BaseModel):
    """一次节点调用的公共信封。

    ``metadata`` 在节点调度时注入（``trace_id`` / ``session_id`` / ``node``）。后面三项是
    **每轮可变的运行环境**，同样由编排层注入——agent 装配一次、长期复用，项目与会话
    切换只换这几个字段：

    - ``environment``：给人看的环境说明（项目目录 / 中间目录 / 会话目录…），进 system 段末尾
    - ``workspace``：当前项目的工作文件夹（工具的相对路径基准）
    - ``session_dir``：当前会话目录（会话级文件如 detail/ 的定位依据）

    ``stop_check`` 是取消信号：用户点「停止」后它返回 ``True``，节点据此在**当前工具调用
    结束后**收尾（不再发起新的模型或工具调用），把"已经做了什么"交代清楚再结束。
    """

    original_task: str = ""
    environment: str = ""
    workspace: str = ""
    session_dir: str = ""
    #: 取消检查（编排层注入，不参与序列化）：返回 True 表示"做完手上这一件就停"
    stop_check: Optional[Callable[[], bool]] = Field(default=None, exclude=True)


class AgentNode(Protocol):
    """工作流节点：接收本角色的请求模型，返回本角色的结果模型。"""

    def execute(self, request: AgentRequest) -> Any: ...


# ---------------------------------------------------------------- 计划

class PlanStep(BaseModel):
    """计划中的一步：派给谁、做什么、怎么验收。

    兼容旧格式：读取到 ``description`` 而无 ``goal`` 时映射为 ``goal``（断点恢复旧计划）。

    步骤一律串行执行：每一步都要单独执行、单独通过评审，不存在"同时跑、整组评一次"。
    """

    goal: str = ""
    expected_output: str = ""
    #: 派给哪个 worker：workers 注册表里的名字（空或未知 → 编排层回退到默认 worker）
    worker: str = ""
    #: 这一步允许用多少次工具调用：规划时由 Mentor 估，用完就停下等阶段审核（0 = 不限额）
    tool_budget: int = 0

    @model_validator(mode="before")
    @classmethod
    def compat(cls, data):
        if isinstance(data, dict):
            data = dict(data)
            if data.get('worker') == 'designer':
                data['worker'] = 'visualizer'
            description = data.pop("description", "")
            if not data.get("goal") and description:
                data["goal"] = description
            data.pop("tools", None)
            data.pop("action", None)
        return data


class Plan(BaseModel):
    """Mentor 生成的完整计划。"""

    requirement: str = ""
    output_name: str = ""
    steps: List[PlanStep] = Field(default_factory=list)


class PlanningRequest(AgentRequest):
    """Mentor 的唯一输入：本次任务需求 + 跨任务摘要 + 澄清记录。

    可用worker清单**不在这里**：它在装配时进了 mentor 的 system 固定层（启动后不变）。

    ``plan_feedback`` 是**上一次规划被退回的原因**（例如某步的 worker 不存在）：
    计划校验不通过时不替模型挑一个默认 worker，而是把问题退回让它重新规划。
    """

    task: str
    prior_summary: str = ""
    clarifications: str = ""
    plan_feedback: str = ""


class PlanningResult(BaseModel):
    """Mentor 的统一结果，业务分支不使用异常表示。

    ``mode`` 只有三种：``plan``（开始工作）、``clarify``（**一次**集中询问细节）、
    ``unable``（缺信息或能力，直接说明）。没有"直接回答"这条路径——不需要动手的
    短问答也落成一步计划，照常执行与评审。
    """

    mode: Literal["clarify", "unable", "plan"]
    questions: List[dict] = Field(default_factory=list)
    reason: str = ""
    plan: Optional[Plan] = None


class FinalRequest(AgentRequest):
    """收尾请求：所有步骤都通过了，请 Mentor 直接回答用户最初的提问。

    这是**唯一**要求"面向用户写正文"的调用：输入是原始需求、实际做了什么、产出
    文件与已知限制，输出一段可以直接读的回答（不是"任务已完成"这类交代）。
    """

    task: str
    plan_outline: str = ""
    step_summaries: str = ""
    files: List[str] = Field(default_factory=list)
    limitations: List[str] = Field(default_factory=list)
    prior_summary: str = ""


class FinalAnswer(BaseModel):
    """收尾回答：直接给用户读的 Markdown 正文。"""

    content: str = ""


# ---------------------------------------------------------------- 执行

#: 工具执行回调：给定工具名与参数，返回 (输出文本, 是否出错)。
ToolRunner = Callable[[str, dict[str, Any]], tuple[str, bool]]


class ToolCall(BaseModel):
    """一次工具调用记录（工具名、参数、完整输出与是否出错）。"""

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    #: 工具完整输出（供评审上下文与步骤明细落盘）
    result: str = ""
    is_error: bool = False


class ExecutionResult(BaseModel):
    """Worker 单步执行结果。

    ``status`` 只描述**这一步**：``completed``（达成）/ ``partial``（做了一部分，缺口写在
    ``limitations``）/ ``needs_input``（缺用户才能给的信息）。**做不到不单独设状态**：
    worker 如实写 ``partial`` + ``limitations``，由评审的 Mentor 判断要不要改剩余计划。
    """

    status: Literal["completed", "partial", "needs_input"]
    content: str = ""
    summary: str = ""
    #: 给评审者的**交接说明**：产出在哪、关键证据落盘位置（文件 + 段/行）、建议抽查哪几处。
    #: 评审据此按需取证，不必自己遍历目录、逐个文件翻——这是评审最大的成本来源。
    handoff: str = ""
    question: str = ""
    files: List[str] = Field(default_factory=list)
    sources: List[str] = Field(default_factory=list)
    limitations: List[str] = Field(default_factory=list)
    tool_calls: List[ToolCall] = Field(default_factory=list)
    #: 语言模型给的字段里没有这一项：工具调用达到配额时由系统标记，编排层据此发起阶段审核
    budget_exhausted: bool = False


class ExecutionRequest(AgentRequest):
    """Worker 的唯一输入。"""

    context_id: str = ""
    continuation: bool = False
    step: PlanStep
    feedback: str = ""
    previous_results: str = ""
    requirement: str = ""
    #: 计划本身（mentor 的 requirement/steps 渲染好的文本，不含执行状态）
    plan_outline: str = ""
    #: 原始请求用于日志；后续用户决定进入执行提示，覆盖已经过时的规划要求。
    user_decisions: List[str] = Field(default_factory=list)
    #: 交付文件名（来自计划的 output_name）
    output_name: str = ""


# ---------------------------------------------------------------- 评审

class ReviewFinding(BaseModel):
    """可定位、可修复的审核发现；证据内容由审核者回查来源。"""

    model_config = {"str_strip_whitespace": True}

    severity: Literal["blocking", "advisory"]
    criterion: str = Field(min_length=1)
    location: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    correction: str = Field(min_length=1)


class ReviewResult(BaseModel):
    """Mentor 的评审结论。

    - **通过**：给出 ``comment``（本步摘要：目标与完成方法）、``file_path`` 与 ``file_content``
      （产出文件的路径与内容），这些进日志，供后续步骤直接查看，不必再去读文件。
    - **不通过**：只给 ``comment``（未通过的原因），返修时它是一个步骤内的增量上下文。
    """

    approved: bool
    goal_check: str = ""
    plan_update: Optional[Plan] = None
    complete: bool = False
    findings: List[ReviewFinding] = Field(default_factory=list)
    #: 通过时=步骤摘要（本步目标与完成方法）；不通过时=未通过的原因
    comment: str = ""
    #: 产出文件路径（通过时给；后续步骤据此定位）
    file_path: str = ""
    #: 产出文件内容（通过时给；后续步骤直接看，省一次 read）
    file_content: str = ""


class StageReview(BaseModel):
    """工具调用配额用尽时的阶段审核：继续做 / 调整计划 / 停下。

    与 ``ReviewResult`` 的区别：评审看的是**做出来的东西**，阶段审核看的是**还该不该继续**。
    所以它不带 approved，而是给一个动作，并且可以追加工具调用配额。
    """

    #: continue=接着做这一步（可给 hint 与追加配额）/ adjust=改剩余计划 / stop=停在这里
    action: Literal["continue", "adjust", "stop"] = "continue"
    comment: str = ""
    #: 继续时给 worker 的下一步提示（往哪儿使劲、别做什么）
    hint: str = ""
    #: 追加的工具调用次数；0 表示不给（还不到 0 就按默认追加）
    extra_budget: int = 0
    #: adjust 时带回来的剩余计划（与评审的 plan_update 同形）
    plan_update: Optional[Plan] = None


class StageReviewRequest(AgentRequest):
    """阶段审核请求：这一步做到哪了、已用多少配额、计划还剩什么。"""

    original_task: str = ""
    task: str = ""
    step: PlanStep = Field(default_factory=PlanStep)
    result: ExecutionResult = Field(default_factory=lambda: ExecutionResult(status="partial"))
    plan_outline: str = ""
    completed_summary: str = ""
    user_decisions: List[str] = Field(default_factory=list)
    prior_summary: str = ""
    #: 上一次给的计划不合法时的退回说明（与评审的 plan_feedback 同义）
    plan_feedback: str = ""
    budget: int = 0
    used: int = 0


class ReviewRequest(AgentRequest):
    """Mentor 的唯一输入：任务需求、原始请求与后续用户补充。

    ``plan_outline`` 是整份计划（本步标了「本步」）。评审要用它判断**越界**：本步是否做了
    后面步骤该做的活——不掌握计划就无法发现"抢跑"，实测这是调用次数失控的主因。
    """

    task: str
    user_decisions: List[str] = Field(default_factory=list)
    prior_summary: str = ""
    completed_summary: str = ""
    previous_review: Optional[ReviewResult] = None
    step: PlanStep
    result: ExecutionResult
    plan_outline: str = ""
    #: 本步产出文件的**正文**（由编排层读出来，最多 ARTIFACT_TEXT_CHARS 字）：
    #: 评审要看实际交付内容，不该只看worker的自述
    artifact: str = ""
    #: 上一次给出的剩余计划为什么被退回（worker 缺失或写错）：评审据此重新给出合法计划
    plan_feedback: str = ""


# ---------------------------------------------------------------- 任务

class TaskResult(BaseModel):
    """一次任务的最终结果。"""

    content: str = ""
    step_results: List[ExecutionResult] = Field(default_factory=list)
    #: 任务终态：completed / rejected / aborted / failed
    status: str = "completed"


class StepAttempt(BaseModel):
    """一次步骤尝试：执行结果与（可选的）评审结论。"""

    #: 第几次尝试（从 0 起）：排查"为什么返工"时先看这个序号
    attempt: int = 0
    result: ExecutionResult
    review: Optional[ReviewResult] = None


class TaskStep(BaseModel):
    """任务内一个步骤的完整记录（同一任务内部可见）。"""

    index: int
    goal: str
    expected_output: str = ""
    #: 由哪个 worker 做的（明细里保留，便于回溯"谁做了什么"）
    worker: str = ""
    #: 评审通过后压缩成的步骤摘要（做法与目的）：任务内上下文只带它，细节见产出文件与 detail/
    summary: str = ""
    #: 评审给出的**交付物要点**（比 summary 长，含关键数据/口径/结论）：下一步据此直接接着干，
    #: 不必为了拿正文再 read 一次产出文件
    deliverable: str = ""
    #: 本步产出文件（已落盘；压缩摘要的一部分，下一步按需 read 复用）
    files: List[str] = Field(default_factory=list)
    #: 本步**完整产出**的落盘位置（会话 internal 目录下的 .md：摘要 + 交接 + 正文全文）。
    #: 任务进行中 ``detail/`` 还没有本任务记录，下一步只能靠这个文件拿到正文。
    output_file: str = ""
    attempts: List[StepAttempt] = Field(default_factory=list)
    #: 本步骤的节点异常（如有）
    errors: List[str] = Field(default_factory=list)
    #: 步骤状态：pending（没轮到）/ running（做了一半被打断）/ completed（通过评审）/ failed（不通过且没救回来）
    status: str = "pending"


class TaskSummary(BaseModel):
    """写进 ``summary.json`` 的任务摘要（跨任务默认可见）。

    ``task_seq`` 同时是 ``detail/`` 里文件名的前缀：``task{task_seq}_step{NN}.json``
    就是该任务每一步的明细，可整份读。
    """

    task_seq: int
    #: mentor 整合后的完整需求（含关键假设，给后续规划用）
    requirement: str = ""
    #: 用户实际输入的那句话：历史列表里显示它，别把整合后的长需求推给用户
    original_request: str = ""
    task_summary: str = ""
    plan: Optional[Plan] = None
    steps: List[TaskStep] = Field(default_factory=list)
    interruption_log: List[dict] = Field(default_factory=list)
    status: str = "completed"
    content: str = ""
    files: List[str] = Field(default_factory=list)
    #: 中止/失败原因（status 为 aborted / failed 时说明为什么停）——事后诊断用
    abort_reason: str = ""


class TaskDetail(BaseModel):
    """写进 ``detail/`` 的任务明细（任务内部完整上下文，按任务号存档）。"""

    interruption_log: List[dict] = Field(default_factory=list)
    task_seq: int
    requirement: str = ""
    original_request: str = ""
    plan_history: List[dict] = Field(default_factory=list)
    plan: Optional[Plan] = None
    steps: List[TaskStep] = Field(default_factory=list)
    user_decisions: List[str] = Field(default_factory=list)
    status: str = "completed"
    content: str = ""
    files: List[str] = Field(default_factory=list)
    #: 中止/失败原因（status 为 aborted / failed 时说明为什么停）——事后诊断用
    abort_reason: str = ""


class Clarifier:
    """澄清提问的用户交互协议（展示层实现，编排层只依赖本协议）。

    计划不再需要用户确认：Mentor 给出计划即执行，所以这里只剩"信息不足时问清楚"。
    """

    def ask_clarification(self, questions: List[dict]) -> List[str]:
        raise NotImplementedError


# ---------------------------------------------------------------- jsonout
# ---------------------------------------------------------------- extract_json
def extract_json(text: str) -> Any:
    """返回文本中可解析的 JSON 对象；找不到时抛 :class:`ValueError`。"""
    return parse_json_object(text)


# ---------------------------------------------------------------- model_output
#: 工具调用标记：``<|DSML|function_calls>…</|DSML|function_calls>`` 一类（各家叫法不同）
_TOOL_MARKUP = re.compile(r"<\|[^|>]*\|[^>]*>.*?(?:</\|[^|>]*\|[^>]*>|$)", re.S)
#: ``` 围栏（含 ```tool / ```json 这类带语言标注的）
_FENCE = re.compile(r"```[a-zA-Z0-9_-]*\n(.*?)(?:```|$)", re.S)
#: 尾逗号：``{"a": 1,}`` / ``[1, 2,]``
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def strip_fences(text: str) -> str:
    """去掉 Markdown 代码围栏，只留里面的正文（没有围栏时原样返回）。"""
    body = (text or "").strip()
    if not body.startswith("```"):
        return body
    match = _FENCE.search(body)
    return (match.group(1) if match else body).strip()


def strip_tool_markup(text: str) -> str:
    """去掉模型误输出的工具调用标记（工具被关掉时它会这么写）。"""
    cleaned = _TOOL_MARKUP.sub("", text or "")
    # 语言标注为 tool 的围栏整块丢弃（那是"我想调工具"的文本形式，不是结果）
    cleaned = re.sub(r"```tool\b.*?(?:```|$)", "", cleaned, flags=re.S)
    return cleaned


def drop_trailing_commas(text: str) -> str:
    """删掉对象/数组末尾多余的逗号。"""
    return _TRAILING_COMMA.sub(r"\1", text)


def close_truncated(text: str) -> str:
    """尽力补全被截断的 JSON：从第一个 ``{`` 起补齐未闭合的括号。

    只在"确实是被截断"（引号配平、括号不配平）时生效；补完仍不合法就原样返回。
    """
    start = text.find("{")
    if start < 0:
        return text
    fragment = text[start:]
    if fragment.count('"') % 2 != 0:
        return text  # 停在字符串中间：补引号只会更糟
    stack: list[str] = []
    in_string = False
    escaped = False
    for char in fragment:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]" and stack:
            stack.pop()
    if not stack:
        return fragment
    return fragment.rstrip().rstrip(",") + "".join(reversed(stack))


def parse_json_object(text: str) -> dict:
    """从模型输出里取出一个 JSON 对象；四道兜底都失败时抛 ``ValueError``。

    候选按"解析跨度最大者优先、跨度相同取更靠后者"挑选——前者保证拿到最外层对象，
    后者保证模型"先给示例、再给结果"时取到结果。
    """
    return parse_json(text, want_dict=True)


def parse_json(text: str, *, want_dict: bool) -> Any:
    """按四道容错依次尝试解析；``want_dict`` 决定只认对象还是任意 JSON 值。"""
    steps = (
        ("原样", lambda value: value),
        ("去掉围栏", strip_fences),
        ("去掉工具标记", lambda value: strip_tool_markup(strip_fences(value))),
        ("修尾逗号", lambda value: drop_trailing_commas(strip_tool_markup(strip_fences(value)))),
        (
            "补全截断",
            lambda value: close_truncated(drop_trailing_commas(strip_tool_markup(strip_fences(value)))),
        ),
    )
    tried: list[str] = []
    scalar: Any = None
    for label, transform in steps:
        candidate = transform(text or "")
        parsed = largest_value(candidate, want_dict=want_dict)
        if parsed is None:
            tried.append(label)
            continue
        if want_dict or isinstance(parsed, (dict, list)):
            return parsed
        # 只拿到标量：记着，等后面几步（修尾逗号 / 补全）试完——坏掉的对象往往能救回来，
        # 而前面几步只能捞到里面的某个字符串（例如 {"items": [1, 2,]} 里的 "items"）
        scalar = parsed if scalar is None else scalar
        tried.append(label)
    if scalar is not None:
        return scalar
    raise ValueError(
        "输出里找不到可解析的 JSON（已尝试：" + "、".join(tried) + "）"
        "；请只输出一个 JSON 对象，不要解释、不要代码围栏、不要工具调用标记"
    )


def largest_value(source: str, *, want_dict: bool) -> Any:
    """在文本里挑一个跨度最大、最靠后的 JSON 值；没有则返回 ``None``。

    排序键依次是：**是否结构性值**（对象/数组）→ 跨度 → 靠后。结构性优先很重要：不这样
    的话，``{"items": [1, 2,]}`` 这种尾逗号坏对象会退化成取到里面那个字符串 ``"items"``。
    ``raw_decode`` 需要完整字面量，所以逐个起点试探是安全的（半截的 ``tru``、``12`` 会失败跳过）。
    """
    decoder = json.JSONDecoder(strict=False)
    best: Optional[tuple[tuple[bool, int, int], Any]] = None
    for index, char in enumerate(source):
        if char not in '{["-0123456789tfn':
            continue
        try:
            data, end = decoder.raw_decode(source, index)
        except ValueError:
            continue
        if want_dict and not isinstance(data, dict):
            continue
        rank = (isinstance(data, (dict, list)), end - index, index)
        if best is None or rank >= best[0]:
            best = (rank, data)
    return None if best is None else best[1]


# ---------------------------------------------------------------- base
# ---------------------------------------------------------------- 通用规则
# ---------------------------------------------------------------- rules


# ---------------------------------------------------------------- agent
logger = logging.getLogger(__name__)

#: 轮次用尽后的收尾指令：关掉工具，逼模型用已有信息给结论（而不是白跑一趟整步作废）
FINAL_ROUND_HINT = (
    "【收尾】工具轮次已用尽，本轮不要再调用任何工具，也不要输出任何工具调用标记"
    "（例如 <|DSML|function_calls>、```tool 之类）。"
    "请基于上面已经获得的信息，只输出一个 JSON 对象："
    "任务已达成则 status=completed；只完成一部分则 status=partial，并在 limitations 里写清还缺什么。"
)

TOOL_FAILURE_HINT = (
    "【失败提示】上一次工具调用失败。请依据实际错误选择合适做法：可等待后重试、"
    "调整参数、更换方法或换用来源；确实需要用户提供信息或决定时输出 needs_input 状态；"
    "方法已穷尽仍做不完就输出 partial，并在 limitations 里写清缺口——是否调整计划由评审判断。"
    "每次调用都带来新信息：同参重复无新产出时，改为调整参数、换方法或换来源。"
)

#: 输出撞到长度上限时的收尾指令：内容没写完，JSON 也不完整——让它把长内容改走文件
TRUNCATED_HINT = (
    "【上一条输出被长度上限截断】内容没有写完，结构化结果也就不完整。"
    "请按输出协议**重新给出完整结果**：长正文用 write / docx 等工具写进文件，"
    "JSON 里 content 只放摘要与文件路径（控制在 2000 字以内），不要重复已被截断的那份内容。"
)

#: 配额用尽时的收尾指令：要"阶段汇报"，不是终态结论
BUDGET_HINT = (
    "【配额用尽】本步工具调用已用 {used}/{budget} 次。先不要再调用工具，给出**阶段性** JSON："
    "status 用 partial，summary 写已经做到哪一步、拿到了什么，limitations 写清还差什么、"
    "下一步打算怎么做。审核会据此决定是追加配额、调整计划还是停下。"
)

#: 运行中压缩的提示词：合并历史任务摘要，近的写清楚、远的写简略
HISTORY_SUMMARY_PROMPT = (
    "【历史任务摘要压缩】下面是本会话此前已完成任务的摘要。请把它们合并成一份更短的总摘要：\n"
    "1. **越近的任务写得越清楚**：最近一个任务保留关键数字、产出文件名与结论；\n"
    "   越早的任务越简略，一句话说明做了什么、产出了什么即可。\n"
    "2. 保留任务编号，方便按 summary.json 的 /records/<任务号> 回读细节。\n"
    "3. 保留仍然有用的产出文件路径；已被后续任务取代的可以不列。\n"
    "4. 不新增事实、不编造数字，只压缩已有信息。\n"
    "直接输出替换这段历史摘要的新文本，不要任何解释或前后缀。"
)

#: 每累计这么多次工具调用，就让模型自己判断一次"还要不要继续、问题在哪"
SELF_CHECK_EVERY = 30
#: 自检提示：关掉工具，只要一句 JSON；解析失败就当作继续（不打断正常执行）
SELF_CHECK_HINT = (
    "【自检】本步已累计 {count} 次工具调用。先不要调用工具，只输出一句 JSON："
    '{{"continue": true/false, "problem": "当前卡在哪（没有就留空）", "next": "下一步做什么"}}。'
    "判断依据：同一目标是否反复尝试却没有新信息、错误是否一直一样、现有信息是否已经够交付。"
    "确认能交付时给 continue=false，并在下一条消息直接输出终态 JSON；只是慢但还在推进就给 continue=true。"
)


#: 压缩时单条**工具结果**保留的上限（字，头尾各留一半）
COMPRESS_TOOL_CHARS = 6_000
#: 压缩后至少保留最近几个「assistant + 它的工具结果」回合
COMPRESS_KEEP_ROUNDS = 4

#: 模型窗口未知时的兜底（与页面「上下文占用」的显示口径一致）
DEFAULT_CONTEXT_LIMIT = 1_048_576


Parser = Callable[[str, List[ToolCall]], Any]


ExhaustedHandler = Callable[[List[ToolCall], str, List[str]], Any]



_SEGMENT_JOIN = "\n\n"


class BaseAgent:
    """无状态 Agent 基类：启动时自装配 + 事件协议 + 运行上下文 + 回合（单轮或工具循环）。"""

    name: str = "agent"
    final_hint: str = FINAL_ROUND_HINT
    #: 是否是worker（决定目录位置：``agent/<角色>/`` 还是 ``agent/worker/<名字>/``）
    is_worker: bool = False
    #: 装配层注入的额外固定段（例如 mentor 的「可用worker」清单）：由 :meth:`assemble` 拼进 system
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
        #: 运行中压缩的触发比例（0 = 关闭）。只对 mentor 生效：worker 每步新建上下文，
        #: 单次工具输出另有 200k 硬阀，不需要再压一次
        self.compress_ratio = 0.0
        #: 固定层（system + 工具 schema）字符数缓存，见 :meth:`_fixed_layer_chars`
        self._fixed_chars: Optional[int] = None
        #: 输出上限（token）：0 表示用端点默认值，装配时由 settings 覆盖
        self.max_output_tokens = 0
        self.system_prompt = system_prompt or ""
        #: 固定层的尾段（技能读取说明）：排在运行环境之前
        self.system_tail = ""
        self.context = MessageContext(system_prompt=self.system_prompt, suffix=suffix)
        self.listeners: List[Listener] = []
        #: 本角色可用的工具定义与执行入口（:meth:`assemble` 装好后由工具箱提供；
        #: 也可以直接注入，测试与一次性脚本用得上）
        self.tool_defs = list(tool_defs or [])
        self.tool_runner = tool_runner
        self.max_tool_rounds = max_tool_rounds
        #: 模型上下文窗口（token）：装配时由 settings 覆盖；0 表示未知、不做自动压缩
        self.context_limit = 0
        #: 装配产物：自己认领的工具与技能（未装配时为空）
        self.tools: List[dict] = []
        self.skills: List[dict] = []
        self.toolbox: Optional[ToolBox] = None
        #: 本轮取消检查（:meth:`execute` 按请求注入；None 表示不检查）
        self._stop_check: Optional[Callable[[], bool]] = None

    # ---------------------------------------------------------------- 装配
    def assemble(self, settings) -> List[str]:
        """读自己的注册表并完成装配（启动时一次），返回加载错误。

        **注册表即归属**（见 :mod:`backend.orchestration.orchestrate`）：角色能用的工具与技能完全由
        ``backend/agents/agents.yaml`` 里以角色名为键声明——没列出的文件存在也不加载，列了却缺失是错误。
        ``access: read-only`` 的角色声明写类工具会在校验阶段被拒绝。

        顺序：注册表 → 工具 → 技能 → 提示词 → system prompt → 工具箱 / 技能箱。
        system prompt 只放**稳定信息**（角色规范 + 通用规则 + 工具清单 + 技能清单）；项目路径等
        **可变信息**由编排层每轮注入（:attr:`MessageContext.suffix`）。
        """
        from backend.orchestration.orchestrate import load_registry, load_skills, load_tools, unlisted

        errors: List[str] = []
        entry, entry_errors = load_registry(settings.agent_dir, self.name)
        errors.extend(entry_errors)
        self.registry: dict = entry or {}
        self.tools, tool_errors = load_tools(settings.agent_dir, self.registry)
        errors.extend(tool_errors)
        self.skills, skill_errors = load_skills(settings.agent_dir, self.registry)
        errors.extend(skill_errors)
        prompt, prompt_errors = self.own_prompt(settings)
        errors.extend(prompt_errors)

        self.toolbox = ToolBox(self.tools, self.skills, output_limit=getattr(settings, "tool_output_limit", None), access=self.registry.get("access","read-write"))
        self.tool_defs = self.toolbox.schemas()
        # 上下文水位：窗口未配时用与页面显示一致的兜底值（deepseek-flash 的 1M）
        self.context_limit = int(settings.model_context_limit or DEFAULT_CONTEXT_LIMIT)
        self.max_output_tokens = int(getattr(settings, "max_output_tokens", 0) or 0)
        self.compress_ratio = float(getattr(settings, "context_compress_ratio", 0.0) or 0.0)
        self._fixed_chars = None
        # 固定层顺序：角色提示词（含通用规则）→ 额外固定段 → 工具与技能 ‖ 技能读取（tail）
        # ‖ 运行环境（suffix，每轮注入，永远最后）
        self.system_prompt = _SEGMENT_JOIN.join(
            segment
            for segment in (
                prompt,
                self.extra_system,
                self.toolbox.index(self.skill_title),
            )
            if segment
        )
        self.system_tail = self.skill_hint(settings)
        self.context = MessageContext(
            system_prompt=self.system_prompt, suffix=self.context.suffix, tail=self.system_tail
        )
        # 目录里存在但注册表没列的能力：只诊断，不加载（否则"放个文件就生效"会绕过注册表）
        for note in unlisted(settings.agent_dir, self.registry):
            logger.debug(note)
        return errors

    def skill_hint(self, settings) -> str:
        """技能清单与读取方式：技能库不暴露绝对路径，正文走 ``read`` 的 skill 通道。"""
        if not self.skills:
            return ""
        execution_hint = (
            '脚本用run_code：file_path=skill://技能名/scripts/脚本.py，args=[参数...]。'
            if 'run_code' in self.toolbox.names
            else '本角色只读取方法与证据；需要运行脚本时交给具备对应skill的Worker。'
        )
        return (
            '【技能读取】按上方描述选择本角色skill，先read(targets=[{"skill":"技能名"}])读取SKILL.md，'
            '仅按任务需要读取references；' + execution_hint
        )

    def own_prompt(self, settings) -> tuple[str, List[str]]:
        """自己的提示词：注册表 ``prompt`` 字段指向 ``backend/prompts/<文件名>``。"""
        from backend.orchestration.orchestrate import load_prompt

        return load_prompt(settings.agent_dir, self.registry or {"name": self.name})

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
        clone.context = MessageContext(
            system_prompt=self.system_prompt,
            suffix=request.environment,
            workspace=request.workspace,
            tail=self.system_tail,
        )
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
        return bool(check())

    def runner_for(self, request: AgentRequest) -> Optional[ToolRunner]:
        """按本轮运行环境绑定工具路径；未装配工具箱时沿用注入的 runner。"""
        if self.toolbox is None:
            return self.tool_runner
        from backend.utils.paths import Roots

        return self.toolbox.runner(
            workspace=request.workspace,
            session_dir=request.session_dir,
            vision=self.describe_image,
            stopped=self.should_stop,
            roots=Roots(
                request.workspace,
                skills=[str(skill.get("name") or "") for skill in self.skills],
                session_dir=request.session_dir,
                access=self.registry.get("access","read-write"),
            ),
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
            listener(kind, data)

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
            max_tokens=self.max_output_tokens or None,
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
        """固定层（system 提示 + 工具 schema）的字节数：装配后不变，算一次就缓存。"""
        if self._fixed_chars is None:
            tools = json.dumps(self.tool_defs, ensure_ascii=False) if self.tool_defs else ""
            self._fixed_chars = (
                len((self.system_prompt or "").encode("utf-8"))
                + len((self.system_tail or "").encode("utf-8"))
                + len((self.context.suffix or "").encode("utf-8"))
                + len(tools.encode("utf-8"))
            )
        return self._fixed_chars

    def maybe_compress(self) -> None:
        """运行中压缩（第三级）：**只给 mentor**。

        worker 每步新建上下文、单次工具输出又有 200k 硬阀，不会靠累积撑爆窗口；
        mentor 每次要读完整计划、已完成摘要与证据，单次调用内工具往返最容易把上下文推高。
        所以这里对 worker 直接返回，避免多做一层无收益的处理。

        先按 ``COMPRESS_TOOL_CHARS`` 截断超长工具结果（头尾各留一半 + 落盘路径），
        仍超预算再成组丢弃最旧的「assistant + 工具结果」往返（保留最近几轮）。
        """
        if self.is_worker or not self.compress_ratio or not self.context_limit:
            return
        used = (self.context.size() + self._fixed_layer_chars()) // CHARS_PER_TOKEN
        if used < self.context_limit * self.compress_ratio:
            return
        before = self.context.size()
        # 先针对"已完成任务的历史摘要"做一次总结：把全部历史任务合并成一份总摘要。
        # 只压这一块，不动本次审核的上下文（历史任务的细节仍可按 json_pointer 回读）。
        if self._summarize_history():
            self.emit("context_compressed", agent=self.name, before_chars=before,
                      after_chars=self.context.size(), used_tokens=used,
                      limit=self.context_limit, ratio=self.compress_ratio, scope="history")
            return
        # 历史摘要已经是最紧凑形态，仍超预算才丢最旧的工具往返
        if not self.context.compress(max(1, before // 2)):
            return
        self.emit("context_compressed", agent=self.name, before_chars=before,
                  after_chars=self.context.size(), used_tokens=used,
                  limit=self.context_limit, ratio=self.compress_ratio, scope="rounds")

    def _summarize_history(self) -> bool:
        """把历史任务摘要交给模型合并成一份总摘要（越近越详细）。

        只替换上下文里那段「【本会话任务历史】」，不动本次审核的请求与证据。
        总结失败、或没变短，都返回 False（交给上层走丢往返的兜底）。
        """
        target = None
        for message in self.context.conversation:
            if str(message.get("content") or "").lstrip().startswith("【本会话任务历史】"):
                target = message
                break
        if target is None:
            return False
        original = str(target.get("content") or "")
        saved = self.context.tool_defs
        self.context.set_fixed_layer(tool_defs=[])
        self.context.add("user", HISTORY_SUMMARY_PROMPT + "\n\n" + original)
        try:
            response = self.complete(interruptible=False)
        except Exception:  # noqa: BLE001 - 总结失败不该打断正在进行的审核
            logger.debug("历史摘要总结失败", exc_info=True)
            return False
        finally:
            self.context.set_fixed_layer(tool_defs=saved)
            self.context.conversation.pop()  # 去掉这次临时请求
        summary = str(response.parsed.get("content") or "").strip()
        if not summary or len(summary) >= len(original):
            return False
        target["content"] = "【本会话任务历史（已合并）】\n" + summary
        return True

    def run_turn(
        self,
        parse: Parser,
        *,
        retry_hint: str,
        error_cls: type[Exception],
        exhausted: Optional[ExhaustedHandler] = None,
        summarize_on_stop: bool = False,
        tool_budget: int = 0,
    ) -> Any:
        """运行一次回合并解析结果（只对流式增量与工具调用发事件）。

        ``summarize_on_stop``：用户中止时是否让模型交代"已经做了什么"。worker需要
        （它的中途成果对用户有交接价值）；Mentor / Mentor 不需要，直接抛取消。
        """
        records: List[ToolCall] = []
        current_goal = ""
        #: 下一次自检的触发点：每 SELF_CHECK_EVERY 次工具调用问一次模型要不要继续
        next_check = SELF_CHECK_EVERY
        premature_partial_prompted = False
        self.budget_exhausted = False
        rounds = self.max_tool_rounds if (self.tool_defs or self.tool_runner) else 1
        for round_index in range(rounds):
            if self.should_stop():
                return self.finalize_stopped(parse, records, summarize_on_stop)
            if rounds > 3 and round_index == rounds - 3:
                self.context.add("user", "【剩余3轮】优先执行尚未运行的必要验证并返回终态 JSON。不要再新建额外核验脚本或手写步骤日志；summary/handoff 和系统工具轨迹已承担交接。未验证项如实列入 limitations。")
            # 每轮开始前先看上下文水位：逼近模型窗口就压缩，避免请求直接被拒
            # 轮次事件：前端可显示"第 N/M 轮"，也是统计真实往返次数的唯一口径
            # 运行中压缩：只在 mentor 上生效（worker 直接返回）
            self.maybe_compress()
            self.emit("llm_round", agent=self.name, round=round_index + 1, total=rounds)
            response = self.complete()
            if self.should_stop():
                # 本次调用被用户中断（流式就地停下）：不再解析半截输出，重新要一份交代
                return self.finalize_stopped(parse, records, summarize_on_stop)
            if response.parsed["stop_reason"] == "error":
                if summarize_on_stop:
                    result = self.finalize_stopped(parse, records, True)
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
                try:
                    progress=json.loads(strip_fences(content))
                except (ValueError,TypeError):
                    progress=None
                if self.is_worker and isinstance(progress,dict) and round_index+1<rounds:
                    if progress.get('status')=='continue':
                        self.context.add('user','continue是中途状态。继续本步执行，不重新读已核验材料；实际交付完成后才返回completed，真实阻塞才返回partial。')
                        continue
                    description=' '.join(str(progress.get(key) or '') for key in ['summary','content','limitations'])
                    if (progress.get('status')=='partial' and not premature_partial_prompted and records
                            and round_index+3<rounds and not any(record.is_error for record in records[-3:])
                            and re.search(r'尚未写|未落盘|尚未生成|仅完成.*读取|未写入.*文件',description)):
                        premature_partial_prompted=True
                        self.context.add('user','你报告的缺口是尚未执行的文件交付，近期工具调用没有失败。若现有工具仍可完成，请在剩余轮次继续写出并核验实际产物，不把待办当作阻塞；若确实无法继续，下一次partial须给出具体阻塞证据。')
                        continue
                return self.parse_with_retry(content, parse, retry_hint, error_cls, records)
            if content.strip():
                current_goal = content.strip()
            batch, any_failed = self.run_tool_calls(response.parsed["tool_calls"], round_index, current_goal)
            records.extend(batch)
            if self.should_stop():
                return self.finalize_stopped(parse, records, summarize_on_stop)
            if any_failed:
                self.context.add("user", TOOL_FAILURE_HINT)
            # 每 SELF_CHECK_EVERY 次工具调用让模型自检一次：比计数器猜"原地打转"准，
            # 也允许它在信息够了的时候主动收尾，而不是硬跑满轮次
            if tool_budget and len(records) >= tool_budget:
                # 配额用完：先把已有进展交出来（partial + 缺口），由编排层发起阶段审核，
                # 审核决定追加配额继续 / 改计划 / 就此停下
                self.budget_exhausted = True
                return self.finalize_round(
                    parse,
                    records,
                    error_cls,
                    exhausted,
                    f"本步工具调用达到配额（{len(records)}/{tool_budget} 次），先交阶段性结果。",
                    [f"工具调用达到配额（{len(records)}/{tool_budget} 次），等待阶段审核。"],
                    hint=BUDGET_HINT.format(used=len(records), budget=tool_budget),
                )
            if len(records) >= next_check:
                next_check += SELF_CHECK_EVERY
                verdict = self.self_check(len(records))
                if verdict is not None and not verdict.get("continue", True):
                    return self.finalize_round(
                        parse,
                        records,
                        error_cls,
                        exhausted,
                        str(verdict.get("problem") or "自检判定当前信息已足够或无法继续。"),
                        [str(verdict.get("next") or "按自检结论收尾。")],
                        hint=SELF_CHECK_HINT.format(count=len(records)),
                    )
                if verdict is not None and verdict.get("next"):
                    self.context.add("user", f"【自检结论】下一步：{verdict['next']}")
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
        run_code / image_describe / delete / move）由它们自己的批量参数在内部并发执行。
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
            records.append(ToolCall(name=name, arguments=arguments_for(index), result=output or "", is_error=is_error))
            if started:
                from backend.utils.errors import tool_error_hint
                hint = tool_error_hint(output) if is_error and output.strip() else ""
                self.emit("tool_execution_end", agent=self.name, tool=name, tool_call_id=call_ids[index],
                          is_error=is_error, error_hint=hint)
            checkpoint()

        for index in range(len(calls)):
            if self.should_stop():
                finish(index, "工具调用已取消；未启动工具", True, started=False)
                continue
            name = str(calls[index].get("name", ""))
            self.emit("tool_execution_start", agent=self.name, tool=name,
                      arguments=self.display_arguments(arguments_for(index)), tool_call_id=call_ids[index])
            try:
                output, is_error = self.run_tool(name, arguments_for(index))
            except Exception:  # noqa: BLE001 - 单个调用失败不该中断其余调用
                output, is_error = traceback.format_exc(), True
            finish(index, output, is_error)
        return records, any(record.is_error for record in records)

    def display_arguments(self, arguments: Any) -> Any:
        """事件里只展示相对项目目录的路径：绝对路径既暴露系统布局，页面看着也吵。

        只影响**展示**，工具拿到的仍是模型原样给的参数。
        """
        workspace = str(getattr(getattr(self, "context", None), "workspace", "") or "").rstrip("\\/")
        if not workspace or not isinstance(arguments, (dict, list, str)):
            return arguments

        def scrub(value: Any) -> Any:
            if isinstance(value, str):
                for prefix in (workspace + "\\", workspace + "/", workspace):
                    if value.startswith(prefix):
                        trimmed = value[len(prefix):].lstrip("\\/")
                        return trimmed or "."
                return value
            if isinstance(value, dict):
                return {key: scrub(item) for key, item in value.items()}
            if isinstance(value, list):
                return [scrub(item) for item in value]
            return value

        return scrub(arguments)

    def self_check(self, count: int) -> Optional[dict]:
        """让模型自己判断要不要继续：关掉工具问一次，只要一句 JSON。

        比"计数器猜原地打转"准：模型知道自己在做什么、还差什么。解析失败或调用失败
        都返回 None（当作继续）——自检本身不该打断正在推进的执行。
        """
        saved = self.context.tool_defs
        self.context.set_fixed_layer(tool_defs=[])
        self.context.add("user", SELF_CHECK_HINT.format(count=count))
        try:
            response = self.complete(interruptible=False)
        except Exception:  # noqa: BLE001 - 自检失败按"继续"处理
            logger.debug("自检调用失败", exc_info=True)
            return None
        finally:
            self.context.set_fixed_layer(tool_defs=saved)
        content = str(response.parsed.get("content") or "").strip()
        if not content.startswith("{"):
            return None
        try:
            data = json.loads(content)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def run_tool(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        if self.tool_runner is None:
            return f"错误：工具执行器未配置，无法调用 {name}", True
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
        except Exception as error:  # noqa: BLE001 - 重试必须携带具体错误，避免重复同一不合法字段
            self.context.add("user", retry_hint+'\n具体协议错误：'+str(error)[:1200])
            retry = self.complete()
            if retry.parsed["stop_reason"] == "error":
                raise error_cls(retry.parsed["error_text"])
            self.context.add("assistant", retry.parsed["content"], tool_calls=retry.parsed["tool_calls"] or None)
            try:
                return parse(retry.parsed["content"], records)
            except error_cls:
                raise
            except Exception as error:
                failure = error_cls(str(error))
                # 解析失败前工具可能已经写了文件：把记录随异常带出去，
                # 否则编排层只能留一句错误，那一步的产出就跟着丢了。
                failure.records = list(records)
                raise failure from error

    def finalize_round(
        self,
        parse: Parser,
        records: List[ToolCall],
        error_cls: type[Exception],
        exhausted: Optional[ExhaustedHandler],
        summary: str,
        limitations: List[str],
        hint: Optional[str] = None,
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
        self.context.add("user", "\n".join(part for part in (hint, self.final_hint) if part))
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

        不请求模型总结，断网时仍能保留已经返回的工具结果。产出的是**可读摘要**而不是
        原始 JSON 堆：这段会进任务摘要与下一次规划的上下文，它得说明"用户停止前已经
        做过什么、哪些已返回结果"，而不是把工具回执再倒一遍。
        """
        if not summarize:
            raise CancelledError("用户中止了任务")
        lines: List[str] = []
        for record in records:
            arguments = record.arguments or {}
            target = (arguments.get("path") or arguments.get("file_path")
                      or arguments.get("file") or arguments.get("query") or "")
            mark = "失败" if record.is_error else "完成"
            lines.append(f"- {record.name}({str(target)[:80]}) {mark}")
        body = "\n".join(lines) or "（本步还没有执行过工具）"
        return parse(json.dumps({
            "status": "partial",
            "summary": "用户停止了任务，已保留停止前的实际执行记录",
            "content": (
                "【用户停止】本步被用户停止，以下为停止前实际执行过并已返回结果的操作"
                "（未返回的操作不能视为完成）：\n" + body
            ),
            "limitations": ["用户停止了任务，本步未完成"],
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


# ---------------------------------------------------------------- state
#: 估算 token 的粗略系数：字符数 / 该值（与 base.complete 里的口径一致）
CHARS_PER_TOKEN = 3


class MessageContext:
    """分层上下文：system 固定层（提示/工具/技能）+ 会话层。"""

    def __init__(self, system_prompt: str = "", suffix: str = "", workspace=None, tail: str = "") -> None:
        self.system_prompt = system_prompt
        self.suffix = suffix
        #: 尾段（技能读取说明）：排在 suffix（运行环境）之前
        self.tail = tail
        self.tool_defs: List[dict] = []
        self.conversation: List[dict] = []
        self.workspace = workspace

    def reset(self, system_prompt: Optional[str] = None) -> None:
        if system_prompt is not None:
            self.system_prompt = system_prompt
        self.conversation = []

    def set_fixed_layer(self, tool_defs: Optional[List[dict]] = None) -> None:
        """固定层只有工具 schema；技能正文由模型用 read 直接读技能文件，不进 system。"""
        if tool_defs is not None:
            self.tool_defs = list(tool_defs)

    def add(self, role: str, content: str, **fields: Any) -> None:
        message: dict[str, Any] = {"role": role, "content": content}
        if fields:
            message.update({key: value for key, value in fields.items() if value is not None})
        self.conversation.append(message)

    def to_llm_messages(self) -> List[dict]:
        segments = [self.system_prompt, self.tail, self.suffix]
        system_content = "\n\n".join(segment for segment in segments if segment)
        messages: List[dict] = []
        if system_content:
            messages.append({"role": "system", "content": system_content})
        messages.extend(self.conversation)
        return messages

    # ---------------------------------------------------------------- 占用与压缩
    # ---------------------------------------------------------------- 运行中压缩
    def size(self) -> int:
        """会话层字符数（只算 content）：压缩触发的估算口径。"""
        return sum(len(str(message.get("content") or "")) for message in self.conversation)

    def rounds(self) -> List[tuple[int, int]]:
        """把会话层切成成组区间：一条带 tool_calls 的 assistant + 它后面紧跟的 tool 结果。

        成组是因为单独丢一半会让 tool_call_id 找不到对应结果，请求直接被端点拒掉。
        """
        groups: List[tuple[int, int]] = []
        index = 0
        while index < len(self.conversation):
            message = self.conversation[index]
            if message.get("role") == "assistant" and message.get("tool_calls"):
                end = index + 1
                while end < len(self.conversation) and self.conversation[end].get("role") == "tool":
                    end += 1
                groups.append((index, end))
                index = end
            else:
                index += 1
        return groups

    def truncate_long_messages(self) -> bool:
        """把超长工具结果截成头尾两段，全文落盘（返回是否有改动）。

        保留头尾而不是整条丢掉：开头通常是表头或命令回显，结尾通常是结论或报错，
        模型据此判断"要不要去读全文"。
        """
        changed = False
        for message in self.conversation:
            if message.get("role") != "tool":
                continue
            content = str(message.get("content") or "")
            if len(content) <= COMPRESS_TOOL_CHARS:
                continue
            head = COMPRESS_TOOL_CHARS // 2
            tail = COMPRESS_TOOL_CHARS - head
            saved = spill_output("tool-result", content, workspace=self.workspace)
            message["content"] = (
                content[:head]
                + f"\n…（此处省略 {len(content) - head - tail:,} 字；全文 {saved or '落盘失败'}，可用 read 回读）…\n"
                + content[-tail:]
            )
            changed = True
        return changed

    def compress(self, budget: int, keep_rounds: int = COMPRESS_KEEP_ROUNDS) -> bool:
        """压缩会话层：先截超长工具结果，仍超预算就成组丢弃最旧的往返。

        只动**会话层**：system 固定层（角色提示 / 工具 schema / 运行环境）一个字都不改——
        它们决定行为，压掉会让模型跑偏。丢掉的往返全文落盘，保留一条回读路径，
        所以"丢了什么"始终可查。返回是否发生改动。
        """
        changed = self.truncate_long_messages()
        if self.size() <= budget:
            return changed
        groups = self.rounds()
        if len(groups) <= keep_rounds:
            return changed
        drop = groups[:-keep_rounds] if keep_rounds > 0 else groups
        kept_from = drop[-1][1]
        head = self.conversation[: groups[0][0]]
        dropped = [self.conversation[start:end] for start, end in drop]
        saved = spill_output("dropped-rounds", json.dumps(dropped, ensure_ascii=False), workspace=self.workspace)
        note = {
            "role": "user",
            "content": (
                f"（更早的 {len(dropped)} 组「助手 + 工具结果」往返已省略以腾出上下文；"
                f"全文 {saved or '落盘失败'}，需要时用 read 回读。）"
            ),
        }
        self.conversation = [*head, note, *self.conversation[kept_from:]]
        return True

    def estimated_tokens(self) -> int:
        """整份请求（含 system 与工具 schema）的 token 估算，口径与 ``BaseAgent.complete`` 一致。"""
        encoded = json.dumps(
            {"messages": self.to_llm_messages(), "tools": self.tool_defs}, ensure_ascii=False
        )
        return (len(encoded.encode("utf-8")) + 2) // CHARS_PER_TOKEN


# ---------------------------------------------------------------- toolbox
#: 单次输出超过 24,000 字符时保存全文并返回回读路径，减少重复输入成本。
OUTPUT_LIMIT = 24_000

#: 溢出内容的落盘位置：系统临时目录下按会话/工具分文件，不污染工作区
_SPILL_PREFIX = "peifeng-output-"


def spill_output(tool: str, output: str, workspace=None) -> str:
    """把超长输出写到**项目内** ``internal/spill/``，返回路径（失败返回空串）。

    必须落在项目里：模型的 ``read`` 只允许项目目录，写到系统临时目录等于"给了路径却读不了"。
    """
    import tempfile

    folder = None
    if workspace:
        folder = Path(workspace) / "internal" / "spill"
        folder.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".txt",
        prefix=f"{_SPILL_PREFIX}{tool}-",
        delete=False,
        dir=str(folder) if folder else None,
    )
    with handle:
        handle.write(output)
    return handle.name


def guard_output(tool: str, output: str, limit: int = OUTPUT_LIMIT, workspace=None) -> str:
    """输出兜底：没超就原样返回；超了就把全文落盘到项目内，只回一条能定位到文件的提示。"""
    if len(output) <= limit:
        return output
    saved = spill_output(tool, output, workspace)
    size = f"{len(output):,}"
    if not saved:
        # 写不出来时也只能提示（不能静默砍内容）
        return (
            f"（工具 {tool} 的输出有 {size} 字符，超过 {limit:,} 上限，且无法落盘；"
            "请改小参数或分段调用）"
        )
    shown = saved
    if workspace:
        try:
            shown = Path(saved).resolve().relative_to(Path(workspace).resolve()).as_posix()
        except (OSError, ValueError):
            shown = saved
    hint = (
        f"（工具 {tool} 的输出有 {size} 字符，超过 {limit:,} 上限：**完整内容已存到 {shown}**，"
        f"没有截断。用 read 分段查看（path 用这个相对路径 + offset/limit，或加 grep 定位））"
    )
    return hint


class ToolBox:
    """某个 agent 可见的工具（由 ``tools/`` 的目录层级决定，见 ``shared/loader.load_agent_tools``）。"""

    def __init__(self, tools: List[dict], skills: Optional[List[dict]] = None, output_limit: Optional[int] = None, access: str = "read-write") -> None:
        self.access = access
        self.tools: List[dict] = list(tools)
        self._by_name: dict[str, dict] = {tool["name"]: tool for tool in self.tools}
        #: 本角色认领的技能（``skills/<名字>/SKILL.md`` 的 frontmatter，不含正文）
        self.skills: List[dict] = list(skills or [])
        self._skill_by_name: dict[str, dict] = {str(item.get("name")): item for item in self.skills}
        #: 单次输出兜底上限（None = 用模块默认 OUTPUT_LIMIT）
        self.output_limit: int = int(output_limit) if output_limit else OUTPUT_LIMIT

    def __bool__(self) -> bool:
        return bool(self.tools)

    @property
    def names(self) -> List[str]:
        """工具名（按装配顺序）。"""
        return [tool["name"] for tool in self.tools]

    def schemas(self) -> List[dict]:
        """转为 Chat Completions 的 function schema。

        并发的表达方式只有一种：工具自己的**批量参数**（``paths`` / ``queries`` / ``urls`` /
        ``commands`` / ``items``）。所以这里不需要再往每个工具上挂调度参数。
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["parameters"],
                },
            }
            for tool in self.tools
        ]

    def index(self, skill_title: str = "本角色可用技能") -> str:
        """system 里的能力清单：工具与技能放同一段（都是"我有什么"，分开看要多跳一次）。"""
        blocks: list[str] = []
        if self.tools:
            blocks.append("工具：\n" + "\n".join(
                f"- {tool['name']}：{tool['description']}" for tool in self.tools
            ))
        lines = [
            f"- {skill.get('name')}：{skill.get('description')}"
            for skill in self.skills
            if skill.get("name") and skill.get("description")
        ]
        if lines:
            blocks.append(f"技能（{skill_title}）：\n" + "\n".join(lines))
        return "\n\n【可用工具与技能】\n" + "\n\n".join(blocks) if blocks else ""

    @property
    def skill_names(self) -> List[str]:
        """技能名（按目录顺序），用于诊断与测试。"""
        return [str(skill.get("name")) for skill in self.skills]

    def skill(self, name: str) -> Optional[dict]:
        """按名字取技能条目（只含元数据，不含正文）。"""
        return self._skill_by_name.get((name or "").strip())

    def runner(
        self,
        *,
        workspace=None,
        session_dir=None,
        vision=None,
        stopped=None,
        roots=None,
    ) -> ToolRunner:
        """绑定本轮路径，返回给模型用的工具执行入口。

        ``workspace``（项目工作文件夹）、``session_dir``（当前会话目录）与 ``roots``
        （路径白名单，见 :mod:`backend.orchestration.orchestrate`）由宿主注入，不出现在工具的 parameters 里：
        即便模型在参数里塞了同名键，也以这里注入的为准。
        """
        if roots is None:
            from backend.utils.paths import Roots
            roots = Roots(workspace, skills=self.skill_names, session_dir=session_dir, access=self.access)
        context = {
            "session_dir": session_dir,
            "base_dir": Path(workspace) if workspace else None,
            # 路径边界：工具只能读写白名单内的位置
            "roots": roots,
            # 看图能力由 agent 注入：工具签名里写 vision 就能用
            "vision": vision,
            "_stopped": stopped,
        }
        return lambda name, arguments: self.run(name, arguments, context)

    def run(self, name: str, arguments: Any, context: dict) -> tuple[str, bool]:
        """调用一个工具：正常返回即成功，异常记为失败（作为结果回给模型）。"""
        tool = self._by_name.get(name)
        if tool is None:
            return f"错误：未知工具 {name}（可用：{'、'.join(self.names) or '无'}）", True
        argument_note = getattr(arguments, 'normalization_note', '')
        payload = dict(arguments) if isinstance(arguments, dict) else {}
        if set(payload) == {'_raw'}:
            detail = ''
            try:
                json.loads(payload['_raw'])
            except (ValueError, TypeError) as error:
                detail = f'解析位置：{error}。'
            return f'错误：{name}参数不是合法的完整JSON，原文未执行。{detail}请检查targets数组的方括号，并正确转义content中的引号、换行和反斜杠后重发；复杂程序优先复用skill脚本。', True
        schema = tool.get("parameters") or {}
        properties = schema.get("properties") or {}
        normalized_fields = []
        item_schema = properties.get('targets', {}).get('items', {})
        target_fields = set(item_schema.get('properties', {}))
        for variant in item_schema.get('oneOf', []):
            target_fields.update(variant.get('properties', {}))
        if 'targets' in properties and 'targets' not in payload and payload and set(payload) <= target_fields:
            normalized_fields = sorted(payload)
            payload = {'targets': [payload]}
        # Repair only an unambiguous single-target envelope: some models put one
        # target field beside targets. Never broadcast fields to multiple targets
        # or override a conflicting value.
        if 'targets' in properties and isinstance(payload.get('targets'), list) and len(payload['targets']) == 1 and isinstance(payload['targets'][0], dict):
            item_schema = properties['targets'].get('items', {})
            fields = set(item_schema.get('properties', {}))
            for variant in item_schema.get('oneOf', []):
                fields.update(variant.get('properties', {}))
            extras = set(payload) - set(properties)
            if extras and extras <= fields and all(key not in payload['targets'][0] or payload['targets'][0][key] == payload[key] for key in extras):
                payload['targets'] = [dict(payload['targets'][0])]
                for key in extras:
                    payload['targets'][0][key] = payload.pop(key)
                normalized_fields.extend(sorted(extras))
        if "targets" in properties:
            targets = payload.get("targets")
            if not isinstance(targets, list) or not targets or any(not isinstance(target, dict) for target in targets):
                return f"错误：{name} 需要非空 targets 对象数组，每项包含一次操作的参数。", True
        if schema.get("additionalProperties") is False and set(payload) - set(properties):
            return f"错误：{name} 不接受参数 {sorted(set(payload) - set(properties))}，可用参数 {list(properties)}。", True
        # 只注入工具签名接受的上下文：不收 session_dir 的工具不会因此 TypeError
        payload.update(accepted_context(tool["func"], {key: value for key, value in context.items() if value}))
        try:
            from backend.utils.files import run_staged

            payload.pop("_stopped", None)
            if tool.get("source"):
                payload.pop("vision", None)
                # session_dir 不注入 payload（它不是工具参数），单独传给事务层：
                # 覆盖/删除前的原文件备份按会话目录落盘，模型看不到也不该控制这个路径
                result = run_staged(
                    tool["func"],
                    payload,
                    context.get("_stopped"),
                    context.get("vision"),
                    session_dir=context.get("session_dir"),
                )
            else:
                # Explicitly injected in-memory tools are used by embedders/tests.
                result = tool["func"](**payload)
        except RuntimeError as error:
            # 工具的约定是"失败抛 RuntimeError"，消息本身就是写给模型看的一句话；
            # 再贴 traceback 只会浪费 token、还可能把模型带偏
            return f"错误：{error}", True
        except Exception as error:  # noqa: BLE001 - 未预期的异常才带栈，便于排查
            return f"错误：{type(error).__name__}: {error}\n{traceback.format_exc()}", True
        body = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        if argument_note:
            body = '参数兼容：'+argument_note+'\n'+body
        if normalized_fields:
            body = f"参数兼容：已将单目标外层字段 {normalized_fields} 移入 targets[0]，未覆盖冲突值。\n" + body
        # 内容型工具（read/fetch/search）先加"外部内容"边界与注入警告，再走超长落盘：
        # 顺序反了就变成落盘纯文本，模型读到的东西没有边界
        from backend.utils.paths import wrap_external

        body = wrap_external(name, body)
        # 统一兜底：任何工具的输出超长都落盘给路径，不截断、也不让它撑爆上下文
        return guard_output(name, body, self.output_limit, context.get("base_dir")), bool(getattr(result, 'has_errors', False))


def accepted_context(func: Callable, injected: dict) -> dict:
    """只保留 ``func`` 签名接受的注入项；签名不可读时宁可不注入。"""
    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return {}
    if any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values()):
        return dict(injected)
    return {key: value for key, value in injected.items() if key in parameters}


# ---------------------------------------------------------------- llm
# ---------------------------------------------------------------- llm

#: 端点专属附加体：思考模式**固定关闭**（随请求显式下发，行为不依赖端点默认值）。
#: 不开的理由：带 tools 的多轮必须把上一轮的 ``reasoning_content`` 原样回传，本项目不做这件事，
#: 开了整条工具链会 400；关掉思考换来的是稳定可跑的工具循环。
NO_THINKING_BODY: dict[str, Any] = {"thinking": {"type": "disabled"}}


class RequestMessage(BaseModel):
    """一次 /chat/completions 请求体。"""

    model: str
    messages: List[dict[str, Any]]
    tools: Optional[List[dict[str, Any]]] = None
    temperature: float = 0.2
    #: 输出上限（token）。None 表示不下发，用端点默认值
    max_tokens: Optional[int] = None
    stream: bool = False
    #: 流式时让它顺带返回用量（``{"include_usage": True}``）：最后一个 chunk 带 ``usage``。
    #: 不是所有 OpenAI 兼容端点都认这个键，所以只在真的走流式时下发，端点拒绝时去掉重试。
    stream_options: Optional[dict[str, Any]] = None
    #: 端点专属的附加字段（如 DeepSeek 的 ``thinking`` 开关）：OpenAI SDK 不认识它，
    #: 要作为 ``extra_body`` 传下去，所以单独放一个字段、不混进 SDK 的已知参数
    extra_body: Optional[dict[str, Any]] = None


class ResponseMessage(BaseModel):
    """一次模型响应；错误信息编码在 ``error`` 中。"""

    status: str = "completed"
    #: 助手正文（流式时为增量拼接结果）
    content: str = ""
    #: 工具调用，统一形状：``{id, name, arguments, type}``
    tool_calls: List[dict[str, Any]] = Field(default_factory=list)
    #: 端点给出的结束原因：stop / length / tool_calls / content_filter
    finish_reason: str = "stop"
    #: 端点返回的用量（``prompt_tokens`` / ``completion_tokens`` / ``total_tokens``）：
    #: 这是账单口径的真实值，上下文占用应优先用它，而不是字符估算。端点不返回时为空。
    usage: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None

    @property
    def parsed(self) -> dict[str, Any]:
        """规范化为统一视图：content / tool_calls / has_tool_calls / stop_reason / error_text。"""
        return {
            "content": self.content,
            "tool_calls": self.tool_calls,
            "has_tool_calls": bool(self.tool_calls),
            "stop_reason": self.stop_reason(),
            "error_text": self.error_text(),
        }

    def stop_reason(self) -> str:
        if self.status == "failed":
            return "error"
        # 截断要排在 tool_calls 之前：被 max_tokens 截断时，模型往往已经吐出半个
        # tool_calls（参数 JSON 没写完）。先判 tool_calls 会把这个信号盖成 tool_use，
        # 上层于是拿着残缺参数去执行——真实轨迹里 26 次参数畸形都是这么来的。
        if self.finish_reason == "length":
            return "length"
        if self.tool_calls:
            return "tool_use"
        return "stop"

    def error_text(self) -> str:
        if isinstance(self.error, dict):
            return str(self.error.get("message") or self.error.get("code") or "")
        return str(self.error or "")


class NormalizedArguments(dict):
    def __init__(self, values, note):
        super().__init__(values)
        self.normalization_note=note


def complete_final_container(text: str):
    """仅补一个末尾缺失容器括号；不补字段、字符串、数值或源码内容。

    只接受**唯一一处错位**：缺失的左括号在栈里，且该处之后只剩闭合符或空白。
    尾部整体缺括号（如 ``{"content":"x"``）**不补**——那与"内容被截断"无法区分，
    补完会得到看似完整的残缺数据，比拒绝更危险。
    """
    stack = []
    quoted = escaped = False
    insertion = None
    closing = {'{': '}', '[': ']'}
    for index, character in enumerate(text):
        if quoted:
            if escaped:
                escaped = False
            elif character == '\\':
                escaped = True
            elif character == '"':
                quoted = False
            continue
        if character == '"':
            quoted = True
        elif character in '{[':
            stack.append(character)
        elif character in '}]':
            if not stack:
                return None
            if closing[stack[-1]] != character:
                if insertion is not None or len(stack) < 2 or closing[stack[-2]] != character:
                    return None
                if any(tail not in '}] \r\n\t' for tail in text[index:]):
                    return None
                insertion = (index, closing[stack.pop()])
            stack.pop()
    if stack or quoted or insertion is None:
        return None
    index, character = insertion
    try:
        values = json.loads(text[:index] + character + text[index:], strict=False)
    except ValueError:
        return None
    if isinstance(values, dict):
        return NormalizedArguments(values, '仅补全末尾一个缺失容器括号；字段和值未改动。')
    return None


def parse_arguments(raw: Any) -> dict[str, Any]:
    """解析工具调用参数。

    只解析完整外层JSON对象，兼容围栏、字符串外尾逗号及完整对象后的多余闭括号；不从损坏源码字符串中捞取嵌套对象。解析失败保留原文供重试，不能执行部分参数。
    """
    if isinstance(raw, dict):
        return raw
    text = "{}" if raw is None else (raw if isinstance(raw, str) else str(raw))
    body = text.strip()
    if body.startswith('```') and body.endswith('```') and '\n' in body:
        body = body.split('\n', 1)[1][:-3].strip()
    # Tool arguments must be the complete outer value. Searching arbitrary JSON
    # fragments inside a damaged code string can select a different operation.
    cleaned = []
    quoted = escaped = False
    for index, character in enumerate(body):
        if quoted:
            cleaned.append(character)
            if escaped:
                escaped = False
            elif character == '\\':
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
            cleaned.append(character)
        elif character == ',' and body[index+1:].lstrip().startswith(('}', ']')):
            continue
        else:
            cleaned.append(character)
    try:
        data = json.loads(''.join(cleaned), strict=False)
    except ValueError:
        # Accept only redundant closing punctuation after a complete outer
        # object. Never search for a nested fragment or fill missing content.
        try:
            candidate = ''.join(cleaned)
            data, end = json.JSONDecoder(strict=False).raw_decode(candidate)
            suffix = candidate[end:].strip()
            if not isinstance(data, dict) or not suffix or any(char not in '}] \r\n\t' for char in suffix):
                return {"_raw": text}
        except ValueError:
            recovered=complete_final_container(''.join(cleaned))
            return recovered if recovered is not None else {"_raw": text}
    return data if isinstance(data, dict) else {"_raw": text}


def normalize_call(call: dict, name: str = "", arguments: Any = None) -> dict[str, Any]:
    """把工具调用规范化为内部统一形状（``arguments`` 允许是流式分片列表）。"""
    raw = call.get("arguments") if "arguments" in call else arguments
    if isinstance(raw, list):
        raw = "".join(str(part) for part in raw)
    return {
        "id": str(call.get("id") or ""),
        "name": str(call.get("name") or name or ""),
        "arguments": parse_arguments(raw),
        "type": "function",
    }


def image_data_uri(image_path, max_side: int = 1400) -> str:
    """把图片读成 data URI；超过 max_side 先等比缩小（省 token，也避开各家大小限制）。"""
    import base64
    import io

    from PIL import Image

    from backend.utils.text import image_source
    from pathlib import Path

    with Image.open(image_source(Path(image_path))) as image:
        if max(image.size) > max_side:
            image.thumbnail((max_side, max_side), Image.LANCZOS)
        buffer = io.BytesIO()
        ready = image.convert("RGB") if image.mode not in ("RGB", "L") else image
        ready.save(buffer, format="JPEG", quality=88)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


class LLMProvider(ABC):
    """LLM 后端统一接口：入参消息用 dict，返回 ResponseMessage，绝不抛异常。"""

    @abstractmethod
    def complete(
        self,
        messages: List[dict],
        tools: Optional[List[dict]] = None,
        temperature: float = 0.2,
        events: Optional[Callable[[str, dict[str, Any]], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        max_tokens: Optional[int] = None,
    ) -> ResponseMessage:
        """发送一次完整对话；events 为可选流事件回调 (kind, data)。

        ``should_stop`` 在流式接收的每个分片之间检查：返回 True 就**就地停下**并返回
        已收到的内容（用户点了停止时不用再等整段输出）。
        """

    def describe_image(self, image_path, question: str, max_side: int = 1400) -> str:
        """看一张图并回答针对它的问题；默认后端不支持时抛错。"""
        raise NotImplementedError("当前模型后端不支持看图")


def to_chat_messages(messages: List[dict]) -> list[dict]:
    """把内部消息 dict 列表转换为 Chat Completions 的 messages。

    内部形状：``assistant`` 的 ``tool_calls`` 是 ``{id, name, arguments}``、
    工具结果用 ``role="tool"`` + ``tool_call_id``；这里补上协议要求的包装与
    ``arguments`` 的 JSON 字符串形式。
    """
    items: list[dict] = []
    for message in messages:
        role = message.get("role") or "user"
        content = str(message.get("content") or "")
        if role == "tool":
            items.append(
                {"role": "tool", "tool_call_id": message.get("tool_call_id") or "", "content": content}
            )
        elif role == "assistant" and message.get("tool_calls"):
            items.append(
                {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {
                            "id": call.get("id") or "",
                            "type": "function",
                            "function": {
                                "name": call.get("name", ""),
                                "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                            },
                        }
                        for call in message.get("tool_calls") or []
                    ],
                }
            )
        else:
            items.append({"role": role if role in ("user", "assistant", "system") else "user", "content": content})
    return items


def normalize_usage(raw: Any) -> Optional[dict[str, Any]]:
    """把端点返回的 ``usage`` 规整成固定三个整数字段；没有有效数据时返回 ``None``。

    端点之间字段名与空值写法不一致（有的给 ``null``、有的干脆不返回），统一在这里收口，
    上层只判断"有没有 prompt_tokens"。SDK 给的是 pydantic 对象、代理偶尔给普通对象或 dict，
    三种都要能读。
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = as_dict(raw)
        except TypeError:
            data = {key: getattr(raw, key, None) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    usage: dict[str, Any] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = data.get(key)
    usage[key] = int(value)
    return usage or None


def from_completion(payload: dict) -> ResponseMessage:
    """把 Chat Completions 响应体转换为 :class:`ResponseMessage`。"""
    choices = payload.get("choices") or []
    if not choices:
        return ResponseMessage(status="failed", error={"message": "响应中没有 choices"})
    choice = choices[0]
    message = choice.get("message") or {}
    calls = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        calls.append(
            normalize_call(
                {"id": call.get("id"), "name": function.get("name")},
                arguments=function.get("arguments"),
            )
        )
    return ResponseMessage(
        content=str(message.get("content") or ""),
        tool_calls=calls,
        finish_reason=str(choice.get("finish_reason") or "stop"),
        usage=normalize_usage(payload.get("usage")),
    )


def as_dict(obj: Any) -> dict:
    """把 OpenAI SDK 响应对象（或 dict）转成 dict。"""
    if isinstance(obj, dict):
        return obj
    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        return model_dump()
    raise TypeError(f"无法将 {type(obj).__name__} 转换为响应字典")


class OpenAIProvider(LLMProvider):
    """OpenAI 兼容端点（DeepSeek / Ollama / OpenAI 通用）。"""

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: Optional[str] = None,
        timeout: float = 120.0,
        max_retries: int = 2,
        retry_backoff: float = 5.0,
        stream: bool = True,
    ) -> None:
        from openai import OpenAI

        self.model = model
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.stream = stream
        #: 单次调用墙钟上限（秒）：流式无响应或保活占用时兜底中止，避免主流程永久阻塞
        self.timeout = timeout
        self.client = OpenAI(base_url=base_url, api_key=api_key or "not-set", timeout=timeout)

    @staticmethod
    def sdk_payload(request: RequestMessage) -> dict[str, Any]:
        """把请求体翻成 SDK 的调用参数：``extra_body`` 单独取出，作为关键字传给 SDK。"""
        payload = request.model_dump(exclude_unset=True)
        extra = payload.pop("extra_body", None)
        if extra:
            payload["extra_body"] = extra
        if payload.get("stream_options") is None:
            payload.pop("stream_options", None)  # 不需要时不要把这个键发给端点
        return payload

    def describe_image(self, image_path, question: str, max_side: int = 1400) -> str:
        """把图与问题发给模型，返回文字回答。"""
        request = RequestMessage(
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {"type": "image_url", "image_url": {"url": image_data_uri(image_path, max_side)}},
                    ],
                }
            ],
            temperature=0.2,
            extra_body=dict(NO_THINKING_BODY),
        )
        response = self.complete_once(request)
        if response.error or response.status != "completed":
            detail = (response.error or {}).get("message") or "模型返回错误"
            raise RuntimeError(f"看图失败：{detail}")
        return response.content.strip()

    def build_request(
        self,
        messages: List[dict],
        tools: Optional[List[dict]] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
    ) -> RequestMessage:
        """把消息 dict 流组装为 ``/chat/completions`` 请求体（system 保持为一条消息）。"""
        return RequestMessage(
            model=self.model,
            messages=to_chat_messages(messages),
            tools=tools or None,
            temperature=temperature,
            max_tokens=max_tokens,
            extra_body=dict(NO_THINKING_BODY),
        )

    def complete(
        self,
        messages: List[dict],
        tools: Optional[List[dict]] = None,
        temperature: float = 0.2,
        events: Optional[Callable[[str, dict[str, Any]], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        max_tokens: Optional[int] = None,
    ) -> ResponseMessage:
        request = self.build_request(messages, tools, temperature, max_tokens)
        if not self.stream:
            return self.complete_once(request)
        # 流式时顺带要用量：端点会在最后一个 chunk 里给出 usage（账单口径的 prompt_tokens）
        stream_request = request.model_copy(
            update={"stream": True, "stream_options": {"include_usage": True}}
        )
        return self.complete_stream_with_deadline(stream_request, events, should_stop)

    # ------------------------------------------------------------ 流式
    def complete_stream_with_deadline(
        self,
        request: RequestMessage,
        events: Optional[Callable[[str, dict[str, Any]], None]],
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> ResponseMessage:
        """墙钟超时兜底：流式无响应或保活占用时中止本次调用，避免永久阻塞。

        读取在 daemon 线程进行，超时后主流程立即返回失败响应；该线程不阻止进程退出，
        其占用随进程结束释放。失败原因（异常文本）一并保留。
        """
        outcome: dict[str, Any] = {}

        def worker() -> None:
            try:
                outcome["message"] = self.complete_stream(request, events, should_stop)
            except BaseException as error:  # noqa: BLE001 - 转交主线程处理
                outcome["error"] = error

        thread = threading.Thread(target=worker, name="llm-stream", daemon=True)
        thread.start()
        thread.join(timeout=self.timeout)
        if "message" in outcome:
            return outcome["message"]
        if "error" in outcome:
            return self.failed(outcome["error"])
        return self.failed(f"流式请求超过 {self.timeout:.0f}s 未返回，已中止本次调用。")

    def complete_stream(
        self,
        request: RequestMessage,
        events: Optional[Callable[[str, dict[str, Any]], None]],
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> ResponseMessage:
        """流式接收：转发文本增量、累积分片工具调用；仅端点明确拒绝流式且未收到内容时回退一次。

        ``should_stop`` 命中时跳出接收循环，把**已收到的部分**作为结果返回——上层据
        :attr:`ResponseMessage.finish_reason` 为 ``"stopped"`` 判断这是被中止的内容，
        不会当成模型的完整输出。
        """
        received_text = False
        stopped = False
        stream = None
        try:
            stream = self.client.chat.completions.create(**self.sdk_payload(request))
            content_parts: list[str] = []
            calls: dict[int, dict] = {}
            finish_reason = ""
            usage: Optional[dict[str, Any]] = None
            for chunk in stream:
                if should_stop is not None and should_stop():
                    # 用户点了停止：保留已收到的内容就地返回，剩下的不再接收
                    stopped = True
                    break
                # 用量在最后一个 chunk 里（那一块通常没有 choices，所以要先取再跳过）
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    usage = normalize_usage(chunk_usage)
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                choice = choices[0]
                delta = getattr(choice, "delta", None)
                if delta is not None:
                    text = str(getattr(delta, "content", "") or "")
                    if text:
                        received_text = True
                        content_parts.append(text)
                        if events is not None:
                            events("text_delta", {"delta": text})
                    for item in getattr(delta, "tool_calls", None) or []:
                        slot = calls.setdefault(
                            int(getattr(item, "index", 0) or 0),
                            {"id": "", "name": "", "arguments": []},
                        )
                        if getattr(item, "id", None):
                            slot["id"] = str(item.id)
                        function = getattr(item, "function", None)
                        if function is not None:
                            if getattr(function, "name", None):
                                slot["name"] = str(function.name)
                            if getattr(function, "arguments", None):
                                # 分片累积到列表，最后 join：字符串 += 是 O(n²)，长参数会明显放大
                                slot["arguments"].append(str(function.arguments))
                if getattr(choice, "finish_reason", None):
                    finish_reason = str(choice.finish_reason)
            return ResponseMessage(
                content="".join(content_parts),
                tool_calls=[
                    normalize_call(
                        slot,
                        name=slot.get("name") or "",
                        arguments="".join(slot.get("arguments") or []),
                    )
                    for _, slot in sorted(calls.items())
                ],
                # 被用户中止时明确标成 stopped：上层据此走收尾，而不是把半截正文当完整输出
                finish_reason="stopped" if stopped else (finish_reason or "stop"),
                usage=usage,
            )
        except Exception as error:  # noqa: BLE001
            if request.stream_options and self.rejects_stream_options(error, received_text):
                # 端点不认 stream_options：去掉它重试，宁可拿不到 usage 也要拿到内容
                logger.info("端点不支持 stream_options，去重后重试流式请求")
                return self.complete_stream(
                    request.model_copy(update={"stream_options": None}), events, should_stop
                )
            if self.rejects_streaming(error, received_text):
                try:
                    return self.complete_once(request.model_copy(update={"stream": False}))
                except Exception:  # noqa: BLE001
                    pass
            return self.failed(error)
        finally:
            if stream is not None:
                stream.close()

    @staticmethod
    def rejects_streaming(error: Exception, received_text: bool) -> bool:
        """端点是否明确拒绝流式：4xx、未收到任何内容，且错误文本指向流式不受支持。"""
        status = getattr(error, "status_code", None)
        if status is None or not 400 <= status < 500 or received_text:
            return False
        message = str(error).lower()
        return any(word in message for word in ("stream", "unsupported", "not supported"))

    @staticmethod
    def rejects_stream_options(error: Exception, received_text: bool) -> bool:
        """端点是否因为 ``stream_options`` 报错：4xx、未收到内容，且文本指向这个参数。

        单独判断是为了"只去掉这个键重试"，而不是整条退化成非流式（那样连增量输出都没了）。
        """
        status = getattr(error, "status_code", None)
        if status is None or not 400 <= status < 500 or received_text:
            return False
        message = str(error).lower()
        return any(word in message for word in ("stream_options", "include_usage"))

    # ------------------------------------------------------------ 非流式
    def complete_once(self, request: RequestMessage) -> ResponseMessage:
        """非流式请求：429/5xx 线性退避重试，其余错误直接编码返回。"""
        last_error = ""
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.chat.completions.create(**self.sdk_payload(request))
                return from_completion(as_dict(response))
            except Exception as error:  # noqa: BLE001
                status = getattr(error, "status_code", None)
                last_error = str(error)
                if status is not None and (status == 429 or 500 <= status <= 599) and attempt < self.max_retries:
                    time.sleep(self.retry_backoff * (attempt + 1))
                    continue
                return self.failed(error)
        return self.failed(f"重试 {self.max_retries} 次后仍失败（{last_error}）")

    @staticmethod
    def failed(reason: Any) -> ResponseMessage:
        """把失败原因（异常对象或文本）编码为失败响应，保留原始信息。"""
        message = reason if isinstance(reason, str) else f"{type(reason).__name__}: {reason}"
        return ResponseMessage(status="failed", error={"message": message})


