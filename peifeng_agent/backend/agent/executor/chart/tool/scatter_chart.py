"""scatter_chart 工具：画散点图，适合看两个数值变量之间的关系。

蓝白科研风：白底、深蓝系配色、浅灰虚线网格、去上右边框、300 dpi 输出。
数据可以直接给（x + y），也可以从 .xlsx / .csv 按列名取（source）。

工具约定：文件头 information 元数据 + 与 name 同名的业务函数；成功 return 文本，失败抛 RuntimeError。
"""
from __future__ import annotations

#: 一次最多处理几个目标
MAX_BATCH = 0  #: 0 = 不限条数（输出量另有上限保护）

from peifeng_agent.backend.utils.text import as_dict_list, call_many

from typing import Optional

from peifeng_agent.backend.utils import chart_kit as kit

information = {
    "name": "scatter_chart",
    "description": (
        "画散点图（PNG）：观察两个数值变量之间的关系（相关、离群点、分群）。"
        "用于：看两个量是否同向变化、找异常点时。"
        "数据可直接给 x + y 两个等长数组，"
        "也可用 source={file, sheet?, category, values:[x列, y列]} 从 .xlsx/.csv 取。"
        "蓝白科研风配色、中文字体、300 dpi 已内置；"
        "返回出图回执（标题、轴标签、点数与取值范围、实际像素、中文字体）。相对路径以 <项目目录> 为基准"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "output": {"type": "string", "description": "输出图片路径（.png，放 <项目目录> 下）"},
            "title": {"type": "string", "description": "标题，写结论（如 课时量与成绩正相关）"},
            "xlabel": {"type": "string", "description": "x 轴标签，带单位"},
            "ylabel": {"type": "string", "description": "y 轴标签，带单位"},
            "x": {"type": "array", "description": "x 轴数值数组"},
            "y": {"type": "array", "description": "y 轴数值数组，与 x 等长"},
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
                "description": "从表格取数：{file, sheet?, category, values:[x列, y列]}，前两列即 x 与 y",
            },
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


def scatter_chart(
    output: str = "",
    title: str = "",
    xlabel: str = "",
    ylabel: str = "",
    x: Optional[list] = None,
    y: Optional[list] = None,
    source: Optional[dict] = None,
    style: Optional[dict] = None,
    charts: list = None,
    base_dir=None,
) -> str:
    """画散点图并保存为 PNG，返回落盘结果；相对路径以 base_dir 为基准。"""
    batch = as_dict_list(charts, "charts", MAX_BATCH)
    if batch:
        return call_many(scatter_chart, batch, "charts", base_dir=base_dir, preview="output")
    if not str(output or "").strip():
        raise RuntimeError("scatter_chart 需要 output（单个）或 charts（多个）之一")
    target = kit.prepare(output, base_dir)

    if isinstance(source, dict) and source:
        labels, data = kit.load_table(source, base_dir)
        if len(data) < 2:
            raise RuntimeError("source.values 需要两列：第一列作 x，第二列作 y")
        xs, ys = data[0]["values"], data[1]["values"]
    else:
        xs = kit.numbers(x, "x")
        ys = kit.numbers(y, "y")
    if len(xs) != len(ys):
        raise RuntimeError(f"x 有 {len(xs)} 个值，y 有 {len(ys)} 个：两者必须等长")
    if not xs:
        raise RuntimeError("散点为空：请给 x 与 y")

    spec = kit.apply(style)
    figure, axes = kit.plt.subplots(figsize=(5.8, 4.2))
    axes.scatter(xs, ys, s=42, color=kit.palette_of(spec)[0], alpha=0.85, edgecolors="white", linewidths=0.6)
    kit.clean_axes(axes, grid_axis="y", spec=spec)
    axes.grid(True, axis="x", linestyle="--", linewidth=0.6, color=kit.GRID)
    if title:
        kit.put_title(figure, axes, title, spec)
    if xlabel:
        axes.set_xlabel(xlabel)
    if ylabel:
        axes.set_ylabel(ylabel)
    figure.tight_layout()
    kit.save(figure, target, spec)
    return kit.report(
        target,
        kind="散点图",
        axes=axes,
        series=[{"name": "x", "values": xs}, {"name": "y", "values": ys}],
        notes=f"点数：{len(xs)}",
    )
