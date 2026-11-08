"""规划者 Agent：判断需求完整度、生成分步计划，必要时用只读工具核对材料。

每次任务只规划一次；请求里带跨任务摘要与本会话已完成任务，供衔接与判断可行性。
"""
from __future__ import annotations

from peifeng_agent.backend.agent.common.base import BaseAgent
from peifeng_agent.backend.utils.contracts import Plan, PlanningRequest, PlanningResult
from peifeng_agent.backend.utils.exceptions import PlanningError
from peifeng_agent.backend.utils.extract_json import extract_json


def parse_planning_result(text: str, *, max_steps: int = 0) -> PlanningResult:
    """解析 Planner 的四种输出，只保留必要的协议校验。

    ``max_steps`` 大于 0 时截断超出的步骤（而不是整份计划作废）：模型多给几步很常见，
    为此让整次规划失败、再重试一遍，代价比直接砍掉尾部大得多。
    """
    data = extract_json(text)
    if not isinstance(data, dict):
        raise ValueError("Planner 输出必须是 JSON 对象")
    mode = str(data.get("mode") or "").strip()
    if mode == "clarify":
        source = data.get("clarify") or {}
        questions = [
            {
                "question": str(item.get("question") or "").strip(),
                "options": [str(option) for option in item.get("options") or [] if str(option).strip()],
            }
            for item in source.get("questions") or []
            if isinstance(item, dict) and str(item.get("question") or "").strip()
        ]
        if not questions:
            raise ValueError("clarify 模式缺少有效问题")
        return PlanningResult(mode="clarify", questions=questions)
    if mode == "direct":
        answer = str(data.get("answer") or "").strip()
        if not answer:
            raise ValueError("direct 模式缺少 answer")
        return PlanningResult(mode="direct", answer=answer)
    if mode == "unable":
        reason = str(data.get("reason") or "").strip()
        return PlanningResult(mode="unable", reason=reason or "该任务无可用技能覆盖，无法服务。")
    if mode == "plan":
        plan = Plan.model_validate(data)
        if max_steps and len(plan.steps) > max_steps:
            plan = plan.model_copy(update={"steps": plan.steps[:max_steps]})
        return PlanningResult(mode="plan", plan=plan)
    raise ValueError(f"未知 Planner 模式：{mode or '空'}")


class PlannerAgent(BaseAgent):
    """规划者：单次返回澄清、直接回答、无法服务或完整计划。"""

    name = "planner"

    def __init__(self, *args, max_steps: int = 0, **kwargs) -> None:
        """``max_steps``：计划步数上限（0 表示不限，超出截断）。"""
        super().__init__(*args, **kwargs)
        self.max_steps = max_steps

    @property
    def skill_title(self) -> str:
        """Planner 名下是规划 SOP（这类任务该怎么拆、验收怎么定）。"""
        return "可用技能（规划 SOP）"

    def execute_role(self, request: PlanningRequest) -> PlanningResult:
        """执行一次规划；需要确认材料是否存在或内容时，可用只读工具查看文件与目录。"""
        self.reset()
        self.set_fixed_layer(tool_defs=self.tool_defs)
        self.context.add("user", self.build_request(request))
        return self.run_turn(
            parse=lambda text, records: parse_planning_result(text, max_steps=self.max_steps),
            retry_hint="上次输出不符合协议。请只输出一个合法 JSON 对象，模式为 clarify、direct、unable 或 plan。",
            error_cls=PlanningError,
        )

    def build_request(self, request: PlanningRequest) -> str:
        """构建一次规划请求：只放本次任务的事实（worker清单与输出协议在 system 固定层）。"""
        prompt = f"任务需求：{request.task}"
        if request.clarifications:
            prompt += f"\n\n此前与用户的澄清问答：\n{request.clarifications}\n已澄清的问题不再追问"
        if request.prior_summary:
            prompt += f"\n\n本会话已完成任务（供衔接，聚焦尚未完成的工作）：\n{request.prior_summary}"
        return prompt
