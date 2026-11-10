"""pie_chart 工具：画饼图（或环形图），适合展示构成与占比。

蓝白科研风：白底、深蓝系配色、无边框、300 dpi 输出。
数据可以直接给（categories + values），也可以从 .xlsx / .csv 按列名取（source）。

工具约定：文件头 information 元数据 + 与 name 同名的业务函数；成功 return 文本，失败抛 RuntimeError。
"""
from __future__ import annotations

#: 一次最多处理几个目标
MAX_BATCH = 0  #: 0 = 不限条数（输出量另有上限保护）

from peifeng_agent.backend.utils.text import as_dict_list, call_many

from typing import Optional

from peifeng_agent.backend.utils import chart_kit as kit

#: 饼图的分类上限：再多就分不清
MAX_SLICES = 8

information = {
    "name": "pie_chart",
    "description": (
        "画饼图（PNG），展示各部分的占比，分类不超过 8 个。"
        "用于看构成与比例：经费构成、生源分布、时间分配。"
        "蓝白科研风配色、中文字体、300 dpi 已内置；返回出图回执（标题、分类、各项占比、实际像素、中文字体）。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "output": {"type": "string", "description": "输出图片路径（.png，放 <项目目录> 下）"},
            "title": {"type": "string", "description": "标题，写结论而不是图名（如 经费六成用于教学）"},
            "categories": {"type": "array", "description": "各部分的名称，如 [教学, 科研, 行政]"},
            "values": {"type": "array", "description": "各部分的数值，与 categories 等长"},
            "style": {
                "type": "object",
                "description": (
                    "样式覆盖（可选，来自技能的 style 段）：font/font_latin/font_size/title_size/"
                    "tick_size/legend_size/palette/text/grid_color/spine/background/show_grid/"
                    "grid_axis/title_position(top|bottom)/legend(auto|none)/dpi。不传用默认蓝白科研风"
                ),
            },
            "source": {
                "type": "object",
                "description": "从表格取数：{file, sheet?, category, values:[列名]}，只取第一列数值",
            },
            "donut": {"type": "boolean", "description": "true 时画环形图（中间留空）"},
        },
        "required": [],
    },
}


information["parameters"]["properties"]["charts"] = {
    "type": "array",
    "description": (
        "一次处理多个目标（与 output 互斥，）：每项是对象，"
        "键与单次调用的参数同名（如 {output: ...}）。要产出多个时用这个，不要一个一次调用"
    ),
    "items": {
        "type": "object",
        # 元素字段 = 单次调用的全部参数（只留类型，不复制描述，控制体积）；
        # 只声明文件名会让模型以为不能传 blocks/slides，生成出空文档
        "properties": {
            key: {"type": spec.get("type", "string")}
            for key, spec in information["parameters"]["properties"].items()
            if key != "charts"
        },
        "required": ["output"],
    },
}


def pie_chart(
    output: str = "",
    title: str = "",
    categories: Optional[list] = None,
    values: Optional[list] = None,
    source: Optional[dict] = None,
    donut: bool = False,
    style: Optional[dict] = None,
    charts: list = None,
    base_dir=None,
) -> str:
    """画饼图并保存为 PNG，返回落盘结果；相对路径以 base_dir 为基准。"""
    batch = as_dict_list(charts, "charts", MAX_BATCH)
    if batch:
        return call_many(pie_chart, batch, "charts", base_dir=base_dir, preview="output")
    if not str(output or "").strip():
        raise RuntimeError("pie_chart 需要 output（单个）或 charts（多个）之一")
    target = kit.prepare(output, base_dir)

    if isinstance(source, dict) and source:
        labels, data = kit.load_table(source, base_dir)
        numbers = data[0]["values"]
    else:
        labels = [str(item) for item in (categories or [])]
        numbers = kit.numbers(values, "values")
    if not labels:
        raise RuntimeError("没有分类标签：请给 categories，或用 source 指定分类列")
    if len(labels) != len(numbers):
        raise RuntimeError(f"分类有 {len(labels)} 个，数值有 {len(numbers)} 个：两者必须等长")
    if len(labels) > MAX_SLICES:
        raise RuntimeError(f"饼图分类过多（{len(labels)} 个，上限 {MAX_SLICES}）：先合并小项再画")
    if sum(numbers) <= 0:
        raise RuntimeError("数值合计为 0，无法计算占比")

    spec = kit.apply(style)
    figure, axes = kit.plt.subplots(figsize=(5.6, 4.6))
    wedges, texts, autotexts = axes.pie(
        numbers,
        labels=labels,
        autopct="%1.1f%%",
        startangle=90,
        colors=[kit.palette_of(spec)[index % len(kit.palette_of(spec))] for index in range(len(labels))],
        wedgeprops={"linewidth": 1.2, "edgecolor": "white", "width": 0.45 if donut else 1.0},
        textprops={"color": kit.TEXT, "fontsize": 9},
        pctdistance=0.78,
    )
    for text in autotexts:
        text.set_color("white")
        text.set_fontsize(8.5)
    axes.axis("equal")
    if title:
        kit.put_title(figure, axes, title, spec)
    figure.tight_layout()
    kit.save(figure, target, spec)
    shape = "环形图" if donut else "饼图"
    total = sum(numbers)
    shares = "、".join(f"{label} {value / total * 100:.1f}%" for label, value in list(zip(labels, numbers))[:8])
    return kit.report(
        target,
        kind=shape,
        axes=axes,
        categories=labels,
        notes=f"占比：{shares}",
    )
