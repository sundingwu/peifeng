from pathlib import Path
import sys
for _parent in Path(__file__).resolve().parents:
    if (_parent / 'backend').is_dir():
        sys.path.insert(0, str(_parent))
        break
"""Crossref 文献元数据批处理，接口依据 https://api.crossref.org/swagger-ui/index.html。"""

import json
from datetime import datetime, timezone
from urllib.parse import quote, urlencode
from urllib.request import Request
from urllib.error import HTTPError
from backend.security.policy import open_public as urlopen

from backend.utils.web import run_parallel_safe, run_targets

information = {
    "name": "literature",
    "description": "批量查询 Crossref 文献或核验 DOI。返回书目元数据，不代表已阅读全文。每项提供 query 或 doi。",
    "parameters": {
        "type": "object",
        "properties": {
            "targets": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "doi": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    "additionalProperties": False,
                },
            },
        },
        "required": ["targets"],
        "additionalProperties": False,
    },
}


def lookup(target: dict) -> list[dict]:
    """单项查询，固定 Crossref 端点，避免把 DOI 当任意网址访问。"""
    query = str(target.get("query") or "").strip()
    doi = str(target.get("doi") or "").strip()
    if bool(query) == bool(doi):
        raise ValueError("每项必须且只能提供 query 或 doi")
    limit = target.get("limit", 5)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise ValueError("limit 必须是 1 到 20 的整数")
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if doi.lower().startswith(prefix):
            doi = doi[len(prefix):].strip()
            break
    endpoint = "https://api.crossref.org/works"
    url = endpoint + ("/" + quote(doi, safe="") if doi else "?" + urlencode({"query.bibliographic": query, "rows": limit}))
    request = Request(url, headers={"User-Agent": "PeiFeng/0.1 (academic metadata assistant)", "Accept": "application/json"})
    try:
        with urlopen(request, timeout=30) as response:
            message = json.load(response)["message"]
    except HTTPError as error:
        if not doi or error.code != 404:
            raise
        return lookup_datacite(doi)
    works = [message] if doi else message.get("items", [])
    return [
        {
            "doi": work.get("DOI", ""),
            "title": work.get("title", []),
            "authors": [" ".join(filter(None, [author.get("given"), author.get("family")])) for author in work.get("author", [])],
            "published": work.get("published", {}).get("date-parts", []),
            "venue": work.get("container-title", []),
            "url": work.get("URL", ""),
            "type": work.get("type", ""),
            "registry": "Crossref",
            "source_url": url,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "source_level": "metadata_only",
            "field_scope": "仅返回所列字段；未返回字段不表示原记录没有该内容。",
        }
        for work in works
    ]


def lookup_datacite(doi: str) -> list[dict]:
    """Crossref 未收录的 DOI 查询公开 DataCite；404 不等于 DOI 无效。

    接口依据 https://support.datacite.org/docs/how-do-i-query-the-rest-api-and-whats-in-the-response
    """
    url = 'https://api.datacite.org/dois/' + quote(doi, safe='/')
    request = Request(url, headers={'User-Agent':'PeiFeng/0.1 (academic metadata assistant)',
                                    'Accept':'application/vnd.api+json'})
    with urlopen(request, timeout=30) as response:
        work = json.load(response)['data']['attributes']
    year = work.get('publicationYear')
    return [{'doi':work.get('doi',''), 'title':[row['title'] for row in work.get('titles',[]) if row.get('title')],
             'authors':[row.get('name') or ' '.join(filter(None,[row.get('givenName'),row.get('familyName')])) for row in work.get('creators',[])],
             'published':[[year]] if year else [], 'venue':[work['publisher']] if work.get('publisher') else [],
             'url':work.get('url',''), 'type':work.get('types',{}).get('resourceTypeGeneral',''),
             'registry':'DataCite', 'source_url':url, 'retrieved_at':datetime.now(timezone.utc).isoformat(),
             'source_level':'metadata_only', 'field_scope':'仅返回所列字段；未返回字段不表示原记录没有该内容。'}]


def literature(targets: list[dict]) -> str:
    """有限并发，逐项隔离错误，返回顺序与输入完全一致。"""
    if not isinstance(targets, list) or not targets:
        raise ValueError("targets 必须是非空对象数组")
    if any(not isinstance(target, dict) for target in targets):
        raise ValueError("targets 每项必须是对象")
    results = run_parallel_safe(lookup, targets, workers=3, thread_prefix="literature")
    return json.dumps(
        {"results": [
            {"target": target, "works": works or [], "error": str(error) if error else None}
            for target, (works, error) in zip(targets, results)
        ], "source": "Crossref", "evidence": "bibliographic metadata"},
        ensure_ascii=False,
    )


def verify_target(target, base_dir=None):
    works = lookup({key: value for key, value in target.items() if key in {'query', 'doi', 'limit'}})
    if not target.get('output'):
        return json.dumps(works, ensure_ascii=False)
    if not target.get('doi') or len(works) != 1:
        raise ValueError('报告导出需要一个明确DOI及一条返回记录')
    expected = str(target['doi']).strip().lower()
    for prefix in ['https://doi.org/', 'http://doi.org/', 'doi:']:
        if expected.startswith(prefix):
            expected = expected[len(prefix):].strip()
            break
    work = works[0]
    if str(work.get('doi','')).lower() != expected:
        raise ValueError('返回DOI与请求不一致，未导出报告')
    from backend.utils.skills import resolve_file
    output = resolve_file(target['output'], base_dir)
    if output.suffix.lower() != '.md':
        raise ValueError('报告output必须是.md文件')
    def cell(value):
        return str(value).replace('|', '\\|').replace('\n', ' ').replace('\r', ' ')
    dates = work.get('published') or []
    year = dates[0][0] if dates and dates[0] else '未返回'
    title = (work.get('title') or ['未返回'])[0]
    authors = '; '.join(work.get('authors') or []) or '未返回'
    missing = [name for name, value in [('title',title),('authors',authors),('year',year)] if value == '未返回']
    template = (Path(__file__).parent.parent/'references/doi-report.md').read_text(encoding='utf-8')
    text = template.format_map({
        'doi':cell(work['doi']), 'title':cell(title), 'authors':cell(authors), 'year':cell(year),
        'source_url':work.get('source_url','未记录'), 'retrieved_at':work.get('retrieved_at','未记录'),
        'verification': '返回DOI一致，所需字段齐全。' if not missing else '字段缺失，尚不能完整核验：'+', '.join(missing),
    })
    import os, tempfile
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=output.parent)
    try:
        with os.fdopen(descriptor,'w',encoding='utf-8') as stream:
            stream.write(text)
        os.replace(temporary,output)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return json.dumps({'records':works,'report':str(output),'metadata_complete':not missing,'missing_fields':missing,'scope':'仅核验导出表所列字段，不填充其他书目信息'},ensure_ascii=False)


def run(targets, base_dir=None):
    return run_targets(lambda target: verify_target(target, base_dir), targets, workers=1)

if __name__ == '__main__':
    from backend.utils.skills import run_script_cli
    raise SystemExit(run_script_cli(run, __doc__))
