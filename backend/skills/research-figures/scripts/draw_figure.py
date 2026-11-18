"""Nature Figure本地适配：基础图形兼容入口，统一执行出版导出与审核。"""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[4]))
from backend.utils.skills import load_skill_script
from backend.utils.skill_runtime import run_cli,project_path
from backend.utils import paths

from copy import deepcopy

from backend.utils import charts as kit

from backend.utils.web import run_targets

from backend.utils.charts import resolve_style

from typing import Optional

def bar_chart_target(
    output: str = '',
    title: str = '',
    xlabel: str = '',
    ylabel: str = '',
    categories: Optional[list] = None,
    series: Optional[list] = None,
    source: Optional[dict] = None,
    horizontal: bool = False,
    show_values: bool = False,
    caption: str = '',
    svg_output: str = '',
    style: Optional[dict] = None,
    base_dir=None
) -> str:
    """画柱状或条形图并保存为 PNG，返回落盘结果；相对路径以 base_dir 为基准。"""
    if not str(output or "").strip():
        raise RuntimeError("bar_chart 需要 output 参数")
    target = kit.prepare(output, base_dir)
    vector = kit.resolve(svg_output,base_dir) if svg_output else None
    if vector and vector.suffix.lower()!='.svg':
        raise RuntimeError('svg_output必须是SVG路径')

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
            bars = axes.barh(
                [position + offset for position in positions],
                item["values"],
                height=width,
                xerr=item.get("errors"),
                capsize=spec['error_capsize'] if item.get("errors") else 0,
                error_kw={'ecolor':spec['error_color'],'elinewidth':spec['error_linewidth'],'capthick':spec['error_linewidth']},
                label=item["name"],
                color=color,
            )
        else:
            bars = axes.bar(
                [position + offset for position in positions],
                item["values"],
                width=width,
                yerr=item.get("errors"),
                capsize=spec['error_capsize'] if item.get("errors") else 0,
                error_kw={'ecolor':spec['error_color'],'elinewidth':spec['error_linewidth'],'capthick':spec['error_linewidth']},
                label=item["name"],
                color=color,
            )
        if show_values or item.get('counts') is not None:
            padding = max(max(abs(value) for value in item['values']),1)*.035
            for position,bar in enumerate(bars):
                value = item['values'][position]
                error = item.get('errors',[0]*len(bars))[position]
                label = f'{value:.2f}' if show_values else ''
                if item.get('counts') is not None:
                    label += ('\n' if label else '') + f'n={item["counts"][position]}'
                endpoint = value+error+padding if value>=0 else value-error-padding
                if horizontal:
                    axes.text(endpoint,bar.get_y()+bar.get_height()/2,label,ha='left' if value>=0 else 'right',va='center',fontsize=spec['value_size'],color=spec['text'])
                else:
                    axes.text(bar.get_x()+bar.get_width()/2,endpoint,label,ha='center',va='bottom' if value>=0 else 'top',fontsize=spec['value_size'],color=spec['text'])
            axes.margins(**({'x':.2} if horizontal else {'y':.2}))
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
    if caption:
        import unicodedata
        size = float(spec['caption_size'])
        width = max(20,int(figure.get_figwidth()*72/(size*.6)*.92))
        lines, current, units = [], '', 0
        for character in str(caption):
            extra = 2 if unicodedata.east_asian_width(character) in 'WF' else 1
            if character=='\n' or units+extra>width:
                lines.append(current);current='';units=0
                if character=='\n':continue
            current+=character;units+=extra
        if current:lines.append(current)
        footer = (len(lines)+1)*size*1.4/72
        height = figure.get_figheight()+footer
        figure.set_size_inches(figure.get_figwidth(),height)
        figure.text(.02,.01,'\n'.join(lines),ha='left',va='bottom',fontsize=size,color=spec['text'])
        figure.tight_layout(rect=(0,footer/height,1,1))
    else:
        figure.tight_layout()
    save_figure(figure, target, spec)
    if vector and vector.resolve()!=target.with_suffix('.svg').resolve():
        from backend.utils.skill_runtime import atomic_output
        vector.parent.mkdir(parents=True,exist_ok=True)
        with atomic_output(vector) as candidate:
            candidate.write_bytes(target.with_suffix('.svg').read_bytes())
    shape = "条形图" if horizontal else "柱状图"
    notes = []
    if vector:notes.append(f'可编辑SVG（保留文本节点）：{vector}')
    if caption:notes.append(f'图注：{caption}')
    for item in data:
        notes.append(f'{item["name"]}实际数值：{item["values"]}；误差：{item.get("errors","未提供")}；样本数：{item.get("counts","未提供")}')
    return kit.report(target, kind=shape, axes=axes, categories=labels, series=data,notes='\n'.join(notes))

