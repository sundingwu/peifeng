"""worker Agent：执行单个步骤，可多轮调用工具（读写、执行命令、生成文档）。"""
from __future__ import annotations

import logging
from typing import List

from peifeng_agent.backend.agent.common.base import BaseAgent
from peifeng_agent.backend.utils.contracts import (
    ExecutionRequest,
    ExecutionResult,
    ToolCall,
)
from peifeng_agent.backend.utils.exceptions import ExecutionError
from peifeng_agent.backend.utils.extract_json import extract_json

logger = logging.getLogger(__name__)


def normalize_result(data: dict) -> dict:
    """兼容极简输出格式（chart 只给 status / content / file_path）。

    - ``status: "failed"`` → ``needs_replan``：做不到就重排计划，而不是整任务失败
    - 没有 ``summary`` → 用 ``content`` 顶上（下游摘要从它压）
    - 单个 ``file_path`` → ``files``（产出文件列表只有一份时用它）
    - ``failed`` 的原因写进 ``limitations``：重排与返修都能看到，不白干
    """
    if not isinstance(data, dict):
        return data
    data = dict(data)
    raw_status = str(data.get("status") or "").strip().lower()
    if raw_status == "failed":
        data["status"] = "needs_replan"
    if not str(data.get("summary") or "").strip():
        data["summary"] = str(data.get("content") or "").strip()
    file_path = str(data.get("file_path") or "").strip()
    if file_path and not data.get("files"):
        data["files"] = [file_path]
    if data.get("status") == "needs_replan":
        if not data.get("limitations"):
            reason = str(data.get("content") or "").strip()
            if reason:
                data["limitations"] = [reason]
    return data


def parse_result(text: str, tool_calls: List[ToolCall]) -> ExecutionResult:
    """把终态输出解析为执行结果，并补齐本次工具记录。"""
    try:
        data = extract_json(text)
        result = ExecutionResult.model_validate(normalize_result(data))
    except Exception as error:
        raise ExecutionError(f"Executor 输出无法解析：{error}") from error
    result.tool_calls = tool_calls
    if result.status == "needs_input" and not result.question.strip():
        raise ExecutionError("needs_input 状态缺少 question")
    if not result.summary.strip():
        raise ExecutionError("Executor 输出缺少 summary")
    return result


def produced_files(records: List[ToolCall]) -> List[str]:
    """从工具调用记录里认出本步产出过哪些文件（写类工具的 file/path 参数）。"""
    names: List[str] = []
    for record in records:
        if record.is_error or record.name not in {"docx", "xlsx", "pptx", "write"}:
            continue
        target = record.arguments.get("file") or record.arguments.get("path")
        if target and str(target) not in names:
            names.append(str(target))
    return names


#: 零工具调用却给出"已完成"时的纠正提示（只补一轮，避免与死循环叠加）
NO_TOOL_ACTION_HINT = (
    "【纠正】你这一轮没有调用任何工具就给出了结论，但本步是要动手做事的："
    "凡是需要读写文件、检索、成稿、套版的动作，必须先用工具真正执行，再按工具返回如实汇报。"
    "声称「已落盘」「已生成」「已复用」却没有任何工具记录的一律视为未完成。"
    "如果本步确实不需要工具（纯结论/纯说明），请在 summary 里写明「本步无需工具，依据是哪几条已有信息」。"
)


def exhausted_result(records: List[ToolCall], summary: str, limitations: List[str]) -> ExecutionResult:
    """轮次用尽、且收尾调用也没给出合法 JSON 时的兜底。

    **不整步作废**：只要真做过事（有工具记录），就用记录拼一个 ``partial``——把"做了什么、
    产出哪些文件"写进 content，让编排层按"部分完成"走返修或收尾；一个调用都没有才交回
    ``needs_replan``（任务就此停止，'为什么做不到'进总结汇报）。
    """
    if not records:
        return ExecutionResult(status="needs_replan", content="", summary=summary, limitations=limitations)
    recent = []
    for record in records[-6:]:
        target = record.arguments.get("path") or record.arguments.get("command") or ""
        mark = "出错" if record.is_error else "完成"
        recent.append(f"- {record.name}({str(target)[:60]}) {mark}")
    content = f"{summary}\n本轮已执行的操作（尾段）：\n" + "\n".join(recent)
    return ExecutionResult(
        status="partial",
        content=content,
        summary=summary,
        files=produced_files(records),
        limitations=[*limitations, "收尾时未给出结构化结果，已按工具记录兜底"],
        tool_calls=records,
    )


