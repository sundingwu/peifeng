"""Worker：执行单个步骤（LLM ↔ 工具循环），按步骤隔离上下文。"""

from __future__ import annotations

import json
import logging
from copy import copy
from pathlib import Path
from typing import List
from backend.agents.base import BaseAgent, MessageContext
from backend.agents.base import (
    ExecutionRequest,
    ExecutionResult,
    ToolCall,
)
from backend.utils.errors import ExecutionError
from backend.agents.base import extract_json


# ---------------------------------------------------------------- worker
logger = logging.getLogger(__name__)


def fact_handoff(workspace: str) -> str:
    """Find bounded, task-local evidence references without replaying tool logs."""
    if not workspace or not Path(workspace).is_dir():
        return ''
    root = Path(workspace).resolve()
    candidates = sorted(set([*root.glob('*.json'), *(root/'internal').glob('*.json')]))[:64]
    lines = []
    for path in candidates:
        if not path.resolve().is_relative_to(root) or path.stat().st_size > 1024*1024:
            continue
        payload = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(payload,dict) or payload.get('format') != 'peifeng.evidence.v1':
            continue
        try:
            from backend.utils.skills import load_skill_script
            _, identifiers = load_skill_script('writer','document-formatting','bind_facts').bind_text('',path.resolve())
            lines.append(f'- {path.relative_to(root).as_posix()}；已复算事实ID：{", ".join(identifiers[:16])}')
        except (OSError, ValueError, KeyError, TypeError) as error:
            lines.append(f'- {path.relative_to(root).as_posix()}；事实未通过复算，须先修复：{str(error)[:160]}')
        if len(lines) >= 8:
            break
    if not lines:
        return ''
    return '当前项目配对事实引用（只索引本项目根目录与internal/，不是验收结论）：\n' + '\n'.join(lines) + '\n直接Markdown用write(facts=路径)，Word用document-formatting的create_word.py（action=markdown,facts=路径），自动附加精确配对结果段；不要再手写一份符号/缺配数量。'


def normalize_result(data: dict) -> dict:
    """兼容极简输出格式（chart 只给 status / content / file_path）。

    - ``status: "failed"`` → ``partial``：做不到就如实说做了一部分，缺口写进 limitations，
      要不要改剩余计划由评审的 Mentor 决定，worker 不替它做判断
    - 收到已废弃的 ``needs_replan`` 同样归为 ``partial``（旧提示词残留的容错）
    - 单个 ``file_path`` → ``files``（产出文件列表只有一份时用它）
    - **完成时只给路径**（新的约定）：worker 不再复述文件内容，正文由编排层从文件读。
      所以 ``summary`` 用"已产出 <文件名>"兜底，没有它就等于丢了下游衔接的锚点。
    """
    if not isinstance(data, dict):
        return data
    data = dict(data)
    raw_status = str(data.get("status") or "").strip().lower()
    if raw_status in {"failed", "needs_replan"}:
        data["status"] = "partial"
    if raw_status == 'continue':
        data['status']='partial'
        data['limitations']=[*(data.get('limitations') or []),'模型返回了中途状态continue；仅保留进展，不代表本步完成。']
    file_path = str(data.get("file_path") or "").strip()
    if file_path and not data.get("files"):
        data["files"] = [file_path]
    if not str(data.get("summary") or "").strip():
        # 完成时不再要求复述正文：用产出文件名兜底，保住"下一步知道这步产出了什么"这个锚点
        said = str(data.get("content") or "").strip()
        if said:
            data["summary"] = said
        elif file_path:
            data["summary"] = f"已产出 {file_path.replace(chr(92), '/').rsplit('/', 1)[-1]}"
        else:
            data["summary"] = "本步已完成" if data.get('status')=='completed' else '本步尚未确认完成'
    if data.get("status") == "partial":
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
        raise ExecutionError(f"Worker 输出无法解析：{error}") from error
    result.tool_calls = tool_calls
    if result.status == "needs_input" and not result.question.strip():
        raise ExecutionError("needs_input 状态缺少 question")
    if not result.summary.strip():
        raise ExecutionError("Worker 输出缺少 summary")
    return result


