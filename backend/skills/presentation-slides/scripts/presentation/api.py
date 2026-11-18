"""本地PPT函数接口：直接处理文件与对象，不启动命令行引擎。"""
import json
from pathlib import Path

from .messages import collect_messages
from .pptx_to_svg.converter import ConvertOptions,convert_pptx_to_svg
from .svg_quality.checker import SVGQualityChecker
from .svg_to_pptx.pptx_package.exporter import ExportOptions,export_project
from .pptx_delivery_check import audit_pptx_delivery


def import_presentation(source: Path,workspace: Path) -> dict:
    result=convert_pptx_to_svg(source,workspace,ConvertOptions(roundtrip=True))
    return {'pages':len(result.slides),'canvas':list(result.canvas_px),
            'diagnostic_count':len(result.diagnostics)}


def validate_workspace(workspace: Path,route: str) -> dict:
    checker=SVGQualityChecker(quick_generate=route=='generate')
    if route=='template':
        checker.check_roundtrip_workspace(str(workspace))
    else:
        checker.check_directory(str(workspace))
    report=workspace/'validation/svg_quality_report.json'
    report.parent.mkdir(parents=True,exist_ok=True)
    checker.export_json_report(str(report),target=str(workspace),stage='final')
    result=json.loads(report.read_text(encoding='utf-8'))
    if checker.summary['errors']:
        raise RuntimeError('SVG质量检查未通过：'+json.dumps(result,ensure_ascii=False)[-16000:])
    return result


def export_presentation(workspace: Path,output: Path,route: str,preserve_motion: bool) -> str:
    options=ExportOptions(project_path=str(workspace),output=str(output),quiet=True,
                          roundtrip=route=='template',quick_generate=route=='generate',
                          native_objects=True,with_notes=True,no_animations=not preserve_motion)
    with collect_messages() as messages:
        code=export_project(options)
    log=''.join(messages)
    if code:
        raise RuntimeError('PPT导出失败：'+log[-16000:])
    return log
