"""快捷任务模板：常见任务做成"填几个字段就直接执行"的预设方案。

**为什么要它**：意图识别全交给大模型有两个代价——每次都要多花一轮规划调用，而且同一个
常见任务（写汇报、画数据图、整理表格）每次拆出来的步骤还不一样。常见任务固定成模板后：
字段一填、``Plan`` 直接可用，规划成本归零、步骤稳定可预期。**不在模板里的任务照旧走
LLM 规划**，两条路互不影响。

模板是**内容不是逻辑**，所以放在这里当常量：加一个模板 = 加一条 :data:`PRESETS` 记录
（+ 一条测试）。每个模板都写成**单步且该步就有实物产出**——实测"准备步 + 产出步"的
两步计划成本是单步的两倍。
"""
from __future__ import annotations

import re
from typing import Any, Optional

from peifeng_agent.backend.utils.contracts import Plan, PlanStep
from peifeng_agent.backend.utils.exceptions import PlanningError

#: 表单字段占位符：模板里写 ``{key}``，提交时用表单值替换
_PLACEHOLDER = re.compile(r"\{(\w+)\}")

#: 每个模板：id / name / description（入口上显示）/ fields（表单）/ requirement 与 steps（Plan 模板）
PRESETS: list[dict[str, Any]] = [
    {
        "id": "official_docx",
        "name": "公文排版",
        "description": "内容已经写好，只按公文格式排版（离线，不联网）",
        "fields": [
            {"key": "source", "label": "内容稿件", "type": "file", "required": True,
             "placeholder": "按示例格式写的 .md / .txt"},
        ],
        "requirement": "把 {source} 按党政机关公文格式排版成 docx",
        "steps": [
            {
                "worker": "office_docs",
                "goal": "把 {source} 按公文格式（GB/T 9704）排版并落盘为 docx",
                "expected_output": "排版后的 .docx：标题二号小标宋、正文三号仿宋、行距 28 磅",
            }
        ],
    },
    {
        "id": "ppt_pages",
        "name": "PPT 套版",
        "description": "每页写什么已经定了，只按页面稿套成演示稿（离线，不联网）",
        "fields": [
            {"key": "source", "label": "页面稿", "type": "file", "required": True,
             "placeholder": "按示例格式写的 .md / .txt"},
        ],
        "requirement": "把 {source} 套成 pptx",
        "steps": [
            {
                "worker": "office_docs",
                "goal": "把 {source} 的每页标题与要点套成 pptx",
                "expected_output": "演示稿 .pptx：封面 + 每个「## 页标题」一页",
            }
        ],
    },
    {
        "id": "report_docx",
        "name": "写一份汇报文档",
        "description": "把素材整理成一份 .docx 汇报（正式版式）",
        "fields": [
            {"key": "topic", "label": "主题", "placeholder": "如：数据结构课程学习总结", "required": True},
            {"key": "materials", "label": "素材文件", "type": "file", "fallback": "未指定（由你按主题在项目目录里检索可用的材料）"},
            {"key": "length", "label": "篇幅", "default": "800 字左右"},
        ],
        "requirement": "写一份汇报文档《{topic}》（{length}）。素材：{materials}",
        "steps": [
            {
                "worker": "office_docs",
                "goal": "成文《{topic}》并落盘为 .docx：篇幅 {length}，素材取自 {materials}",
                "expected_output": "{topic}.docx 存在，含标题、分节正文与落款，篇幅符合「{length}」",
            }
        ],
    },
    {
        "id": "chart_png",
        "name": "画一张数据图",
        "description": "从表格数据出一个图表 PNG",
        "fields": [
            {"key": "source", "label": "数据文件", "type": "file", "required": True},
            {"key": "metric", "label": "看什么指标", "placeholder": "如：各产品每月的销售额对比", "required": True},
            {"key": "chart", "label": "图型", "default": "柱状图"},
        ],
        "requirement": "把 {source} 画成一张{chart}：{metric}",
        "steps": [
            {
                "worker": "chart",
                "goal": "按「{metric}」出一张{chart}并落盘为 PNG：数据取自 {source}",
                "expected_output": "PNG 存在且可打开；图形类型为{chart}；标题与坐标轴可读出「{metric}」的结论",
            }
        ],
    },
    {
        "id": "table_xlsx",
        "name": "整理成表格",
        "description": "把数据清洗后整理成 .xlsx（含汇总行）",
        "fields": [
            {"key": "source", "label": "数据文件", "type": "file", "required": True},
            {"key": "summary", "label": "怎么汇总", "default": "按第一列分组，给出条数与金额合计"},
            {"key": "output", "label": "输出文件名", "default": "汇总表.xlsx"},
        ],
        "requirement": "把 {source} 整理成 {output}：{summary}",
        "steps": [
            {
                "worker": "office_docs",
                "goal": "整理 {source} 并落盘为 {output}：先清洗（去重复、去空值），再按「{summary}」汇总",
                "expected_output": "{output} 存在，含表头、明细与汇总行，数字与源表可逐项复算",
            }
        ],
    },
    {
        "id": "pptx_deck",
        "name": "做一份汇报 PPT",
        "description": "把素材做成一份 .pptx 汇报",
        "fields": [
            {"key": "topic", "label": "主题", "placeholder": "如：本科毕业设计中期汇报", "required": True},
            {"key": "materials", "label": "素材文件", "type": "file", "fallback": "未指定（由你按主题在项目目录里检索可用的材料）"},
            {"key": "pages", "label": "页数", "default": "5 页"},
        ],
        "requirement": "把素材做成一份《{topic}》汇报 PPT（{pages}）",
        "steps": [
            {
                "worker": "office_docs",
                "goal": "产出《{topic}》.pptx（{pages}）：素材取自 {materials}",
                "expected_output": "{topic}.pptx 存在且可打开，页数为「{pages}」，每页标题与要点齐全",
            }
        ],
    },
    {
        "id": "archive_files",
        "name": "整理一个目录",
        "description": "把目录里的文件按规则归档并留下清单",
        "fields": [
            {"key": "target", "label": "要整理的目录", "type": "dir", "required": True},
            {"key": "rule", "label": "归档规则", "default": "按扩展名分类到各自子目录"},
        ],
        "requirement": "整理 {target}：{rule}",
        "steps": [
            {
                "worker": "file_manager",
                "goal": "按「{rule}」归档 {target} 并留下可核对清单：先 dry_run 预演冲突，归档后核对数量守恒",
                "expected_output": "{target} 下文件已按「{rule}」归位；有一份清单写明每个文件去了哪；数量守恒核对通过",
            }
        ],
    },
]


