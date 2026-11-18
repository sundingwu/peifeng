"""核对项目文件并生成清单与外部哈希回执；不判断科学结论。"""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[4]))
import ast
import json
import xml.etree.ElementTree as ET
from backend.utils.skill_runtime import project_path,file_digest,atomic_output


def readable(path: Path) -> str:
    suffix=path.suffix.lower()
    if suffix=='.json':
        def reject(value):raise ValueError('非标准JSON常量：'+value)
        json.loads(path.read_text(encoding='utf-8-sig'),parse_constant=reject)
        return '标准JSON可解析'
    if suffix=='.py':
        ast.parse(path.read_text(encoding='utf-8-sig'))
        return 'Python语法可解析；未执行'
    if suffix in {'.md','.txt','.csv','.bib'}:
        path.read_text(encoding='utf-8-sig')
        return 'UTF-8文本可读；未验证正文主张'
    if suffix=='.svg':
        ET.parse(path)
        return 'SVG XML可解析；未视觉渲染'
    if suffix in {'.png','.jpg','.jpeg'}:
        from PIL import Image
        with Image.open(path) as picture:picture.verify()
        return '图片可解码；未核验图中数值'
    if suffix in {'.docx','.xlsx','.pptx','.pdf'}:
        from backend.utils.skill_libraries import format_reader
        reader=format_reader(suffix)
        if suffix=='.pdf':return f'PDF可解析，共{reader.page_count(path)}页；未视觉渲染'
        reader.inspect_document(path)
        return 'Office结构可解析；未原生分页渲染'
    return '仅核验原始字节和存在性'


def process(target: dict, base: Path) -> dict:
    files=target.get('files')
    if not isinstance(files,list) or not files or any(not isinstance(name,str) or not name.strip() for name in files):
        raise ValueError('files必须为非空项目文件路径数组')
    manifest=project_path(target['manifest'],base); report=project_path(target['report'],base)
    if manifest.suffix.lower()!='.md' or report.suffix.lower()!='.json':
        raise ValueError('manifest须为.md，report须为.json')
    records=[]; snapshots=[]; seen=set()
    for name in files:
        path=project_path(name,base)
        if path in seen or path in {manifest,report}:continue
        seen.add(path)
        if not path.is_file():raise ValueError('必需文件不存在：'+name)
        digest=file_digest(path); checked=readable(path)
        if file_digest(path)!=digest:raise RuntimeError('检查期间输入发生改变：'+name)
        records.append({'path':path.relative_to(base.resolve()).as_posix(),'bytes':path.stat().st_size,
                        'sha256':digest,'read_check':checked})
        snapshots.append((path,digest))
    if not records:raise ValueError('files至少需要一个清单/回执以外的实际输入文件')
    manifest_name=manifest.relative_to(base.resolve()).as_posix(); report_name=report.relative_to(base.resolve()).as_posix()
    lines=['# 实际交付清单','',
           '此清单只记录实际存在的文件、字节数、SHA-256和只读可解析检查，不证明正文、数据来源或科学结论正确。',
           '历史数值核验快照保留原版本信息；当前文件哈希以本轮外部回执为准。清单和回执不嵌入自身哈希，避免自引用循环。','',
           '| 文件 | 字节数 | SHA-256 | 实际检查 |','| --- | ---: | --- | --- |']
    for row in records:
        lines.append(f"| {row['path']} | {row['bytes']} | {row['sha256']} | {row['read_check']} |")
    lines += [f'| {manifest_name} | — | 见外部JSON回执 | 本轮生成，不自引用 |',
              f'| {report_name} | — | 不自引用 | 本轮生成的外部校验回执 |','']
    with atomic_output(manifest,sources=snapshots) as candidate:
        candidate.write_text('\n'.join(lines),encoding='utf-8')
    digest=file_digest(manifest)
    records.append({'path':manifest_name,'bytes':manifest.stat().st_size,'sha256':digest,'read_check':'本轮清单，哈希记录于外部JSON回执'})
    result={'status':'pass','files':records,'report':report_name,'self_hash':None,
            'scope':'存在性、原始字节SHA-256和所列格式只读可解析检查；不证明科学主张，不含Office原生视觉。'}
    with atomic_output(report,sources=[*snapshots,(manifest,digest)]) as candidate:
        candidate.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    return {'status':'pass','files':[manifest_name,report_name],'checked_files':len(records),'scope':result['scope']}


if __name__=='__main__':
    from backend.utils.skill_runtime import run_cli
    run_cli(process)