def produced_files(records: List[ToolCall]) -> List[str]:
    """从write参数和成功脚本的结构化产物回执收集实际交付路径。"""
    names: List[str] = []
    def add(value):
        if isinstance(value, str) and value and value not in names:
            names.append(value)
    def collect(payload):
        if isinstance(payload, dict):
            for value in payload.get('files', []):
                add(value)
            for value in payload.values():
                if isinstance(value, (dict, list)):
                    collect(value)
        elif isinstance(payload, list):
            for value in payload:
                collect(value)
    for record in records:
        if record.name == 'write' and not record.is_error:
            entries = [record.arguments, *(record.arguments.get('targets') or [])]
            for entry in entries:
                if isinstance(entry,dict):add(entry.get('path') or entry.get('file'))
        elif record.name == 'run_code':
            # 部分批处理失败时，成功项仍可能已经产生有效文件。
            for text in [record.result, *record.result.splitlines()]:
                try:collect(json.loads(text))
                except (ValueError,TypeError):pass
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
    产出哪些文件"写进 content，让编排层按"部分完成"走返修或收尾。一个调用都没有同样交
    ``partial``：由评审的 Mentor 判断这一步是重做、改计划还是就此停止。
    """
    if not records:
        return ExecutionResult(status="partial", content="", summary=summary, limitations=limitations)
    recent = []
    for record in records[-6:]:
        target = record.arguments.get("path") or record.arguments.get("file_path") or ""
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


class WorkerAgent(BaseAgent):
    """worker：执行单个步骤，内部为 LLM ↔ 工具循环。

    所有worker共用这一个类；差异来自三处：共享契约 :attr:`extra_system`、
    **自己的提示词** ``backend/prompts/<名字>.md``（职责与边界），以及注册表里给这个
    角色登记的工具与技能。
    """

    name = "worker"
    is_worker = True
    extra_system = """【所有Worker统一任务契约】
