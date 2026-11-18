"""PPT制作：直接调用skill内的原生导入、编辑校验与导出实现。"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from pptx import Presentation

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0,str(SKILL/'scripts'))
from presentation.api import import_presentation,validate_workspace,export_presentation
from presentation.messages import collect_messages
from presentation.stamp_native_fallbacks import stamp_file
from presentation.compose import compose_pages
DEFAULT_TEMPLATE = SKILL / 'assets' / 'project-defense-template.pptx'
GROUP_TEMPLATE = SKILL / 'assets' / 'group-meeting-template.pptx'
GUIDES = {'svg','shapes','charts','tables','text'}


def project_path(value: str, base: Path) -> Path:
    path = (base / value).resolve()
    if not path.is_relative_to(base.resolve()):
        raise ValueError('路径必须位于当前项目内')
    return path


def choose_template(plan: dict, base: Path) -> Path:
    if plan.get('template'):
        return project_path(plan['template'], base)
    kind = plan.get('template_kind', plan.get('mode', 'defense'))
    if kind not in {'defense', 'group'}:
        raise ValueError('模板类型必须为defense或group')
    return GROUP_TEMPLATE if kind == 'group' else DEFAULT_TEMPLATE


def walk(shapes):
    for shape in shapes:
        yield shape
        if shape.shape_type == 6:
            yield from walk(shape.shapes)


def inspect_template(path: Path) -> dict:
    book = Presentation(path)
    pages = []
    for number, slide in enumerate(book.slides, 1):
        shapes = []
        for shape in walk(slide.shapes):
            row = {'id': shape.shape_id, 'name': shape.name,
                   'box': [round(value / 914400, 3) for value in
                           (shape.left, shape.top, shape.width, shape.height)]}
            if shape.has_text_frame:
                row['text'] = shape.text
            shapes.append(row)
        pages.append({'template_page': number, 'shapes': shapes,
                      'has_animation': any(node.tag.endswith('}timing') for node in slide._element.iter())})
    return {'template': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'size_inches': [book.slide_width / 914400, book.slide_height / 914400],
            'pages': pages, 'coordinate_note': '组合内box为局部英寸；导入后的SVG使用viewBox坐标。'}


def prepare(plan: dict, base: Path) -> dict:
    workspace = project_path(plan['workspace'], base)
    if workspace.exists() and any(workspace.iterdir()):
        raise ValueError('编辑工作区必须为空；继续编辑请直接使用已有工作区')
    template = choose_template(plan, base)
    digest = hashlib.sha256(template.read_bytes()).hexdigest()
    with collect_messages() as messages:
        imported=import_presentation(template,workspace)
    log=''.join(messages)
    if hashlib.sha256(template.read_bytes()).hexdigest() != digest:
        raise RuntimeError('导入期间源模板发生变化')
    return {'status': 'completed', 'workspace': str(workspace), 'template_sha256': digest,
            'summary': str(workspace / 'authoring-svg-flat' / 'authoring_summary.json'),
            'conversion_report': str(workspace / 'validation' / 'conversion-report.json'), 'import':imported,'log': log}


def copy_guide(plan: dict, base: Path) -> dict:
    topic = plan.get('topic', 'svg')
    if topic not in GUIDES:
        raise ValueError('topic支持：' + ', '.join(GUIDES))
    target = project_path(plan.get('output', f'internal/ppt-guides/{topic}.md'), base)
    if target.suffix.lower() != '.md':
        raise ValueError('规则导出文件必须为.md')
    target.parent.mkdir(parents=True, exist_ok=True)
    text=(SKILL/'references/group-meeting.md').read_text(encoding='utf-8')
    heading='## 技术语法：'+topic
    start=text.index(heading)
    end=text.find('\n## ',start+len(heading))
    target.write_text(text[start:end if end!=-1 else len(text)],encoding='utf-8')
    return {'status': 'completed', 'guide': str(target), 'topic': topic,
            'scope': '仅参考技术语法；执行流程以培风skill为准，不启用多agent或外部服务。'}


def build(plan: dict, base: Path) -> dict:
    if any(key in plan for key in ('slides', 'elements', 'style')):
        raise ValueError('旧slides/elements/style制作参数已迁移为SVG工作区，请按技能参考使用prepare/build')
    workspace = project_path(plan['workspace'], base)
    output = project_path(plan['output'], base)
    route = plan.get('route', 'template')
    if route not in {'template', 'generate'}:
        raise ValueError('route必须为template或generate')
    if not workspace.is_dir():
        raise ValueError('工作区不存在，请先prepare或创建svg_output')
    if output.suffix.lower() != '.pptx':
        raise ValueError('输出必须为.pptx')
    if output.exists() and not plan.get('overwrite', False):
        raise ValueError('输出已存在；覆盖需显式overwrite=true')
    protected = {DEFAULT_TEMPLATE.resolve(), GROUP_TEMPLATE.resolve(),
                 (workspace / 'sources' / 'source.pptx').resolve()}
    manifest = workspace / 'analysis' / 'roundtrip_manifest.json'
    if manifest.is_file():
        original = json.loads(manifest.read_text(encoding='utf-8')).get('source', {}).get('originalPath')
        if original:
            protected.add(Path(original).resolve())
    if output in protected:
        raise ValueError('不能覆盖源模板或工作区原稿')
    expected = plan.get('expected_pages')
    if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int) or expected < 1):
        raise ValueError('expected_pages必须为正整数')
    with collect_messages() as messages:
        validate_workspace(workspace,route)
    log=''.join(messages)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.ppt-', dir=output.parent) as folder:
        candidate = Path(folder) / 'candidate.pptx'
        log += export_presentation(workspace,candidate,route,plan.get('preserve_motion',False))
        from check_presentation import check_file
        check=check_file(candidate,expected)
        if check['errors']:
            raise RuntimeError('生成文件检查失败：'+json.dumps(check,ensure_ascii=False))
        candidate.replace(output)
    check['report']['file']['path'] = str(output)
    return {'status': 'completed', 'engine': 'PeiFeng native presentation', 'output': str(output),
            'pages': check['pages'], 'check': check, 'workspace': str(workspace), 'log': log}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--base-dir', type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        plan = json.loads(args.input.read_text(encoding='utf-8-sig'))
        if not isinstance(plan, dict):
            raise ValueError('参数必须为JSON对象')
        base = args.base_dir.resolve()
        report = project_path(plan['report'], base) if plan.get('report') else None
        if report and (report.suffix.lower() != '.json' or report == args.input.resolve()):
            raise ValueError('report必须为独立的.json文件')
        action = plan.get('action', 'build')
        if action == 'inspect':
            result = inspect_template(choose_template(plan, base))
        elif action == 'prepare':
            result = prepare(plan, base)
        elif action == 'guide':
            result = copy_guide(plan, base)
        elif action == 'compose':
            result = compose_pages(plan, base)
        elif action == 'sync_native':
            target=project_path(plan['path'],base)
            paths=sorted(target.glob('*.svg')) if target.is_dir() else [target]
            result={'status':'completed','objects':[{'path':str(path),'result':stamp_file(path,write=bool(plan.get('write',False)))} for path in paths]}
        elif action == 'build':
            result = build(plan, base)
        else:
            raise ValueError('action支持inspect/prepare/compose/guide/sync_native/build')
        if report:
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps({'status': 'completed', 'report': str(report), 'pages': result.get('pages') if isinstance(result.get('pages'), int) else None}, ensure_ascii=False))
        else:
            print(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        print(json.dumps({'status': 'failed', 'error': str(error)}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