def bar_chart(targets: list[dict], base_dir=None) -> str:
    """统一批处理入口，每项独立参数。"""
    return run_targets(lambda target: bar_chart_target(**target, base_dir=base_dir), targets, workers=1, base_dir=base_dir)

from typing import Optional

def line_chart_target(
    output: str = '',
    title: str = '',
    xlabel: str = '',
    ylabel: str = '',
    categories: Optional[list] = None,
    series: Optional[list] = None,
    source: Optional[dict] = None,
    style: Optional[dict] = None,
    markers: bool = True,
    base_dir=None
) -> str:
    """画折线并保存为 PNG，返回落盘结果；相对路径以 base_dir（项目工作文件夹）为基准。"""
    if not str(output or "").strip():
        raise RuntimeError("line_chart 需要 output 参数")
    target = kit.prepare(output, base_dir)

    if isinstance(source, dict) and source:
        labels, data = kit.load_table(source, base_dir)
    else:
        labels = [str(item) for item in (categories or [])]
        data = kit.clean_series(series)
    kit.check_lengths(labels, data)

    spec = kit.apply(style)
    figure, axes = kit.plt.subplots(figsize=kit.figure_size(len(labels)))
    positions = kit.x_positions(labels)
    for index, item in enumerate(data):
        axes.errorbar(
            positions,
            item["values"],
            yerr=item.get("errors"),
            capsize=4 if item.get("errors") else 0,
            label=item["name"],
            color=kit.palette_of(spec)[index % len(kit.palette_of(spec))],
            marker="o" if markers else None,
            markersize=4,
            linewidth=1.8,
        )
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
    save_figure(figure, target, spec)
    return kit.report(target, kind="折线图", axes=axes, categories=labels, series=data)

def line_chart(targets: list[dict], base_dir=None) -> str:
    """统一批处理入口，每项独立参数。"""
    return run_targets(lambda target: line_chart_target(**target, base_dir=base_dir), targets, workers=1, base_dir=base_dir)

from typing import Optional

MAX_SLICES = 8

def pie_chart_target(
    output: str = '',
    title: str = '',
    categories: Optional[list] = None,
    values: Optional[list] = None,
    source: Optional[dict] = None,
    donut: bool = False,
    style: Optional[dict] = None,
    base_dir=None
) -> str:
    """画饼图并保存为 PNG，返回落盘结果；相对路径以 base_dir 为基准。"""
    if not str(output or "").strip():
        raise RuntimeError("pie_chart 需要 output 参数")
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
    save_figure(figure, target, spec)
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

def pie_chart(targets: list[dict], base_dir=None) -> str:
    """统一批处理入口，每项独立参数。"""
    return run_targets(lambda target: pie_chart_target(**target, base_dir=base_dir), targets, workers=1, base_dir=base_dir)

from typing import Optional

def scatter_chart_target(
    output: str = '',
    title: str = '',
    xlabel: str = '',
    ylabel: str = '',
    x: Optional[list] = None,
    y: Optional[list] = None,
    source: Optional[dict] = None,
    style: Optional[dict] = None,
    base_dir=None
) -> str:
    """画散点图并保存为 PNG，返回落盘结果；相对路径以 base_dir 为基准。"""
    if not str(output or "").strip():
        raise RuntimeError("scatter_chart 需要 output 参数")
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
    save_figure(figure, target, spec)
    return kit.report(
        target,
        kind="散点图",
        axes=axes,
        series=[{"name": "x", "values": xs}, {"name": "y", "values": ys}],
        notes=f"点数：{len(xs)}",
    )

def scatter_chart(targets: list[dict], base_dir=None) -> str:
    """统一批处理入口，每项独立参数。"""
    return run_targets(lambda target: scatter_chart_target(**target, base_dir=base_dir), targets, workers=1, base_dir=base_dir)

import json

import re

from pathlib import Path

from xml.sax.saxutils import escape

