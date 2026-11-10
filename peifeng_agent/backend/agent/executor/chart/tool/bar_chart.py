"""bar_chart 工具：画柱状图（或条形图），适合分类之间的数值对比。

蓝白科研风：白底、深蓝系配色、浅灰虚线网格、去上右边框、300 dpi 输出。
数据可以直接给（categories + series），也可以从 .xlsx / .csv 按列名取（source）。

工具约定：文件头 information 元数据 + 与 name 同名的业务函数；成功 return 文本，失败抛 RuntimeError。
"""
from __future__ import annotations

#: 一次最多处理几个目标
MAX_BATCH = 0  #: 0 = 不限条数（输出量另有上限保护）

from peifeng_agent.backend.utils.text import as_dict_list, call_many

from typing import Optional

from peifeng_agent.backend.utils import chart_kit as kit

information = {
    "name": "bar_chart",
    "description": (
        "画柱状图（PNG），展示分类之间的数值对比。"
        "用于比较各班、各科、各月这类分类的高低。"
        "蓝白科研风配色、中文字体、300 dpi 已内置；返回出图回执（标题、轴标签、分类、系列范围、实际像素、中文字体）。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "output": {"type": "string", "description": "输出图片路径（.png，放 <项目目录> 下）"},
            "title": {"type": "string", "description": "标题，写结论而不是图名"},
            "xlabel": {"type": "string", "description": "x 轴标签（条形图时是数值轴）"},
            "ylabel": {"type": "string", "description": "y 轴标签，带单位"},
            "categories": {"type": "array", "description": "分类标签"},
            "series": {
                "type": "array",
                "description": "数据系列 [{name, values}]，values 与 categories 等长",
            },
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
                "description": "从表格取数：{file, sheet?, category, values:[列名]}",
            },
            "horizontal": {"type": "boolean", "description": "true 时画条形图（分类名长时用）"},
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


def bar_chart(
    output: str = "",
    title: str = "",
    xlabel: str = "",
    ylabel: str = "",
    categories: Optional[list] = None,
    series: Optional[list] = None,
    source: Optional[dict] = None,
    horizontal: bool = False,
    style: Optional[dict] = None,
    charts: list = None,
    base_dir=None,
) -> str:
    """画柱状或条形图并保存为 PNG，返回落盘结果；相对路径以 base_dir 为基准。"""
    batch = as_dict_list(charts, "charts", MAX_BATCH)
    if batch:
        return call_many(bar_chart, batch, "charts", base_dir=base_dir, preview="output")
    if not str(output or "").strip():
        raise RuntimeError("bar_chart 需要 output（单个）或 charts（多个）之一")
    target = kit.prepare(output, base_dir)

    if isinstance(source, dict) and source:
        labels, data = kit.load_table(source, base_dir)
    else:
        labels = [str(item) for item in (categories or [])]
        data = kit.clean_series(series)
    kit.check_lengths(labels, data)

    spec = kit.apply(style)
    figure, axes = kit.plt.subplots(figsize=kit.figure_size(len(labels), horizontal=horizontal))
    positions = kit.x_positions(labels)
    width = 0.8 / max(1, len(data))
    for index, item in enumerate(data):
        color = kit.palette_of(spec)[index % len(kit.palette_of(spec))]
        offset = (index - (len(data) - 1) / 2) * width
        if horizontal:
            axes.barh(
                [position + offset for position in positions],
                item["values"],
                height=width,
                label=item["name"],
                color=color,
            )
        else:
            axes.bar(
                [position + offset for position in positions],
                item["values"],
                width=width,
                label=item["name"],
                color=color,
            )
    if horizontal:
        kit.label_y(axes, positions, labels)
        kit.clean_axes(axes, grid_axis="x", spec=spec)
    else:
        kit.label_x(axes, positions, labels)
        kit.clean_axes(axes, grid_axis="y", spec=spec)
    kit.place_legend(axes, len(data), spec=spec)
    if title:
        kit.put_title(figure, axes, title, spec, pad=kit.title_pad(axes, len(data)))
    if xlabel:
        axes.set_xlabel(xlabel)
    if ylabel:
        axes.set_ylabel(ylabel)
    figure.tight_layout()
    kit.save(figure, target, spec)
    shape = "条形图" if horizontal else "柱状图"
    return kit.report(target, kind=shape, axes=axes, categories=labels, series=data)
