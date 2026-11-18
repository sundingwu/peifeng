"""编排主链：规划 → 逐步执行与评审 → 收尾；并负责把注册表装配成可运行的 Agent。"""

from __future__ import annotations

import os
import sys
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union
import re
import json
import logging
import threading
import time
from queue import Empty, Queue
from typing import Any, Callable, List, Optional
from backend.memory.store import (
    INTERNAL_DIR,
    DetailStore,
    SessionData,
    SessionInfo,
    SummaryStore,
)
from backend.agents.base import (
    ARTIFACT_SUFFIXES,
    CORE_ROLES,
    AgentNode,
    AgentRequest,
    Clarifier,
    ExecutionRequest,
    ExecutionResult,
    FinalRequest,
    Listener,
    Plan,
    PlanningRequest,
    PlanningResult,
    PlanStep,
    ReviewRequest,
    ReviewFinding,
    ReviewResult,
    StageReview,
    StageReviewRequest,
    StepAttempt,
    TaskDetail,
    TaskResult,
    TaskStep,
    TaskSummary,
)
from config import Settings
from backend.agents.base import OpenAIProvider
from backend.agents.mentor import MentorAgent
from backend.agents.worker import WorkerAgent, produced_files
from backend.utils.errors import CancelledError, ExecutionError, PlanningError
import importlib.util
import tempfile
import yaml
from backend.agents.base import CORE_ROLES


"""编排主链：规划 → 逐步执行与评审 → 收尾；并负责把注册表装配成可运行的 Agent。"""




# ---------------------------------------------------------------- forward
logger = logging.getLogger(__name__)

DEFAULT_WORKFLOW = "plan_and_execute"

#: 等待节点事件或补充输入的最长时间（秒）：事件到达即刻转发，无事件时按此节奏检查输入
POLL_INTERVAL = 0.05

#: 规划阶段最多几轮模型往返（含用户在执行前补充要求触发的重排）
PLANNING_ROUNDS = 3

# ---------------------------------------------------------------- 上下文压缩
#: 没有步骤记录时，任务结果的兼容摘要长度。
TASK_RESULT_CHARS = 160
#: 步骤压缩摘要（"做法与目的"）的字数上限：下一步与评审都靠它衔接
STEP_SUMMARY_CHARS = 240
#: 模型给的计划里 worker 不合法时，最多再让它重出几次（规划阶段另有 PLANNING_ROUNDS 兜底）
PLAN_REPAIR_LIMIT = 2

#: internal/archive/ 里最多保留多少个历史中间文件
INTERNAL_ARCHIVE_LIMIT = 40

#: 从 expected_output 里认出产出文件名（用来判断交付文件是否真的落地）
_OUTPUT_FILE_RE = re.compile(
    r"(?:[\w\-.（）()]{1,60}[\\/])*[\w\-.（）()]{1,60}\.(?:md|txt|csv|json|png|jpe?g|svg|docx|xlsx|pptx|pdf)",
    re.IGNORECASE,
)


def output_file_exists(workspace, name: str) -> bool:
    """Allow prose wrappers in output_name without misreading them as filenames."""
    root = Path(workspace).resolve()
    normalized = name.replace('\\', '/')
    candidate=(root/normalized).resolve()
    if candidate.is_relative_to(root) and candidate.is_file():
        return True
    for opening in ('（', '('):
        if opening in normalized:
            candidate=(root/normalized.rsplit(opening,1)[-1]).resolve()
            if candidate.is_relative_to(root) and candidate.is_file():
                return True
    return False


def output_is_mentioned(name: str, mentioned: set[str]) -> bool:
    """未限定目录的交付名可由唯一完整路径承接；不同明确目录不互相替代。"""
    normalized = name.replace('\\', '/').casefold()
    if normalized in mentioned:
        return True
    if '/' in normalized:
        return False
    matches = [path for path in mentioned if path.rsplit('/', 1)[-1] == normalized]
    return len(matches) == 1


def missing_step_outputs(workspace, expected: str, files: list[str]) -> list[str]:
    """检查明确文件交付；短文件名可由本次回执中唯一真实完整路径承接。"""
    root=Path(workspace).resolve()
    delivered=set()
    for value in files:
        path=(root/value).resolve()
        if path.is_relative_to(root) and path.is_file():
            delivered.add(path.relative_to(root).as_posix().casefold())
    missing=[]
    for match in _OUTPUT_FILE_RE.finditer(expected):
        name=match.group(0).replace('\\','/')
        if output_file_exists(root,name) or output_is_mentioned(name,delivered):
            continue
        parts=name.split('/')
        if len(parts)>1 and any(_OUTPUT_FILE_RE.fullmatch(part) for part in parts[:-1]):
            # 自然语言常用a.md/b.md并列资料，不把前一个文件当作目录。
            for part in parts:
                if _OUTPUT_FILE_RE.fullmatch(part):
                    missing.extend(missing_step_outputs(root,part,files))
            continue
        if '/' not in name:
            # 裸文件名（历史计划里常见"与 stats.json 一致"这种输入引用）：
            # 先看本步回执里有没有同名产物，再在项目里找，跳过 internal/ 中间目录。
            # 有同名多份（备份/副本）也算满足——规范做法是让 expected_output 写完整路径，
            # 这里只是兜底，不该因为多一份副本就把整步判死。
            folded = name.casefold()
            if any(path.rsplit('/', 1)[-1] == folded for path in delivered):
                continue
            for path in root.rglob(name):
                if not path.is_file() or not path.resolve().is_relative_to(root):
                    continue
                if 'internal' in path.relative_to(root).parts:
                    continue
                break
            else:
                missing.append(name)
                continue
            continue
        missing.append(name)
    return sorted(set(missing))


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


# ---------------------------------------------------------------- 任务作用域


@dataclass
class TaskState:
    """一次任务的运行态（按任务号存档在 ``summary.json 的 records 字段``）。"""

    task_seq: int = 0
    original_request: str = ""
    requirement: str = ""
    workflow: str = ""
    plan: Optional[Plan] = None
    steps: List[TaskStep] = field(default_factory=list)
    step_results: List[ExecutionResult] = field(default_factory=list)
    plan_history: List[dict] = field(default_factory=list)
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
            status='running',
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
        step.attempts.append(StepAttempt(attempt=len(step.attempts), result=result, review=review))
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
        ``summary.json 的 records 字段`` 里。

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
        step.status = "completed"

    def to_detail(self) -> TaskDetail:
        """转为落盘的明细记录。"""
        return TaskDetail(
            task_seq=self.task_seq,
            requirement=self.requirement,
            original_request=self.original_request,
            plan_history=list(self.plan_history),
            plan=self.plan,
            steps=list(self.steps),
            user_decisions=list(self.user_decisions),
            status=self.status,
            content=self.content,
            files=list(self.files),
            abort_reason=self.abort_reason or "",
        )


#: 阶段审核说"继续"却没给 extra_budget 时，默认追加的工具调用次数
DEFAULT_EXTRA_BUDGET = 15

# ---------------------------------------------------------------- 任务内提示构建
def build_request(
    kind: str,
    state: TaskState,
    *,
    step: Optional[PlanStep] = None,
    result: Optional[ExecutionResult] = None,
    index: int = -1,
    artifact: str = "",
    feedback: str = "",
    prior_summary: str = "",
    clarifications: str = "",
    plan_feedback: str = "",
) -> Union[PlanningRequest, ExecutionRequest, ReviewRequest]:
    """按阶段组装请求：``plan`` 规划 / ``step`` 单步执行 / ``review`` 单步评审。

    三种阶段的输入差别很大，集中在一处便于对照；缺必需参数直接抛 ValueError，
    不拿"看起来能用"的默认值糊过去。

    * ``plan``：``prior_summary``（跨任务摘要）、``clarifications``、``plan_feedback``（上次退回原因）。
    * ``step``：``step``/``index``/``feedback``（返修时非空，表示续做）。
    * ``review``：``step``/``result``/``artifact``（产出正文）。
    * ``stage``：``step``/``result``——工具调用配额用尽时的阶段审核。计划只给到本步——
      给全计划会诱导 worker 把后面几步的活一起做掉（实测有一次三步的活被第一步全干完）。
    """
    if kind == "plan":
        return PlanningRequest(
            task=state.requirement or state.original_request,
            prior_summary=prior_summary,
            clarifications=clarifications,
            plan_feedback=plan_feedback,
        )
    if kind == "step":
        if step is None:
            raise ValueError("build_request('step') 需要 step")
        return ExecutionRequest(
            original_task=state.original_request,
            context_id=f"{state.task_seq}:{index}",
            continuation=bool(feedback),
            step=step,
            # 用户决定只走 user_decisions：放进 feedback 会被 worker 提示词重复注入一次
            feedback=feedback,
            previous_results="",
            requirement=state.requirement,
            plan_outline="",
            user_decisions=list(state.user_decisions),
            output_name=state.output_name,
        )
    if kind == "review":
        if step is None or result is None:
            raise ValueError("build_request('review') 需要 step 与 result")
        return ReviewRequest(
            original_task=state.original_request,
            task=state.requirement,
            user_decisions=list(state.user_decisions),
            step=step,
            result=result,
            plan_outline=_plan_outline(state, index),
            completed_summary=build_completed_prompt(state),
            artifact=artifact,
        )
    if kind == "stage":
        if step is None or result is None:
            raise ValueError("build_request('stage') 需要 step 与 result")
        return StageReviewRequest(
            original_task=state.original_request,
            task=state.requirement,
            step=step,
            result=result,
            plan_outline=_plan_outline(state, index),
            completed_summary=build_completed_prompt(state),
            user_decisions=list(state.user_decisions),
            budget=step.tool_budget,
            used=len(result.tool_calls),
        )
    raise ValueError(f"未知阶段：{kind!r}")


