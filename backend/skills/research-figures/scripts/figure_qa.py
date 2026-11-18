"""Nature Figure审核适配；用现有PDFium替换PyMuPDF几何读取。"""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[4]))
import ctypes
import json
import tempfile
from backend.orchestration.orchestrate import load_module

ROOT = Path(__file__).resolve().parents[3] / 'vendor' / 'nature_figure' / 'scripts'


def module(name):
    return load_module(ROOT/(name+'.py'))


def pdf_geometry(path, core):
    import pypdfium2 as pdfium
    from pypdfium2 import raw
    pages=[]
    with pdfium.PdfDocument(path) as document:
        for number,page in enumerate(document,1):
            height=page.get_height()
            def box(rect):
                x0,y0,x1,y1=map(float,rect)
                return min(x0,x1),height-max(y0,y1),max(x0,x1),height-min(y0,y1)
            geometry=core.PageGeometry(number,(0,0,page.get_width(),height))
            text=page.get_textpage()
            try:
                for index in range(text.count_rects()):
                    bounds=text.get_rect(index)
                    content=text.get_text_bounded(*bounds).strip()
                    if content:
                        geometry.texts.append(core.TextBox(index,content,box(bounds)))
            finally:text.close()
            for index,obj in enumerate(page.get_objects()):
                bounds=box(obj.get_bounds())
                if obj.type==raw.FPDF_PAGEOBJ_IMAGE:
                    geometry.images.append(core.FilledRegion(index,bounds,'image'))
                if obj.type!=raw.FPDF_PAGEOBJ_PATH:continue
                fill=ctypes.c_int();stroke=ctypes.c_int()
                raw.FPDFPath_GetDrawMode(obj.raw,ctypes.byref(fill),ctypes.byref(stroke))
                if fill.value:geometry.fills.append(core.FilledRegion(index,bounds,'path'))
                if not stroke.value:continue
                matrix=obj.get_matrix();segments=[];last=None;start=None;controls=[]
                def point(x,y):
                    px,py=matrix.on_point(x,y)
                    return px,height-py
                for i in range(raw.FPDFPath_CountSegments(obj.raw)):
                    segment=raw.FPDFPath_GetPathSegment(obj.raw,i)
                    x=ctypes.c_float();y=ctypes.c_float()
                    raw.FPDFPathSegment_GetPoint(segment,ctypes.byref(x),ctypes.byref(y))
                    current=point(x.value,y.value);kind=raw.FPDFPathSegment_GetType(segment)
                    if kind==raw.FPDF_SEGMENT_MOVETO:last=start=current;controls=[]
                    elif kind==raw.FPDF_SEGMENT_LINETO:
                        if last is not None:segments.append((last,current))
                        last=current
                    elif kind==raw.FPDF_SEGMENT_BEZIERTO:
                        controls.append(current)
                        if len(controls)==3 and last is not None:
                            origin=last;a,b,end=controls
                            for step in range(1,17):
                                t=step/16;u=1-t
                                current=tuple(u**3*origin[k]+3*u*u*t*a[k]+3*u*t*t*b[k]+t**3*end[k] for k in [0,1])
                                segments.append((last,current));last=current
                            controls=[]
                    if raw.FPDFPathSegment_GetClose(segment) and last is not None and start is not None:
                        segments.append((last,start));last=start
                width=ctypes.c_float();raw.FPDFPageObj_GetStrokeWidth(obj.raw,ctypes.byref(width))
                if segments:geometry.strokes.append(core.StrokePath(index,bounds,float(width.value),tuple(segments)))
            pages.append(geometry)
            page.close()
    return pages


def audit_pdf(path: Path) -> dict:
    text=module('audit_pdf_text').audit_pdf(path.read_bytes(),minimum_pt=5)
    collision=module('audit_figure_collisions')
    result=collision.audit_geometries(pdf_geometry(path,collision))
    return {'files':[],'text':text,'collisions':result,
            'passed':bool(text['auditable'] and not text['below_minimum_count'] and result['auditable'] and not result['summary']['fail']),
            'scope':'PDFium文字/路径几何与Nature规则；曲线按16段近似，自动检查不能替代最终尺寸目视检查。'}


def audit_svg(path: Path) -> dict:
    core=module('audit_svg_schematic')
    result=core.audit_svg(path.read_bytes(),5,24,400)
    return {'verdict':core.verdict(result),**result,
            'scope':'Nature SVG字体/结构审查；碰撞和交叉须目视复核，不冒充完整几何验收。'}


def audit_file(path: Path) -> dict:
    """导出文件与自定义Python源码均可检查，结果不冒充科学结论。"""
    if path.suffix.lower()=='.pdf':
        return audit_pdf(path)
    if path.suffix.lower()=='.svg':
        report=audit_svg(path)
        return {'files':[],'passed':report['verdict']!='FAIL',**report}
    if path.suffix.lower()=='.py':
        from dataclasses import asdict
        core=module('validate_figure')
        findings=core.validate_source(path.read_text(encoding='utf-8-sig'),'python')
        summary=core.summarize(findings,False)
        return {'files':[],'passed':summary['ready'],'summary':summary,
                'findings':[asdict(row) for row in findings],
                'scope':'上游Python源码静态预检；间接调用的导出/对齐函数可能无法识别，须结合实际导出报告判断。'}
    raise ValueError('audit仅支持PDF、SVG或Python源码')


def save_bundle(figure,target: Path,spec: dict,tiff=False) -> dict:
    import matplotlib.pyplot as plt
    target.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.figure-',dir=target.parent) as folder:
        stage=Path(folder)/target.name
        alignment=module('audit_panel_alignment').require_matplotlib_panel_alignment(
            figure,json_out=stage.with_suffix('.alignment.json'),tolerance_pt=1.5,strict=True)
        with plt.rc_context({'pdf.fonttype':42,'svg.fonttype':'none'}):
            for extension in ['.pdf','.svg','.png']+(['.tiff'] if tiff else []):
                figure.savefig(stage.with_suffix(extension),dpi=int(spec.get('dpi',300)),bbox_inches='tight',facecolor=spec.get('background','white'))
        report=audit_pdf(stage.with_suffix('.pdf'))
        report['alignment']=alignment
        stage.with_suffix('.audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        if not report['passed']:
            plt.close(figure)
            raise ValueError('图件未通过Nature文字/碰撞审核：'+json.dumps(report,ensure_ascii=False)[-9000:])
        outputs={}
        for file in Path(folder).iterdir():
            destination=target.parent/file.name
            file.replace(destination)
            outputs[destination.suffix.lstrip('.') if destination.suffix!='.json' else destination.name]=str(destination)
    plt.close(figure)
    return outputs
