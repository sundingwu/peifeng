"""流程编排与转发：在 Planner / Executor / Critic 之间转发请求与结果，直到任务收尾。

- 流程：plan -> execute -> review -> rework（TaskOrchestrator）；失败就地停止并出总结汇报
- 状态：TaskState（任务级，落 detail.json）/ SessionState（会话级，落 summary.json）
- 提示：build_* 是纯函数，把状态翻译成给模型的请求与提示
- 调度：节点在 daemon 线程内执行，主线程转发事件并轮询页面插入的补充输入
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable, List, Optional

from peifeng_agent.backend.memory.session_store import (
    INTERNAL_DIR,
    DetailStore,
    SessionData,
    SessionInfo,
    SummaryStore,
)
from peifeng_agent.backend.utils.contracts import (
    ARTIFACT_SUFFIXES,
    AgentNode,
    AgentRequest,
    Clarifier,
    ExecutionRequest,
    ExecutionResult,
    Listener,
    Plan,
    PlanningRequest,
    PlanningResult,
    PlanStep,
    ReviewRequest,
    ReviewResult,
    StepAttempt,
    StepOutcome,
    TaskDetail,
    TaskResult,
    TaskStep,
    TaskSummary,
)
from peifeng_agent.backend.utils.exceptions import CancelledError, ExecutionError, PlanningError

logger = logging.getLogger(__name__)

DEFAULT_WORKFLOW = "plan_and_execute"

#: 等待节点事件或补充输入的最长时间（秒）：事件到达即刻转发，无事件时按此节奏检查输入
POLL_INTERVAL = 0.05

#: Planner 第二次仍要求澄清时追加的强制约束（只追加一次）
FORCE_PLAN_HINT = (
    "（Planner 不得再次提问：已澄清一轮。按最合理默认直接输出 plan 或 direct，并把关键假设写入 requirement）"
)

# ---------------------------------------------------------------- 上下文压缩
#: 没有步骤记录时，任务结果的兼容摘要长度。
TASK_RESULT_CHARS = 160
#: 步骤压缩摘要（"做法与目的"）的字数上限：下一步与评审都靠它衔接
STEP_SUMMARY_CHARS = 240
#: 一次任务里最多重排几次计划：worker 说"做不到"就重排剩余步骤，但不能无限重排
REPLAN_LIMIT = 2

#: 从 expected_output 里认出产出文件名（用来判断同组步骤是否会写同一个文件）
_OUTPUT_FILE_RE = re.compile(
    r"[\w\-.（）()]{1,60}\.(?:md|txt|csv|json|png|jpe?g|svg|docx|xlsx|pptx|pdf)",
    re.IGNORECASE,
)


def unshare_group_outputs(plan: Plan) -> List[str]:
    """同组并行的两步如果指向同一个产出文件，把后面那步拆出并行组。

    并行只在"产出互不冲突"时成立：两个 worker 同时写同一个文件，最后谁赢是碰运气
    （实测有一次 png 被两边各画一遍、md 被删了重写）。返回改动说明，便于写进日志。
    """
    notes: List[str] = []
    claimed: dict[str, int] = {}
    for index, step in enumerate(plan.steps):
        if not step.group:
            continue
        names = {item.group(0).lower() for item in _OUTPUT_FILE_RE.finditer(step.expected_output or "")}
        clash = names & set(claimed)
        if clash:
            first = claimed[sorted(clash)[0]]
            notes.append(
                f"步骤{index + 1} 与步骤{first + 1} 都要产出 {sorted(clash)[0]}：并行会互相覆盖，已改为串行"
            )
            step.group = 0
            continue
        for name in names:
            claimed[name] = index
    return notes
#: 交付物**内容的**上限：下一步与评审直接看产出文件的正文，而不是worker的转述。
#: 超过这个长度就截断并注明文件路径（完整内容仍可用 read 读）。
ARTIFACT_TEXT_CHARS = 6000
#: 交付物解析的体积上限：超过就不解析（几十万行的 xlsx 解析会卡住一步）
ARTIFACT_MAX_BYTES = 5 * 1024 * 1024


def lead(text: str, limit: int) -> str:
    """取首段并截到 ``limit`` 字（压缩用）：尽量在句末断开，超出部分标注省略。

    只取首段是为了让压缩结果**始终是一行**（提示里一步一行才好读）；断句优先落在句号、
    分号这类边界上，避免把"用什么方法达成了什么目的"切成半句，下游读不出意思。
    """
    first = (text or "").strip().split("\n", 1)[0].strip()
    if len(first) <= limit:
        return first
    clipped = first[:limit]
    for mark in ("。", "；", "！", "？", ";", ".", "!", "?"):
        index = clipped.rfind(mark)
        if index >= limit // 2:
            return clipped[: index + 1].rstrip()
    return clipped.rstrip() + "…"


# ---------------------------------------------------------------- 步骤产出落盘
#: 步骤完整产出统一写成 Markdown（会话 internal 目录下，属中间产物，不随交付复制到用户目录）
STEP_OUTPUT_SUFFIX = ".md"


def step_output_path(session_dir, task_seq: int, index: int, worker: str) -> Path:
    """本步完整产出的落盘路径：``<会话目录>/internal/task<任务号>_step<步号>_<worker>.md``。

    同一任务同一步骤只有一个路径（重跑覆盖成最新一版），所以下游拿到的路径始终有效。
    """
    owner = re.sub(r"[^\w.-]+", "_", worker or "").strip("._") or "step"
    name = f"task{task_seq}_step{index + 1}_{owner}{STEP_OUTPUT_SUFFIX}"
    return Path(session_dir) / INTERNAL_DIR / name


def render_step_output(task_seq: int, index: int, step: PlanStep, result: ExecutionResult) -> str:
    """把一步的产出渲染成可独立阅读的 Markdown：摘要、交接、局限、来源、正文全文。

    正文（``content``）在任务内上下文里会被压缩掉，落盘这一份就是下游唯一能读到的原文。
    """
    lines = [
        f"# 任务 {task_seq} · 步骤 {index + 1}：{step.goal or '（未填目标）'}",
        "",
        f"- 状态：{result.status}",
        f"- worker：{step.worker or '（未指定）'}",
    ]
    if step.expected_output:
        lines.append(f"- 预期产出：{step.expected_output}")
    if result.files:
        lines.append("- 产出文件：" + "、".join(result.files))
    lines.append("")
    if result.summary:
        lines += ["## 结果摘要", "", result.summary, ""]
    if result.handoff:
        lines += ["## 交接说明", "", result.handoff, ""]
    if result.limitations:
        lines += ["## 局限与未证实事项", ""] + [f"- {item}" for item in result.limitations] + [""]
    if result.sources:
        lines += ["## 来源", ""] + [f"{number}. {url}" for number, url in enumerate(result.sources, 1)] + [""]
    if result.content:
        lines += ["## 正文", "", result.content, ""]
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------- 任务作用域


@dataclass
class TaskState:
    """一次任务的运行态（按任务号存档在 ``detail.json``）。"""

    task_seq: int = 0
    original_request: str = ""
    requirement: str = ""
    workflow: str = ""
    plan: Optional[Plan] = None
    steps: List[TaskStep] = field(default_factory=list)
    step_results: List[ExecutionResult] = field(default_factory=list)
    user_decisions: List[str] = field(default_factory=list)
    abort_reason: Optional[str] = None
    output_name: str = ""
    status: str = "completed"
    content: str = ""
    files: List[str] = field(default_factory=list)

    @classmethod
    def start(cls, task_seq: int, task: str, workflow: str) -> "TaskState":
        """新建一次任务的运行态。

        用"每任务新建实例"而不是逐字段清空：新增字段时不会再有人忘了在重置里补一行。
        """
        return cls(
            task_seq=task_seq,
            original_request=task,
            requirement=task,
            workflow=workflow,
        )

    def set_plan(self, plan: Plan) -> None:
        """记录当前计划，并为每一步建立记录槽位。"""
        self.plan = plan
        self.steps = [
            TaskStep(
                index=index,
                goal=step.goal,
                expected_output=step.expected_output,
                worker=step.worker,
            )
            for index, step in enumerate(plan.steps)
        ]

    def apply_file_agreement(self, plan: Plan) -> None:
        """套用计划里的文件交付约定。"""
        self.output_name = str(plan.output_name or "").strip()

    def step_of(self, index: int) -> TaskStep:
        """取指定步骤的记录（必要时补齐空槽位）。"""
        while len(self.steps) <= index:
            self.steps.append(TaskStep(index=len(self.steps), goal=""))
        return self.steps[index]

    def record_attempt(self, index: int, result: ExecutionResult, review: Optional[ReviewResult] = None) -> None:
        """记录一次步骤尝试；评审没通过时把该步标成 ``failed``（通过了之后会被 accept_step 覆盖）。"""
        step = self.step_of(index)
        step.attempts.append(StepAttempt(result=result, review=review))
        if review is not None and not review.approved:
            step.status = "failed"

    def record_error(self, index: int, message: str) -> None:
        """记录一次步骤级节点异常。"""
        step = self.step_of(index)
        step.errors.append(message)
        step.status = "failed"

    def mark_step(self, index: int, status: str) -> None:
        """直接改写某步状态（中止时把当前步标成 ``running``，说明它做到一半被打断）。"""
        self.step_of(index).status = status

    def accept_step(
        self,
        index: int,
        result: ExecutionResult,
        output_file: str = "",
        review: Optional[ReviewResult] = None,
        artifact: str = "",
    ) -> None:
        """记录一个已通过的步骤，并把该步**压缩成摘要**（任务内上下文之后只带这一条）。

        摘要的职责是"让下游能接着干"：写清**用什么方法达成了什么目的**（取自
        ``result.summary``，缺失时用正文首段），产出文件另存于 ``step.files``——下一步
        据此 ``read`` 复用，不必重跑这一步。压缩只作用于"注入模型上下文的那一份"：完整
        正文落在 ``step.output_file``（由编排器落盘后传入），每次尝试与工具输出仍在
        ``detail.json`` 里。

        ``step.deliverable`` 优先取**评审通过时回传的产出内容**（``review.file_content``，
        它已经核对过），评审没给时才用编排层直接从文件读出来的 ``artifact``。
        两种来源都是"正文"，下一步据此直接接着干，不必为了拿正文再读一次文件。
        """
        self.step_results.append(result)
        step = self.step_of(index)
        detail = ((review.file_content if review is not None else "") or "").strip() or (artifact or "").strip()
        if detail:
            step.deliverable = detail
        step.summary = lead(result.summary, STEP_SUMMARY_CHARS) or lead(result.content, STEP_SUMMARY_CHARS)
        step.files = list(result.files or [])
        # 评审通过时回传的路径也计入产出（它核对过的那份）
        reviewed_path = str(getattr(review, "file_path", "") or "").strip() if review is not None else ""
        if reviewed_path and reviewed_path not in step.files:
            step.files.append(reviewed_path)
        step.output_file = output_file
        # 组号写进记录：detail.json 里同组几步各自一条，靠这个字段认出"它们是同时跑的"
        step.group = int(getattr(self.plan.steps[index], "group", 0) or 0) if self.plan and index < len(self.plan.steps) else 0
        step.status = "completed"

    def to_detail(self) -> TaskDetail:
        """转为落盘的明细记录。"""
        return TaskDetail(
            task_seq=self.task_seq,
            requirement=self.requirement,
            original_request=self.original_request,
            plan=self.plan,
            steps=list(self.steps),
            user_decisions=list(self.user_decisions),
            status=self.status,
            content=self.content,
            files=list(self.files),
            abort_reason=self.abort_reason or "",
        )


# ---------------------------------------------------------------- 任务内提示构建
def build_review_request(
    state: TaskState,
    step: PlanStep,
    result: ExecutionResult,
    index: int = -1,
    artifact: str = "",
) -> ReviewRequest:
    """单步评审请求：最终需求（已整合用户决定）+ 本步 + 执行证据 + **整份计划** + **产出正文**。

    计划是评审判断"越界"的唯一依据：不告诉它后面几步要做什么，它就没法发现本步抢跑了
    后面步骤的活。``artifact`` 是产出文件的正文，评审据此核对**实际交付内容**，
    而不是只看worker的自述。
    """
    return ReviewRequest(
        task=state.requirement,
        step=step,
        result=result,
        # 只给到本步：给全计划会诱导 worker 把后面几步的活一起做掉（实测有一次三步的活
        # 被第一步全干完，后两步只能重复劳动）
        plan_outline=build_plan_outline(state, index, upto=index),
        artifact=artifact,
    )


def build_plan_request(
    state: TaskState,
    *,
    prior_summary: str = "",
    clarifications: str = "",
) -> PlanningRequest:
    """规划请求（针对当前任务；prior_summary 为跨任务摘要）。

    可用worker清单不在这里传：它随装配进了 planner 的 system 固定层。
    """
    # 判据放在**任务正文末尾**而不是 system prompt：实测同样的要求写在 system 里，
    # 模型仍会把「写一份 md」判成 direct（2.7 秒、零调用）；贴着任务说才起作用。
    task = state.requirement or state.original_request
    if "【输出形式】" not in task:
        task = (
            f"{task}\n\n【输出形式】这个任务是否要求产出文件？"
            "提到文件名或后缀（md/docx/xlsx/pptx/png/pdf）、或提到落盘/保存/导出/生成一份，"
            "就**必须返回 plan**，并让每一步的 expected_output 写明产出文件名——"
            "把内容写在回答里用户拿不到东西。确实只是一段解释或建议时才返回 direct。"
        )
    return PlanningRequest(
        task=task,
        prior_summary=prior_summary,
        clarifications=clarifications,
    )


def build_plan_outline(state: TaskState, current: int = -1, upto: int = -1) -> str:
    """计划本身：每步交给谁、做什么、验收口径——**不带执行状态**。

    计划是 planner 给的原始编排（与 ``detail.json`` 里的 ``plan`` 同形）。``current`` 只用来
    在行尾打个 ``（本步）`` 指针，说明"现在做这一步"；已完成/未做不在这里标注——已完成的
    进度由「已完成步骤」那段摘要表达。
    """
    if state.plan is None or not state.plan.steps:
        return ""
    total = len(state.plan.steps)
    # upto >= 0 时只列到这一步（含）：worker 看不到后续步骤，就不会"顺手把后面的活也做了"
    end = min(upto + 1, total) if upto >= 0 else total
    headline = f"计划（共 {total} 步）" if end == total else f"计划（共 {total} 步，这里列到第 {end} 步）"
    lines = [headline]
    for index, step in enumerate(state.plan.steps[:end]):
        owner = f"（{step.worker}）" if step.worker else ""
        line = f"{index + 1}. {step.goal}{owner}"
        details: list[str] = []
        if step.expected_output:
            details.append(f"预期：{step.expected_output}")
        if details:
            line += " —— " + "；".join(details)
        if index == current:
            line += "（本步）"
        lines.append(line)
    if state.output_name:
        # 交付文件名是任务事实（planner 定的）；"长结果落盘、短结果进 content"是规则，在 system 里
        lines.append(f"交付文件：{state.output_name}")
    return "\n".join(lines)


def build_step_request(
    state: TaskState,
    step: PlanStep,
    *,
    index: int,
    feedback: str,
    session_history: str,
) -> ExecutionRequest:
    """单步执行请求：任务需求 + 计划 + 已完成步骤摘要 + 本步内容（技能由worker自己读）。"""
    return ExecutionRequest(
        step=step,
        # 用户决定只走 user_decisions：放进 feedback 会被 executor 提示词重复注入一次
        feedback=feedback,
        previous_results=build_completed_prompt(state),
        session_history=session_history,
        requirement=state.requirement,
        plan_outline=build_plan_outline(state, index),
        original_request=state.original_request,
        user_decisions=list(state.user_decisions),
        output_name=state.output_name,
    )


def artifact_text(files, workspace, limit: int = ARTIFACT_TEXT_CHARS) -> str:
    """把本步产出的文档读成正文——下一步与评审都看**实际内容**，不是worker的转述。

    只读文本类（``.md/.txt/.csv/.json`` 与 ``.docx/.xlsx/.pptx/.pdf``，后者由对应库解析），
    图片等二进制跳过。超过 ``limit`` 字截断并注明文件路径（全文仍可用 ``read`` 拿）。
    """
    from peifeng_agent.backend.utils.text import content_lines, is_readable

    root = Path(workspace) if workspace else Path(".")
    blocks: list[str] = []
    for name in files or []:
        path = Path(str(name))
        if not path.is_absolute():
            path = root / path
        try:
            if not path.is_file() or not is_readable(path):
                continue
            if path.stat().st_size > ARTIFACT_MAX_BYTES:
                blocks.append(f"【{path.name}】文件过大（{path.stat().st_size // 1024 // 1024} MB），未解析；需要时用 read 分段看")
                continue
            text = "\n".join(content_lines(path)).strip()
        except Exception:  # noqa: BLE001 - 单个文件解析失败不该拖垮整步
            continue
        if not text:
            continue
        if len(text) > limit:
            text = text[:limit].rstrip() + f"\n…（超出 {limit} 字已截断，完整内容见 {path.name}）"
        blocks.append(f"【{path.name}】\n{text}")
    return "\n\n".join(blocks)


def build_completed_prompt(state: TaskState) -> str:
    """已完成步骤的**压缩摘要**：只有事实——目标、worker、结果、产出文件、完整产出路径。

    每一步在评审通过时就被压成这一条（见 :meth:`TaskState.accept_step`）：完整正文、每次
    尝试与工具输出留在该步的 ``output_file`` 与 ``detail.json``。**这里不写“该怎么读、不要重跑”这类说明**——那是
    系统提示里「通用规则」的职责（system 说规则、user 说事实）。
    """
    lines: list[str] = []
    for step in state.steps:
        if not step.summary and not step.deliverable:
            continue
        owner = f"（{step.worker}）" if step.worker else ""
        lines.append(f"- 步骤 {step.index + 1}：{step.goal}{owner}")
        # 评审给的交付物要点优先：它含关键数据与结论，够下一步直接接着干
        lines.append(f"  结果：{step.deliverable or step.summary}")
        if step.files:
            lines.append(f"  产出：{'、'.join(step.files)}")
        if step.output_file:
            lines.append(f"  完整产出：{step.output_file}")
    return "\n".join(lines)


def build_rework_prompt(review: ReviewResult, result: ExecutionResult, failures: List[ReviewResult]) -> str:
    """返修提示：本轮意见 + 此前意见历史 + 执行证据**摘要**（细节让模型自己读）。"""
    lines = [f"未通过评审：{review.comment or '（评审未说明原因）'}"]
    if len(failures) > 1:
        history = "\n".join(
            f"{index + 1}. {item.comment}" for index, item in enumerate(failures[:-1])
        )
        lines.append(f"此前评审意见：\n{history}")
    append_evidence(lines, result, "交付正文")
    return "\n".join(lines)


def describe_step_failure(index: int, step: PlanStep, result: Optional[ExecutionResult], failures: List[ReviewResult]) -> str:
    """一句话说明"为什么停在这一步"（细节交给 :func:`build_failure_report`）。

    不再重规划：遇到问题就地停止，把这条问题描述连同进度一起记进会话记录，
    用户在下一轮请求里能看到上次是怎么失败的。
    """
    label = f"步骤 {index + 1}（{step.goal or '目标未填'}）"
    if result is not None and result.status == "needs_replan":
        said = lead(result.summary or "", STEP_SUMMARY_CHARS) or "（worker未说明原因）"
        return f"{label}无法继续：{said}"
    if failures:
        last = failures[-1]
        reason = lead(last.comment or "", STEP_SUMMARY_CHARS) or "（评审未给出意见）"
        return f"{label}连续 {len(failures)} 次未通过评审：{reason}"
    return f"{label}未能完成"


def build_task_result(state: TaskState, content: str, status: str) -> TaskResult:
    """任务的终态结果。"""
    return TaskResult(content=content, step_results=list(state.step_results), status=status)


def build_failure_report(state: TaskState, reason: str) -> str:
    """任务没做成时的总结汇报：进度、卡在哪、产出什么、下一步建议。

    写进 ``content``／``detail.json``／``summary.json`` 三处，所以它同时是"给用户看的交代"
    和"给下一次规划的上下文"——用户重新提要求时，Planner 从会话摘要里就能读到这份报告，
    不用再从零猜"上次到底做到哪、为什么没成"。
    """
    total = len(state.steps)
    done = [step for step in state.steps if step.status == "completed"]
    lines = ["【任务未完成总结】", f"原因：{reason}"]
    lines.append(f"进度：已完成 {len(done)}/{total} 步" if total else "进度：尚未形成计划")
    if done:
        lines.append("已完成并通过评审的步骤：")
        for step in done:
            produced = f"（产出：{'、'.join(step.files)}）" if step.files else ""
            lines.append(f"- 步骤 {step.index + 1}：{step.goal} → {step.summary or '（无摘要）'}{produced}")
            if step.output_file:
                lines.append(f"  完整产出：{step.output_file}")
    blocked = next((step for step in state.steps if step.status != "completed"), None)
    if blocked is not None:
        lines.append(f"停在这一步：步骤 {blocked.index + 1}（{blocked.goal or '目标未填'}）")
        if blocked.expected_output:
            lines.append(f"它的预期输出：{blocked.expected_output}")
        last = blocked.attempts[-1] if blocked.attempts else None
        if last is not None:
            said = lead(last.result.summary or last.result.content, STEP_SUMMARY_CHARS)
            if said:
                lines.append(f"最后一次尝试：{said}")
            if last.result.limitations:
                lines.append("它自己说明的限制：\n" + "\n".join(f"- {item}" for item in last.result.limitations))
            if last.review is not None and not last.review.approved:
                lines.append(f"最近一次评审意见：{last.review.comment}")
        for message in blocked.errors:
            lines.append(f"节点异常：{message}")
    files = list(state.files or [])
    if files:
        lines.append("已产出的文件（可能未通过评审，可直接取用）：\n" + "\n".join(f"- {item}" for item in files))
    lines.append(
        "建议：说明要改哪一步、补什么材料，或把范围缩小后重新提交；"
        "已完成的步骤结果都在会话记录里，可以直接沿用，不必重做。"
    )
    return "\n".join(lines)


def build_tool_summary(result: ExecutionResult) -> str:
    """工具调用一览：一行一次（工具名 + 目标 + 成败），出错时带简短的错误信息。

    不列参数、不列完整输出：那些留在 ``detail.json`` 里，需要细节时自己读。
    """
    if not result.tool_calls:
        return "（未调用工具）"
    lines: list[str] = []
    for call in result.tool_calls:
        goal = f"（目标：{call.goal}）" if call.goal else ""
        status = "成功" if not call.is_error else "出错"
        detail = "\n参数：" + json.dumps(call.arguments, ensure_ascii=False) + "\n完整返回：" + call.result
        lines.append(f"- {call.name}{goal} {status}{detail}")
    return "\n".join(lines)


def append_evidence(lines: list[str], result: ExecutionResult, content_label: str) -> None:
    """把执行证据压成**摘要**追加进提示：做法与目的 + 正文首段 + 产出位置 + 限制 + 工具一览。

    完整正文与每一次工具调用的参数/输出都不进上下文——它们在产出文件与 ``detail.json``
    里，模型需要时自己读。所以无论返修多少轮，提示都不会随执行证据膨胀；而"产出在哪、
    怎么读"始终保留，返修不必重做已经做对的部分。
    """
    lines.append(f"做法与目的：{result.summary or '（无）'}")
    if result.content:
        lines.append(f"{content_label}（摘要）：{result.content}")
    if result.files:
        lines.append(f"产出（已落盘，需完整内容时 read 读取）：{'、'.join(result.files)}")
    if result.limitations:
        lines.append(f"限制：{'；'.join(result.limitations)}")
    lines.append(f"工具调用一览：\n{build_tool_summary(result)}")
    if result.content or result.tool_calls:
        lines.append(
            "（需要完整正文时按上面的产出路径 read（没有落盘产出则以本次摘要为准）；"
            "工具调用输出与每次尝试只在 detail.json 里，任务结束后可用 read 的 json_path 取。"
            "修的时候按根因改——不要只调整措辞、绕开评审指出的那一项，或把同一件事重做一遍）"
        )


# ---------------------------------------------------------------- 会话作用域


@dataclass
class SessionState:
    """一次会话的运行态（对应 ``summary.json``）：本会话已完成的各任务摘要。"""

    info: Optional[SessionInfo] = None
    tasks: List[TaskSummary] = field(default_factory=list)

    @property
    def name(self) -> str:
        """会话名（用于定位 summary.json 与 detail.json）。"""
        if self.info is None:
            return ""
        return self.info.file_name or self.info.title

    def reset(self, info: SessionInfo, tasks: Optional[List[TaskSummary]] = None) -> None:
        """绑定会话并载入既有任务摘要。"""
        self.info = info
        self.tasks = list(tasks or [])

    def add_task(self, summary: TaskSummary) -> None:
        """追加一条任务摘要。"""
        self.tasks.append(summary)

    def next_task_seq(self) -> int:
        """下一个任务号（1 起）。"""
        return self.tasks[-1].task_seq + 1 if self.tasks else 1


# ---------------------------------------------------------------- 跨任务摘要构建
def operation_messages(messages: list[dict]) -> list[dict]:
    """Ignore legacy request envelopes that already contain session history."""
    result = list(messages)
    while result and result[0].get("role") == "system":
        result.pop(0)
    if result and result[0].get("role") == "user":
        result.pop(0)
    return result


def task_overview(status: str, steps: list[TaskStep], content: str, reason: str = "") -> str:
    """Build a task-level summary locally from step summaries and terminal facts."""
    parts = [f"任务状态：{status}"]
    for step in steps:
        if step.status == "completed":
            parts.append(f"步骤{step.index + 1}已完成：{step.summary or step.goal}")
        elif status != "completed":
            parts.append(f"步骤{step.index + 1}未完成：{step.goal}（{step.status}）")
    if reason:
        parts.append("中断原因：" + reason)
    if not steps:
        parts.append("结果：" + lead(content, TASK_RESULT_CHARS))
    return "\n".join(parts)


def build_session_summary(state: SessionState, *, with_paths: bool = True) -> str:
    """把跨任务摘要拼成一段文本。

    ``with_paths=False`` 给 Planner 用：**不给产物正文的路径**。Planner 只需要知道
    "上次做了什么、产出了什么"，给它路径它会真的去读——实测有一次规划为此花了 15 轮
    读上一个任务的 internal 记录，而那个任务与本次毫无关系。
    """
    if not state.tasks:
        return ""
    lines = ["【本会话任务历史】"]
    latest = state.tasks[-1]
    for summary in state.tasks:
        lines.append(f"- 任务 {summary.task_seq}：{summary.requirement}")
        overview = summary.task_summary or task_overview(
            summary.status, summary.steps, summary.content, summary.abort_reason
        )
        lines.append("  任务摘要：" + overview)
        if summary.files:
            lines.append("  文件：" + "、".join(summary.files))
        # 已完成步骤的正文在哪：有落盘路径就给路径，没有就给该步的明细文件。
        # 没有这行，下游只知道"这步做过"，却不知道正文怎么取——就是"重复劳动或凭空编"的起点。
        readable = [
            f"步骤{step.index + 1}（{step.status}，{step.worker or '未指定'}）→ "
            + (
                step.output_file
                if step.output_file
                else f"detail/task{summary.task_seq}_step{step.index + 1:02d}.json"
            )
            for step in summary.steps
            if step.output_file or step.status == "completed"
        ]
        if with_paths and readable and not (summary is latest and summary.status in {"aborted", "failed"}):
            lines.append("  可读产出：" + "；".join(readable))
        if summary is latest and summary.status in {"aborted", "failed"}:
            if summary.plan:
                lines.append("  中断任务计划：" + summary.plan.model_dump_json())
            for step in summary.steps:
                output = f"（完整产出：{step.output_file}）" if step.output_file else ""
                lines.append(f"  步骤{step.index + 1}（{step.status}）：{step.summary or step.goal}{output}")
            entries = summary.interruption_log
            if entries and "role" in entries[0]:
                entries = operation_messages(entries)
            else:
                entries = [{**entry, "messages": operation_messages(entry.get("messages", []))} for entry in entries]
            if entries:
                lines.append("  中断步骤完整操作记录：" + json.dumps(entries, ensure_ascii=False))
        lines.append(
            f'  明细：read(dir="<会话目录>/detail", grep="task{summary.task_seq}_step")'
            "（每步一个文件，直接全量读即可）"
        )
    return "\n".join(lines)


def build_task_summary(state: TaskState) -> TaskSummary:
    """任务结束时生成摘要（明细按任务号存档，任务号就是它的键）。"""
    return TaskSummary(
        task_seq=state.task_seq,
        requirement=state.requirement,
        original_request=state.original_request,
        task_summary=task_overview(state.status, state.steps, state.content, state.abort_reason or ""),
        plan=state.plan,
        # 摘要里**只留步骤摘要**：attempts 里的工具调用与输出在 detail/taskN_stepMM.json，
        # 否则 summary.json 会跟着任务越写越大（实测一个单步任务就 46 KB）
        steps=[step.model_copy(update={"attempts": []}) for step in state.steps],
        status=state.status,
        content=state.content,
        files=list(state.files),
        # 中止/失败原因也要进摘要：下次接手时"为什么停下"比"停在哪"更需要先知道
        abort_reason=state.abort_reason or "",
    )


class TaskOrchestrator:
    """任务流程编排：公开入口只有 :meth:`run_task`，其余为内部协作步骤。

    流程（规划 → 逐步骤执行/评审/返修 → 收尾）、单步的返修循环（:meth:`run_step`）
    与节点调度（:meth:`dispatch_node`）都在本类内。

    **没有重规划**：任何一步做不到就直接停下并出一份总结汇报（见 :meth:`fail_task`），
    问题描述连同进度写进会话记录——用户下一轮请求里能看到上次为什么失败。
    """

    def __init__(
        self,
        planner,
        workers: dict,
        critic,
        clarifier: Clarifier,
        step_max_retries: int = 3,
        listener: Optional[Listener] = None,
        summary_store: Optional[SummaryStore] = None,
        detail_store: Optional[DetailStore] = None,
        cancelled: Optional[Callable[[], bool]] = None,
        artifact_dir: Optional[Path] = None,
        worker_specs: Optional[list] = None,
        default_executor: str = "",
    ) -> None:
        #: worker：名字 → 节点（启动时按注册表构造；计划里每步用 worker 指定由谁做）
        self.workers: dict[str, AgentNode] = {name: node for name, node in workers.items() if name}
        if not self.workers:
            raise ValueError("至少要注册一个worker")
        clash = {"planner", "critic"} & set(self.workers)
        if clash:
            raise ValueError(f"worker名与固定角色冲突：{'、'.join(sorted(clash))}")
        #: 默认worker：计划里 worker 为空或未知时回退到它
        self.default_executor = default_executor or next(iter(self.workers))
        #: worker定义（名字 + 职责），用于给 Planner 分派依据
        self.worker_specs: list = list(worker_specs or [])
        #: 工作流节点（规划者 + worker + 评审者）；节点事件经订阅进入本类的事件队列，由主线程转发
        self.nodes: dict[str, AgentNode] = {"planner": planner, **self.workers, "critic": critic}
        #: 澄清提问的交互协议（信息不足时问用户；计划不再需要确认）
        self.clarifier = clarifier
        self.step_max_retries = max(1, step_max_retries)
        #: 工作流事件监听器（:meth:`emit` 使用）；Agent 事件另走 `agent_listener`（前缀不同）
        self.listener = listener
        #: Agent 事件监听器（节点事件与阶段边界），由应用层挂载
        self.agent_listener: Optional[Listener] = None
        #: 执行期间页面插入的补充要求：仅在节点返回边界并入任务
        self.pending: Queue = Queue()
        #: 补充输入取用回调（无节点事件时按 :data:`POLL_INTERVAL` 节奏轮询）
        self.poll_input: Optional[Callable[[], Optional[str]]] = None
        #: 节点事件队列（节点在 daemon 线程产出，主线程消费）
        self.events: Queue = Queue()
        #: 线程局部状态：并行组会同时跑多个节点，实例属性会被互相踩
        self._local = threading.local()
        #: 注入每次节点调用的元信息（trace_id / session_id）
        self.metadata: dict = {}
        #: 每轮注入的运行环境（environment / workspace / session_dir）：由应用层随项目与会话更新
        self.runtime: dict = {"environment": "", "workspace": "", "session_dir": ""}
        for node in self.nodes.values():
            clear = getattr(node, "clear_listeners", None)
            if callable(clear):
                clear()  # agent 是复用实例：重建编排器时先摘掉旧订阅，避免事件重复/泄漏
            subscribe = getattr(node, "subscribe", None)
            if callable(subscribe):
                subscribe(lambda kind, data: self.events.put((kind, data)))
        self.summary_store = summary_store
        self.detail_store = detail_store
        #: 用户中止检查：每个步骤边界调用一次
        self.cancelled = cancelled
        #: 产出目录（项目工作文件夹）：收尾时兜底登记本任务新产出的文档
        self.artifact_dir = Path(artifact_dir) if artifact_dir else None
        #: 本任务开始时间（兜底扫描用）
        self.started_at = 0.0
        self.session = SessionState()
        self.task = TaskState()
        #: 本任务成果文件列表的缓存（收尾会多次汇总，兜底扫描要遍历工作目录）
        self._files_cache: Optional[List[str]] = None
        #: 本任务是否已经收尾记账（保证 finalize_task 幂等，见其 docstring）
        self._finalized = False
        #: 快捷模板给的现成计划：有它就跳过 request_plan，直接激活执行
        self._preset_plan: Optional[Plan] = None

    # ---------------------------------------------------------------- 对外入口
    def bind_session(self, session: SessionData) -> None:
        """绑定会话并载入其已有任务摘要。"""
        self.session.reset(session.meta, session.tasks)
    def run_task(
        self,
        task: str,
        workflow: str = DEFAULT_WORKFLOW,
        plan: Optional[Plan] = None,
    ) -> TaskResult:
        """执行一次任务：规划 → 执行（逐步评审/返修）→ 收尾，返回终态结果。

        计划不再需要用户确认：Planner 给出计划即执行，进度通过 ``plan_ready`` / ``step_start``
        / ``step_review`` 事件推给页面。

        ``plan`` 给定时**跳过 LLM 规划**，直接执行这份计划（快捷模板走这条路）：
        常见任务的步骤是固定的，再花一轮规划调用只是浪费钱和不确定性。
        """
        if workflow not in self._WORKFLOWS:
            raise PlanningError(f"未知工作流 {workflow}（可用：{'、'.join(self._WORKFLOWS)}）")
        if self.summary_store is not None and self.session.name:
            saved = self.summary_store.load(self.session.name)
            if saved is None:
                raise PlanningError("无法读取会话日志，拒绝在缺失历史的情况下启动任务")
            self.session.reset(saved.meta, saved.tasks)
        self.drain_input()  # 上一任务输入已经在收尾时写入日志
        self.task = TaskState.start(
            self.session.next_task_seq(),
            task,
            workflow,
        )
        self._files_cache = None
        self._finalized = False
        self._preset_plan = plan
        self.interruption_log = []
        self.replan_used = 0
        self.started_at = time.time()
        self.metadata = {"trace_id": uuid.uuid4().hex, "session_id": self.session.name}
        return self._WORKFLOWS[workflow](self)

    # ---------------------------------------------------------------- 中止
    def check_cancel(self) -> None:
        """在"没有中途成果可交代"的边界上响应中止（任务开始、规划/澄清等待里）。

        执行阶段的取消不走这里：节点在工具边界自己收尾（见 :meth:`run_node`）。
        """
        if self.stopping():
            raise CancelledError("用户中止了任务")

    def stopping(self) -> bool:
        """用户是否已要求中止（容忍取消检查本身出错）。"""
        try:
            return bool(self.cancelled() if self.cancelled is not None else False)
        except Exception:  # noqa: BLE001 - 取消检查出错不该影响任务推进
            return False

    # ---------------------------------------------------------------- 事件
    def emit(self, kind: str, **data) -> None:
        if self.listener:
            try:
                self.listener(kind, data)
            except Exception:  # noqa: BLE001
                logger.debug("工作流事件 %s 处理失败", kind, exc_info=True)

    # ---------------------------------------------------------------- 节点调度
    def submit_input(self, text: str) -> None:
        """登记一条用户补充要求，在节点返回边界交给流程并入任务。"""
        if text.strip():
            self.pending.put(text.strip())

    def dispatch_node(self, node: str, request: AgentRequest) -> tuple[Any, list[str]]:
        """调用一个节点，返回 ``(结果, 期间收到的用户补充要求)``。

        每轮都把**运行环境**（环境说明 / 工作目录 / 会话目录）注入请求：agent 装配一次后
        长期复用，靠这几个字段跟随当前项目——切换项目不需要重新装配。
        """
        if node not in self.nodes:
            raise ValueError(f"未知工作流节点：{node}")
        request = request.model_copy(
            update={
                "metadata": {**self.metadata, **request.metadata, "node": node},
                # 取消信号随请求下发：节点据此在**当前工具调用结束后**收尾并交代做了什么
                "stop_check": self.cancelled,
                **self.runtime,
            }
        )
        result = self.run_node(node, request)
        updates = self.drain_input()
        return result, updates

    def run_node(self, node: str, request: AgentRequest) -> Any:
        """在 daemon 线程执行节点，主线程转发事件、轮询补充输入，异常转交主线程重新抛出。

        用户中止时的语义是「**做完手上这一件就停**」：取消信号已经随请求下发，节点会在
        工具边界收尾并交代做了什么，所以这里继续等它返回（而不是丢下它跑），只用一个
        宽限兜底——真卡住了才强行结束，宁可丢掉这次的交代也不能让页面一直转圈。
        """
        holder: dict = {}
        #: 工具在飞计数放**线程局部**：并行组会让多个 run_node 同时跑，实例属性会互相踩
        self._local.active = 0
        node_stopped = threading.Event()
        original_stop = request.stop_check
        request = request.model_copy(update={
            "stop_check": lambda: node_stopped.is_set() or bool(original_stop and original_stop())
        })
        stop_requested_at: Optional[float] = None

        def worker() -> None:
            try:
                holder["result"] = self.nodes[node].execute(request)
            except BaseException as error:  # noqa: BLE001 - 转交主线程重新抛出
                holder["error"] = error

        thread = threading.Thread(target=worker, name=f"agent-{node}", daemon=True)
        thread.start()
        while thread.is_alive():
            self.wait_for_activity()
            if self.poll_input:
                text = self.poll_input()
                if text:
                    self.submit_input(text)
            if self.cancelled is not None and self.cancelled():
                if stop_requested_at is None:
                    stop_requested_at = time.time()
                    node_stopped.set()
                if time.time() - stop_requested_at >= 1.0 and not getattr(self._local, "active", 0):
                    # 兜底：节点在宽限内没交出交代（例如卡在一个很长的命令里），强行结束
                    node_stopped.set()
                    thread.join(timeout=1.0)
                    self.deliver_events()
                    raise CancelledError("用户中止了任务（节点未在宽限内返回）")
        self.deliver_events()
        if "error" in holder:
            raise holder["error"]
        return holder["result"]

    def wait_for_activity(self) -> None:
        """等待节点事件到来（最多 :data:`POLL_INTERVAL` 秒），避免空转轮询。"""
        try:
            kind, data = self.events.get(timeout=POLL_INTERVAL)
        except Empty:
            return
        self.notify(kind, data)
        self.deliver_events()

    def deliver_events(self) -> None:
        """把队列里积压的节点事件全部转发出去。"""
        while True:
            try:
                kind, data = self.events.get_nowait()
            except Empty:
                return
            self.notify(kind, data)

    def drain_input(self) -> list[str]:
        """取走当前已登记的补充要求。"""
        updates: list[str] = []
        while True:
            try:
                updates.append(self.pending.get_nowait())
            except Empty:
                return updates

    def notify(self, kind: str, data: dict) -> None:
        """转发 Agent 事件（节点事件与阶段边界）。"""
        if kind == "tool_execution_start":
            self._local.active = getattr(self._local, "active", 0) + 1
        elif kind == "tool_execution_end":
            self._local.active = max(0, getattr(self._local, "active", 0) - 1)
        if kind == "context_checkpoint":
            agent = data.get("agent", "")
            entries = getattr(self, "interruption_log", [])
            self.interruption_log = [entry for entry in entries if entry.get("agent") != agent]
            self.interruption_log.append({"agent": agent, "messages": data.get("messages", [])})
            self.write_detail()
            return
        if self.agent_listener:
            self.agent_listener(kind, data)

    # ---------------------------------------------------------------- 计划
    def check_plan(self, plan: Plan) -> None:
        """结构校验：把每步落到实际worker上。

        ``worker`` 为空或不在注册表里时回退到默认worker（Planner 偶尔会写错或省略），
        回退结果写回计划，后续提示词与事件都用这个确定的worker名。
        技能与工具由worker / 评审者自己匹配加载，计划里不携带技能信息。
        """
        if not plan.steps:
            raise PlanningError("计划没有可执行步骤")
        for step in plan.steps:
            # 每一行都是"执行"：审核由编排层在每步之后独立进行，不占计划里的一步。
            # 所以 worker 写 critic（或写错）都回退到默认worker。
            if step.worker not in self.workers:
                step.worker = self.default_executor
        # 同组并行但写同一个文件 → 拆成串行（并行会让两个 worker 互相覆盖产物）
        for note in unshare_group_outputs(plan):
            self.emit("plan_adjusted", reason=note)

    def ask_user(self, question: str, options: Optional[list] = None) -> str:
        answers = self.clarifier.ask_clarification([{"question": question, "options": options or []}])
        answer = (answers[0] if answers else "").strip() or "（未选择）"
        self.task.user_decisions.append(f"{question} → {answer}")
        return answer

    def request_plan(self) -> PlanningResult:
        """规划请求：澄清最多"交互一轮 + 强制一轮"，之后明确失败，不再无限追问。

        追加上限是因为 Planner 可能反复返回 clarify：提示文本会线性膨胀、每轮都重进
        prompt，而 ``step_max_retries`` 有配额，这里同样需要。
        """
        asked_once = False
        forced_once = False
        clarifications = "\n".join(self.task.user_decisions)
        # planner 只要摘要，不要产物正文路径：给它路径它会去读，实测算过 15 轮全用在读历史
        prior_summary = build_session_summary(self.session, with_paths=False)
        while True:
            self.check_cancel()
            request = build_plan_request(
                self.task,
                prior_summary=prior_summary,
                clarifications=clarifications,
            )
            result, updates = self.dispatch_node("planner", request)
            if updates:
                self.task.user_decisions.extend(updates)
                clarifications = append(clarifications, "\n".join(updates))
                continue
            if result.mode != "clarify":
                return result
            if forced_once:
                raise PlanningError("Planner 连续追问澄清、无法给出计划；请把需求拆小或补充信息后重试")
            if asked_once:
                forced_once = True
                clarifications = append(clarifications, FORCE_PLAN_HINT)
                continue
            asked_once = True
            answers = self.clarifier.ask_clarification(result.questions)
            clarification = "\n".join(
                f"问题：{item.get('question', '')}\n回答：{answer}"
                for item, answer in zip(result.questions, answers)
            )
            self.task.user_decisions.append(clarification)
            clarifications = append(clarifications, clarification)

    def non_plan_result(self, result: PlanningResult) -> Optional[object]:
        """direct/unable → 直接收尾返回 TaskResult；plan → None。"""
        if result.mode == "direct":
            return self.finalize_task(result.answer)
        if result.mode == "unable":
            return self.finalize_task(result.reason)
        if result.plan is None:
            raise PlanningError("Planner 未返回有效计划")
        return None

    def activate_plan(self, plan: Plan) -> Plan:
        """把计划落到实际worker上、记为当前计划，并推给页面。

        ``plan_ready`` 事件带**整份计划**（每步的worker与目标），页面据此渲染计划面板；
        它也是"当前在哪一步"的基准——之后每个 ``step_start`` / ``step_review`` 都按 step 序号对号入座。
        """
        self.check_plan(plan)
        self.task.requirement = plan.requirement or self.task.requirement or self.task.original_request
        self.task.apply_file_agreement(plan)
        self.task.set_plan(plan)
        self.emit(
            "plan_ready",
            requirement=self.task.requirement,
            steps=[
                {
                    "index": index,
                    "goal": step.goal,
                    "expected_output": step.expected_output,
                    "worker": step.worker,
                    # 并行组号：前端据此把同组的步骤并排显示
                    "group": int(getattr(step, "group", 0) or 0),
                }
                for index, step in enumerate(plan.steps)
            ],
        )
        return plan

    # ---------------------------------------------------------------- 步骤推进
    def replan_remaining(
        self, plan: Plan, index: int, result: ExecutionResult
    ) -> Optional[Plan]:
        """worker 说做不到 → 让 planner 只重排**剩余**步骤，已完成的不重做。

        失败原因、已完成步骤与产出都进 planner 的输入，它据此换做法或缩小范围——已经拿到
        的成果不会因为某一步做不到而白费。重排次数有上限（:data:`REPLAN_LIMIT`），
        超了或 planner 给不出计划就返回 ``None``，由调用方按失败收尾，不会无限重排。
        """
        if self.replan_used >= REPLAN_LIMIT:
            return None
        self.replan_used += 1
        blocked = plan.steps[index]
        reason = str(result.content or result.summary or "").strip()
        limits = "；".join(result.limitations or [])
        done = build_completed_prompt(self.task)
        note = (
            f"\n\n【第 {index + 1} 步做不到，只需重排剩余步骤】\n"
            f"这一步的目标：{blocked.goal}\n"
            f"做不到的原因：{reason or '（未说明）'}"
            + (f"\n已知限制：{limits}" if limits else "")
            + "\n已完成的部分（不要重做，也不要重复产出）：\n"
            + (done or "（无）")
            + "\n请给出**剩余**步骤的计划；如果确实无法完成，返回 unable 并说明缺什么。"
        )
        request = build_plan_request(
            self.task,
            prior_summary=build_session_summary(self.session) + note,
            clarifications="\n".join(self.task.user_decisions),
        )
        try:
            outcome, _updates = self.dispatch_node("planner", request)
        except Exception:  # noqa: BLE001 - 重排失败不该把已有成果一起丢掉
            return None
        if outcome.mode != "plan" or outcome.plan is None or not outcome.plan.steps:
            return None
        merged = Plan(
            requirement=self.task.requirement,
            output_name=outcome.plan.output_name or plan.output_name,
            steps=[*plan.steps[:index], *outcome.plan.steps],
        )
        # 记录槽位跟着截断并重建：plan.steps 与 task.steps 必须一一对应，否则序号会错位
        self.task.plan = merged
        del self.task.steps[index:]
        for position in range(index, len(merged.steps)):
            step = merged.steps[position]
            self.task.steps.append(
                TaskStep(
                    index=position,
                    goal=step.goal,
                    expected_output=step.expected_output,
                    worker=step.worker,
                    group=int(step.group or 0),
                )
            )
        self.emit(
            "plan_ready",
            requirement=self.task.requirement,
            steps=[
                {"index": position, "worker": step.worker, "goal": step.goal, "group": step.group}
                for position, step in enumerate(merged.steps)
            ],
        )
        self.emit("plan_replanned", index=index, reason=reason, steps=len(merged.steps))
        return merged

    def step_loop(self, plan: Plan):
        """主链：按组推进——独立步骤逐条跑，并行组同时跑并**整组评审一次**。

        用户中止时不再抛异常丢弃一切：**停在当前工具调用之后**，把已完成的部分连同
        当前步的交代写成中止说明，作为本次任务的终态（用户之后可以据此接着改）。

        某一步（或某一组）做不到就**就地停止**并发一份总结汇报：不重规划、不重试整条计划。
        问题描述与进度的落盘由 :meth:`fail_task` 负责。
        """
        if self.stopping():
            return self.finalize_task(self.stop_content(None), status="aborted")
        total = len(plan.steps)
        index = 0
        while index < total:
            step = plan.steps[index]
            members = self.group_members(plan, index)
            if len(members) > 1:
                approved, results, review, interrupted = self.run_group(plan, members)
                latest = results[-1] if results else None
                if self.stopping():
                    for member in members:
                        self.task.mark_step(member, "running")
                    return self.finalize_task(self.stop_content(latest), status="aborted")
                if approved and not interrupted:
                    index = members[-1] + 1
                    continue
                failures = [review] if review is not None else []
                return self.fail_task(
                    describe_step_failure(members[0], plan.steps[members[0]], latest, failures)
                )
            result, review, failures, interrupted = self.run_step(step, index, total)
            if self.stopping():
                # 节点已在工具边界收尾并交代了做了什么：直接按中止结束，不再评审/返修
                self.task.mark_step(index, "running")  # 标成"做到一半被打断"，明细里看得出停在哪
                return self.finalize_task(self.stop_content(result), status="aborted")
            if review.approved and not interrupted:
                index += 1
                continue
            # worker 明确说"这一步做不到"→ 自动重排剩余步骤（已完成的成果保留）
            if result is not None and result.status == "needs_replan":
                replanned = self.replan_remaining(plan, index, result)
                if replanned is not None:
                    plan = replanned
                    total = len(plan.steps)
                    continue  # index 不变：新计划在 index 处就是"换个做法"的那一步
            return self.fail_task(describe_step_failure(index, step, result, failures))
        # 完成时 worker 只给路径、不复述正文（正文在产出文件里），这里用"产出清单"兜底，
        # 保证终态 content 不会是空的
        last = self.task.step_results[-1] if self.task.step_results else None
        content = (last.content if last else "") or last.summary if last else ""
        if not content:
            produced = [name for step in self.task.steps for name in (step.files or [])]
            content = "任务已完成。" + (f"产出：{'、'.join(produced[:6])}" if produced else "")
        return self.finalize_task(content)

    def group_members(self, plan: Plan, index: int) -> List[int]:
        """从 index 起收集相邻且 ``group`` 相同的成员；``group`` 为 0 时只返回自己。"""
        group = int(getattr(plan.steps[index], "group", 0) or 0)
        if group <= 0:
            return [index]
        members = [index]
        cursor = index + 1
        while cursor < len(plan.steps) and int(getattr(plan.steps[cursor], "group", 0) or 0) == group:
            members.append(cursor)
            cursor += 1
        return members

    def run_group(
        self, plan: Plan, members: List[int]
    ) -> tuple[bool, List[ExecutionResult], Optional[ReviewResult], bool]:
        """并行执行一组步骤，跑完**整组评审一次**；组内任何一步做不到就整组停止。

        为什么整组只评一次：组内几步本来就互不依赖、产出互不冲突，逐步评会白花往返。
        返修同样整组重跑（受 ``step_max_retries`` 限制）。
        """
        rounds = max(1, self.step_max_retries)
        feedback = ""
        results: List[ExecutionResult] = []
        review: Optional[ReviewResult] = None
        for attempt in range(rounds):
            group_no = int(getattr(plan.steps[members[0]], "group", 0) or 0)
            for member in members:
                self.emit(
                    "step_start",
                    index=member,
                    total=len(plan.steps),
                    attempt=attempt,
                    goal=plan.steps[member].goal,
                    worker=plan.steps[member].worker,
                    group=group_no,
                )
            results = self.execute_members(plan, members, feedback)
            if self.stopping():
                return False, results, None, True
            blocked = next((item for item in results if item.status in {"needs_input", "needs_replan"}), None)
            if blocked is not None:
                review = ReviewResult(
                    approved=False,
                    comment="；".join(blocked.limitations) or blocked.summary or "组内有步骤做不到",
                )
                # 组内**每个**成员都要收到评审事件：只发第一个，其余在前端会永远停在"执行中"
                for member in members:
                    self.emit(
                        "step_review", index=member, approved=False,
                        summary=review.comment, attempt=attempt, group=group_no,
                    )
                return False, results, review, True
            review = self.review_group(plan, members, results)
            for member in members:
                self.emit(
                    "step_review", index=member, approved=review.approved,
                    summary=review.comment, attempt=attempt, group=group_no,
                )
            if self.stopping():
                return False, results, review, True
            if review.approved:
                workspace = self.runtime.get("workspace") or ""
                for member, result in zip(members, results):
                    output_file = self.persist_step_output(member, plan.steps[member], result)
                    # 组内每步各自一条记录（detail.json 里分开存），共用同一次评审结论；
                    # 各自存自己的产出正文，下一步按步取用
                    self.task.accept_step(
                        member, result, output_file=output_file, review=review,
                        artifact=artifact_text(result.files, workspace),
                    )
                return True, results, review, False
            # 整组返修：把组内每个结果的做法与证据都给到，避免只看到第一个
            feedback = "\n\n".join(build_rework_prompt(review, item, []) for item in results)
        return False, results, review, True

    def execute_members(self, plan: Plan, members: List[int], feedback: str) -> List[ExecutionResult]:
        """并行执行组内各步：每步交给自己的worker，结果按 ``members`` 顺序返回。"""
        session_history = build_session_summary(self.session)

        def work(member: int) -> tuple[int, ExecutionResult]:
            step = plan.steps[member]
            request = build_step_request(
                self.task,
                step,
                index=member,
                feedback=feedback,
                session_history=session_history,
            )
            result, _updates = self.dispatch_node(step.worker or self.default_executor, request)
            return member, result

        with ThreadPoolExecutor(max_workers=len(members), thread_name_prefix="step") as pool:
            done = dict(pool.map(work, members))
        return [done[member] for member in members]

    def review_group(
        self, plan: Plan, members: List[int], results: List[ExecutionResult]
    ) -> ReviewResult:
        """整组一次评审：把组内所有 step + result + **各自的产出正文** 一起交给 Critic。"""
        workspace = self.runtime.get("workspace") or ""
        artifact = "\n\n".join(
            artifact_text(result.files, workspace) for result in results
        ).strip()
        request = ReviewRequest(
            task=self.task.requirement,
            step=plan.steps[members[0]],
            result=results[0],
            plan_outline=build_plan_outline(self.task, members[0]),
            artifact=artifact,
            grouped=[
                StepOutcome(index=member, step=plan.steps[member], result=result)
                for member, result in zip(members, results)
            ],
        )
        review, _updates = self.dispatch_node("critic", request)
        return review

    def run_step(
        self, step: PlanStep, index: int, total: int
    ) -> tuple[ExecutionResult, ReviewResult, List[ReviewResult], bool]:
        """执行并评审一个步骤（含澄清与返修），把尝试与结果并入任务状态。

        返回 ``(最后结果, 最后评审, 未通过的评审列表, 是否中断)``：``中断`` 表示这一步
        做不到（评审连续不通过、执行输出无法解析、worker自认做不下去）——调用方据此
        停止整个任务，不再重规划。

        用户在步骤执行/评审期间提的补充要求会并入 ``user_decisions`` 并作为返修反馈
        继续当前步，**不会重排计划**。
        """
        session_history = build_session_summary(self.session)
        # 本步交给谁：check_plan 已把 worker 规范化为注册表里的worker
        worker = step.worker or self.default_executor
        feedback = ""
        failures: List[ReviewResult] = []
        attempts: List[StepAttempt] = []
        errors: List[str] = []
        updates: List[str] = []
        result: Optional[ExecutionResult] = None
        review: Optional[ReviewResult] = None
        #: 这一步做不下去了（评审连续不通过 / 输出无法解析 / worker自认做不到）
        interrupted = False
        while True:
            self.emit("step_start", index=index, total=total, attempt=len(failures), goal=step.goal, worker=worker)
            try:
                request = build_step_request(
                    self.task,
                    step,
                    index=index,
                    feedback=feedback,
                    session_history=session_history,
                )
                result, executed_updates = self.dispatch_node(worker, request)
            except ExecutionError as error:
                errors.append(f"结构化输出解析失败：{error}")
                review = ReviewResult(approved=False, comment=str(error))
                failures.append(review)
                self.emit(
                    "step_review", index=index, approved=False, summary="执行输出无法解析", attempt=len(failures)
                )
                if len(failures) >= self.step_max_retries:
                    result = ExecutionResult(
                        status="needs_replan",
                        content="",
                        summary="执行输出多次无法解析，当前步骤无法继续。",
                        limitations=[f"结构化输出解析失败：{error}"],
                    )
                    interrupted = True
                    break
                feedback = f"上一步执行未能返回合法结果（{error}）。请重新执行本步，确保最终输出为合法 JSON。"
                continue
            attempt = StepAttempt(result=result)
            attempts.append(attempt)
            if self.stopping():
                # 用户中止：worker已把"做了什么、产出在哪、还差什么"交代在 result 里，
                # 本步不再评审（记录仍进明细，用户之后可以据此接着改）
                review = ReviewResult(
                    approved=False,
                    comment=result.summary or "用户中止了任务。",
                )
                break
            if executed_updates:
                # 用户在执行期间提了新要求：并入任务（下一轮上下文可见），
                # 带着它继续返修本步——不重排计划。
                updates.extend(executed_updates)
                feedback = "用户在执行期间补充了要求：\n" + "\n".join(f"- {item}" for item in executed_updates)
                continue
            if result.status == "needs_input":
                answer = self.ask_user(result.question or "请补充信息以继续")
                feedback = build_rework_prompt(
                    ReviewResult(approved=False, comment=f"用户补充：{answer}"), result, failures
                )
                continue
            if result.status == "needs_replan":
                review = ReviewResult(
                    approved=False,
                    comment="；".join(result.limitations) or result.summary or "worker请求重新规划。",
                )
                interrupted = True
                self.emit(
                    "step_review", index=index, approved=False, summary=review.comment, attempt=len(attempts)
                )
                break
            # 每一步都必须过 Critic（不看是谁派的、也不接受"这步不用审"的声明）
            # 评审要看产出文件的**实际正文**，不能只看worker的自述
            artifact = artifact_text(result.files, self.runtime.get("workspace") or "")
            review_request = build_review_request(self.task, step, result, index, artifact=artifact)
            review, review_updates = self.dispatch_node("critic", review_request)
            attempt.review = review
            self.emit(
                "step_review",
                index=index,
                approved=review.approved,
                summary=review.comment,
                attempt=len(attempts),
            )
            if self.stopping():
                break  # 评审期间用户点停止：结论已记录，直接收尾
            if review_updates:
                updates.extend(review_updates)
                feedback = "用户补充了要求：\n" + "\n".join(f"- {item}" for item in review_updates)
                continue
            if review.approved:
                break
            failures.append(review)
            feedback = build_rework_prompt(review, result, failures)
            if len(failures) >= self.step_max_retries:
                interrupted = True
                break
        for recorded in attempts:
            self.task.record_attempt(index, recorded.result, recorded.review)
        for message in errors:
            self.task.record_error(index, message)
        if updates:
            self.task.user_decisions.extend(updates)
        if review.approved:
            output_file = self.persist_step_output(index, step, result)
            self.task.accept_step(
                index, result, output_file=output_file, review=review, artifact=artifact
            )
        elif attempts:
            # 没通过的步骤同样落盘：这份是"做到哪、卡在哪"的唯一完整记录，下一轮按路径查阅
            self.task.step_of(index).output_file = self.persist_step_output(index, step, attempts[-1].result)
        return result, review, failures, interrupted

    # ---------------------------------------------------------------- 收尾
    def stop_content(self, latest: Optional[ExecutionResult]) -> str:
        """中止说明：只写一句"任务已停止"。

        详细的"做了什么、产出了哪些文件"已经落盘在明细与会话记录里，用户要看时再看——
        中止时在对话里铺一大段文字反而盖住了他真正想做的事。
        """
        return "任务已停止。"

    def collect_files(self) -> List[str]:
        """汇总成果文件：已通过步骤声明的产出；中止/失败的任务再加上未通过那次的产出。

        中止时用户要按「已产出的文件（未通过评审，可直接取用）」接手，所以那次尝试
        声明的文件也列出来；正常完成的任务只列通过评审的步骤产出——否则半成品会被
        一起复制到用户的工作目录。声明为空时兜底扫描工作目录。

        结果在本任务内缓存：收尾路径会多次调用，而兜底扫描要遍历整个工作目录。
        """
        if self._files_cache is not None:
            return list(self._files_cache)
        files: List[str] = []
        for result in self.task.step_results:
            for name in result.files or []:
                if name not in files:
                    files.append(name)
        if self.task.abort_reason:
            for step in self.task.steps:
                for attempt in step.attempts:
                    for name in attempt.result.files or []:
                        if name not in files:
                            files.append(name)
        self._files_cache = files or self.recent_artifacts()
        return list(self._files_cache)

    def recent_artifacts(self) -> List[str]:
        """兜底：本任务开始后出现在工作目录（顶层）的文档，避免"文件已生成却报无成果"。"""
        if self.artifact_dir is None or not self.started_at or not self.artifact_dir.is_dir():
            return []
        found: List[str] = []
        try:
            for path in sorted(self.artifact_dir.iterdir()):
                if not path.is_file() or path.suffix.lower() not in ARTIFACT_SUFFIXES:
                    continue
                if path.stat().st_mtime >= self.started_at:
                    found.append(str(path))
        except OSError:
            logger.debug("兜底扫描产出目录失败：%s", self.artifact_dir, exc_info=True)
        return found

    def persist_step_output(self, index: int, step: PlanStep, result: ExecutionResult) -> str:
        """把一步的完整产出落盘到会话的 ``internal`` 目录，返回路径（落不下去时返回空串）。

        **为什么必须落盘**：任务内上下文只带压缩摘要（:data:`STEP_SUMMARY_CHARS`），而
        ``detail.json`` 只在任务收尾时写入——任务进行中它里面还没有本任务的记录。所以这个
        文件是"本步正文传给下一步"的唯一通道：下一步据 ``step.output_file`` 直接 read。

        落盘失败只是降级（下一步只能看摘要），不中断任务。
        """
        session_dir = str(self.runtime.get("session_dir") or "")
        if not session_dir:
            return ""
        path = step_output_path(session_dir, self.task.task_seq, index, step.worker or self.default_executor)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(render_step_output(self.task.task_seq, index, step, result), encoding="utf-8")
        except OSError:
            logger.warning("步骤产出落盘失败：%s", path, exc_info=True)
            return ""
        return str(path)

    def write_detail(self) -> int:
        """把每个步骤的明细各写一个文件（``detail/task{N}_step{MM}.json``），返回任务号。

        按步骤拆开：单个文件很小，需要哪一步就直接全量读——不用按字段下钻，
        也不会出现"整份 detail 越读越大"的问题。
        """
        if self.detail_store is None or not self.session.name:
            return 0
        detail = self.task.to_detail()
        detail.interruption_log = list(getattr(self, "interruption_log", []))
        return self.detail_store.save(self.session.name, detail)

    def save_session(self) -> None:
        """把会话状态写回 summary.json。"""
        if self.summary_store is None or self.session.info is None:
            return
        self.summary_store.save(SessionData(meta=self.session.info, tasks=list(self.session.tasks)))

    def finalize_task(self, content: str, status: str = "completed") -> TaskResult:
        """任务收尾：写明细 → 生成并并入任务摘要 → 保存会话 → 返回终态结果。

        **幂等**：同一个任务只会记账一次——中止路径上编排层与应用层都可能来收尾，
        重复调用只返回已有结果，避免 ``summary.json`` 里出现同一个任务的两条摘要。

        ``status="aborted"``（用户中止）时对话里只留一句"任务已停止"；已产出什么、做到哪一步，
        都在 ``summary.json`` 与 ``detail.json`` 里，用户要看时再看。
        """
        if self._finalized:
            return build_task_result(self.task, self.task.content, self.task.status)
        if status == "aborted":
            if not self.task.abort_reason:
                reason = getattr(self, "interruption_reason", None)
                self.task.abort_reason = (reason() if reason else "") or "用户中止了任务"
            # 中止时对话里只留一句话：产出了什么、做到哪，都已在 detail.json 与会话记录里
            content = "任务已停止。"
            self.emit("orchestrator_aborted", reason=self.task.abort_reason)
        pending = self.drain_input()
        close_input = getattr(self, "close_input", None)
        if close_input:
            pending.extend(close_input())
        elif self.poll_input:
            while True:
                text = self.poll_input()
                if text is None:
                    break
                pending.append(text)
        if pending:
            self.task.user_decisions.extend(pending)
            content = "尚未处理的用户补充要求（后续规划需结合最新需求处理）：\n" + "\n".join(pending) + "\n\n" + content
        self.task.status = status
        self.task.content = content
        self.task.files = self.collect_files()
        self.write_detail()
        summary = build_task_summary(self.task)
        if self.task.user_decisions:
            summary.task_summary += "\n用户补充与决定：\n" + "\n".join(self.task.user_decisions)
        summary.interruption_log = list(getattr(self, "interruption_log", [])) if status != "completed" else []
        existing = next((i for i, item in enumerate(self.session.tasks) if item.task_seq == summary.task_seq), None)
        if existing is None:
            self.session.add_task(summary)
        else:
            self.session.tasks[existing] = summary
        self.save_session()
        self._finalized = True
        return build_task_result(self.task, content, status)

    def fail_task(self, reason: str) -> TaskResult:
        """任务失败（异常 / 某步做不到）时的收尾：生成一份**总结汇报**并落盘。

        汇报写进 ``content`` → 同时进 ``detail.json`` 与 ``summary.json``：用户看得出"做到哪、
        产出什么"，下一次规划也能从会话摘要里读到它（上下文可见），不必重头猜。
        """
        # 先记原因：collect_files 靠它把"未通过评审那次"的产出也纳进来（失败时要能直接取用）
        self.task.abort_reason = reason
        self._files_cache = None
        self.task.files = self.collect_files()
        report = build_failure_report(self.task, reason)
        self.emit("orchestrator_failed", reason=reason)
        return self.finalize_task(report, status="failed")

    def plan_chain(self):
        """plan_and_execute 主链：澄清→激活计划（直接执行）→step_loop→收尾。

        快捷模板给了现成计划时跳过澄清与规划，直接激活执行（少一轮 LLM 调用、
        步骤也稳定）；否则照旧走 Planner。

        在"还没有中途成果"的边界（规划、澄清请求）上被取消时，也按中止收尾：
        写一份说明再结束，而不是只留一个失败摘要。
        """
        try:
            if self._preset_plan is not None:
                return self.step_loop(self.activate_plan(self._preset_plan))
            first = self.request_plan()
            direct = self.non_plan_result(first)
            if direct is not None:
                return direct
            return self.step_loop(self.activate_plan(first.plan))
        except CancelledError:
            return self.finalize_task(self.stop_content(None), status="aborted")

    #: 工作流注册表：唯一事实来源；各工作流的语义见对应方法 docstring。
    _WORKFLOWS = {"plan_and_execute": plan_chain}


def append(text: str, addition: str) -> str:
    """追加一段说明（首段不带前导换行）；已经包含的内容不再重复追加。"""
    if not text:
        return addition
    if addition in text:
        return text
    return f"{text}\n{addition}"