def draw_target(output: str, nodes: list[dict], edges: list[dict], direction: str = 'LR', style: dict = None, base_dir=None) -> str:
    if direction not in {'LR', 'TB'} or not nodes:
        raise ValueError('需要非空 nodes，direction 取 LR 或 TB')
    identifiers = [node['id'] for node in nodes]
    if any(not value for value in identifiers) or len(set(identifiers)) != len(identifiers):
        raise ValueError('节点 id 必须非空且唯一')
    positions = {identifier: index for index, identifier in enumerate(identifiers)}
    for edge in edges:
        if edge['from'] not in positions or edge['to'] not in positions:
            raise ValueError('边必须引用存在的节点 id')
    target = Path(output)
    if not target.is_absolute():
        target = Path(base_dir or Path.cwd()) / target
    if target.suffix.lower() != '.svg':
        raise ValueError('output 必须使用 .svg 后缀')
    spec = resolve_style(style)
    font_size = float(spec['node_font_size'])
    if not 0 < font_size <= 156:
        raise ValueError('node_font_size必须大于0且适合节点可用宽度（不超过156）')
    line_height = max(20,font_size*1.25)
    # Preserve the existing 18-unit line at the default 16px, scale with style.
    wrap_width = max(1,int(18*16/font_size))
    def attribute(value):
        return escape(str(value),{'"':'&quot;'})
    border, edge_color, background, fill, text_color = [attribute(spec[name]) for name in ['node_border','edge_color','background','node_fill','node_text']]
    family = ','.join(str(item) for item in [spec.get('font_latin'),spec.get('font'),'Microsoft YaHei','Arial','sans-serif'] if item)
    wrapped = []
    for node in nodes:
        label = str(node['label'])
        if not label.strip() or len(label) > 120:
            raise ValueError('节点标签需为1到120字符；复杂解释请放图注')
        lines, line, length = [], '', 0
        for token in re.findall(rf'[A-Za-z0-9_.-]{{1,{wrap_width}}}|[^A-Za-z0-9_.-]', label):
            token_width = sum(1 if character.isascii() else 2 for character in token)
            if line and length + token_width > wrap_width:
                lines.append(line.strip())
                line, length = '', 0
            line += token
            length += token_width
        if line:
            lines.append(line.strip())
        wrapped.append(lines)
    node_height = max(80, max(len(lines) for lines in wrapped) * line_height + 24)
    center = node_height / 2
    horizontal = direction == 'LR'
    width, height = (len(nodes) * 260 + 40, 180 + node_height) if horizontal else (360, len(nodes) * (node_height + 80) + 40)
    coordinates = [(60 + index * 260, 100) if horizontal else (110, 40 + index * (node_height + 80)) for index in range(len(nodes))]
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">', f'<defs><marker id="arrow" markerWidth="10" markerHeight="10" refX="9" refY="5" orient="auto"><path d="M0,0 L10,5 L0,10 Z" fill="{edge_color}"/></marker></defs>', f'<rect width="100%" height="100%" fill="{background}"/>']
    for edge in edges:
        first, last = positions[edge['from']], positions[edge['to']]
        x1, y1 = coordinates[first]
        x2, y2 = coordinates[last]
        if first == last:
            points = f'{x1+180},{y1+center} {x1+215},{y1+center} {x1+215},{y1-35} {x1+90},{y1-35} {x1+90},{y1}'
        elif horizontal and last == first + 1:
            points = f'{x1+180},{y1+center} {x2},{y2+center}'
        elif horizontal:
            points = f'{x1+90},{y1} {x1+90},40 {x2+90},40 {x2+90},{y2}'
        elif last == first + 1:
            points = f'{x1+90},{y1+node_height} {x2+90},{y2}'
        else:
            points = f'{x1},{y1+center} 40,{y1+center} 40,{y2+center} {x2},{y2+center}'
        parts.append(f'<polyline points="{points}" fill="none" stroke="{edge_color}" stroke-width="{attribute(spec["edge_width"])}" marker-end="url(#arrow)"/>')
    for lines, (x, y) in zip(wrapped, coordinates):
        parts.append(f'<rect x="{x}" y="{y}" width="180" height="{node_height}" rx="{attribute(spec["node_radius"])}" fill="{fill}" stroke="{border}" stroke-width="{attribute(spec["node_border_width"])}"/>')
        for index, line in enumerate(lines):
            text_y = y + center + (index - (len(lines)-1)/2) * line_height
            parts.append(f'<text x="{x+90}" y="{text_y}" text-anchor="middle" dominant-baseline="middle" font-family="{attribute(family)}" font-size="{font_size}" fill="{text_color}">{escape(line)}</text>')
    parts.append('</svg>')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('\n'.join(parts), encoding='utf-8')
    return json.dumps({'output': str(target), 'nodes': len(nodes), 'edges': len(edges), 'editable': True}, ensure_ascii=False)

