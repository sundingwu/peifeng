"""跨层数据契约（按依赖顺序）：节点信封 -> 计划 -> 执行 -> 评审 -> 任务。

本模块是叶子层：不 import peifeng_agent 的任何兄弟子包。
"""

from __future__ import annotations

from typing import Any, Callable, List, Literal, Optional, Protocol

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------- 节点调用

#: 事件监听器：接收 (事件名, 事件数据)。
Listener = Callable[[str, dict[str, Any]], None]

#: 固定角色（各一个）：规划者与评审者。worker是多个，由 ``workers/`` 注册表在启动时注册。
CORE_ROLES: tuple[str, ...] = ("planner", "critic")

#: 评审者角色名：计划里 worker 写它 = 该步是独立验收（由 Critic 只读核对产物）
CRITIC_ROLE = "critic"

#: 交付成果时认可的文档扩展名（避免把脚本、临时文件算成交付物）
ARTIFACT_SUFFIXES = (".docx", ".xlsx", ".pptx", ".pdf", ".md", ".txt", ".csv")


class AgentRequest(BaseModel):
    """一次节点调用的公共信封。

    ``metadata`` 在节点调度时注入（``trace_id`` / ``session_id`` / ``node``）。后面三项是
    **每轮可变的运行环境**，同样由编排层注入——agent 装配一次、长期复用，项目与会话
    切换只换这几个字段：

    - ``environment``：给人看的环境说明（项目目录 / 中间目录 / 会话目录…），进 system 段末尾
    - ``workspace``：当前项目的工作文件夹（工具的相对路径基准）
    - ``session_dir``：当前会话目录（会话级文件如 detail.json 的定位依据）

    ``stop_check`` 是取消信号：用户点「停止」后它返回 ``True``，节点据此在**当前工具调用
    结束后**收尾（不再发起新的模型或工具调用），把"已经做了什么"交代清楚再结束。
    """

    metadata: dict[str, Any] = Field(default_factory=dict)
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

    ``group`` 是**并行组号**：0 表示这一步单独跑；大于 0 时，**相邻且组号相同**的几步会
    同时交给各自的 worker，跑完由评审一次性验收整组（不再逐步评审）。
    只在"彼此不依赖、产出互不冲突"时才该标同一组。
    """

    goal: str = ""
    expected_output: str = ""
    #: 派给哪个 worker：workers 注册表里的名字（空或未知 → 编排层回退到默认 worker）
    worker: str = ""
    #: 并行组号：0 = 独立执行；>0 且相邻同号的几步并行执行、整组评审一次
    group: int = 0

    @model_validator(mode="before")
    @classmethod
    def compat(cls, data):
        if isinstance(data, dict):
            data = dict(data)
            description = data.pop("description", "")
            if not data.get("goal") and description:
                data["goal"] = description
            data.pop("tools", None)
            data.pop("action", None)
        return data


class Plan(BaseModel):
    """Planner 生成的完整计划。"""

    requirement: str = ""
    output_name: str = ""
    steps: List[PlanStep] = Field(default_factory=list)


class PlanningRequest(AgentRequest):
    """Planner 的唯一输入：本次任务需求 + 跨任务摘要 + 澄清记录。

    可用worker清单**不在这里**：它在装配时进了 planner 的 system 固定层（启动后不变）。
    """

    task: str
    prior_summary: str = ""
    clarifications: str = ""


class PlanningResult(BaseModel):
    """Planner 的统一结果，业务分支不使用异常表示。"""

    mode: Literal["clarify", "direct", "unable", "plan"]
    questions: List[dict] = Field(default_factory=list)
    answer: str = ""
    reason: str = ""
    plan: Optional[Plan] = None


# ---------------------------------------------------------------- 执行

#: 工具执行回调：给定工具名与参数，返回 (输出文本, 是否出错)。
ToolRunner = Callable[[str, dict[str, Any]], tuple[str, bool]]


class ToolCall(BaseModel):
    """一次工具调用记录（按叙述顺序：轮次 → 目的 → 调用 → 输出）。"""

    #: 工具调用所属轮次（日志按轮记录）
    round: int | None = None
    #: 本轮工具调用的目的（模型在发起调用前写的那句说明）
    goal: str = ""
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    #: 工具完整输出（供评审上下文与步骤明细落盘）
    result: str = ""
    is_error: bool = False


class ExecutionResult(BaseModel):
    """Executor 单步执行结果。"""

    status: Literal["completed", "partial", "needs_input", "needs_replan"]
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


class ExecutionRequest(AgentRequest):
    """Worker 的唯一输入。"""

    step: PlanStep
    feedback: str = ""
    previous_results: str = ""
    session_history: str = ""
    requirement: str = ""
    #: 计划本身（planner 的 requirement/steps 渲染好的文本，不含执行状态）
    plan_outline: str = ""
    #: 原始用户请求与用户决定：**不进 user 消息**（需求已由 planner 整合进 requirement），
    #: 保留字段供日志与调试查看"为什么是这个需求"
    original_request: str = ""
    user_decisions: List[str] = Field(default_factory=list)
    #: 交付文件名（来自计划的 output_name）
    output_name: str = ""


# ---------------------------------------------------------------- 评审

class ReviewResult(BaseModel):
    """Critic 的评审结论。

    - **通过**：给出 ``comment``（本步摘要：目标与完成方法）、``file_path`` 与 ``file_content``
      （产出文件的路径与内容），这些进日志，供后续步骤直接查看，不必再去读文件。
    - **不通过**：只给 ``comment``（未通过的原因），返修时它是一个步骤内的增量上下文。
    """

    approved: bool
    #: 通过时=步骤摘要（本步目标与完成方法）；不通过时=未通过的原因
    comment: str = ""
    #: 产出文件路径（通过时给；后续步骤据此定位）
    file_path: str = ""
    #: 产出文件内容（通过时给；后续步骤直接看，省一次 read）
    file_content: str = ""


class StepOutcome(BaseModel):
    """并行组里一个成员的执行结果（给评审一次看全组）。"""

    index: int
    step: PlanStep
    result: ExecutionResult


class ReviewRequest(AgentRequest):
    """Critic 的唯一输入：**只给最终需求**（已整合用户决定），不给原始请求与决定流水。

    ``plan_outline`` 是整份计划（本步标了「本步」）。评审要用它判断**越界**：本步是否做了
    后面步骤该做的活——不掌握计划就无法发现"抢跑"，实测这是调用次数失控的主因。

    **并行组**：``grouped`` 非空时，这一批评审的是**整组并行步骤**（``step``/``result`` 只是
    组内第一个，其余在 ``grouped`` 里）。整组一起判断是否达标——组内各步本来就互不依赖，
    分开评会白花往返。
    """

    task: str
    step: PlanStep
    result: ExecutionResult
    plan_outline: str = ""
    #: 本步产出文件的**正文**（由编排层读出来，最多 ARTIFACT_TEXT_CHARS 字）：
    #: 评审要看实际交付内容，不该只看worker的自述
    artifact: str = ""
    #: 并行组的完整成员（含第一个）：非空表示"整组评审一次"
    grouped: List[StepOutcome] = Field(default_factory=list)


# ---------------------------------------------------------------- 任务

class TaskResult(BaseModel):
    """一次任务的最终结果。"""

    content: str = ""
    step_results: List[ExecutionResult] = Field(default_factory=list)
    #: 任务终态：completed / rejected / aborted / failed
    status: str = "completed"


class StepAttempt(BaseModel):
    """一次步骤尝试：执行结果与（可选的）评审结论。"""

    result: ExecutionResult
    review: Optional[ReviewResult] = None


class TaskStep(BaseModel):
    """任务内一个步骤的完整记录（同一任务内部可见）。"""

    index: int
    goal: str
    expected_output: str = ""
    #: 由哪个 worker 做的（明细里保留，便于回溯"谁做了什么"）
    worker: str = ""
    #: 评审通过后压缩成的步骤摘要（做法与目的）：任务内上下文只带它，细节见产出文件与 detail.json
    summary: str = ""
    #: 评审给出的**交付物要点**（比 summary 长，含关键数据/口径/结论）：下一步据此直接接着干，
    #: 不必为了拿正文再 read 一次产出文件
    deliverable: str = ""
    #: 所属并行组号（0 = 独立步骤）。同组的几步在 detail.json 里**各自一条记录**，
    #: 但共用同一次评审结论——分开存是为了回溯时能看清"这几步是同时跑的、各自做了什么"
    group: int = 0
    #: 本步产出文件（已落盘；压缩摘要的一部分，下一步按需 read 复用）
    files: List[str] = Field(default_factory=list)
    #: 本步**完整产出**的落盘位置（会话 internal 目录下的 .md：摘要 + 交接 + 正文全文）。
    #: 任务进行中 ``detail.json`` 还没有本任务记录，下一步只能靠这个文件拿到正文。
    output_file: str = ""
    attempts: List[StepAttempt] = Field(default_factory=list)
    #: 本步骤的节点异常（如有）
    errors: List[str] = Field(default_factory=list)
    #: 步骤状态：pending（没轮到）/ running（做了一半被打断）/ completed（通过评审）/ failed（不通过且没救回来）
    status: str = "pending"


class TaskSummary(BaseModel):
    """写进 ``summary.json`` 的任务摘要（跨任务默认可见）。

    ``task_seq`` 同时是 ``detail.json`` 里该任务的键：要细节就用 ``read`` 的 ``json_path`` 去取。
    """

    task_seq: int
    #: planner 整合后的完整需求（含关键假设，给后续规划用）
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
    """写进 ``detail.json`` 的任务明细（任务内部完整上下文，按任务号存档）。"""

    interruption_log: List[dict] = Field(default_factory=list)
    task_seq: int
    requirement: str = ""
    original_request: str = ""
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

    计划不再需要用户确认：Planner 给出计划即执行，所以这里只剩"信息不足时问清楚"。
    """

    def ask_clarification(self, questions: List[dict]) -> List[str]:
        raise NotImplementedError
