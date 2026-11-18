from pathlib import Path
import sys
for _parent in Path(__file__).resolve().parents:
    if (_parent / 'backend').is_dir():
        sys.path.insert(0, str(_parent))
        break
import hashlib, json, sys
from backend.utils.skills import resolve_file, dispatch
from backend.memory.store import write_json

def paper_target(target: dict, base_dir=None) -> str:
    from pypdf import PdfReader
    if 'source_bundle' in target and type(target['source_bundle']) is not bool:
        raise ValueError('source_bundle必须为布尔值')
    path = resolve_file(target['path'], base_dir)
    reader = PdfReader(path)
    pages = target.get('pages', list(range(1, len(reader.pages)+1)))
    if not isinstance(pages,list) or not pages or any(type(page) is not int or page < 1 or page > len(reader.pages) for page in pages):
        raise ValueError('pages使用有效的1起始页码')
    if len(set(pages))!=len(pages):
        raise ValueError('pages不能含重复页码')
    full_scope=set(pages)==set(range(1,len(reader.pages)+1))
    result = {'source': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
              'level': 'extracted_full_text' if full_scope else 'extracted_partial_text',
              'page_count':len(reader.pages),'read_pages':pages,'full_scope':full_scope,
              'pages': [{'page': page, 'text': reader.pages[page-1].extract_text() or ''} for page in pages]}
    result['warnings'] = ['无文本页需要用户提供文字或符合约束的本地OCR，不能视为已读'] if any(not page['text'].strip() for page in result['pages']) else []
    if result['warnings']:
        result['level']='extracted_text_with_gaps'
    if target.get('source_bundle'):
        metadata=reader.metadata or {}
        result.update(format='peifeng.paper-source.v1',
                      metadata={'title':metadata.get('/Title'),'author':metadata.get('/Author')},
                      inventory_status='not_checked',
                      evidence_inventory={'figures':[],'tables':[],'equations':[]})
        for page in result['pages']:
            page['pdf_page']=page['page']
    if target.get('output'):
        write_json(resolve_file(target['output'], base_dir), result)
    return json.dumps(result, ensure_ascii=False)

def run(targets, base_dir=None):
    return dispatch(paper_target, targets, base_dir)

if __name__ == '__main__':
    from backend.utils.skills import run_script_cli
    raise SystemExit(run_script_cli(run, __doc__))
