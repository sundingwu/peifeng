"""在模板空白内容页上排入文字与图件，保留原生背景、母版和版式。"""
from copy import deepcopy
import base64
import json
from pathlib import Path
import unicodedata
import xml.etree.ElementTree as ET

from pptx import Presentation

SVG='http://www.w3.org/2000/svg'
ET.register_namespace('', SVG)


def project_file(value: str, base: Path) -> Path:
    path=(base/value).resolve()
    if not path.is_relative_to(base.resolve()):
        raise ValueError('路径必须位于当前项目内')
    return path


def wrap_lines(text: str, width: float, size: float) -> list[str]:
    lines=[]
    for paragraph in str(text).splitlines() or ['']:
        current=''; used=0.0
        for character in paragraph:
            advance=size*(1 if unicodedata.east_asian_width(character) in {'W','F'} else 0.56)
            if current and used+advance>width:
                lines.append(current); current=''; used=0.0
            current+=character; used+=advance
        lines.append(current)
    return lines


def add_text(parent, identifier, text, x, y, width, size, color, font, max_lines):
    lines=wrap_lines(text,width,size)
    if len(lines)>max_lines:
        raise ValueError(f'{identifier}内容过密（{len(lines)}行，上限{max_lines}），请精简或拆页')
    height=len(lines)*size*1.5
    group=ET.SubElement(parent,'{'+SVG+'}g',{'id':identifier,'data-pptx-bounds':f'{x} {y-size} {width} {height+size}'})
    node=ET.SubElement(group,'{'+SVG+'}text',{'x':str(x),'y':str(y),'font-size':str(size),
        'font-family':font,'fill':color})
    for number,line in enumerate(lines):
        ET.SubElement(node,'{'+SVG+'}tspan',{'x':str(x),'y':str(y+number*size*1.5)}).text=line


