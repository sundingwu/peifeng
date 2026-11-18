"""迁移上游批注、run合并、OOXML校验；纯Python处理修订接受。"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from word_files import validate_office
from word_editing.api import edit_package, validate_package
from word_editing.office import helpers
from copy import deepcopy
import shutil
import tempfile
import zipfile

import defusedxml.minidom
from lxml import etree

from backend.utils.skill_runtime import (
    atomic_output, file_digest, project_path,
)

WORD = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'


def rewrite_parts(path: Path, replacements: dict[str, bytes]) -> None:
    with tempfile.TemporaryDirectory(dir=path.parent) as folder:
        temporary = Path(folder) / path.name
        with zipfile.ZipFile(path) as source, zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as output:
            for item in source.infolist():
                if not item.is_dir():
                    output.writestr(item, replacements.get(item.filename, source.read(item)))
        temporary.replace(path)


def add_comment_anchor(path: Path, paragraph_index: int) -> None:
    with zipfile.ZipFile(path) as archive:
        comments = defusedxml.minidom.parseString(archive.read('word/comments.xml'))
        identifiers = [int(n.getAttributeNS(WORD, 'id')) for n in comments.getElementsByTagNameNS(WORD, 'comment')]
        identifier = str(max(identifiers))
        document = defusedxml.minidom.parseString(archive.read('word/document.xml'))
    body = document.getElementsByTagNameNS(WORD, 'body')[0]
    paragraphs = [node for node in body.childNodes if node.nodeType == node.ELEMENT_NODE and node.localName == 'p']
    if isinstance(paragraph_index, bool) or not isinstance(paragraph_index, int) or not 1 <= paragraph_index <= len(paragraphs):
        raise ValueError('paragraph必须为inspect所列的有效段号，从1开始')
    paragraph = paragraphs[paragraph_index - 1]
    def element(name):
        node = document.createElementNS(WORD, 'w:' + name)
        node.setAttributeNS(WORD, 'w:id', identifier)
        return node
    start, end = element('commentRangeStart'), element('commentRangeEnd')
    first = next((n for n in paragraph.childNodes if n.nodeType == n.ELEMENT_NODE and n.localName != 'pPr'), None)
    paragraph.insertBefore(start, first)
    paragraph.appendChild(end)
    run = document.createElementNS(WORD, 'w:r')
    run.appendChild(element('commentReference'))
    paragraph.appendChild(run)
    rewrite_parts(path, {'word/document.xml': document.toxml(encoding='utf-8')})


def accept_revisions(data: bytes) -> bytes:
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    root = etree.fromstring(data, parser)
    name = lambda local: '{' + WORD + '}' + local
    deleted_marks = [p for p in root.iter(name('p')) if p.find('./' + name('pPr') + '/' + name('rPr') + '/' + name('del')) is not None]
    # 删除段落标记意味着与下一段连接；倒序处理连续的删除标记。
    for paragraph in reversed(deleted_marks):
        following = paragraph.getnext()
        if following is None or following.tag != name('p'):
            raise ValueError('段落标记删除跨越表格或文档边界，不能安全自动接受；请人工确认结构')
        properties = paragraph.find(name('pPr'))
        if properties is not None:
            paragraph.remove(properties)
        next_properties = following.find(name('pPr'))
        if next_properties is not None:
            paragraph.insert(0, deepcopy(next_properties))
        for child in list(following):
            if child.tag != name('pPr'):
                paragraph.append(child)
        following.getparent().remove(following)
    for node in reversed(list(root.iter())):
        parent = node.getparent()
        if parent is None or not isinstance(node.tag, str):
            continue
        local = etree.QName(node).localname
        if node.tag in {name('del'), name('moveFrom'), name('delText'), name('moveFromRangeStart'), name('moveFromRangeEnd'), name('moveToRangeStart'), name('moveToRangeEnd'), name('tblGridChange'), name('numberingChange')} or (node.tag.startswith('{' + WORD + '}') and local.endswith('PrChange')):
            parent.remove(node)
        elif node.tag in {name('ins'), name('moveTo')}:
            position = parent.index(node)
            for child in list(node):
                parent.insert(position, child)
                position += 1
            parent.remove(node)
    return etree.tostring(root, encoding='utf-8', xml_declaration=True)


def process(target: dict, base: Path) -> dict:
    action = target['action']
    source = project_path(target['path'], base)
    if action == 'validate':
        log = validate_package(source, project_path(target['original'], base) if target.get('original') else None, target.get('author'))
        return {'files': [], 'status': 'pass', 'log': log, 'scope': 'XML/关系及可选修订检查，不是渲染验收'}
    if action == 'unpack':
        output = project_path(target['directory'], base)
        if output.exists():
            raise ValueError('解包目录已存在，请选新目录，避免混入旧内容')
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=output.parent) as folder:
            temporary = Path(folder) / 'unpacked'
            with zipfile.ZipFile(source) as archive:
                helpers.safe_extract(archive, temporary)
            shutil.move(str(temporary), str(output))
        return {'files': [], 'directory': str(output)}
    if action not in {'comment', 'merge_runs', 'accept_changes', 'pack'}:
        raise ValueError('Word action支持create/inspect/patch/markdown/comment/merge_runs/accept_changes/validate/unpack/pack')
    output = project_path(target['file'], base)
    if output.suffix.lower() != '.docx':
        raise ValueError('输出必须为.docx')
    if output.exists() and not target.get('overwrite', False):
        raise ValueError('输出已存在；覆盖需overwrite=true')
    sources = [] if source.is_dir() else [(source, file_digest(source))]
    with atomic_output(output, sources=sources) as candidate:
        if action == 'comment':
            edit_package(source, candidate, text=target['text'], author=target.get('author', '培风'), parent=target.get('parent'))
            if target.get('parent') is None:
                add_comment_anchor(candidate, target['paragraph'])
        elif action == 'merge_runs':
            edit_package(source, candidate)
        elif action == 'pack':
            for entry in source.rglob('*'):
                if entry.is_symlink() or (hasattr(entry, 'is_junction') and entry.is_junction()):
                    raise ValueError('打包目录包含符号链接或目录联接，请先移除链接')
            helpers.rezip(source, candidate)
        else:
            shutil.copy2(source, candidate)
            with zipfile.ZipFile(source) as archive:
                updates = {n: accept_revisions(archive.read(n)) for n in archive.namelist() if n.startswith('word/') and n.endswith('.xml')}
            rewrite_parts(candidate, updates)
        validate_office(candidate)
        validation_log = validate_package(candidate, source if action != 'pack' else (project_path(target['original'], base) if target.get('original') else None))
    return {'files': [str(output)], 'file': str(output), 'action': action, 'status': 'pass', 'validation_log': validation_log}


if __name__ == '__main__':
    from backend.utils.skill_runtime import run_cli
    run_cli(process)