def preset_catalog() -> list[dict[str, Any]]:
    """给页面的模板清单：只含入口名称与表单定义，不含 Plan 模板。"""
    return [
        {
            "id": preset["id"],
            "name": preset["name"],
            "description": preset["description"],
            "fields": preset["fields"],
        }
        for preset in PRESETS
    ]


def preset_by_id(preset_id: str) -> dict[str, Any]:
    """按 id 取模板；未知 id 直接报错（页面传来的值必须可信，不静默回退）。"""
    for preset in PRESETS:
        if preset["id"] == preset_id:
            return preset
    known = "、".join(preset["id"] for preset in PRESETS)
    raise PlanningError(f"未知的快捷模板：{preset_id}（可用：{known}）")


def fill_fields(preset: dict[str, Any], values: Optional[dict[str, Any]] = None) -> dict[str, str]:
    """把表单值补全成"每个字段都有可渲染文本"的字典。

    ``required`` 字段没填就报错；非必填的用 ``default``，再没有就用 ``fallback``——
    ``file`` / ``dir`` 这类选择器默认是"没选"，但渲染出来的句子不能留空位
    （"素材："这种半句话没法执行）。
    """
    given = {str(key): str(value).strip() for key, value in (values or {}).items()}
    filled: dict[str, str] = {}
    for field in preset["fields"]:
        value = given.get(field["key"]) or str(field.get("default") or "").strip()
        if field.get("required") and not value:
            raise PlanningError(f"「{preset['name']}」需要填写「{field['label']}」")
        filled[field["key"]] = value or str(field.get("fallback") or "")
    return filled


def render(template: str, values: dict[str, str]) -> str:
    """把 ``{key}`` 占位符替换成表单值；不认识的占位符原样保留，不抛 KeyError。"""

    def replace(match: re.Match[str]) -> str:
        return values.get(match.group(1), match.group(0))

    return _PLACEHOLDER.sub(replace, template)


def build_plan(preset_id: str, values: Optional[dict[str, Any]] = None) -> Plan:
    """把模板 + 表单值渲染成一份可直接执行的 ``Plan``（跳过 LLM 规划）。"""
    preset = preset_by_id(preset_id)
    filled = fill_fields(preset, values)
    steps = [
        PlanStep(
            worker=render(str(step.get("worker") or ""), filled),
            goal=render(str(step.get("goal") or ""), filled),
            expected_output=render(str(step.get("expected_output") or ""), filled),
        )
        for step in preset["steps"]
    ]
    if not steps:
        raise PlanningError(f"模板 {preset_id} 没有定义步骤")
    return Plan(
        requirement=render(str(preset.get("requirement") or preset["name"]), filled),
        steps=steps,
    )