def _plan_outline(state: TaskState, current: int = -1, upto: int = -1) -> str:
    """计划本身：每步交给谁、做什么、验收口径——**不带执行状态**。

    计划是 mentor 给的原始编排（与 ``summary.json 的 records 字段`` 里的 ``plan`` 同形）。``current`` 只用来
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
    if state.output_name and end == total:
        # 交付文件名是任务事实（mentor 定的）；"长结果落盘、短结果进 content"是规则，在 system 里
        lines.append(f"交付文件：{state.output_name}")
    return "\n".join(lines)


def build_completed_prompt(state: TaskState) -> str:
    """已完成步骤的**压缩摘要**：只有事实——目标、worker、结果、产出文件、完整产出路径。

    每一步在评审通过时就被压成这一条（见 :meth:`TaskState.accept_step`）：完整正文、每次
    尝试与工具输出留在该步的 ``output_file`` 与 ``summary.json 的 records 字段``。**这里不写“该怎么读、不要重跑”这类说明**——那是
    系统提示里「通用规则」的职责（system 说规则、user 说事实）。
    """
    lines: list[str] = []
    for step in state.steps:
        if not step.summary and not step.deliverable:
            continue
        owner = f"（{step.worker}）" if step.worker else ""
        lines.append(f"- 步骤 {step.index + 1}：{step.goal}{owner}")
        # 评审给的交付物要点优先：它含关键数据与结论，够下一步直接接着干
        lines.append(f"  结果：{step.summary or '已通过，详情见产出'}")
        if step.files:
            lines.append(f"  产出：{'、'.join(step.files)}")
        if step.output_file:
            lines.append(f"  完整产出：{step.output_file}")
        if step.attempts:
            handoff = step.attempts[-1].result.handoff
            if handoff:
                lines.append(f'  交接：{lead(handoff, 600)}')
    historical = []
    known = {path for step in state.steps for path in step.files}
    for revision in state.plan_history:
        for replaced in revision.get('replaced_steps', []):
            paths = [*replaced.get('files', [])]
            if replaced.get('output_file'):
                paths.append(replaced['output_file'])
            for attempt in replaced.get('attempts', []):
                paths.extend(attempt.get('result', {}).get('files', []))
            for path in paths:
                if path and path not in known:
                    known.add(path)
                    historical.append(path)
    if historical:
        lines.append('先前路线产物引用（未等同当前目标已验收；使用前核对当前文件）：' + '、'.join(historical[:24]))
    return "\n".join(lines)


def build_rework_prompt(review: ReviewResult, result: ExecutionResult, failures: List[ReviewResult]) -> str:
    """返修提示：本轮意见 + 此前意见历史 + 执行证据**摘要**（细节让模型自己读）。"""
    lines = [f"未通过评审：{review.comment or '（评审未说明原因）'}"]
    if len(failures) > 1:
        history = "\n".join(
            f"{index + 1}. {item.comment}" for index, item in enumerate(failures[:-1])
        )
        lines.append(f"此前评审意见：\n{history}")
    if review.findings:
        lines = ["只修复以下阻断项，保留已正确内容、代码结构与数据；不要全量重写或补做未要求的文档："]
        lines.extend(item.model_dump_json() for item in review.findings if item.severity == "blocking")
    append_evidence(lines, result, "交付正文")
    return "\n".join(lines)


def describe_step_failure(index: int, step: PlanStep, result: Optional[ExecutionResult], failures: List[ReviewResult]) -> str:
    """一句话说明"为什么停在这一步"（细节交给 :func:`build_failure_report`）。

    不再重规划：遇到问题就地停止，把这条问题描述连同进度一起记进会话记录，
    用户在下一轮请求里能看到上次是怎么失败的。
    """
    label = f"步骤 {index + 1}（{step.goal or '目标未填'}）"
    if failures:
        last = failures[-1]
        reason = lead(last.comment or "", STEP_SUMMARY_CHARS) or "（评审未给出意见）"
        return f"{label}连续 {len(failures)} 次未通过评审：{reason}"
    return f"{label}未能完成"


def build_failure_report(state: TaskState, reason: str) -> str:
    """任务没做成时的总结汇报：进度、卡在哪、产出什么、下一步建议。

    写进 ``content``／``summary.json 的 records 字段``／``summary.json`` 三处，所以它同时是"给用户看的交代"
    和"给下一次规划的上下文"——用户重新提要求时，Mentor 从会话摘要里就能读到这份报告，
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


def append_evidence(lines: list[str], result: ExecutionResult, content_label: str) -> None:
    """把执行证据压成**摘要**追加进提示：做法与目的 + 正文首段 + 产出位置 + 限制 + 工具一览。

    完整正文与每一次工具调用的参数/输出都不进上下文——它们在产出文件与 ``summary.json 的 records 字段``
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
    if result.tool_calls:
        lines.append("工具调用一览：")
        for call in result.tool_calls:
            status = "成功" if not call.is_error else "出错"
            detail = "\n参数：" + json.dumps(call.arguments, ensure_ascii=False) + "\n完整返回：" + call.result
            lines.append(f"- {call.name} {status}{detail}")
    else:
        lines.append("工具调用一览：（未调用工具）")
    if result.content or result.tool_calls:
        lines.append(
            "（需要完整正文时按上面的产出路径 read（没有落盘产出则以本次摘要为准）；"
            "工具调用输出与每次尝试在会话 summary.json 的 /records/<任务号>/steps 字段，使用json_pointer按需读取。"
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
        """会话名（用于定位 summary.json 与 summary.json 的 records 字段）。"""
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
def task_overview(status: str, steps: list[TaskStep], content: str, reason: str = "") -> str:
    """Build a task-level summary locally from step summaries and terminal facts."""
    parts = [f"任务状态：{status}"]
    for step in steps:
        if step.status == "completed":
            parts.append(f"步骤{step.index + 1}已完成：{step.summary or step.goal}")
        elif status != "completed":
            # 中止/失败的步骤要带上"已经做到哪"：接手的人先要知道这一步做过什么，
            # 否则只会重做或凭空补写。
            did = ""
            if step.attempts:
                did = lead(step.attempts[-1].result.summary or "", STEP_SUMMARY_CHARS)
            parts.append(
                f"步骤{step.index + 1}未完成：{step.goal}（{step.status}）"
                + (f"；已做到：{did}" if did else "")
            )
    if reason:
        parts.append("中断原因：" + reason)
    if not steps:
        parts.append("结果：" + lead(content, TASK_RESULT_CHARS))
    return "\n".join(parts)


def build_session_summary(state: SessionState, *, with_paths: bool = True) -> str:
    """把跨任务摘要拼成一段文本。

    摘要默认带细节读取索引，不包含工具正文或产物全文；Mentor只在当前目标需要时回读。
    压缩不在这里做——运行中压缩会把这段整体交给模型重新总结（见 ``BaseAgent._summarize_history``）。
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
                else f"summary.json#/records/{summary.task_seq}/steps/{step.index}"
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
            lines.append(f"  中断明细：<会话目录>/summary.json#/records/{summary.task_seq}（按需读取）")
        lines.append(f'  明细：read(targets=[{{"session": true, "path": "summary.json", "json_pointer": "/records/{summary.task_seq}"}}])（只读取需要的字段）')

    return "\n".join(lines)


def build_task_summary(state: TaskState) -> TaskSummary:
    """任务结束时生成摘要（明细按任务号存档，任务号就是它的键）。"""
    return TaskSummary(
        task_seq=state.task_seq,
        requirement=state.requirement,
        original_request=state.original_request,
        task_summary=task_overview(state.status, state.steps, state.content, state.abort_reason or ""),
        plan=state.plan,
        # 摘要里**只留步骤摘要**：attempts 里的工具调用与输出在 summary.json 的 records 字段，
        # 否则 summary.json 会跟着任务越写越大（实测一个单步任务就 46 KB）
        steps=[step.model_copy(update={"attempts": [], "deliverable": "", "errors": []}) for step in state.steps],
        status=state.status,
        content=state.content,
        files=list(state.files),
        # 中止/失败原因也要进摘要：下次接手时"为什么停下"比"停在哪"更需要先知道
        abort_reason=state.abort_reason or "",
    )