def diagram(targets: list[dict], base_dir=None) -> str:
    return run_targets(lambda target: draw_target(**target, base_dir=base_dir), targets, base_dir=base_dir)

def keep_grid_clear_of_labels(figure):
    """仅在数据标签周围断开网格线，保留网格样式和所有数据位置。"""
    figure.canvas.draw()
    renderer = figure.canvas.get_renderer()
    padding = figure.dpi * 2 / 72
    for axes in figure.axes:
        boxes = [text.get_window_extent(renderer).padded(padding)
                 for text in axes.texts if text.get_visible() and text.get_text()]
        if not boxes:
            continue
        for line in [*axes.get_xgridlines(), *axes.get_ygridlines()]:
            if not line.get_visible():
                continue
            points = line.get_transform().transform(line.get_path().vertices)
            if len(points) != 2:
                continue
            horizontal = abs(points[0][1]-points[1][1]) < .01
            dimension = 0 if horizontal else 1
            fixed = float(points[0][1-dimension])
            intervals = [(float(min(points[:,dimension])),float(max(points[:,dimension])))]
            changed = False
            for box in boxes:
                low, high = (box.y0,box.y1) if horizontal else (box.x0,box.x1)
                if not low <= fixed <= high:
                    continue
                start, stop = (box.x0,box.x1) if horizontal else (box.y0,box.y1)
                remaining = []
                for left,right in intervals:
                    if stop<=left or start>=right:
                        remaining.append((left,right))
                    else:
                        changed = True
                        if start>left:remaining.append((left,start))
                        if stop<right:remaining.append((stop,right))
                intervals = remaining
            if not changed:
                continue
            line.set_visible(False)
            for left,right in intervals:
                display = [(left,fixed),(right,fixed)] if horizontal else [(fixed,left),(fixed,right)]
                coordinates = axes.transData.inverted().transform(display)
                axes.plot(coordinates[:,0],coordinates[:,1],color=line.get_color(),
                          linewidth=line.get_linewidth(),linestyle=line.get_linestyle(),
                          alpha=line.get_alpha(),zorder=(axes.yaxis if horizontal else axes.xaxis).get_zorder(),scalex=False,scaley=False)


def save_figure(figure,target,spec):
    keep_grid_clear_of_labels(figure)
    qa=load_skill_script('visualizer','research-figures','figure_qa')
    qa.save_bundle(figure,target,spec)


def process(target: dict,base: Path) -> dict:
    paths.bind(paths.Roots(base))
    kind=target.pop('kind',target.pop('action','bar'))
    if kind in {'volcano','roc','dotplot','marginal','paired'}:
        return load_skill_script('visualizer','research-figures','nature_templates').process({'kind':kind,**target},base)
    if kind=='audit':
        return load_skill_script('visualizer','research-figures','figure_qa').audit_file(project_path(target['path'],base))
    handlers={'bar':bar_chart,'line':line_chart,'pie':pie_chart,'scatter':scatter_chart,'diagram':diagram}
    if kind not in handlers:raise ValueError('不支持的图型')
    output=project_path(target['output'],base)
    if kind=='diagram':
        import tempfile
        with tempfile.TemporaryDirectory(prefix='.diagram-',dir=base) as folder:
            candidate=Path(folder)/output.name
            staged={**target,'output':str(candidate)}
            result=handlers[kind]([staged],base)
            report=load_skill_script('visualizer','research-figures','figure_qa').audit_svg(candidate)
            if report['verdict']=='FAIL':raise ValueError('结构图未通过SVG审核：'+json.dumps(report,ensure_ascii=False))
            output.parent.mkdir(parents=True,exist_ok=True)
            candidate.replace(output)
        record=output.with_suffix('.qa.json')
        record.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        return {'files':[str(output),str(record)],'audit':report,'log':result.replace(str(candidate),str(output))}
    result=handlers[kind]([target],base)
    files=[str(output),*[str(output.with_suffix(ext)) for ext in ['.pdf','.svg','.audit.json','.alignment.json']]]
    if target.get('svg_output'):files.append(str(project_path(target['svg_output'],base)))
    return {'files':list(dict.fromkeys(files)),'log':result}


if __name__=='__main__':run_cli(process,fields=('kind','output','path','title','xlabel','ylabel',
    'categories','series','svg_output','caption','horizontal','markers','style','source'))
