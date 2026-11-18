"""Mentor：理解目标、制定计划、审核每次进度并调整剩余计划（只读，不代做执行）。"""

from __future__ import annotations

from typing import Any
from backend.agents.base import BaseAgent
from backend.agents.base import (
    FinalAnswer,
    FinalRequest,
    Plan,
    PlanningRequest,
    PlanningResult,
    ReviewRequest,
    ReviewResult,
    StageReview,
    StageReviewRequest,
)
from backend.utils.errors import PlanningError, ReviewError
from backend.agents.base import extract_json


# ---------------------------------------------------------------- mentor
#: 通过时回传的产出内容上限：它要进后续步骤的上下文，太长会挤占空间
MAX_FILE_CONTENT = 6000


def parse_final_answer(text: str) -> FinalAnswer:
    """Reject an internal review envelope so the normal retry can request prose."""
    content = str(text or '').strip()
    if not content:
        raise ValueError('收尾回答不能为空')
    if content.startswith(('{', '```')):
        try:
            payload = extract_json(content)
        except ValueError:
            payload = None
        if isinstance(payload, dict) and 'approved' in payload and any(
            key in payload for key in ('goal_check', 'findings', 'plan_update', 'complete')
        ):
            raise ValueError('收尾回答误用了内部审核JSON；请直接回答用户原始问题')
    return FinalAnswer(content=content)


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
        update = data.get('plan_update')
        if update == [] and as_bool(data.get('approved', False)) and as_bool(data.get('complete', False)):
            update = None
        if isinstance(update, list):
            update = {'steps': update}
        findings = data.get('findings') or []
        if isinstance(findings, list):
            findings=[dict(item,severity='blocking') if isinstance(item,dict) and item.get('severity') in {'major','critical'} else item for item in findings]
            findings = [
                dict(item, criterion='建议项：未指定验收依据')
                if isinstance(item, dict) and item.get('severity') == 'advisory' and not str(item.get('criterion') or '').strip()
                else item for item in findings
            ]
        result = ReviewResult(
            findings=findings,
            goal_check=str(data.get("goal_check") or "").strip(),
            plan_update=Plan.model_validate(update) if update is not None else None,
            complete=as_bool(data.get("complete", False)),
            approved=as_bool(data.get("approved", False)),
            comment=str(data.get("comment", "") or "").strip(),
            file_path=str(data.get("file_path", "") or "").strip(),
            # 产出内容要进后续步骤上下文，超长截断以免挤占空间
            file_content=str(data.get("file_content", "") or "")[:MAX_FILE_CONTENT],
        )
        if not result.goal_check:
            raise ValueError("每次进度审核必须给出goal_check，检查目标和情况是否变化")
        if result.plan_update is not None and not result.plan_update.steps:
            raise ValueError("剩余计划不能为空；目标全部达成时应审核通过并complete=true")
        blocking = any(item.severity == "blocking" for item in result.findings)
        if (result.approved and blocking) or (not result.approved and not blocking and result.plan_update is None):
            raise ValueError("审核结论与发现不一致：拒绝须有带依据、位置、证据和最小修复的 blocking 项；通过不能含 blocking 项")
        if result.complete and (not result.approved or result.plan_update is not None):
            raise ValueError("complete要求本步通过，且不能同时给剩余计划")
        return result
    except Exception as error:  # noqa: BLE001
        raise ReviewError(f"Mentor 输出无法解析为评审结论: {error}") from error


def parse_stage_review(text: str) -> StageReview:
    """解析阶段审核 JSON：continue / adjust / stop，可带 hint 与追加配额。

    比评审宽松：这里没有"通过/不通过"的强一致性约束，只要求动作合法、adjust 必须带计划。
    """
    try:
        data = extract_json(text)
        update = data.get("plan_update")
        if isinstance(update, list):
            update = {"steps": update}
        result = StageReview(
            action=str(data.get("action") or "continue").strip().lower(),
            comment=str(data.get("comment", "") or "").strip(),
            hint=str(data.get("hint", "") or "").strip(),
            extra_budget=max(0, int(data.get("extra_budget") or 0)),
            plan_update=Plan.model_validate(update) if update is not None else None,
        )
        if result.action not in {"continue", "adjust", "stop"}:
            raise ValueError(f"action 只能是 continue/adjust/stop，当前 {result.action!r}")
        if result.action == "adjust" and (result.plan_update is None or not result.plan_update.steps):
            raise ValueError("action=adjust 必须给出非空的 plan_update")
        if result.action != "adjust" and result.plan_update is not None:
            raise ValueError("只有 action=adjust 才能带 plan_update")
        return result
    except Exception as error:  # noqa: BLE001
        raise ReviewError(f"Mentor 输出无法解析为阶段审核结论: {error}") from error