class TaskOrchestrator:
    """Mentor统一规划与审核，Worker执行；保留摘要，按进度调整剩余计划。

    仅步骤内返工复用Worker上下文；通过、改派或任务结束后释放。
    原始尝试、工具轨迹与计划调整留在明细日志，不自动拼进Mentor摘要。
    """

    def __init__(
        self,
        mentor,
        workers: dict,
        clarifier: Clarifier,
        step_max_retries: int = 3,
        listener: Optional[Listener] = None,
        summary_store: Optional[SummaryStore] = None,
        detail_store: Optional[DetailStore] = None,
        cancelled: Optional[Callable[[], bool]] = None,
        artifact_dir: Optional[Path] = None,
        worker_specs: Optional[list] = None,
    ) -> None:
        #: worker：名字 → 节点（启动时按注册表构造；计划里每步用 worker 指定由谁做）
        self.workers: dict[str, AgentNode] = {name: node for name, node in workers.items() if name}
        if not self.workers:
            raise ValueError("至少要注册一个worker")
        clash = {"mentor"} & set(self.workers)
        if clash:
            raise ValueError(f"worker名与固定角色冲突：{'、'.join(sorted(clash))}")
        #: worker定义（名字 + 职责），用于给 Mentor 分派依据
        self.worker_specs: list = list(worker_specs or [])
        #: 工作流节点（规划者 + worker + 评审者）；节点事件经订阅进入本类的事件队列，由主线程转发
        self.nodes: dict[str, AgentNode] = {"mentor": mentor, **self.workers}
        #: 澄清提问的交互协议（信息不足时问用户；计划不再需要确认）
        self.clarifier = clarifier
        self.step_max_retries = max(1, step_max_retries)
        #: 工作流事件监听器（:meth:`emit` 使用）；Agent 事件另走 `agent_listener`（前缀不同）
        self.listener = listener
        #: Agent 事件监听器（节点事件与阶段边界），由应用层挂载
        self.agent_listener: Optional[Listener] = None
        #: 执行期间页面插入的补充要求：仅在节点返回边界并入任务
        self.pending: Queue = Queue()
        self._input_lock = threading.Lock()
        #: 补充输入取用回调（无节点事件时按 :data:`POLL_INTERVAL` 节奏轮询）
        self.poll_input: Optional[Callable[[], Optional[str]]] = None
        #: 节点事件队列（节点在 daemon 线程产出，主线程消费）
        self.events: Queue = Queue()
        #: 线程局部状态：并行组会同时跑多个节点，实例属性会被互相踩
        self._local = threading.local()
        #: 注入每次节点调用的元信息（trace_id / session_id）
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

        计划不再需要用户确认：Mentor 给出计划即执行，进度通过 ``plan_ready`` / ``step_start``
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
        for worker in self.workers.values():
            if hasattr(worker, "step_contexts"):
                worker.step_contexts.clear()
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
        self.started_at = time.time()
        try:
            return self._WORKFLOWS[workflow](self)
        except Exception as error:
            self.task.abort_reason=f'{type(error).__name__}: {error}'
            try:
                self.finalize_task('任务因实际异常停止，已保留现有产物与轨迹。'+self.task.abort_reason,status='failed')
            except Exception:
                logger.exception('保存任务异常终态失败')
            raise

    # ---------------------------------------------------------------- 中止
    def check_cancel(self) -> None:
        """在"没有中途成果可交代"的边界上响应中止（任务开始、规划/澄清等待里）。

        执行阶段的取消不走这里：节点在工具边界自己收尾（见 :meth:`run_node`）。
        """
        if self.stopping():
            raise CancelledError("用户中止了任务")

    def stopping(self) -> bool:
        """用户是否已要求中止（容忍取消检查本身出错）。"""
        return bool(self.cancelled() if self.cancelled is not None else False)

    # ---------------------------------------------------------------- 事件
    def emit(self, kind: str, **data) -> None:
        if self.listener:
            self.listener(kind, data)

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
        # Preserve historical plans after the public role rename.
        node = 'visualizer' if node == 'designer' else node
        if node not in self.nodes:
            raise ValueError(f"未知工作流节点：{node}")
        request = request.model_copy(
            update={
                # 取消信号随请求下发：节点据此在**当前工具调用结束后**收尾并交代做了什么
                "stop_check": self.cancelled,
                **self.runtime,
            }
        )
        result = self.run_node(node, request)
        with self._input_lock:
            updates = self.drain_input()
            self.task.user_decisions.extend(updates)
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
            context_id = data.get('context_id', '')
            entries = getattr(self, "interruption_log", [])
            with self._input_lock:
                entries = getattr(self, 'interruption_log', [])
                self.interruption_log = [entry for entry in entries
                                         if (entry.get('agent'), entry.get('context_id', '')) != (agent, context_id)]
                self.interruption_log.append({"agent": agent, 'context_id': context_id,
                                              "messages": data.get("messages", [])})
                self.write_detail()
            return
        if self.agent_listener:
            self.agent_listener(kind, data)

    # ---------------------------------------------------------------- 计划
    def check_plan(self, plan: Plan) -> None:
        """计划结构校验：每步必须指定本会话注册过的 worker。

        **不替模型挑一个默认 worker**：worker 写错或漏写，说明这一步派给谁都是猜的，
        做出来的东西方向就错了。校验失败时由调用方把问题退回给 Mentor 重新规划
        （:meth:`plan_problems` 给出可读原因）。
        技能与工具由worker / 评审者自己匹配加载，计划里不携带技能信息。
        """
        problems = self.plan_problems(plan)
        if problems:
            raise PlanningError("计划不合法：" + "；".join(problems))

    def plan_problems(self, plan: Plan) -> List[str]:
        """列出计划里不能执行的地方（空列表 = 合法），用于退回模型重新规划。"""
        if not plan.steps:
            return ["计划没有可执行步骤"]
        known = "、".join(self.workers)
        problems: List[str] = []
        for index, step in enumerate(plan.steps):
            name = str(step.worker or "").strip()
            if not name:
                problems.append(f"步骤{index + 1}（{step.goal or '目标未填'}）没有指定 worker")
            elif name not in self.workers:
                problems.append(f"步骤{index + 1}（{step.goal or '目标未填'}）的 worker「{name}」不存在，可用：{known}")
        return problems

    def ask_user(self, question: str, options: Optional[list] = None) -> str:
        answers = self.clarifier.ask_clarification([{"question": question, "options": options or []}])
        answer = (answers[0] if answers else "").strip() or "（未选择）"
        self.task.user_decisions.append(f"{question} → {answer}")
        return answer

    def request_plan(self) -> PlanningResult:
        """规划请求：不足时**只允许一次集中澄清**，之后必须给出计划或说明做不了。

        澄清一次就够——多次追问会让提示线性膨胀（每轮都重进 prompt），而用户要的是
        "问清细节 → 开始工作，或者直接说能力不足"。第二次仍返回 clarify 说明一轮问不清，
        按 ``unable`` 收尾并把还缺什么写清楚，而不是硬推一个猜测的计划。
        """
        asked_once = False
        clarifications = "\n".join(self.task.user_decisions)
        # mentor 只要摘要，不要产物正文路径：给它路径它会去读，实测算过 15 轮全用在读历史
        prior_summary = build_session_summary(self.session)
        last_problems: List[str] = []
        rounds = 0
        while True:
            if rounds >= PLANNING_ROUNDS:
                if last_problems:
                    return PlanningResult(
                        mode="unable",
                        reason="Mentor 给出的计划始终无法执行（" + "；".join(last_problems)
                        + "），已停止：请重试，或把需求拆小后再说一次。",
                    )
                return PlanningResult(
                    mode="unable",
                    reason=f"规划连续 {rounds} 轮仍未收敛，已停止：请把需求拆小或补充关键信息后重试。",
                )
            rounds += 1
            self.check_cancel()
            request = build_request(
                "plan",
                self.task,
                prior_summary=prior_summary,
                clarifications=clarifications,
                plan_feedback="\n".join(last_problems),
            )
            result, updates = self.dispatch_node("mentor", request)
            if updates:
                clarifications = append(clarifications, "\n".join(updates))
                continue
            if result.mode == "plan" and result.plan is not None:
                last_problems = self.plan_problems(result.plan)
                if last_problems:
                    # 计划不合法就退回重做：不替模型改 worker，也不挑一个默认角色顶上
                    self.emit("plan_rejected", problems=last_problems)
                    continue
                last_problems = []
            if result.mode != "clarify":
                return result
            if asked_once:
                pending = "；".join(str(item.get("question") or "").strip() for item in result.questions if item.get("question"))
                return PlanningResult(
                    mode="unable",
                    reason="已集中询问一次仍未明确：" + (pending or "Mentor 未能确定必要信息")
                    + "。请补齐这些信息后重试。",
                )
            asked_once = True
            answers = self.clarifier.ask_clarification(result.questions)
            clarification = "\n".join(
                f"问题：{item.get('question', '')}\n回答：{answer}"
                for item, answer in zip(result.questions, answers)
            )
            self.task.user_decisions.append(clarification)
            clarifications = append(clarifications, clarification)

    def non_plan_result(self, result: PlanningResult) -> Optional[object]:
        """unable → 直接收尾返回 TaskResult；plan → None。

        没有"直接回答"这条路径：不需要动手的短问答也走一步计划，照常执行与评审。
        """
        if result.mode == "unable":
            return self.finalize_task(result.reason)
        if result.plan is None:
            raise PlanningError("Mentor 未返回有效计划")
        return None

    def activate_plan(self, plan: Plan) -> Plan:
        """把计划落到实际worker上、记为当前计划，并推给页面。

        ``plan_ready`` 事件带**整份计划**（每步的worker与目标），页面据此渲染计划面板；
        它也是"当前在哪一步"的基准——之后每个 ``step_start`` / ``step_review`` 都按 step 序号对号入座。
        """
        problems = self.plan_problems(plan)
        if problems:
            raise PlanningError("计划不合法：" + "；".join(problems))
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
                }
                for index, step in enumerate(plan.steps)
            ],
        )
        return plan

    # ---------------------------------------------------------------- 步骤推进
    def step_loop(self, plan: Plan):
        """主链：逐步推进——每步单独执行、单独评审，通过后才进入下一步。

        用户中止时不再抛异常丢弃一切：**停在当前工具调用之后**，把已完成的部分连同
        当前步的交代写成中止说明，作为本次任务的终态（用户之后可以据此接着改）。

        某一步做不到就**就地停止**并发一份总结汇报：不重试整条计划。要不要改剩余计划
        由评审的 Mentor 用 ``plan_update`` 决定（见 :meth:`update_remaining_plan`）。
        问题描述与进度的落盘由 :meth:`fail_task` 负责。
        """
        if self.stopping():
            return self.finalize_task(self.stop_content(None), status="aborted")
        total = len(plan.steps)
        index = 0
        while index < total:
            step = plan.steps[index]
            result, review, failures, interrupted = self.run_step(step, index, total)
            if self.stopping():
                # 节点已在工具边界收尾并交代了做了什么：直接按中止结束，不再评审/返修
                self.task.mark_step(index, "running")  # 标成"做到一半被打断"，明细里看得出停在哪
                return self.finalize_task(self.stop_content(result), status="aborted")
            if review.complete and review.approved:
                self.update_remaining_plan(plan, index + 1, Plan(), complete=True)
                return self.finalize_task(self.compose_final_answer(result.summary or ""))
            if review.plan_update is not None:
                start = index + 1 if review.approved else index
                plan = self.update_remaining_plan(plan, start, review.plan_update)
                total = len(plan.steps)
                index = start
                continue
            if review.approved and not interrupted:
                index += 1
                continue
            return self.fail_task(describe_step_failure(index, step, result, failures))
        # 完成时 worker 只给路径、不复述正文（正文在产出文件里），这里用"产出清单"兜底，
        # 保证终态 content 不会是空的
        last = self.task.step_results[-1] if self.task.step_results else None
        content = (last.content if last else "") or last.summary if last else ""
        if not content:
            produced = [name for step in self.task.steps for name in (step.files or [])]
            content = "任务已完成。" + (f"产出：{'、'.join(produced[:6])}" if produced else "")
        return self.finalize_task(self.compose_final_answer(content))

    def artifact_excerpts(self, files: List[str], *, limit: int = 3, chars: int = 2000) -> List[str]:
        """读交付文件正文（文本类），给收尾当素材：概述之外还需要能引用的事实。"""
        workspace = self.runtime.get("workspace") or ""
        if not workspace:
            return []
        root = Path(workspace)
        excerpts: List[str] = []
        for name in files[:limit]:
            relative = str(name).replace("\\", "/")
            if relative.startswith("internal/"):
                continue
            path = root / relative
            if not path.is_file() or path.suffix.lower() not in {".md", ".txt", ".csv"}:
                continue
            body = path.read_text(encoding="utf-8", errors="replace").strip()
            if body:
                excerpts.append(f"【交付文件 {relative} 的正文（节选）】\n{body[:chars]}")
        return excerpts

    def compose_final_answer(self, fallback: str = "") -> str:
        """所有步骤都通过后，请 Mentor 写一段**直接回答用户问题**的正文。

        原先收尾直接用最后一步的 summary（"已完成 xxx.md"），读起来像工单回执；
        这里把用户最初的请求、实际计划、各步结论与产出一并交给 Mentor，要求先给结论
        再给支撑。调用失败就退回 ``fallback``，不让收尾把已有成果赔进去。
        """
        plan = self.task.plan
        outline = "\n".join(
            f"{index + 1}. {step.goal}" + (f"（{step.worker}）" if step.worker else "")
            for index, step in enumerate(plan.steps)
        ) if plan is not None and plan.steps else ""
        summaries: list[str] = []
        files: list[str] = []
        limits: list[str] = []
        for position, result in enumerate(self.task.step_results, start=1):
            body = str(result.content or result.summary or "").strip()
            if body:
                summaries.append(f"{position}. {body[:1200]}")
            for name in result.files or []:
                if name not in files:
                    files.append(name)
            for note in result.limitations or []:
                if note not in limits:
                    limits.append(note)
        if not summaries and not files:
            return fallback
        # worker 的 content 只是概述（正文在交付文件里），所以把交付文件正文一并交给收尾，
        # 否则回答只能复述"文件说明了什么"，给不出具体事实。
        excerpts = self.artifact_excerpts(files)
        request = FinalRequest(
            task=self.task.original_request or self.task.requirement,
            plan_outline=outline,
            step_summaries="\n".join(summaries + excerpts),
            files=files,
            limitations=limits,
            prior_summary=build_session_summary(self.session),
        )
        try:
            answer, _updates = self.dispatch_node("mentor", request)
        except Exception:  # noqa: BLE001 - 收尾失败不该否定已完成的成果
            return fallback
        content = str(getattr(answer, "content", "") or "").strip()
        return content or fallback

    def update_remaining_plan(self, plan: Plan, start: int, remaining: Plan, *, complete: bool = False) -> Plan:
        """保留已通过前缀，只替换尚未完成步骤；无需再次调用规划模型。"""
        if complete and start >= len(plan.steps):
            return plan
        if not remaining.steps and not complete:
            raise PlanningError("剩余计划不能为空；结束任务必须由Mentor明确确认目标达成")
        if remaining.steps:
            problems = self.plan_problems(remaining)
            if problems:
                raise PlanningError("计划不合法：" + "；".join(problems))
        # A repair plan must not silently discard later file deliveries.
        workspace = self.runtime.get('workspace')
        if workspace and not self.task.user_decisions and not complete:
            root = Path(workspace)
            replacement = '\n'.join(item.expected_output for item in remaining.steps)
            mentioned = {match.group(0).replace('\\', '/').casefold()
                         for match in _OUTPUT_FILE_RE.finditer(replacement)}
            preserved = []
            for old in plan.steps[start:]:
                files = [match.group(0).replace('\\', '/') for match in _OUTPUT_FILE_RE.finditer(old.expected_output)]
                if any(not output_is_mentioned(name, mentioned) and not output_file_exists(root, name) for name in files):
                    preserved.append(old)
            if preserved:
                remaining = remaining.model_copy(update={'steps': [*remaining.steps, *preserved]})
                self.emit('plan_deliveries_preserved', count=len(preserved),
                          reason='返修计划遗漏尚未交付文件，保留对应后续步骤')
        self.task.plan_history.append({"from_index": start, "plan": plan.model_dump(), "replaced_steps": [step.model_dump() for step in self.task.steps[start:]]})
        for index, step in enumerate(plan.steps[start:], start):
            worker = self.workers.get(step.worker)
            if hasattr(worker, "clear_step"):
                worker.clear_step(str(self.runtime.get("workspace") or ""), f"{self.task.task_seq}:{index}")
        merged = Plan(requirement=remaining.requirement or self.task.requirement, output_name=remaining.output_name or plan.output_name, steps=[*plan.steps[:start], *remaining.steps])
        self.task.plan = merged
        self.task.requirement = merged.requirement
        self.task.steps[start:] = [TaskStep(index=index, goal=step.goal, expected_output=step.expected_output, worker=step.worker) for index, step in enumerate(merged.steps[start:], start)]
        self.task.output_name = merged.output_name
        self._files_cache = None
        self.emit("plan_ready", requirement=self.task.requirement, steps=[{"index": index, "worker": step.worker, "goal": step.goal} for index, step in enumerate(merged.steps)])
        self.emit("plan_replanned", index=start, reason="Mentor根据当前进度调整剩余计划", steps=len(merged.steps))
        return merged

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
        # 本步交给谁：计划校验（plan_problems）已保证这个 worker 存在
        worker = step.worker
        if worker not in self.workers:
            raise PlanningError(f"步骤{index + 1}派给了不存在的 worker：{worker or '（未指定）'}")
        feedback = ""
        failures: List[ReviewResult] = []
        attempts: List[StepAttempt] = []
        errors: List[str] = []
        result: Optional[ExecutionResult] = None
        review: Optional[ReviewResult] = None
        #: 这一步做不下去了（评审连续不通过 / 输出无法解析 / worker自认做不到）
        interrupted = False
        while True:
            self.emit("step_start", index=index, total=total, attempt=len(failures), goal=step.goal, worker=worker)
            try:
                request = build_request(
                    "step",
                    self.task,
                    step=step,
                    index=index,
                    feedback=feedback,
                )
                result, executed_updates = self.dispatch_node(worker, request)
            except ExecutionError as error:
                errors.append(f"结构化输出解析失败：{error}")
                leftover = list(getattr(error, "records", []) or [])
                if leftover:
                    # 解析失败前工具真的执行过：把产出记下来，别让这一步彻底空白。
                    # 否则 summary.json 里这一步只有 errors、attempts 为空，复盘看不到它做过什么。
                    produced = produced_files(leftover)
                    attempt_result = ExecutionResult(
                        status="partial",
                        content="",
                        summary=f"输出无法解析，但已执行 {len(leftover)} 次工具调用"
                                + (f"，产出 {len(produced)} 个文件。" if produced else "。"),
                        files=produced,
                        limitations=[f"结构化输出解析失败：{error}"],
                        tool_calls=list(leftover),
                    )
                    attempts.append(StepAttempt(attempt=len(attempts), result=attempt_result))
                    self.emit("step_attempt_recovered", index=index,
                              calls=len(leftover), files=len(produced))
                review = ReviewResult(approved=False, comment=str(error))
                failures.append(review)
                self.emit(
                    "step_review", index=index, approved=False, summary="执行输出无法解析", attempt=len(failures)
                )
                if len(failures) >= self.step_max_retries:
                    result = attempts[-1].result if attempts else ExecutionResult(
                        status="partial",
                        content="",
                        summary="执行输出多次无法解析，当前步骤无法继续。",
                        limitations=[f"结构化输出解析失败：{error}"],
                    )
                    result.limitations.append("执行输出多次无法解析，当前步骤无法继续。")
                    interrupted = True
                    break
                feedback = f"上一步执行未能返回合法结果（{error}）。请重新执行本步，确保最终输出为合法 JSON。"
                continue
            attempt = StepAttempt(attempt=len(attempts), result=result)
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
                feedback = "用户在执行期间补充了要求：\n" + "\n".join(f"- {item}" for item in executed_updates)
                continue
            if result.budget_exhausted and not self.stopping():
                # 配额用尽：交给 Mentor 做阶段审核（继续做 / 改剩余计划 / 停下），
                # 而不是直接判失败——它比"跑满固定轮次"更早发现方向问题。
                stage, stage_updates = self.review_stage(step, result, index)
                if stage_updates:
                    feedback = "用户补充了要求：\n" + "\n".join(f"- {item}" for item in stage_updates)
                    continue
                if stage.action == "stop":
                    review = ReviewResult(
                        approved=False,
                        comment=stage.comment or "工具调用配额用尽，阶段审核决定停下。",
                    )
                    result.limitations.append(f"阶段审核决定停下：{stage.comment or '（未说明原因）'}")
                    failures.append(review)
                    self.emit("step_review", index=index, approved=False,
                              summary=review.comment, attempt=len(attempts))
                    interrupted = True
                    break
                if stage.action == "adjust" and stage.plan_update is not None:
                    problems = self.plan_problems(stage.plan_update)
                    if problems:
                        self.emit("plan_rejected", problems=problems)
                        # 计划不合法：退回让审核重出一次；仍不合法就按停下处理
                        stage, stage_updates = self.review_stage(
                            step, result, index, feedback="；".join(problems)
                        )
                    if stage.action == "adjust" and stage.plan_update is not None:
                        # 从当前步开始替换：这一步还没做完，它正是要改的那一步
                        self.update_remaining_plan(self.task.plan, index, stage.plan_update)
                        step = self.task.plan.steps[index]
                    elif stage.action == "stop":
                        review = ReviewResult(
                            approved=False,
                            comment=stage.comment or "工具调用配额用尽，阶段审核决定停下。",
                        )
                        result.limitations.append(f"阶段审核决定停下：{stage.comment or '（未说明原因）'}")
                        failures.append(review)
                        interrupted = True
                        break
                    feedback = stage.hint or feedback
                    continue
                # continue（或 adjust 未给计划）：追加配额后接着做这一步
                extra = stage.extra_budget or DEFAULT_EXTRA_BUDGET
                step.tool_budget = max(step.tool_budget, len(result.tool_calls)) + extra
                feedback = stage.hint or feedback
                continue
            if result.status == "needs_input":
                answer = self.ask_user(result.question or "请补充信息以继续")
                feedback = build_rework_prompt(
                    ReviewResult(approved=False, comment=f"用户补充：{answer}"), result, failures
                )
                continue
            # 每一步都必须过 Mentor（不看是谁派的、也不接受"这步不用审"的声明）
            # 评审要看产出文件的**实际正文**，不能只看worker的自述
            artifact = ""  # Mentor先看进度摘要与路径，详细产物按需读取。
            review_request = build_request(
                "review",
                self.task,
                step=step,
                result=result,
                index=index,
                artifact=artifact,
            )
            review_request.prior_summary = session_history
            review_request.previous_review = failures[-1] if failures else None
            # 评审带回来的剩余计划同样要校验：worker 写错/漏写就退回让它重出，
            # 不替它改计划，也不挑一个默认角色顶上。
            plan_repairs = 0
            while True:
                review, review_updates = self.dispatch_node("mentor", review_request)
                problems = self.plan_problems(review.plan_update) if review.plan_update is not None else []
                if not problems:
                    break
                plan_repairs += 1
                self.emit("plan_rejected", problems=problems)
                if plan_repairs > PLAN_REPAIR_LIMIT:
                    review = ReviewResult(
                        approved=False,
                        comment="剩余计划无法执行：" + "；".join(problems),
                    )
                    review_updates = []
                    break
                review_request = review_request.model_copy(
                    update={"plan_feedback": "；".join(problems), "previous_review": review}
                )
            if review_updates:
                review = ReviewResult(approved=False, comment="审核期间收到新要求，当前结果须按最新要求修订。")
            if review.approved and self.runtime.get('workspace'):
                missing=missing_step_outputs(self.runtime['workspace'],step.expected_output,result.files)
                if missing:
                    review=review.model_copy(update={'approved':False,'complete':False,
                        'comment':'明确交付文件尚未生成，不能通过本步：'+'、'.join(missing),
                        'findings':[*review.findings,ReviewFinding(severity='blocking',
                            criterion=step.expected_output,location='、'.join(missing),
                            evidence='本步审核时这些项目文件不存在，且没有本次回执中的唯一完整路径文件可承接。',
                            correction='继续生成缺失文件并核验实际产物；不要仅提交制作计划或草稿。')]})
                    self.emit('step_delivery_check_failed',index=index,files=missing)
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
                feedback = "用户补充了要求：\n" + "\n".join(f"- {item}" for item in review_updates)
                continue
            if review.approved or review.plan_update is not None:
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
        if review.approved:
            worker = self.workers[worker]
            if hasattr(worker, "clear_step"):
                worker.clear_step(str(self.runtime.get("workspace") or ""), f"{self.task.task_seq}:{index}")
            output_file = self.persist_step_output(index, step, result)
            self.task.accept_step(
                index, result, output_file=output_file, review=review, artifact=artifact
            )
            self.write_detail()
        elif attempts:
            # 没通过的步骤同样落盘：这份是"做到哪、卡在哪"的唯一完整记录，下一轮按路径查阅
            self.task.step_of(index).output_file = self.persist_step_output(index, step, attempts[-1].result)
            self.write_detail()
        return result, review, failures, interrupted

    def review_stage(
        self,
        step: PlanStep,
        result: ExecutionResult,
        index: int,
        feedback: str = "",
    ) -> tuple[StageReview, list[str]]:
        """工具调用配额用尽时的阶段审核。

        ``feedback`` 用来把"上一次给的计划不合法"退回给它重出——不替它修计划，
        也不挑一个默认角色顶上（与评审的 plan_update 同一套校验）。
        """
        request = build_request("stage", self.task, step=step, result=result, index=index)
        request.prior_summary = build_session_summary(self.session)
        request.plan_feedback = feedback
        self.emit("stage_review", index=index, step=step.goal,
                  used=len(result.tool_calls), budget=step.tool_budget)
        return self.dispatch_node("mentor", request)

    # ---------------------------------------------------------------- 收尾
    def stop_content(self, latest: Optional[ExecutionResult]) -> str:
        """用户停止时的摘要：停在哪一步、已经做完了什么、被停下的这一步做到哪。

        这段既是任务终态（进对话），也是下一次规划能看到的"上次停在哪儿"的依据——
        所以要写清已完成步骤的结果与产出，而不是只说一句"任务已停止"。
        """
        current = None
        for position, step in enumerate(self.task.steps):
            if step.status == "running":
                current = (position, step)
        # 先落中止原因，再汇总产出：collect_files 靠 abort_reason 决定要不要把"未通过那次
        # 尝试的产出"也算进来——被打断那一步做出来的半成品正是用户最想接手的东西。
        if not self.task.abort_reason:
            reason = getattr(self, "interruption_reason", None)
            base = (reason() if reason else "") or "用户停止了任务"
            self.task.abort_reason = base if current is None else f"{base}（停在第 {current[0] + 1} 步）"
        done = [step for step in self.task.steps if step.status == "completed"]
        produced = [name for name in self.collect_files() if not str(name).replace("\\", "/").startswith("internal/")]
        if current is not None:
            position, step = current
            goal = (step.goal or "").strip()
            if len(goal) > 38:
                goal = goal[:38].rstrip("，、；：（）(） ") + "…"
            head = f"【用户停止】已按用户要求停止，停在第 {position + 1} 步" + (f"（{goal}）" if goal else "") + "。"
        else:
            head = "【用户停止】已按用户要求停止。"
        lines = [head]
        if done:
            lines.append("已完成并通过评审的步骤：")
            for step in done:
                files = f"；产出：{'、'.join(step.files)}" if step.files else ""
                lines.append(f"- 步骤{step.index + 1}：{step.goal} —— {step.summary or '已通过'}{files}")
        if latest is not None:
            did = lead(latest.summary or latest.content or "", STEP_SUMMARY_CHARS)
            lines.append(f"被停止的这一步已做到：{did or '尚无可用结果'}")
            if latest.files:
                lines.append(f"这一步已产出的文件：{'、'.join(latest.files)}")
        if produced:
            lines.append(f"全部产出文件（可能未通过评审，可直接取用）：{'、'.join(produced[:8])}")
        else:
            lines.append("本次还没有产出文件。")
        return "\n".join(lines)

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
        # 交付清单只列用户能直接用的成果：internal/ 下是本任务的中间脚本与核对记录
        self._files_cache = [
            name for name in self._files_cache if not str(name).replace("\\", "/").startswith("internal/")
        ]
        workspace = self.runtime.get('workspace')
        if workspace:
            root = Path(workspace).resolve()
            existing = []
            for name in self._files_cache:
                target = (root / name).resolve()
                if target.is_relative_to(root) and target.is_file():
                    existing.append(name)
            self._files_cache = existing
        return list(self._files_cache)

    def recent_artifacts(self) -> List[str]:
        """兜底：本任务开始后出现在工作目录（顶层）的文档，避免"文件已生成却报无成果"。"""
        if self.artifact_dir is None or not self.started_at or not self.artifact_dir.is_dir():
            return []
        found: List[str] = []
        for path in sorted(self.artifact_dir.iterdir()):
            if not path.is_file() or path.suffix.lower() not in ARTIFACT_SUFFIXES:
                continue
            if path.stat().st_mtime >= self.started_at:
                found.append(str(path))
        return found

    def persist_step_output(self, index: int, step: PlanStep, result: ExecutionResult) -> str:
        """Checkpoint full step evidence in the single session log and return its field locator."""
        if self.detail_store is None or not self.session.name:
            return ''
        self.write_detail()
        return self.detail_store.save_output(self.session.name, self.task.task_seq, index, result)

    def write_detail(self) -> int:
        """Update this task's full record inside the session log."""
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

    def clean_task_files(self) -> List[str]:
        """收尾仅清理本任务在专用 ``internal/tmp/`` 中产生的临时文件。

        internal 中的源码、配方、运行日志和压缩证据仍可能被交付物引用，不能按时间删除。
        ``persist_step_output`` 写的是会话目录，不受影响。
        """
        workspace = self.runtime.get("workspace") or ""
        if not workspace:
            return []
        internal = Path(workspace) / "internal" / "tmp"
        if not internal.is_dir():
            return []
        removed: List[str] = []
        for path in sorted(internal.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if path.is_file() and path.stat().st_mtime >= self.started_at:
                path.unlink()
                removed.append(path.relative_to(Path(workspace)).as_posix())
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        return removed

    def tidy_internal(self) -> List[str]:
        """把**以前任务**留在 ``internal/`` 顶层的中间文件挪进 ``internal/archive/``。

        本次任务的文件留在原位（交付物可能引用 ``internal/xxx.py``），更早的挪走并限量，
        否则每跑一次任务就多几个脚本，项目目录会越来越乱。
        """
        workspace = self.runtime.get("workspace") or ""
        if not workspace or not self.started_at:
            return []
        root = Path(workspace) / "internal"
        if not root.is_dir():
            return []
        archive = root / "archive"
        moved: List[str] = []
        for path in sorted(root.iterdir()):
            if path.name in {"tmp", "archive"} or not path.is_file():
                continue
            if path.stat().st_mtime >= self.started_at - 1:
                continue  # 本次任务的中间产物留在原位
            archive.mkdir(parents=True, exist_ok=True)
            target = archive / path.name
            index = 2
            while target.exists():
                target = archive / f"{path.stem}-{index}{path.suffix}"
                index += 1
            path.replace(target)
            moved.append(target.relative_to(Path(workspace)).as_posix())
        # 归档限量：留最近的一批，更早的删掉（旧交付物早已交付，这里只是可查性）
        kept = sorted(archive.glob("*"), key=lambda item: item.stat().st_mtime, reverse=True)
        for path in kept[INTERNAL_ARCHIVE_LIMIT:]:
            path.unlink()
        return moved

    def finalize_task(self, content: str, status: str = "completed") -> TaskResult:
        """任务收尾：写明细 → 生成并并入任务摘要 → 保存会话 → 返回终态结果。

        **幂等**：同一个任务只会记账一次——中止路径上编排层与应用层都可能来收尾，
        重复调用只返回已有结果，避免 ``summary.json`` 里出现同一个任务的两条摘要。

        ``status="aborted"``（用户中止）时把"停在哪、已完成了什么、被停下的这一步做到哪"
        写成一段摘要交给对话与下一次规划；完整尝试与工具轨迹仍在 ``summary.json 的
        records 字段`` 里。
        """
        if self._finalized:
            return TaskResult(content=self.task.content, step_results=list(self.task.step_results), status=self.task.status)
        workspace = self.runtime.get('workspace')
        if status == 'completed' and workspace:
            expected = self.task.output_name or ''
            if self.task.user_decisions and self.task.plan:
                expected = '\n'.join(step.expected_output for step in self.task.plan.steps)
            missing = missing_step_outputs(workspace,expected,self.collect_files())
            if missing:
                status = 'failed'
                self.task.abort_reason = '声明交付文件缺失：' + '、'.join(dict.fromkeys(missing))
                content = '任务尚未完成。' + self.task.abort_reason + '。已保留现有产物与日志。'
                self.emit('delivery_check_failed', files=missing)
        if status == "aborted":
            if not self.task.abort_reason:
                reason = getattr(self, "interruption_reason", None)
                self.task.abort_reason = (reason() if reason else "") or "用户停止了任务"
            # 摘要既是对话里的终态，也是下一次规划可见的"上次停在哪"
            content = content or "【用户停止】已按用户要求停止。"
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
        self.clean_task_files()
        self.tidy_internal()
        self.write_detail()
        summary = build_task_summary(self.task)
        if self.task.user_decisions:
            summary.task_summary += "\n用户补充与决定：\n" + "\n".join(self.task.user_decisions)
        summary.interruption_log = []  # 完整中断上下文只存任务明细，摘要保留读取索引。
        existing = next((i for i, item in enumerate(self.session.tasks) if item.task_seq == summary.task_seq), None)
        if existing is None:
            self.session.add_task(summary)
        else:
            self.session.tasks[existing] = summary
        self.save_session()
        for worker in self.workers.values():
            if hasattr(worker, "step_contexts"):
                worker.step_contexts.clear()
        self._finalized = True
        return TaskResult(content=content, step_results=list(self.task.step_results), status=status)

    def fail_task(self, reason: str) -> TaskResult:
        """任务失败（异常 / 某步做不到）时的收尾：生成一份**总结汇报**并落盘。

        汇报写进 ``content`` → 同时进 ``summary.json 的 records 字段`` 与 ``summary.json``：用户看得出"做到哪、
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
        步骤也稳定）；否则照旧走 Mentor。

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


"""装配与离线入口：把注册表装配成可运行的 Agent，并提供不调用模型的预设流程。

一个文件两块：
- 装配：读注册表 → 构造 Mentor/Worker → 各自绑定工具与技能；生成环境说明、交付复制、装配缓存
- 离线预设：``PRESETS`` / ``RUNNERS`` / ``run_preset``（公文排版、PPT 套版、数据绘图，断网可用）
"""




# ---------------------------------------------------------------- assembly
@dataclass
class AgentRegistry:
    """启动时装配好的全部角色：编排层按名字统一调度。"""

    #: 角色名 → 已装配的 agent（Mentor / 各Worker）
    nodes: dict[str, AgentNode]
    #: worker定义（注册表顺序：name / description / body / path），给 Mentor 做分派依据
    specs: list[dict] = field(default_factory=list)
    #: 装配期错误（缺文件、坏条目…），由应用层推给页面
    errors: list[str] = field(default_factory=list)

    @property
    def worker_names(self) -> list[str]:
        """所有 worker 的名字（注册表顺序）。"""
        return [spec["name"] for spec in self.specs]


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
    entries, errors = load_registries(settings.agent_dir)
    nodes: dict[str, AgentNode] = {}

    core_entries = [entry for entry in entries if entry.get("kind") == "core"]
    worker_entries = [entry for entry in entries if entry.get("kind") != "core"]
    mentor_entry = next((entry for entry in core_entries if entry["name"] == CORE_ROLES[0]), None)
    if mentor_entry is None:
        errors.append(f"缺少核心角色：{CORE_ROLES[0]}（backend/agents/agents.yaml 里没有）")

    mentor = MentorAgent(
        provider,
        temperature=settings.temperature,
        max_tool_rounds=int(mentor_entry.get("max_tool_rounds") or settings.mentor_rounds) if mentor_entry else settings.mentor_rounds,
        max_steps=settings.plan_max_steps,
    )
    mentor.extra_system = build_worker_index(worker_entries) + plan_limits(settings.plan_max_steps)
    if mentor_entry is not None:
        errors.extend(assemble_agent(mentor, settings))
    nodes["mentor"] = mentor

    for entry in worker_entries:
        name = entry["name"]
        if name in CORE_ROLES:
            errors.append(f"角色名与固定角色冲突：{name}（已跳过）")
            continue
        agent = WorkerAgent(
            provider,
            name=name,
            description=str(entry.get("description") or ""),
            temperature=settings.temperature,
            max_tool_rounds=int(entry.get("max_tool_rounds") or settings.max_tool_rounds),
        )
        errors.extend(assemble_agent(agent, settings))
        nodes[name] = agent

    return AgentRegistry(
        nodes=nodes,
        specs=[entry for entry in worker_entries if entry["name"] in nodes],
        errors=errors,
    )


def build_worker_index(specs: list) -> str:
    """可用worker清单（名字 + 职责）：装配时拼进 Mentor 的 system 固定层。

    只给名字与一句职责——技能清单由各角色自己装配，Mentor 派活不需要知道别人注册了哪些技能。
    """
    lines = [f"- {spec['name']}：{spec.get('description') or ''}" for spec in specs]
    if not lines:
        return ""
    return "【可用worker】\n" + "\n".join(lines) + (
        "\n为每个步骤指定 worker，且必须从上面这份清单里选一个名字：写错名字或不写，整个计划会被退回重做，不会替你回退到别的角色。"
        "技能只能从该worker自己注册的技能清单里选，不能为了通用数据处理而忽略明确的专业技能归属。"
    )


def plan_limits(max_steps: int) -> str:
    """限制计划规模，按真实依赖选择合并。"""
    return (
        f"\n【计划规模】最多 {max_steps} 步。一次角色能直接交付的相关成果合为一步。"
        "研究分析、论文阅读与实验设计由researcher负责；复杂处理或新复现程序由engineer负责；图件由visualizer负责，正式文档由writer负责；分析及其Markdown/引用清单由researcher一步交付，不再派writer整理。"
        "若交付包含新复现程序或自定义日志清洗，代码与生成数据统一由engineer一步负责，不先手工出表再重写程序。"
        "明确协议由执行者直接读取，不另派researcher先写规范或统计概况。步骤一律串行执行：有真实依赖的排在后面，彼此独立的也按顺序列出。"
        "Mentor 在每步进度返回时审核并调整计划，不另列核对或评审步骤。"
        "只列用户所需交付物与必要正确性条件，不扩展实验、字段或装饰性验收要求。"
        "同一角色连续制作的相关产物合为一步，例如结果图与流程图。图件所需的小型分组均值由visualizer辅助计算，不单独派engineer出表；复杂清洗、正式统计推断或复现实验才单独派engineer。"
        "模板选择和结构检查由writer在制作时完成，不派其他角色查找模板、扫描项目资产或编写模板清单。"
    )


#: 装配产物的进程内缓存：键是"角色名 + 是否worker"。
#: 实测装配一次要 1 秒（读 40+ 工具文件、解析 schema、拼 prompt），而这些内容**跨会话完全相同**；
#: 缓存后新会话只花几十毫秒。缓存里存的是不可变字符串，agent 实例仍然每会话新建（上下文有状态）。
_ASSEMBLY_CACHE: dict[str, dict] = {}


def assemble_agent(agent, settings: Settings) -> list[str]:
    """让一个 agent 装配自己（装配过就复用缓存），并把错误标上角色名。

    缓存只省"读文件 + 拼字符串"这段；`context`（对话）永远新建——那是每会话独立的状态。
    注册表、提示词、工具或技能任一改动都会让缓存作废，避免改了能力还吃旧缓存。
    """
    from backend.agents.base import MessageContext
    from backend.agents.base import ToolBox

    # Only eager assembly inputs invalidate prompts. References and scripts are read
    # lazily; traversing their full imported trees for every role is unnecessary.
    entry, _entry_errors = load_registry(settings.agent_dir, agent.name)
    inputs = [prompt_path(settings.agent_dir, entry or {"name": agent.name})]
    for name in (entry or {}).get("tools") or []:
        inputs.append(tool_path(settings.agent_dir, str(name)))
    for name in (entry or {}).get("skills") or []:
        inputs.append(skill_home(settings.agent_dir, str(name)) / "SKILL.md")

    stamp = (
        str(settings.agent_dir), str(settings.data_dir),
        settings.tool_output_limit, settings.model_context_limit,
        getattr(settings, "max_output_tokens", None), settings.context_compress_ratio,
        getattr(agent, "extra_system", ""),
        tuple(str((entry or {}).get(key)) for key in ("access", "prompt", "kind")),
        tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size)
              for path in sorted(set(inputs)) if path.is_file()),
    )
    key = f"{agent.name}:{int(agent.is_worker)}"
    cached = _ASSEMBLY_CACHE.get(key)
    if cached is not None and cached["stamp"] == stamp:
        agent.registry = entry or {}
        agent.tools = cached["tools"]
        agent.skills = cached["skills"]
        agent.tool_defs = cached["tool_defs"]
        agent.system_prompt = cached["system_prompt"]
        agent.system_tail = cached.get("system_tail", "")
        agent.toolbox = ToolBox(agent.tools, agent.skills, output_limit=settings.tool_output_limit, access=agent.registry.get("access","read-write"))
        agent.context = MessageContext(
            system_prompt=agent.system_prompt,
            suffix=agent.context.suffix,
            tail=agent.system_tail,
        )
        from backend.agents.base import DEFAULT_CONTEXT_LIMIT
        agent.context_limit = int(settings.model_context_limit or DEFAULT_CONTEXT_LIMIT)
        agent.max_output_tokens = int(getattr(settings, "max_output_tokens", 0) or 0)
        agent.compress_ratio = float(settings.context_compress_ratio or 0.0)
        agent._fixed_chars = None
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
        "system_tail": agent.system_tail,
    }
    return errors


#: 会话数据文件的形状说明在「通用规则」里（各角色 prompts/*.md 的末节），这里不再重复


#: 环境段里最多列多少个条目：再多也读不过来，超出只报总数
ENV_LISTING_LIMIT = 40


def directory_digest(workspace: Path) -> str:
    """项目目录概况：一行列完（目录带 / 后缀），省掉模型开局的 ls 探查。

    只列一层、按名字排序、超量只报总数；读不动就返回空串（环境信息不该因为权限问题报错）。
    """
    try:
        entries = sorted(Path(workspace).iterdir(), key=lambda path: path.name.casefold())
    except OSError:
        return ""
    if not entries:
        return "（空）"
    names = [f"{item.name}/" if item.is_dir() else item.name for item in entries[:ENV_LISTING_LIMIT]]
    more = len(entries) - len(names)
    return "、".join(names) + (f" …（共 {len(entries)} 项）" if more > 0 else "")


def build_environment(settings: Settings, workspace: Path, session_dir: Path, work_dir: Path) -> str:
    """当前项目的运行环境：**只有会随项目/会话变化的事实**（路径 + 平台 + 目录概况 + 时间）。

    "怎么读、怎么接续"这类规则在 system 的「通用规则」里（装配一次）；这里只回答
    "东西在哪、现在是什么情况"。目录清单与时间每轮都带上，是为了省掉模型开局那几次
    "ls 一下 / date 一下"的探查往返。``<项目目录>`` 是用户真正的工作文件夹：
    回答「目录下有什么」、读写产出文件都以它为准；``<中间目录>`` 只放运行期间的脚本与临时文件。
    """
    platform_hint = (
        "Windows；run_code仅接受应用Python解释器调用，工作目录已绑定项目目录；不使用cmd、cd /d、&&或PowerShell。文件操作使用Python库"
        if os.name == "nt"
        else "Linux/macOS"
    )
    lines = [
        "<运行环境>",
        f"<运行平台>：{platform_hint}",
        f"<Python解释器>：\"{sys.executable}\"（{sys.version.split()[0]}，运行Python脚本优先使用此解释器，已含项目依赖）",
        f"<当前时间>：{time.strftime('%Y-%m-%d %H:%M')}",
        f"<项目目录>：{workspace}（用户的工作文件夹：产出文件写这里，用户也在这里取文件；"
        "用户问「目录下有什么」「当前目录」时指的就是它）",
        f"<中间目录>：{work_dir}（运行期间的脚本与临时文件，不交给用户）",
        f"<会话目录>：{session_dir}（summary.json 为唯一会话日志，用 session=true 和 json_pointer 读取必要字段）",
    ]
    listing = directory_digest(workspace)
    if listing:
        lines.append(f"<项目目录内容>：{listing}")
    lines.append("</运行环境>")
    return "\n".join(lines)


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
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            copied.append(destination)
    return copied


# ---------------------------------------------------------------- registry
"""角色注册表与能力编辑：agents.yaml 的读写、工具/技能/提示词的加载与增删。"""






# ---------------------------------------------------------------- registry
#: agent 包内的三个能力目录与两个注册/配置文件
PROMPT_DIR = "prompts"
TOOL_DIR = "tools"
SKILL_DIR = "skills"
#: 全部角色写在一个注册表文件里：``<agent_dir>/agents/agents.yaml``
REGISTRY_FILE = "agents/agents.yaml"
#: 角色类型与实现类（注册表 ``impl`` 缺省时按 kind 推断）
KINDS = ("core", "worker")
IMPLS = ("mentor", "worker")
ACCESS_MODES = ("read-only", "read-write")
#: 产出文件的工具：``access: read-only`` 的角色声明它们即报错
WRITE_TOOLS = frozenset({"write", "delete", "move"})


def registry_path(agent_dir) -> Path:
    """角色注册表：``<agent_dir>/agents.yaml``（所有角色同一个文件）。"""
    return Path(agent_dir) / REGISTRY_FILE


def tool_path(agent_dir, name: str) -> Path:
    """工具的唯一定位：``<agent_dir>/tools/<name>.py``。"""
    return Path(agent_dir) / TOOL_DIR / f"{name}.py"


def skill_home(agent_dir, name: str) -> Path:
    """技能目录：``<agent_dir>/skills/<name>``。"""
    return Path(agent_dir) / SKILL_DIR / name


def prompt_path(agent_dir, entry: dict) -> Path:
    """角色提示词：注册表 ``prompt`` 字段相对 ``<agent_dir>/prompts/``。"""
    prompt = str(entry.get("prompt") or f"{entry.get('name', '')}.md").strip()
    return Path(agent_dir) / PROMPT_DIR / prompt


def _name_list(value, label: str, role: str) -> tuple[list[str], list[str]]:
    """注册表里的能力清单：必须是字符串数组（去重、保序）。"""
    if value is None:
        return [], []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return [], [f"{role}：{label} 必须是字符串数组"]
    names: list[str] = []
    for item in value:
        name = item.strip()
        if name and name not in names:
            names.append(name)
    return names, []


def validate_entry(agent_dir, entry: dict) -> list[str]:
    """结构校验：名字、kind/impl/access、prompt 是否存在、能力清单是否合法。"""
    role = str(entry.get("name") or "").strip()
    errors: list[str] = []
    if not role:
        return ["注册表缺少 name"]
    if entry.get("kind") not in KINDS:
        errors.append(f"{role}：kind 必须是 {'/'.join(KINDS)}，当前 {entry.get('kind')!r}")
    impl = entry.get("impl") or entry.get("kind")
    if impl not in IMPLS:
        errors.append(f"{role}：impl 必须是 {'/'.join(IMPLS)}，当前 {impl!r}")
    if entry.get("access", "read-write") not in ACCESS_MODES:
        errors.append(f"{role}：access 必须是 {'/'.join(ACCESS_MODES)}，当前 {entry.get('access')!r}")
    if not prompt_path(agent_dir, entry).is_file():
        errors.append(f"{role}：提示词缺失 {prompt_path(agent_dir, entry)}")
    tools, tool_errors = _name_list(entry.get("tools"), "tools", role)
    from backend.security.policy import settings as security_settings
    denied = set(tools) - set(security_settings()["tools"])
    if denied:
        errors.append(f"{role}：工具不在白名单：{sorted(denied)}")
    errors.extend(tool_errors)
    if entry.get("access") == "read-only":
        blocked = [name for name in tools if name in WRITE_TOOLS]
        if blocked:
            errors.append(f"{role}：access=read-only 不能声明写类工具 {'、'.join(blocked)}")
    for name in tools:
        if not tool_path(agent_dir, name).is_file():
            errors.append(f"{role}：注册表列出了工具 {name}，但 {tool_path(agent_dir, name)} 不存在")
    skills, skill_errors = _name_list(entry.get("skills"), "skills", role)
    errors.extend(skill_errors)
    for name in skills:
        if not (skill_home(agent_dir, name) / "SKILL.md").is_file():
            errors.append(f"{role}：注册表列出了技能 {name}，但 {skill_home(agent_dir, name) / 'SKILL.md'} 不存在")
    return errors


def read_all(agent_dir) -> tuple[dict[str, dict], list[str]]:
    """读整份注册表：``{角色名: 条目}``（顺序即 YAML 里的顺序）。

    文件缺失或格式错都只返回说明，不抛异常——由调用方决定当错误还是当"没有角色"。
    """
    path = registry_path(agent_dir)
    if not path.is_file():
        return {}, [f"注册表缺失：{path}"]
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as error:
        return {}, [f"注册表无法解析（{type(error).__name__}: {error}）"]
    if not isinstance(data, dict):
        return {}, ["注册表必须是 YAML 对象：顶层键是角色名"]
    entries: dict[str, dict] = {}
    errors: list[str] = []
    for role, raw in data.items():
        if not isinstance(raw, dict):
            errors.append(f"{role}：每个角色的配置必须是一个 YAML 对象")
            continue
        entry = dict(raw)
        entry["name"] = str(role)
        entry.setdefault("kind", "worker")
        entry.setdefault("impl", entry["kind"])
        entry.setdefault("access", "read-write")
        entry.setdefault("prompt", f"{role}.md")
        entries[str(role)] = entry
    return entries, errors


def load_registry(agent_dir, role: str) -> tuple[Optional[dict], list[str]]:
    """读一个角色；注册表里没有这个角色时返回 ``(None, [说明])``。"""
    entries, errors = read_all(agent_dir)
    entry = entries.get(role)
    if entry is None:
        return None, errors or [f"{role}：注册表里没有这个角色"]
    return entry, errors + validate_entry(agent_dir, entry)


def load_registries(agent_dir) -> tuple[list[dict], list[str]]:
    """全部角色（按注册表顺序）；worker 分派清单也用它。"""
    entries, errors = read_all(agent_dir)
    ordered = list(entries.values())
    for entry in ordered:
        errors.extend(validate_entry(agent_dir, entry))
    return ordered, errors


def load_tools(agent_dir, entry: dict) -> tuple[list, list[str]]:
    """按注册表 ``tools`` 列出的名字加载工具（顺序即注册表顺序）。"""
    role = str(entry.get("name") or "")
    names, errors = _name_list(entry.get("tools"), "tools", role)
    tools: list = []
    from backend.security.policy import settings as security_settings
    for name in names:
        if name not in security_settings()["tools"]:
            errors.append(f"{role}：工具 {name} 不在服务白名单")
            continue
        path = tool_path(agent_dir, name)
        if not path.is_file():
            continue  # 缺失已在 validate_entry 报过，这里不重复
        try:
            tool = parse_tool(path)
        except Exception as error:  # noqa: BLE001 - 坏工具只影响它自己
            errors.append(f"{role}：工具 {name} 无法解析（{type(error).__name__}: {error}）")
            continue
        if tool is None:
            errors.append(f"{role}：工具 {name} 未导出 information 元数据")
            continue
        if tool["name"] != name:
            errors.append(f"{role}：工具文件 {name}.py 导出的名字是 {tool['name']}，两者必须一致")
            continue
        tools.append(tool)
    tools.sort(key=lambda item: item["name"])  # 清单顺序稳定，模型看到的工具表不随注册表顺序变
    return tools, errors


def load_skills(agent_dir, entry: dict) -> tuple[list, list[str]]:
    """按注册表 ``skills`` 列出的名字加载技能（``skills/<名字>/SKILL.md``）。"""
    role = str(entry.get("name") or "")
    names, errors = _name_list(entry.get("skills"), "skills", role)
    skills: list = []
    for name in names:
        path = skill_home(agent_dir, name) / "SKILL.md"
        if not path.is_file():
            continue  # 缺失已在 validate_entry 报过
        try:
            skill = parse_skill(path)
        except Exception as error:  # noqa: BLE001
            errors.append(f"{role}：技能 {name} 无法解析（{type(error).__name__}: {error}）")
            continue
        if skill["name"] != name:
            errors.append(f"{role}：技能 {name} 的 SKILL.md 声明名是 {skill['name']}，两者必须一致")
            continue
        skill["agent"] = role
        skills.append(skill)
    return skills, errors


def load_prompt(agent_dir, entry: dict) -> tuple[str, list[str]]:
    """角色提示词正文：注册表 ``prompt`` 指向的文件。"""
    role = str(entry.get("name") or "")
    path = prompt_path(agent_dir, entry)
    if not path.is_file():
        return "", [f"{role}：提示词文件缺失：{path}"]
    return path.read_text(encoding="utf-8").strip(), []


def unlisted(agent_dir, entry: dict) -> list[str]:
    """目录里存在、但注册表没列的能力：只做诊断，永不隐式加载。"""
    role = str(entry.get("name") or "")
    tools, _ = _name_list(entry.get("tools"), "tools", role)
    skills, _ = _name_list(entry.get("skills"), "skills", role)
    notes: list[str] = []
    tool_folder = Path(agent_dir) / TOOL_DIR
    if tool_folder.is_dir():
        for path in sorted(tool_folder.glob("*.py")):
            if path.stem not in tools:
                notes.append(f"{role}：工具 {path.stem} 存在但未注册，不生效")
    skill_folder = Path(agent_dir) / SKILL_DIR
    if skill_folder.is_dir():
        for child in sorted(skill_folder.iterdir()):
            if child.is_dir() and (child / "SKILL.md").is_file() and child.name not in skills:
                notes.append(f"{role}：技能 {child.name} 存在但未注册，不生效")
    return notes


def split_frontmatter(text: str) -> tuple[dict, str]:
    """解析 SKILL.md：开头 ``---`` 块为元数据，其余为正文；失败返回空 dict + 全文。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            meta = yaml.safe_load("\n".join(lines[1:index])) or {}
            if not isinstance(meta, dict):
                raise ValueError("SKILL frontmatter 必须为YAML对象")
            return meta, "\n".join(lines[index + 1:]).strip()
    raise ValueError("SKILL frontmatter 缺少结束分隔符")


def parse_tool(path: Path) -> Optional[dict]:
    """解析一个工具文件：必须导出 ``information`` 与同名可调用对象。

    ``tools/`` 下每个 ``.py`` 都必须是一个工具——没有"内部模块"这回事
    （工具的实现写在工具文件内部）。
    """
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
        "title": str(meta.get("title") or name).strip(),
        "description": str(meta.get("description") or "").strip(),
        "path": str(path.resolve()),
    }