先完成实际产物与必要自检，再直接回复一个JSON：
{"status":"completed","summary":"本步结果与实际验证","files":["产物相对路径"],"handoff":"来源及核验位置","sources":[],"limitations":[]}
仍能用现有工具生成、修复或检查产物时继续执行，不把“尚未运行生成命令”当作阻塞。仅遇真实阻塞或执行预算耗尽时用partial并说明证据；必须由用户补充的信息用needs_input并给question。status只用这三个值（completed/partial/needs_input），不要用continue、done、ok之类的词；status只判断本步，不替Mentor改计划。短问答可直接在summary给结论；长正文落盘，不在JSON重复。sources为字符串数组；tool_calls与步骤日志由系统记录，不自行填写或另写日志。"""
    #: 提示词只有一份：``backend/prompts/<名字>.md``（通用纪律 + 专项职责都在里面）

    def __init__(self, llm, name: str = "", description: str = "", **kwargs) -> None:
        super().__init__(llm, **kwargs)
        if name:
            self.name = name
        #: 注册表里的职责描述（给 Mentor 的分派依据；也用于日志）
        self.description = description
        self.step_contexts: dict[str, MessageContext] = {}


    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """每个步骤独立上下文；只有同一步返工复用，按context_id隔离并行任务。"""
        clone = copy(self)
        key = f"{request.workspace}:{request.context_id}" if request.context_id else ""
        clone._continuing = bool(key and request.continuation and key in self.step_contexts)
        clone.context = self.step_contexts[key] if clone._continuing else MessageContext(system_prompt=self.system_prompt, suffix=request.environment)
        clone.context.suffix = request.environment
        clone.context.workspace = request.workspace
        clone._stop_check = request.stop_check
        clone.tool_runner = clone.runner_for(request)
        try:
            return clone.execute_role(request)
        finally:
            if key:
                self.step_contexts[key] = clone.context
            clone.emit("context_checkpoint", agent=clone.name, context_id=request.context_id,
                       messages=clone.context.conversation[1:])

    def clear_step(self, workspace: str, context_id: str) -> None:
        self.step_contexts.pop(f"{workspace}:{context_id}", None)

    def execute_role(self, request: ExecutionRequest) -> ExecutionResult:
        """执行单个步骤，直到产出结构化结果。

        skill_names：无——技能由worker自己从【本角色可用技能】里挑，并用 read 读该技能正文。
        previous_results：之前步骤的上下文（细节留在任务内存态与任务明细文件里）。

        **零工具调用的"完成"要纠一次**：本步是动手做事的步骤，一次工具都没调却声称已落盘，
        是典型的幻觉完成（评审会发现没有任何产出文件，白白耗掉一次返修配额）。补一轮纠正，
        逼它要么真的动手、要么如实说"做不到/无需工具"。
        """
        if not getattr(self, '_continuing', False):
            self.reset()
        self.set_fixed_layer(tool_defs=self.tool_defs)
        content = self.build_request(request) if not getattr(self, '_continuing', False) else f"同一步返工，保留已有上下文。当前目标：{request.step.goal}\n最新要求：{request.user_decisions}\n修改意见：{request.feedback}"
        references = fact_handoff(request.workspace)
        if references:
            content += '\n\n' + references
        self.context.add("user", content)
        result = self.run_step_turn(request.step.tool_budget)
        if result.status in {"completed", "partial"} and not result.tool_calls:
            self.context.add("user", NO_TOOL_ACTION_HINT)
            try:
                corrected = self.run_step_turn(request.step.tool_budget)
            except Exception:  # noqa: BLE001 - 纠正轮失败不该丢掉已拿到（但可疑）的结果
                logger.debug("零工具调用纠正轮失败", exc_info=True)
                return result
            return corrected
        return result

    def run_step_turn(self, tool_budget: int = 0) -> ExecutionResult:
        """跑一轮执行回合并解析成 ``ExecutionResult``（协议与兜底同 :meth:`execute_role`）。"""
        result = self.run_turn(
            parse=parse_result,
            retry_hint="上次输出不符合协议。请只输出一个合法 JSON 对象，"
            "字段为 status/content/summary/question/files/sources/limitations；status只允许completed、partial、needs_input，不使用continue。",
            error_cls=ExecutionError,
            exhausted=exhausted_result,
            # 用户中止时要交代"做了什么、产出在哪、还差什么"——这段会进任务摘要，供后续接着改
            summarize_on_stop=True,
            tool_budget=tool_budget,
        )
        # 配额用尽是系统事实，不是模型输出：在这里盖章，编排层据此发起阶段审核
        result.budget_exhausted = self.budget_exhausted
        return result

    def build_request(self, request: ExecutionRequest) -> str:
        """本步 user 请求：**任务需求 → 计划（本步已标注，含交付文件）→ 已完成步骤 → 反馈**。

        计划行里已经写了本步的目标与预期输出，所以不再重复"本步内容"；交付规则
        （长结果落盘、短结果进 content）在 system 的「通用规则」里，这里只给文件名。
        """
        prompt = f"用户原始请求：{request.original_task}\n\n" if request.original_task else ""
        step = request.step
        prompt += (
            "本步骤使用独立上下文，不继承前序对话。缺少输入、来源位置或验收约束时，报告具体缺口，不凭历史猜测。需要历史证据时按会话日志json_pointer读取。\n"
            f"本步唯一执行目标：{step.goal}\n本步验收：{step.expected_output}\n"
            "只执行和交付本步目标，其他步骤由其他 worker 完成。"
            "status 判断本步是否完成，无需等待整项任务完成。\n\n"
        )
        if request.requirement:
            prompt += f"任务需求：{request.requirement}\n\n"
        if request.user_decisions:
            prompt += "用户后续补充（发生冲突时以最新要求为准）：\n" + "\n".join(request.user_decisions) + "\n\n"
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
        if request.feedback:
            prompt += f"评审反馈：{request.feedback}"
        return prompt.rstrip() + "\n"