def compose_pages(plan: dict, base: Path) -> dict:
    workspace=project_file(plan['workspace'],base)
    source=workspace/'sources/source.pptx'
    book=Presentation(source)
    source_slide=plan.get('source_slide',2)
    if type(source_slide) is not int or not 1<=source_slide<=len(book.slides):
        raise ValueError('source_slide必须为模板中的有效页码')
    if len(book.slides[source_slide-1].shapes):
        raise ValueError('compose仅使用无页面级对象的模板内容页；请选择空白内容页，复杂原页用SVG编辑保留对象')
    pages=plan.get('pages')
    if not isinstance(pages,list) or not pages or any(not isinstance(page,dict) for page in pages):
        raise ValueError('pages必须为非空页面对象数组')
    flat=workspace/'authoring-svg-flat'
    original=ET.parse(flat/f'slide_{source_slide:02}.svg').getroot()
    for node in original:
        if node.tag=='{'+SVG+'}g' and not node.get('data-pptx-source-ref'):
            raise ValueError('源内容页SVG已有自定义内容，请使用未修改的空白源页或重新prepare新工作区')
    view=[float(value) for value in original.get('viewBox','').split()]
    if len(view)!=4:
        raise ValueError('模板SVG缺少有效viewBox')
    _,_,width,height=view
    style=plan.get('style') or {}
    font=style.get('font','Microsoft YaHei')
    title_size=float(style.get('title_size',32))*width/1280
    body_size=float(style.get('body_size',26))*width/1280
    if not 18<=title_size*1280/width<=48 or not 18<=body_size*1280/width<=36:
        raise ValueError('title_size须18–48，body_size须18–36（以1280宽画布为基准）')
    accent=style.get('accent','#2F5597'); ink=style.get('text','#24364B')
    prepared=[]; layouts=[]
    for number,page in enumerate(pages,1):
        if not str(page.get('title','')).strip() or not str(page.get('notes','')).strip():
            raise ValueError(f'第{number}页需要title和逐页notes')
        root=deepcopy(original)
        body=ET.SubElement(root,'{'+SVG+'}g',{'id':f'content-{number}','data-peifeng-composed':'true'})
        x=width*0.065; top=height*0.16; full_width=width*0.87
        add_text(body,f'title-{number}',page['title'],x,top,full_width,title_size,accent,font,2)
        bullets=page.get('bullets',[])
        if not isinstance(bullets,list) or any(not isinstance(item,str) for item in bullets):
            raise ValueError('bullets必须为文本数组')
        image=page.get('image')
        text_width=width*(0.40 if image else 0.86)
        body_text='\n'.join('• '+item for item in bullets)
        used_body_size=body_size
        line_limit=int(height*0.53/(used_body_size*1.5))
        if len(wrap_lines(body_text,text_width,used_body_size))>line_limit and style.get('auto_fit_body','body_size' not in style):
            # 默认只允许缩小2px，最低24px（18pt）；更密的内容必须精简，不无限缩字。
            used_body_size=min(body_size,max(24*width/1280,body_size-2*width/1280))
            line_limit=int(height*0.53/(used_body_size*1.5))
        try:
            add_text(body,f'body-{number}',body_text,x,height*0.30,text_width,used_body_size,ink,font,line_limit)
        except ValueError as error:
            raise ValueError(str(error)+'；配图页只保留少量短要点，详细均值/检验表移到notes；也可移除本页配图增加文字宽度。') from error
        layouts.append({'page':number,'body_size':used_body_size,'body_lines':len(wrap_lines(body_text,text_width,used_body_size)),
                        'max_body_lines':line_limit})
        if image:
            path=project_file(image,base)
            if path.suffix.lower() not in {'.png','.jpg','.jpeg'}:
                raise ValueError('image仅支持项目内PNG/JPEG；原生矢量图表使用SVG编辑路径')
            from PIL import Image
            with Image.open(path) as picture:
                picture.verify()
            mime='image/png' if path.suffix.lower()=='.png' else 'image/jpeg'
            group=ET.SubElement(body,'{'+SVG+'}g',{'id':f'figure-{number}',
                'data-pptx-bounds':f'{width*0.51} {height*0.28} {width*0.43} {height*0.54}'})
            ET.SubElement(group,'{'+SVG+'}image',{'x':str(width*0.51),'y':str(height*0.28),
                'width':str(width*0.43),'height':str(height*0.54),'preserveAspectRatio':'xMidYMid meet',
                'href':'data:'+mime+';base64,'+base64.b64encode(path.read_bytes()).decode('ascii')})
        if page.get('source'):
            add_text(body,f'source-{number}',str(page['source']),x,height*0.91,full_width,
                     14*width/1280,'#56677B',font,2)
        filename=f'composed_{number:03}.svg'
        destination=flat/filename
        note=workspace/'notes'/f'composed_{number:03}.md'
        if not plan.get('overwrite',False) and (destination.exists() or note.exists()):
            raise ValueError('compose输出已存在；覆盖需overwrite=true')
        prepared.append((destination,ET.tostring(root,encoding='utf-8',xml_declaration=True),note,page['notes']))
    # 先完成全部页面与边界校验，再落盘，避免密集页失败时写下一半页面。
    for destination,content,note,notes in prepared:
        destination.write_bytes(content)
        note.parent.mkdir(parents=True,exist_ok=True)
        note.write_text(notes,encoding='utf-8')
    page_plan={'schema':'ppt-master.roundtrip-page-plan.v1',
               'pages':[{'source_slide':source_slide,'svg':path.name} for path,_,_,_ in prepared]}
    (workspace/'page_plan.json').write_text(json.dumps(page_plan,ensure_ascii=False,indent=2),encoding='utf-8')
    return {'status':'completed','pages':len(prepared),'workspace':str(workspace),
            'files':[str(item) for path,_,note,_ in prepared for item in [path,note]],
            'layout':layouts,
            'next':'build执行实际质量检查、PPTX导出和交付检查'}