#: 已加载工具模块的缓存：键为 (路径, mtime)，避免每次切会话都重新执行顶层代码
_MODULE_CACHE: dict = {}


def load_module(path: Path):
    """按文件路径动态加载模块（工具实现与技能脚本共用）。

    模块名取 ``agent/`` 之后的相对路径，例如 ``tools/read.py`` →
    ``peifeng_tool_tools_read``、``skills/data-analysis/scripts/foo.py`` →
    ``peifeng_tool_skills_data-analysis_scripts_foo``；按 (路径, mtime) 缓存，
    文件改了才重新加载。
    """
    module_name = f"peifeng_tool_{'_'.join(relative_slug(path))}"
    try:
        stamp = path.stat().st_mtime
    except OSError:
        stamp = 0.0
    cached = _MODULE_CACHE.get((str(path), stamp))
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    if path.parent.name == "scripts" and path.parent.parent.parent.name == "skills":
        # 技能库保持只读与平铺；模块缓存在内存中，不生成磁盘__pycache__。
        exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    else:
        spec.loader.exec_module(module)
    _MODULE_CACHE[(str(path), stamp)] = module
    return module


def relative_slug(path: Path) -> list[str]:
    """``agent/`` 之后的路径片段（去掉扩展名），用来拼唯一模块名。"""
    parts = list(path.with_suffix("").parts)
    for index in range(len(parts) - 1, -1, -1):
        if parts[index] == "agent":
            parts = parts[index + 1:]
            break
    else:
        parts = parts[-2:]
    return [part for part in parts if part not in {"", ".", "..", "\\"}]