def parse_planning_result(text: str, *, max_steps: int = 0) -> PlanningResult:
    """解析 Mentor 的四种输出，只保留必要的协议校验。

    超出 ``max_steps`` 时要求重新合并步骤，不截掉末尾交付物。
    """
    data = extract_json(text)
    if not isinstance(data, dict):
        raise ValueError("Mentor 输出必须是 JSON 对象")
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
    if mode == "unable":
        reason = str(data.get("reason") or "").strip()
        return PlanningResult(mode="unable", reason=reason or "该任务无可用技能覆盖，无法服务。")
    if mode == "plan":
        plan = Plan.model_validate(data)
        if max_steps and len(plan.steps) > max_steps:
            raise ValueError(f"计划超过 {max_steps} 步，请合并相同角色的相关工作，保留全部交付要求")
        return PlanningResult(mode="plan", plan=plan)
    raise ValueError(f"未知 Mentor 模式：{mode or '空'}（只能是 plan / clarify / unable）")


class MentorAgent(BaseAgent):
    """评审者：审核一个执行步骤是否达成预期输出。"""

    name = "mentor"
    final_hint = "工具轮次已用尽。基于实际证据，只输出评审 JSON，包含 approved、comment、file_path、findings；拒绝必须有 blocking 发现，含 criterion/location/evidence/correction；核心证据不足时 approved=false 并在 findings 给出 blocking 缺口及来源位置，不要再调用工具。"

    @property
    def skill_title(self) -> str:
        """Mentor 名下是评审技能（这类产出该怎么验）。"""
        return "Mentor 可用核验技能"

    def __init__(self, *args, max_steps: int = 0, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_steps = max_steps

    def execute_role(self, request):
        """同一 Mentor 按进度摘要规划或审核；详细证据只按需读取。"""
        self.reset()
        planning = isinstance(request, PlanningRequest)
        self.set_fixed_layer(tool_defs=self.tool_defs)
        if isinstance(request, FinalRequest):
            # 收尾只写正文：不提供工具，免得它又去读一堆材料
            self.set_fixed_layer(tool_defs=[])
            self.final_hint = "直接写面向用户的 Markdown 回答正文，不要 JSON，不要「任务已完成」这类交代。"
            content = self.build_final_request(request)
            self.context.add("user", content)
            parser = lambda text, records: parse_final_answer(text)
            return self.run_turn(parse=parser, retry_hint=self.final_hint, error_cls=PlanningError)
        if isinstance(request, StageReviewRequest):
            # 阶段审核也不给工具：它只判断"还该不该继续"，取证交给 worker 与评审
            self.set_fixed_layer(tool_defs=[])
            self.final_hint = (
                '只输出阶段审核JSON：action（continue=接着做/adjust=改剩余计划/stop=停下）、'
                'comment、hint（继续时给worker的下一步提示）、extra_budget（追加多少次工具调用）、'
                'plan_update（仅 adjust 时给）。'
            )
            content = self.final_hint + "\n" + self.build_stage_request(request)
            self.context.add("user", content)
            parser = lambda text, records: parse_stage_review(text)
            return self.run_turn(parse=parser, retry_hint=self.final_hint, error_cls=ReviewError)
        if planning:
            # 可读取规划方法；项目材料调查交给执行者，避免规划阶段先重做任务。
            self.set_fixed_layer(tool_defs=self.tool_defs)
            self.final_hint = '只输出规划JSON，mode为plan/clarify/unable；信息不足时一次性问全，之后必须给出计划或返回 unable。'
            content = self.build_planning_request(request)
            parser = lambda text, records: parse_planning_result(text, max_steps=self.max_steps)
        else:
            self.final_hint = '只输出审核JSON，含approved、goal_check、comment、findings；severity只用blocking或advisory，每项给criterion/location/evidence/correction。需要改变剩余计划时给plan_update，目标全部完成才complete=true。'
            content = self.final_hint + '\n' + self.build_request(request)
            parser = lambda text, records: parse_review_result(text)
        self.context.add('user', content)
        result = self.run_turn(parse=parser, retry_hint=self.final_hint, error_cls=PlanningError if planning else ReviewError)
        if planning:
            return result
        return result

    def build_planning_request(self, request: PlanningRequest) -> str:
        """规划协议按阶段加载；可按需读取规划skill，不预读项目材料。"""
        lines = [
            '【初步规划或剩余计划调整，不是进度审核】',
            '只制定计划，不预读材料或代做计算。最少步骤覆盖全部交付，按依赖串行；同角色相关工作通常合并，但复杂PPT与Word/PDF可分步验收，避免单步超载。',
            '可按需用read读取research-planning的SKILL.md与references方法；项目材料调查交给执行者，不在规划阶段重做。',
            '每步goal自包含目标、输入路径、约束与输出，expected_output为可核验条件；不要依赖前步隐含对话。',
            'expected_output只列本步交付及其验收，输入路径放goal；文件用完整项目相对路径，多个文件用顿号分隔，不用斜杠连接文件名。',
            '仅确实缺必要输入时一次澄清，最多3问。已回答部分则用合理默认补齐偏好；对象/文件/主题等不可缺输入仍缺失才unable。短问答也给一步计划，无需强制另写证据文件。',
            'tool_budget可省略或0表示不限；设置时只估必要调用。expected_output里的交付物与输入都写工作区相对完整路径（deliverables/x.docx、analysis/y.json），只写文件名会被当成要生成的文件。已有进度时保留已通过部分，返回完整剩余步骤。',
            '只回复下列一种JSON：',
            '{"mode":"plan","requirement":"目标与必要假设","output_name":"交付物","steps":[{"worker":"engineer","goal":"自包含步骤","expected_output":"验收条件","tool_budget":0}]}',
            '{"mode":"clarify","clarify":{"questions":[{"question":"必要信息","options":[]}]}}',
            '{"mode":"unable","reason":"必要缺口"}',
            f'用户需求：{request.task}',
            f'历史与当前进度摘要：{request.prior_summary}',
            f'用户补充：{request.clarifications}',
        ]
        if request.plan_feedback:
            lines.append('上一次计划无法执行，请输出修正后的完整计划：'+request.plan_feedback)
        return '\n'.join(lines)

    def build_final_request(self, request: FinalRequest) -> str:
        """收尾提示：要求直接回答用户最初的问题，而不是交代流程。"""
        files = "、".join(request.files) if request.files else "（本次没有产出文件）"
        limits = "；".join(request.limitations) if request.limitations else "（无）"
        return (
            "【收尾：直接回答用户最初的问题】\n"
            f"用户最初的请求：{request.task}\n"
            f"之前会话摘要：{request.prior_summary or '（无）'}\n"
            f"这次实际做的计划：\n{request.plan_outline or '（无）'}\n"
            f"各步结论与产出：\n{request.step_summaries or '（无）'}\n"
            f"产出文件（相对项目目录）：{files}\n"
            f"已知限制：{limits}\n\n"
            "写作要求：\n"
            "- 审核阶段已结束；不要输出 approved、goal_check、findings 等内部审核 JSON。直接写给用户的回答。\n"
            "- 开头 1-2 句**直接回答他问的那件事**（结论先行）；不许写「任务已完成」「已完成以下工作」这类交代。\n"
            "- 接着给支撑结论的关键内容：数字、机制、对比、步骤或原文位置，只写这次真实得到的，别推测。\n"
            "- 每条结论都要落到**具体事实**（数字、专有名词、原文短引、文件名）；"
            "不许用「说明了要达成的核心目标」「记录了推进到哪一步」「这个项目要解决什么问题」"
            "这类只描述结构或复述标题的空话代替内容——下面的交付文件正文就是取事实的地方。\n"
            "- 提到文件就说清文件名与用途，不要罗列长路径清单。\n"
            "- 没做到的、依赖假设的、需要用户确认的，单独一段讲清楚。\n"
            "- 不要复述计划原文，不要讲工具调用过程。\n"
            "- 中文 Markdown；问题短就短答，别为了长而长。"
        )

    def build_stage_request(self, request: StageReviewRequest) -> str:
        """阶段审核提示：这一步做到哪、配额用了多少、计划还剩什么。"""
        lines = [
            "【工具调用配额用尽：阶段审核】",
            f"用户原始请求：{request.original_task or request.task}",
            f"当前任务需求：{request.task}",
            f"本步：{request.step.worker} / {request.step.goal}",
            f"本步验收：{request.step.expected_output or '（未指定）'}",
            f"配额：已用 {request.used} 次 / 分配 {request.budget} 次",
            f"执行状态：{request.result.status}",
            f"产出摘要：{request.result.summary or '（无）'}",
            f"产出文件：{'、'.join(request.result.files) or '（无）'}",
            f"它自述的限制：{'；'.join(request.result.limitations) or '（无）'}",
        ]
        if request.completed_summary:
            lines.append("已完成步骤：\n" + request.completed_summary)
        if request.plan_outline:
            lines.append("当前计划：\n" + request.plan_outline)
        if request.user_decisions:
            lines.append("用户最新补充：\n" + "\n".join(f"- {item}" for item in request.user_decisions))
        lines.append(
            "请判断：这一步是在正常推进、卡在某个具体问题上，还是方向已经错了。"
            "接着做就给 continue 并写清下一步该干什么（hint）与还要多少次调用（extra_budget）；"
            "需要换路子就给 adjust 并给出完整的剩余计划；"
            "已经拿到能交付的东西、或继续投入不划算，就给 stop 并说明停在哪。"
            "不要用阶段审核代替进度评审：产出好不好仍由正常评审判。"
        )
        return "\n".join(lines)

    def build_request(self, request: ReviewRequest) -> str:
        """每次进度只拼目标、计划、摘要与证据索引。"""
        lines = ["【Worker进度审核与剩余计划决策】", f"用户原始请求：{request.original_task or request.task}", f"当前任务需求：{request.task}", f"之前任务摘要：{request.prior_summary}", f"当前已通过步骤摘要：{request.completed_summary}"]
        if request.user_decisions:
            lines.append("用户最新补充：\n" + "\n".join(request.user_decisions))
        if request.plan_outline:
            lines.append("当前计划：\n" + request.plan_outline)
        if request.plan_feedback:
            lines.append("上一次给出的剩余计划无法执行，必须重新给出合法计划（每步都要指定可用 worker）：" + request.plan_feedback)
        step, result = request.step, request.result
        lines.extend([f"Worker进度：{step.worker} / {step.goal}", f"本步验收：{step.expected_output}", f"执行状态：{result.status}", f"产出摘要：{result.summary}", f"产出文件：{result.files}", f"证据索引：{result.handoff}", f"来源：{result.sources}", f"限制：{result.limitations}"])
        if result.tool_calls:
            lines.append("执行回执索引（完整参数与结果在明细日志，需要时读取）：")
            lines.extend(f"- {record.name} {'失败' if record.is_error else '已执行'}：{(record.result or '').splitlines()[0][:240] if record.result else ''}" for record in result.tool_calls)
        lines.extend([
            '按用户验收与实际证据检查目标是否变化。拒绝须有blocking发现或非空plan_update；通过不能含blocking。',
            '每个发现给severity、criterion、location、evidence、correction，只指出影响本次交付的真实问题。',
            'plan_update为{"steps":[...]}，覆盖全部未完成交付；本步通过但后续调整仍approved=true，本步作废则false。',
            '仅全部目标达成时complete=true，此时必须approved=true且plan_update=null。',
            '只回复审核JSON：{"approved":true,"goal_check":"目标及剩余路线判断","comment":"结论","findings":[],"plan_update":null,"complete":false}',
        ])
        return "\n".join(lines)
