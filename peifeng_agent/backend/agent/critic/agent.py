"""评审者 Agent：审核单步执行结果，必要时用只读工具独立核对证据。"""
from __future__ import annotations

import json
from typing import Any

from peifeng_agent.backend.agent.common.base import BaseAgent
from peifeng_agent.backend.utils.contracts import ReviewRequest, ReviewResult
from peifeng_agent.backend.utils.exceptions import ReviewError
from peifeng_agent.backend.utils.extract_json import extract_json

#: 通过时回传的产出内容上限：它要进后续步骤的上下文，太长会挤占空间
MAX_FILE_CONTENT = 6000


def as_bool(value: Any) -> bool:
    """宽松解析 LLM 布尔输出（含中文/数字形式）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"true", "yes", "y", "1", "通过", "是", "ok"}


def parse_review_result(text: str) -> ReviewResult:
    """将评审 JSON 解析为 ReviewResult（approved + comment + file_path + file_content）。"""
    try:
        data = extract_json(text)
        return ReviewResult(
            approved=as_bool(data.get("approved", False)),
            comment=str(data.get("comment", "") or "").strip(),
            file_path=str(data.get("file_path", "") or "").strip(),
            # 产出内容要进后续步骤上下文，超长截断以免挤占空间
            file_content=str(data.get("file_content", "") or "")[:MAX_FILE_CONTENT],
        )
    except Exception as error:  # noqa: BLE001
        raise ReviewError(f"Critic 输出无法解析为评审结论: {error}") from error


class CriticAgent(BaseAgent):
    """评审者：审核一个执行步骤是否达成预期输出。"""

    name = "critic"

    @property
    def skill_title(self) -> str:
        """Critic 名下是评审技能（这类产出该怎么验）。"""
        return "本角色可用评审技能"

    def execute_role(self, request: ReviewRequest) -> ReviewResult:
        """评审单步执行结果；需要时可用只读工具查看产出文件与目录。

        skill_names：无——评审技能由评审者自己从【本角色可用评审技能】里挑，并用 read 读该技能正文。
        """
        self.reset()
        self.set_fixed_layer(tool_defs=self.tool_defs)
        self.context.add("user", self.build_request(request))
        return self.run_turn(
            parse=lambda text, records: parse_review_result(text),
            retry_hint="请将审核结论整理为合法 JSON，包含 approved、summary、comments。",
            error_cls=ReviewError,
        )

    def build_request(self, request: ReviewRequest) -> str:
        """评审请求：任务需求 → 本步要求 → **worker交接说明** → 执行过程 → 结论。

        字段名与worker、日志保持一致（都是 `任务需求`）；工具调用是"过程"的一部分，
        按发生顺序紧跟在本步要求之后，而不是堆到最后当附录。

        ``handoff``（worker的交接说明）放在最前面：它指明产出在哪、证据哪一段值得抽查，
        评审据此**按需取证**，不用自己遍历目录——这是评审最大的成本来源。
        """
        step, result = request.step, request.result
        goal = step.goal
        lines = [f"任务需求：{request.task}"]
        goal_line = f"本步目标：{goal}"
        lines.append(goal_line)
        if step.expected_output:
            lines.append(f"预期输出：{step.expected_output}")
        if request.plan_outline:
            # 计划是判断"越界"的唯一依据：后面几步要做什么，才看得出本步有没有抢跑
            lines.append(f"整体计划（本步已标注，用于判断越界）：\n{request.plan_outline}")
        if request.artifact:
            # 产出文件的实际正文：按它核对，而不是按worker的自述
            lines.append(
                "本步产出文件的正文（评审依据；已按字数截断，超出部分worker自述或 handoff 里有路径）：\n"
                f"{request.artifact}"
            )
        if len(request.grouped) > 1:
            # 并行组：一次评审看全组，逐条列出各自的目标与结果
            lines.append(f"本次评审并行组的 {len(request.grouped)} 个步骤（同时执行，请一起判断是否达标）：")
            for member in request.grouped:
                sub_step, sub_result = member.step, member.result
                sub_goal = sub_step.goal
                lines.append(f"—— 第 {member.index + 1} 步（{sub_step.worker}）：{sub_goal}")
                if sub_step.expected_output:
                    lines.append(f"   预期输出：{sub_step.expected_output}")
                if sub_result.handoff:
                    lines.append(f"   交接说明：{sub_result.handoff}")
                if sub_result.summary:
                    lines.append(f"   执行摘要：{sub_result.summary}")
                lines.append(f"   执行状态：{sub_result.status}")
                lines.append(f"   交付正文：\n{sub_result.content or '（无正文）'}")
                if sub_result.files:
                    lines.append(f"   成果文件：{'、'.join(sub_result.files)}")
                if sub_result.limitations:
                    lines.append(f"   限制：{'；'.join(sub_result.limitations)}")
            if request.artifact:
                lines.append(f"整组产出文件的正文（评审依据）：\n{request.artifact}")
            return "\n".join(lines)
        if result.handoff:
            lines.append(f"worker交接说明（产出位置与建议抽查点，优先据此取证）：\n{result.handoff}")
        if result.tool_calls:
            lines.append("执行过程：")
            for record in result.tool_calls:
                mark = "出错" if record.is_error else ""
                lines.append(
                    f"- {record.name}({json.dumps(record.arguments, ensure_ascii=False)}){mark} → {record.result}"
                )
        else:
            lines.append("执行过程：（未调用工具）")
        lines.append(f"执行状态：{result.status}")
        if result.summary:
            lines.append(f"执行摘要：{result.summary}")
        lines.append(f"交付正文：\n{result.content or '（无正文）'}")
        if result.files:
            lines.append(f"成果文件：{'、'.join(result.files)}")
        if result.sources:
            lines.append(f"来源：{'、'.join(result.sources)}")
        if result.limitations:
            lines.append(f"限制：{'；'.join(result.limitations)}")
        return "\n".join(lines)