# ---------------------------------------------------------------- capabilities
#: 名字白名单：角色名、工具名、技能名都走这一套（防路径穿越与奇怪字符）
NAME_PATTERN = re.compile(r"^[a-z0-9_-]{1,64}$")

#: 新建角色时生成的 prompt 骨架：只放"必须自己写"的部分，通用纪律由 COMMON_RULES 提供
WORKER_PROMPT_TEMPLATE = """# Identity（身份）

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
  "files": ["成品目录内产物路径"],
  "sources": ["采用的来源"],
  "limitations": ["已知限制或未完成项"]
}}
```
"""


def check_name(value: str, label: str) -> str:
    """校验名字白名单；不合法直接报错（不静默修正——页面要能告诉用户为什么不行）。"""
    name = str(value or "").strip()
    if not NAME_PATTERN.match(name):
        raise ValueError(f"{label}只能用英文小写、数字、连字符与下划线（1~64 字符），当前：{value!r}")
    return name


def safe_inside(root: Path, relative: str) -> Path:
    """把相对路径拼到 ``root`` 下并确认没跑出去；越界直接报错。"""
    target = (Path(root) / relative).resolve()
    base = Path(root).resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"路径越界：{relative}")
    return target


def read_registry(agent_dir, role: str) -> dict:
    """读某个角色的原文（不存在则报错）；写回时保留其它角色与其它字段。"""
    entries, _errors = read_all(agent_dir)
    entry = entries.get(role)
    if entry is None:
        raise ValueError(f"角色 {role} 不存在（注册表里没有）")
    return {key: value for key, value in entry.items() if key != "name"}