class ExecutorAgent(BaseAgent):
    """worker：执行单个步骤，内部为 LLM ↔ 工具循环。

    所有worker共用这一个类；差异来自三处：共享契约 ``prompt/executor/system.md``、
    **自己的专项提示词** ``prompt/executor/<名字>.md``（职责与边界）、以及自己目录下的
    工具与技能（``tool/executor/<名字>/``、``skill/executor/<名字>/``）。
    """

    name = "executor"
    is_worker = True
    #: 提示词只有一份：``prompt/executor/<名字>.md``（通用纪律 + 专项职责都在里面）

    def __init__(self, llm, name: str = "", description: str = "", **kwargs) -> None:
        super().__init__(llm, **kwargs)
        if name:
            self.name = name
        #: 注册表里的职责描述（给 Planner 的分派依据；也用于日志）
        self.description = description


    def execute_role(self, request: ExecutionRequest) -> ExecutionResult:
        """执行单个步骤，直到产出结构化结果。

        skill_names：无——技能由worker自己从【本角色可用技能】里挑，并用 read 读该技能正文。
        previous_results：之前步骤的上下文（细节留在任务内存态与任务明细文件里）。

        **零工具调用的"完成"要纠一次**：本步是动手做事的步骤，一次工具都没调却声称已落盘，
        是典型的幻觉完成（评审会发现没有任何产出文件，白白耗掉一次返修配额）。补一轮纠正，
        逼它要么真的动手、要么如实说"做不到/无需工具"。
        """
        self.reset()
        self.set_fixed_layer(tool_defs=self.tool_defs)
        self.context.add("user", self.build_request(request))
        result = self.run_step_turn()
        if result.status in {"completed", "partial"} and not result.tool_calls:
            self.context.add("user", NO_TOOL_ACTION_HINT)
            try:
                corrected = self.run_step_turn()
            except Exception:  # noqa: BLE001 - 纠正轮失败不该丢掉已拿到（但可疑）的结果
                logger.debug("零工具调用纠正轮失败", exc_info=True)
                return result
            return corrected
        return result

    def run_step_turn(self) -> ExecutionResult:
        """跑一轮执行回合并解析成 ``ExecutionResult``（协议与兜底同 :meth:`execute_role`）。"""
        return self.run_turn(
            parse=parse_result,
            retry_hint="上次输出不符合协议。请只输出一个合法 JSON 对象，"
            "字段为 status/content/summary/question/files/sources/limitations。",
            error_cls=ExecutionError,
            exhausted=exhausted_result,
            # 用户中止时要交代"做了什么、产出在哪、还差什么"——这段会进任务摘要，供后续接着改
            summarize_on_stop=True,
        )

    def build_request(self, request: ExecutionRequest) -> str:
        """本步 user 请求：**任务需求 → 计划（本步已标注，含交付文件）→ 已完成步骤 → 反馈**。

        计划行里已经写了本步的目标与预期输出，所以不再重复"本步内容"；交付规则
        （长结果落盘、短结果进 content）在 system 的「通用规则」里，这里只给文件名。
        """
        prompt = ""
        step = request.step
        if request.requirement:
            prompt += f"任务需求：{request.requirement}\n\n"
        if request.plan_outline:
            prompt += f"{request.plan_outline}\n\n"
        else:
            # 没有计划（单步调用）：至少把这一步要做的事写清楚
            goal = step.goal
            prompt += f"本步目标：{goal}"
            if step.expected_output:
                prompt += f"\n预期输出：{step.expected_output}"
            if request.output_name:
                prompt += f"\n交付文件：{request.output_name}"
            prompt += "\n\n"
        if request.previous_results:
            prompt += f"已完成步骤：\n{request.previous_results}\n\n"
        if request.session_history:
            prompt += f"本会话已完成任务：\n{request.session_history}\n\n"
        if request.feedback:
            prompt += f"评审反馈：{request.feedback}"
        return prompt.rstrip() + "\n"
