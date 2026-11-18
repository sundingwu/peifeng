"""文档技能的Python执行入口；不注册为agent tool。"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pdf_forms.extract_form_field_info import write_field_info
from pdf_forms.fill_fillable_fields import fill_pdf_fields
from pdf_forms.fill_pdf_form_with_annotations import fill_pdf_form
from copy import deepcopy
from types import SimpleNamespace
from backend.utils.web import run_targets
from backend.utils.skill_runtime import project_path, file_digest, atomic_output

def build_pdf():
    """Generate Unicode PDF documents using Python libraries only."""

    from pathlib import Path
    from xml.sax.saxutils import escape
    from backend.utils.skill_runtime import project_path as resolve_file
    from backend.utils.web import run_targets



    def pdf(targets, base_dir=None):
        def create(target):
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.cidfonts import UnicodeCIDFont
            from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
            from reportlab.lib.styles import ParagraphStyle
            path = resolve_file(target['file'], base_dir)
            pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
            style = ParagraphStyle('body', fontName='STSong-Light', fontSize=11, leading=17)
            contents = ([target['title'], ''] if target.get('title') else []) + target['text'].splitlines()
            story = [Paragraph(escape(line), style) if line.strip() else Spacer(1, 8) for line in contents]
            path.parent.mkdir(parents=True, exist_ok=True)
            with atomic_output(path) as temporary:
                SimpleDocTemplate(str(temporary)).build(story)
                from pypdf import PdfReader
                if not PdfReader(temporary).pages:
                    raise ValueError('未生成PDF页面')
            return str(path)
        return run_targets(create, targets, workers=1, base_dir=base_dir)
    return pdf

pdf = build_pdf()

from backend.utils.text import file_sha256
from backend.memory.store import write_json
import tempfile
DEFAULT_WIDTH = 1400

def render_pages_target(
    path: str = '',
    width: int = 1400,
    pages: list = None,
    base_dir=None,
    session_dir=None
) -> str:
    """渲染并返回图片路径清单；输出放会话中间目录，相对路径以 base_dir 为基准。"""
    if not str(path or "").strip():
        raise RuntimeError("render_pages 需要 path 参数")
    source = render_resolve(path, base_dir)
    if not source.is_file():
        raise RuntimeError(f"文件不存在：{path}")
    suffix = source.suffix.lower()
    if suffix not in (".pptx", ".ppt", ".pdf", ".docx"):
        raise RuntimeError(f"只支持 .pptx、.ppt、.docx 与 .pdf（当前：{suffix or '无后缀'}）")

    output = (render_resolve(session_dir, None) if session_dir else source.parent) / "render"
    output.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix=source.stem + "-", dir=output))
    size = max(600, min(int(width or DEFAULT_WIDTH), 2400))
    wanted = [int(page) for page in (pages or []) if str(page).strip()]
    source_digest = file_sha256(source)

    if suffix == ".docx":
        pdf = output / "document.pdf"
        render_word(source, pdf)
        images = render_pdf(pdf, output, size, wanted)
    elif suffix == ".pdf":
        images = render_pdf(source, output, size, wanted)
    else:
        images = render_ppt(source, output, size)
        if wanted:
            images = [item for item in images if page_no(item) in wanted]
    if not images:
        raise RuntimeError(f"没有渲染出任何页：{source.name}（检查页码范围或文件是否损坏）")
    if file_sha256(source) != source_digest:
        raise RuntimeError('源文件在渲染期间改变，本次图片不能证明当前版本正确，请重新渲染')
    manifest = write_json(output / 'render.json', {'source': str(source), 'source_sha256': source_digest, 'images': images})

    lines = [f"已渲染 {len(images)} 页（{source.name}，宽 {size} 像素）："]
    lines.extend(f"  {item}" for item in images)
    lines.append(f'源文件 SHA-256：{source_digest}；版本记录：{manifest}')
    lines.append('用 read(targets=[{"path": "图片路径", "question": "检查版面"}]) 查看。')
    return "\n".join(lines)

def render_pdf(source: Path, output: Path, width: int, wanted: list[int]) -> list[str]:
    import pypdfium2 as pdfium
    images = []
    with pdfium.PdfDocument(str(source)) as document:
        document.init_forms()
        for index in range(len(document)):
            if wanted and index + 1 not in wanted:
                continue
            page = document[index]
            try:
                bitmap = page.render(scale=width / page.get_width(), draw_annots=True, may_draw_forms=True)
                try:
                    target = output / f'page-{index+1:03}.png'
                    bitmap.to_pil().save(target)
                    images.append(str(target))
                finally:
                    bitmap.close()
            finally:
                page.close()
    return images

def render_ppt(source: Path, output: Path, width: int) -> list[str]:
    raise RuntimeError('Python-only模式不提供Office原生渲染，请用read view=structure或audit检查PPT结构')

def render_word(source: Path, output: Path) -> None:
    raise RuntimeError('Python-only模式不提供Word原生渲染，请用read view=structure或audit检查Word结构')

def page_no(path) -> int:
    """从文件名里取页码（幻灯片3.png / page-03.png 都能认）。"""
    digits = "".join(char for char in Path(path).stem if char.isdigit())
    return int(digits) if digits else 0

def render_resolve(path, base_dir) -> Path:
    """相对路径以 base_dir 为基准；绝对路径原样使用。"""
    target = Path(str(path))
    if target.is_absolute() or base_dir is None:
        return target
    return Path(base_dir) / target

def render_pages(targets: list[dict], base_dir=None, session_dir=None) -> str:
    """统一批处理入口，每项独立参数。"""
    return run_targets(lambda target: render_pages_target(**target, base_dir=base_dir, session_dir=session_dir), targets, workers=1, base_dir=base_dir)

def add_annotation_appearances(candidate: Path, original: Path) -> None:
    """为新增FreeText补可移植外观流，保留可编辑批注对象。"""
    import io
    import math
    import re
    from xml.sax.saxutils import escape
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import NameObject, DictionaryObject, DecodedStreamObject, ArrayObject, FloatObject
    from reportlab.pdfgen import canvas
    from reportlab.platypus import Paragraph
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.colors import HexColor
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    source = PdfReader(original)
    writer = PdfWriter(clone_from=candidate)
    for number, page in enumerate(writer.pages):
        count = len(source.pages[number].get('/Annots', []))
        for reference in page.get('/Annots', [])[count:]:
            annotation = reference.get_object()
            if annotation.get('/Subtype') != '/FreeText' or annotation.get('/AP'):
                continue
            text = str(annotation.get('/Contents', ''))
            x0, y0, x1, y1 = map(float, annotation['/Rect'])
            width, height = x1-x0, y1-y0
            declaration = str(annotation.get('/DS', ''))
            match = re.search(r'([0-9.]+)pt', declaration)
            size = float(match.group(1)) if match else 14
            if not all(math.isfinite(value) and value > 0 for value in [width, height, size]):
                raise ValueError('标注框宽高和字号必须为正有限数')
            color = re.search(r'color:\s*(#[0-9a-fA-F]{6})', declaration)
            font = 'STSong-Light' if any(ord(char)>127 for char in text) else 'Helvetica'
            if font == 'STSong-Light':
                pdfmetrics.registerFont(UnicodeCIDFont(font))
            style = ParagraphStyle('annotation', fontName=font, fontSize=size, leading=size*1.2,
                                   textColor=HexColor(color.group(1) if color else '#000000'))
            paragraph = Paragraph(escape(text).replace('\n','<br/>'), style)
            _, text_height = paragraph.wrap(max(1,width-4), max(1,height-4))
            if text_height > height-4:
                raise ValueError('标注文字超出框，请扩大entry_bounding_box或调整字号')
            buffer = io.BytesIO()
            drawing = canvas.Canvas(buffer, pagesize=(width,height))
            paragraph.drawOn(drawing,2,height-2-text_height)
            drawing.save()
            appearance_page = PdfReader(io.BytesIO(buffer.getvalue())).pages[0]
            appearance = DecodedStreamObject()
            appearance.set_data(appearance_page.get_contents().get_data())
            appearance.update({NameObject('/Type'):NameObject('/XObject'), NameObject('/Subtype'):NameObject('/Form'),
                               NameObject('/BBox'):ArrayObject([FloatObject(0),FloatObject(0),FloatObject(width),FloatObject(height)]),
                               NameObject('/Resources'):appearance_page['/Resources'].clone(writer)})
            annotation[NameObject('/AP')] = DictionaryObject({NameObject('/N'):writer._add_object(appearance)})
    buffer = io.BytesIO()
    writer.write(buffer)
    candidate.write_bytes(buffer.getvalue())


def process(target: dict, base: Path) -> dict:
    import json
    from pypdf import PdfReader, PdfWriter
    action=target.pop('action','create')
    if action=='create':
        output=project_path(target['file'],base)
        overwrite=target.pop('overwrite',False)
        if output.exists() and not overwrite:
            raise ValueError('输出已存在；覆盖需overwrite=true')
        pdf([target],base)
        return {'files':[str(output)],'pages':len(PdfReader(output).pages),'file':str(output)}
    source=project_path(target['path'],base) if target.get('path') else None
    if action=='render':
        if source.suffix.lower()!='.pdf':
            raise ValueError('这里只提供PDF渲染，Office请使用read结构检查')
        log=render_pages([target],base,session_dir=base/'internal')
        images=[line.strip() for line in log.splitlines() if line.strip().endswith('.png') and Path(line.strip()).is_file()]
        return {'files':images,'source_sha256':file_digest(source),'log':log}
    if action=='form_info':
        with tempfile.TemporaryDirectory() as folder:
            info=Path(folder)/'fields.json'
            write_field_info(str(source), str(info))
            fields=json.loads(info.read_text(encoding='utf-8-sig'))
        return {'files':[],'fields':fields,'fillable':bool(fields),'source_sha256':file_digest(source)}
    if action=='ocr':
        raise ValueError('当前不安装OCR模型或Tesseract；不能把扫描图片当成已提取文字')
    allowed={'merge','select','split','rotate','encrypt','decrypt','watermark','extract_images','fill_form','annotate'}
    if action not in allowed:
        raise ValueError('PDF action支持create/render/merge/select/split/rotate/encrypt/decrypt/watermark/extract_images/form_info/fill_form/annotate')
    if action=='extract_images':
        directory=project_path(target['directory'],base)
        if directory.exists():
            raise ValueError('图片目录已存在，请使用新目录')
        directory.parent.mkdir(parents=True,exist_ok=True)
        files=[]
        with tempfile.TemporaryDirectory(dir=directory.parent) as folder:
            temporary=Path(folder)/'images';temporary.mkdir()
            reader=PdfReader(source)
            for number,page in enumerate(reader.pages,1):
                for index,image in enumerate(page.images,1):
                    name=f'page-{number:03}-{index:03}'+Path(image.name).suffix
                    (temporary/name).write_bytes(image.data);files.append(str(directory/name))
            temporary.replace(directory)
        return {'files':files}
    output=project_path(target['file'],base)
    if output.suffix.lower()!='.pdf':
        raise ValueError('输出必须为.pdf')
    if output.exists() and not target.get('overwrite',False):
        raise ValueError('输出已存在；覆盖需overwrite=true')
    if action=='merge' and (not isinstance(target.get('paths'),list) or not target['paths']):
        raise ValueError('merge需要非空paths数组')
    if action in {'select','split'} and (not isinstance(target.get('pages'),list) or not target['pages']):
        raise ValueError('select/split需要非空pages数组')
    if 'pages' in target and (not isinstance(target['pages'],list) or not target['pages']):
        raise ValueError('pages必须为非空页码数组')
    inputs=[project_path(value,base) for value in target['paths']] if action=='merge' else [source]
    if not inputs or any(path is None for path in inputs):
        raise ValueError('需要PDF输入；merge使用非空paths数组')
    if action=='watermark':inputs.append(project_path(target['watermark'],base))
    if action in {'fill_form','annotate'}:inputs.append(project_path(target['fields'],base))
    snapshots=[(path,file_digest(path)) for path in inputs]
    with atomic_output(output,sources=snapshots) as candidate:
        if action in {'fill_form','annotate'}:
            operation=fill_pdf_fields if action=='fill_form' else fill_pdf_form
            operation(str(source),str(inputs[-1]),str(candidate))
            if action=='annotate':
                add_annotation_appearances(candidate, source)
        else:
            writer=PdfWriter()
            if action=='merge':
                for path in inputs:writer.append(path)
            else:
                reader=PdfReader(source)
                if reader.is_encrypted:
                    if not reader.decrypt(target.get('password','')):
                        raise ValueError('PDF密码不正确')
                selected=target.get('pages') or list(range(1,len(reader.pages)+1))
                if any(isinstance(n,bool) or not isinstance(n,int) or n<1 or n>len(reader.pages) for n in selected):
                    raise ValueError('pages必须为有效的1起始页码数组')
                if action in {'select','split'}:
                    for number in selected:writer.add_page(reader.pages[number-1])
                else:
                    writer.clone_document_from_reader(reader)
                    if action=='rotate':
                        degrees=target['degrees']
                        if isinstance(degrees,bool) or not isinstance(degrees,int) or degrees%90:
                            raise ValueError('degrees必须为90的整数倍')
                        for number in selected:writer.pages[number-1].rotate(degrees)
                    elif action=='watermark':
                        mark=PdfReader(inputs[-1])
                        if len(mark.pages)!=1:raise ValueError('水印PDF必须为单页')
                        for number in selected:writer.pages[number-1].merge_page(mark.pages[0])
                    elif action=='encrypt':
                        if not target.get('password'):raise ValueError('encrypt需要非空password')
                        import importlib.util
                        if not any(importlib.util.find_spec(name) for name in ['cryptography', 'Crypto']):
                            raise ValueError('AES加密需要可选cryptography或PyCryptodome，当前未安装，未生成输出；不自动降级RC4')
                        writer.encrypt(target['password'],algorithm='AES-256')
            with candidate.open('wb') as stream:writer.write(stream)
        check=PdfReader(candidate)
        if check.is_encrypted:check.decrypt(target.get('password',''))
        count=len(check.pages)
        if not count:raise ValueError('输出PDF没有页面')
    return {'files':[str(output)],'file':str(output),'pages':count,'action':action}


if __name__ == '__main__':
    from backend.utils.skill_runtime import run_cli
    run_cli(process)