def write_registry(agent_dir, role: str, data: dict) -> None:
    """写回某个角色：其余角色原样保留，字段顺序稳定，中文不转义。"""
    path = registry_path(agent_dir)
    current: dict = {}
    if path.is_file():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            current = loaded
    current[role] = {key: value for key, value in data.items() if key != "name"}
    text = yaml.safe_dump(current, allow_unicode=True, sort_keys=False, default_flow_style=None, width=200)
    path.write_text(text, encoding="utf-8")


def drop_role(agent_dir, role: str) -> None:
    """从注册表里删掉一个角色（其余角色原样保留）。"""
    path = registry_path(agent_dir)
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict) or role not in loaded:
        raise ValueError(f"角色 {role} 不存在")
    loaded.pop(role)
    text = yaml.safe_dump(loaded, allow_unicode=True, sort_keys=False, default_flow_style=None, width=200)
    path.write_text(text, encoding="utf-8")


def references(agent_dir, capability: str, kind: str, skip: str = "") -> list[str]:
    """哪些角色还在用这个工具/技能：删除前必须看引用，平铺后共享是常态。"""
    entries, _errors = load_registries(agent_dir)
    key = "tools" if kind == "tool" else "skills"
    users = []
    for entry in entries:
        if entry["name"] == skip:
            continue
        if capability in [str(item) for item in entry.get(key) or []]:
            users.append(entry["name"])
    return users


