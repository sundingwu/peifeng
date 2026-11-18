"""Bind exact evidence blocks into Markdown after verifying source hashes."""
from pathlib import Path
import hashlib
import json
import re

PATTERN = re.compile(r'\{\{fact:([A-Za-z0-9_.-]+)\}\}')


def bind_text(text: str, evidence: Path) -> tuple[str, list[str]]:
    payload = json.loads(evidence.read_text(encoding='utf-8-sig'))
    facts, sources = payload.get('facts'), payload.get('sources')
    if payload.get('format') != 'peifeng.evidence.v1' or not isinstance(facts, dict) or not facts or any(not isinstance(key, str) or not isinstance(value, str) or not value.strip() for key,value in facts.items()):
        raise ValueError('事实文件格式无效，未导出文档')
    if not isinstance(sources, list) or not sources:
        raise ValueError('事实文件缺少来源哈希，未导出文档')
    for row in sources:
        if not isinstance(row, dict) or not isinstance(row.get('path'), str) or not isinstance(row.get('sha256'), str):
            raise ValueError('来源记录无效')
        path = evidence.parent / row['path']
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']:
            raise ValueError(f'事实来源已变化或不存在：{row["path"]}；重新计算事实后再导出')
    # A matching hash alone does not establish that the facts were computed
    # correctly. Recompute through the single supported, fixed implementation.
    if payload.get('producer') != 'paired_evidence' or len(sources) != 1 or not isinstance(payload.get('recipe'), dict):
        raise ValueError('不支持的事实生成器或配方，未导出文档')
    from backend.utils.skills import load_skill_script
    module = load_skill_script('engineer','data-analysis','paired_evidence')
    expected = module.calculate_evidence(dict(payload['recipe'], path=str(evidence.parent/sources[0]['path']), output=str(evidence)))
    if expected != payload:
        raise ValueError('事实与来源复算不一致，未导出文档；用原脚本重新生成')
    for match in PATTERN.finditer(text):
        start = text.rfind('\n',0,match.start()) + 1
        end = text.find('\n',match.end())
        line = text[start:end if end >= 0 else len(text)].strip()
        if line != match.group(0):
            raise ValueError('事实占位符必须独立成段，不能插入表格、公式或其他事实句中；也可删除占位符，由facts自动附加配对结果段')
    used = []
    def replace(match):
        name = match.group(1)
        if name not in facts:
            raise ValueError(f'不存在的事实ID：{name}')
        used.append(name)
        return facts[name]
    text = PATTERN.sub(replace, text)
    if '{{fact:' in text:
        raise ValueError('存在未解析事实占位符')
    if not used:
        used = list(facts)
        text = text.rstrip() + '\n\n## 配对结果\n\n' + '\n\n'.join(facts.values()) + '\n'
    return text, list(dict.fromkeys(used))


def bind_facts(source: Path, evidence: Path) -> tuple[str, list[str]]:
    return bind_text(source.read_text(encoding='utf-8-sig'), evidence)