def list_all(agent_dir) -> dict:
    """三栏数据：工具列表（含被哪些角色注册）、技能（按角色展开）、角色与提示词。

    目录里存在但没注册的文件也列出来（``roles`` 为空）：页面要能看见"有文件但没生效"。
    """
    base = Path(agent_dir)
    entries, errors = load_registries(base)
    users: dict[str, list[str]] = {}
    for entry in entries:
        for name in entry.get("tools") or []:
            users.setdefault(str(name), []).append(entry["name"])

    tools: list[dict] = []
    for name in sorted(users):
        path = tool_path(base, name)
        tools.append(
            {
                "name": name,
                "description": _tool_description(path),
                "path": str(path),
                "exists": path.is_file(),
                "roles": sorted(users[name]),
            }
        )
    if (base / TOOL_DIR).is_dir():
        for path in sorted((base / TOOL_DIR).glob("*.py")):
            if path.stem in users:
                continue
            tools.append(
                {
                    "name": path.stem,
                    "description": _tool_description(path),
                    "path": str(path),
                    "exists": True,
                    "roles": [],
                }
            )

    skill_users: dict[str, list[str]] = {}
    for entry in entries:
        for name in entry.get("skills") or []:
            skill_users.setdefault(str(name), []).append(entry["name"])

    skills: list[dict] = []
    if (base / SKILL_DIR).is_dir():
        for home in sorted((base / SKILL_DIR).iterdir()):
            skill_file = home / "SKILL.md"
            if not home.is_dir() or not skill_file.is_file():
                continue
            skills.append(
                {
                    "name": home.name,
                    "title": _skill_title(skill_file),
                    "description": _skill_description(skill_file),
                    "path": str(skill_file),
                    "exists": True,
                    "agents": sorted(skill_users.get(home.name, [])),
                }
            )

    agents = [
        {
            "name": entry["name"],
            "kind": entry.get("kind"),
            "impl": entry.get("impl"),
            "description": entry.get("description") or "",
            "access": entry.get("access"),
            "prompt": entry.get("prompt"),
            "prompt_path": str(prompt_path(base, entry)),
            "tools": [str(item) for item in entry.get("tools") or []],
            "skills": [str(item) for item in entry.get("skills") or []],
            "errors": [item for item in errors if item.startswith(f"{entry['name']}：")],
        }
        for entry in entries
    ]
    return {"tools": tools, "skills": skills, "agents": agents}


def read_tool_source(agent_dir, name: str) -> dict:
    """读一个工具的源码（供页面查看与编辑）。"""
    tool = check_name(name, "工具名")
    path = tool_path(agent_dir, tool)
    if not path.is_file():
        raise ValueError(f"工具 {tool} 不存在")
    return {"name": tool, "path": str(path), "content": path.read_text(encoding="utf-8")}


def save_tool_source(agent_dir, name: str, content: str) -> dict:
    """保存工具源码：先按**文件名解析一遍**，坏文件不落盘。"""
    tool = check_name(name, "工具名")
    target = safe_inside(Path(agent_dir) / TOOL_DIR, f"{tool}.py")
    if not target.is_file():
        raise ValueError(f"工具 {tool} 不存在（新建请用上传）")
    _check_tool_source(tool, content)
    target.write_text(str(content), encoding="utf-8")
    return {"name": tool, "path": str(target)}


def _check_tool_source(name: str, content: str) -> None:
    body = str(content or "")
    if not body.strip():
        raise ValueError("文件内容为空")
    with tempfile.TemporaryDirectory() as folder:
        probe = Path(folder) / f"{name}.py"
        probe.write_text(body, encoding="utf-8")
        try:
            info = parse_tool(probe)
        except Exception as error:  # noqa: BLE001 - 统一成"保存被拒"的提示
            raise ValueError(f"这个文件不是合法工具：{error}") from error
    if info is None:
        raise ValueError("文件里没有可用的工具入口")
    if info["name"] != name:
        raise ValueError(f"工具名「{info['name']}」与文件名「{name}」不一致，模型按名字找工具")


def read_skill_source(agent_dir, name: str, agent: str = "") -> dict:
    """读一个技能的 SKILL.md；技能由目录名唯一定位，``agent`` 仅为兼容而保留。"""
    role = check_name(agent, "角色名") if str(agent or "").strip() else ""
    skill = check_name(name, "技能名")
    path = skill_home(agent_dir, skill) / "SKILL.md"
    if not path.is_file():
        raise ValueError(f"{role} 名下没有技能 {skill}")
    return {"agent": role, "name": skill, "path": str(path), "content": path.read_text(encoding="utf-8")}


def save_skill_source(agent_dir, name: str, content: str, agent: str = "") -> dict:
    """保存 SKILL.md：frontmatter 的 name 必须与目录名一致。"""
    role = check_name(agent, "角色名") if str(agent or "").strip() else ""
    skill = check_name(name, "技能名")
    target = safe_inside(skill_home(agent_dir, skill), "SKILL.md")
    if not target.is_file():
        raise ValueError(f"{role} 名下没有技能 {skill}")
    meta, _ = split_frontmatter(str(content or ""))
    declared = str(meta.get("name") or "").strip()
    if declared != skill:
        raise ValueError(f"frontmatter 的 name「{declared or '空'}」必须与技能目录名「{skill}」一致")
    target.write_text(str(content), encoding="utf-8")
    return {"agent": role, "name": skill, "path": str(target)}


def read_prompt(agent_dir, agent: str) -> dict:
    """读一个角色的提示词（``backend/prompts/<角色>.md``）。"""
    role = check_name(agent, "角色名")
    entry = read_registry(agent_dir, role)
    path = prompt_path(agent_dir, entry)
    if not path.is_file():
        raise ValueError(f"{role} 的提示词文件缺失：{path}")
    return {"agent": role, "path": str(path), "content": path.read_text(encoding="utf-8")}


def save_prompt(agent_dir, agent: str, content: str) -> dict:
    """保存角色提示词：不允许空文件，长度上限 200KB。"""
    role = check_name(agent, "角色名")
    entry = read_registry(agent_dir, role)
    body = str(content or "")
    if not body.strip():
        raise ValueError("提示词不能为空")
    if len(body) > 200_000:
        raise ValueError("提示词过长（超过 200KB）")
    path = prompt_path(agent_dir, entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return {"agent": role, "path": str(path)}


def _tool_description(path: Path) -> str:
    if not path.is_file():
        return "（文件缺失）"
    try:
        info = parse_tool(path)
    except Exception as error:  # noqa: BLE001 - 坏文件也要显示出来
        return f"（解析失败：{error}）"
    return str(info.get("description", "")) if info else ""


def _skill_title(path: Path) -> str:
    """能力面板显示中文名称；未声明标题的自定义技能使用目录名。"""
    try:
        return str(parse_skill(path).get("title") or path.parent.name)
    except Exception:
        return path.parent.name


def _skill_description(path: Path) -> str:
    if not path.is_file():
        return "（文件缺失）"
    try:
        return str(parse_skill(path).get("description", ""))
    except Exception as error:  # noqa: BLE001
        return f"（解析失败：{error}）"


def create_worker(agent_dir: Path, name: str, description: str, prompt: str = "") -> dict:
    """新建一个 worker 角色：写进 ``agents.yaml`` + ``prompts/<名字>.md``。"""
    role = check_name(name, "角色名")
    if role in CORE_ROLES:
        raise ValueError(f"{role} 是核心角色名，不能用作新角色")
    entries, _errors = read_all(agent_dir)
    if role in entries:
        raise ValueError(f"角色 {role} 已存在")
    summary = str(description or "").strip()
    if not summary:
        raise ValueError("请填写职责描述（Mentor 靠它决定把任务派给谁）")
    body = str(prompt or "").strip() or WORKER_PROMPT_TEMPLATE.format(description=summary)
    prompt_target = safe_inside(Path(agent_dir) / PROMPT_DIR, f"{role}.md")
    prompt_target.parent.mkdir(parents=True, exist_ok=True)
    prompt_target.write_text(body + "\n", encoding="utf-8")
    write_registry(
        agent_dir,
        role,
        {
            "kind": "worker",
            "impl": "worker",
            "description": summary,
            "prompt": f"{role}.md",
            "access": "read-write",
            "max_tool_rounds": 45,
            "tools": ["read", "write", "run_code"],
            "skills": [],
        },
    )
    return {"name": role, "path": str(registry_path(agent_dir))}


def delete_worker(agent_dir: Path, name: str) -> dict:
    """删除一个 worker 角色（核心角色与不存在的角色都拒绝）。"""
    role = check_name(name, "角色名")
    if role in CORE_ROLES:
        raise ValueError(f"{role} 是核心角色，不能删除")
    entries, _errors = read_all(agent_dir)
    if role not in entries:
        raise ValueError(f"角色 {role} 不存在")
    entry = entries[role]
    drop_role(agent_dir, role)
    prompt = prompt_path(agent_dir, entry or {"name": role})
    if prompt.is_file():
        prompt.unlink()
    return {"name": role}


def upload_tool(agent_dir: Path, agent: str, content: str, filename: str = "") -> dict:
    """上传一个工具：**先解析校验**再落盘，然后注册到该角色。

    校验三件事：名字合规、``information`` 元数据齐全、导出了与工具同名的函数。
    """
    role = check_name(agent, "角色名") if str(agent or "").strip() else ""
    entry = read_registry(agent_dir, role) if role else {}
    stem = Path(str(filename or "").strip()).stem
    name = check_name(stem, "工具名（文件名）")
    from backend.security.policy import settings as security_settings
    if name not in security_settings()["tools"]:
        raise ValueError(f"工具 {name} 不在服务白名单")
    if role and entry.get("access") == "read-only" and name in WRITE_TOOLS:
        raise ValueError(f"{role} 是只读角色（access: read-only），不能注册写类工具 {name}")
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
        raise ValueError("文件里没有可用的工具入口")
    if info["name"] != name:
        raise ValueError(f"工具名「{info['name']}」与文件名「{name}」不一致：模型按名字找工具，两者必须相同")
    target = safe_inside(Path(agent_dir) / TOOL_DIR, f"{name}.py")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    if role:
        tools = [str(item) for item in entry.get("tools") or []]
        if name not in tools:
            tools.append(name)
            entry["tools"] = tools
            write_registry(agent_dir, role, entry)
    return {"agent": role, "name": name, "path": str(target)}


def delete_tool(agent_dir: Path, agent: str, name: str) -> dict:
    """从角色注册表移除一个工具；没有任何角色再引用时才删文件。"""
    role = check_name(agent, "角色名")
    tool = check_name(name, "工具名")
    entry = read_registry(agent_dir, role)
    tools = [str(item) for item in entry.get("tools") or []]
    if tool not in tools:
        raise ValueError(f"{role} 没有注册工具 {tool}")
    entry["tools"] = [item for item in tools if item != tool]
    write_registry(agent_dir, role, entry)
    others = references(agent_dir, tool, "tool")
    removed = False
    if not others:
        target = tool_path(agent_dir, tool)
        if target.is_file():
            target.unlink()
            removed = True
    return {"agent": role, "name": tool, "deleted_file": removed, "shared_with": others}


def upload_skill(agent_dir: Path, agent: str, content: str, name: str = "") -> dict:
    """上传一个技能：``skills/<名字>/SKILL.md``，frontmatter 的 name 必须与目录名一致。"""
    role = check_name(agent, "角色名") if str(agent or "").strip() else ""
    entry = read_registry(agent_dir, role) if role else {}
    skill = check_name(name, "技能名（目录名）")
    body = str(content or "")
    meta, _ = split_frontmatter(body)
    declared = str(meta.get("name") or "").strip()
    if not declared:
        raise ValueError("SKILL.md 开头需要一个 frontmatter 块，至少写 name: <技能名>")
    if declared != skill:
        raise ValueError(f"frontmatter 的 name「{declared}」与技能目录名「{skill}」不一致")
    target = safe_inside(skill_home(agent_dir, skill), "SKILL.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body if body.endswith("\n") else body + "\n", encoding="utf-8")
    if role:
        skills = [str(item) for item in entry.get("skills") or []]
        if skill not in skills:
            skills.append(skill)
            entry["skills"] = skills
            write_registry(agent_dir, role, entry)
    return {"agent": role, "name": skill, "path": str(target)}


def delete_skill(agent_dir: Path, agent: str, name: str) -> dict:
    """从角色注册表移除一个技能；没有任何角色再引用时才删目录。"""
    role = check_name(agent, "角色名")
    skill = check_name(name, "技能名")
    entry = read_registry(agent_dir, role)
    skills = [str(item) for item in entry.get("skills") or []]
    if skill not in skills:
        raise ValueError(f"{role} 没有注册技能 {skill}")
    entry["skills"] = [item for item in skills if item != skill]
    write_registry(agent_dir, role, entry)
    others = references(agent_dir, skill, "skill")
    removed = False
    if not others:
        target = skill_home(agent_dir, skill)
        if (target / "SKILL.md").is_file():
            shutil.rmtree(target)
            removed = True
    return {"agent": role, "name": skill, "deleted_dir": removed, "shared_with": others}


def drop_tool(agent_dir, name: str) -> dict:
    """从**所有**注册表移除一个工具，再删文件（工具列表栏的"删除"用它）。"""
    tool = check_name(name, "工具名")
    entries, _errors = load_registries(agent_dir)
    for entry in entries:
        tools = [str(item) for item in entry.get("tools") or []]
        if tool in tools:
            entry["tools"] = [item for item in tools if item != tool]
            write_registry(agent_dir, entry["name"], entry)
    target = tool_path(agent_dir, tool)
    removed = False
    if target.is_file():
        target.unlink()
        removed = True
    return {"name": tool, "deleted_file": removed}


def drop_skill(agent_dir, name: str) -> dict:
    """从**所有**注册表移除一个技能，再删目录（技能列表栏的"删除"用它）。"""
    skill = check_name(name, "技能名")
    entries, _errors = load_registries(agent_dir)
    for entry in entries:
        skills = [str(item) for item in entry.get("skills") or []]
        if skill in skills:
            entry["skills"] = [item for item in skills if item != skill]
            write_registry(agent_dir, entry["name"], entry)
    target = skill_home(agent_dir, skill)
    removed = False
    if (target / "SKILL.md").is_file():
        shutil.rmtree(target)
        removed = True
    return {"name": skill, "deleted_dir": removed}


def save_agent_config(agent_dir, agent: str, tools, skills) -> dict:
    """保存某个角色的能力清单（角色栏里勾选工具与技能）。

    三条校验：名字合规、列出的工具/技能必须真实存在、``access: read-only`` 不能有写类工具。
    """
    role = check_name(agent, "角色名")
    entry = read_registry(agent_dir, role)
    names = [str(item).strip() for item in (tools or []) if str(item).strip()]
    skill_names = [str(item).strip() for item in (skills or []) if str(item).strip()]
    seen: set[str] = set()
    ordered_tools: list[str] = []
    for name in names:
        tool = check_name(name, "工具名")
        from backend.security.policy import settings as security_settings
        if tool not in security_settings()["tools"]:
            raise ValueError(f"工具 {tool} 不在服务白名单")
        if tool in seen:
            continue
        seen.add(tool)
        if not tool_path(agent_dir, tool).is_file():
            raise ValueError(f"工具 {tool} 不存在（先在工具栏上传）")
        ordered_tools.append(tool)
    ordered_skills: list[str] = []
    seen.clear()
    for name in skill_names:
        skill = check_name(name, "技能名")
        if skill in seen:
            continue
        seen.add(skill)
        if not (skill_home(agent_dir, skill) / "SKILL.md").is_file():
            raise ValueError(f"技能 {skill} 不存在（先在技能栏上传）")
        ordered_skills.append(skill)
    if entry.get("access") == "read-only":
        blocked = [name for name in ordered_tools if name in WRITE_TOOLS]
        if blocked:
            raise ValueError(f"{role} 是只读角色，不能配置写类工具 {'、'.join(blocked)}")
    entry["tools"] = ordered_tools
    entry["skills"] = ordered_skills
    write_registry(agent_dir, role, entry)
    return {"agent": role, "tools": ordered_tools, "skills": ordered_skills}
