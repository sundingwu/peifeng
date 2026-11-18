from pathlib import Path
import sys
for _parent in Path(__file__).resolve().parents:
    if (_parent / 'backend').is_dir():
        sys.path.insert(0, str(_parent))
        break
"""Local bibliography interoperability and public DOI verification."""
import json
import re
from pathlib import Path
from functools import lru_cache
from backend.utils.skills import tool_schema, resolve_file
from backend.utils.web import run_targets
from backend.utils.skills import load_skill_script
lookup = load_skill_script("researcher", "paper-download", "doi").lookup

information = tool_schema('literature', '本地BibTeX/RIS/NBIB/JSON转换和去重，或公开Crossref/arXiv/PubMed查询；不连接账号。', {
    'operation': {'type':'string','enum':['lookup','convert','deduplicate']}, 'source':{'type':'string','enum':['crossref','arxiv','pubmed']}, 'path':{'type':'string'}, 'output':{'type':'string'}, 'query':{'type':'string'}, 'doi':{'type':'string'}, 'limit':{'type':'integer','minimum':1,'maximum':20}}, ['operation'])


@lru_cache(maxsize=1)
def source_converters():
    '复用Nature的纯Python转换器，并在上游保留其署名。'
    from backend.orchestration.orchestrate import load_module
    path=Path(__file__).parent/'converters.py'
    return load_module(path)


def public_lookup(target):
    source=target.get('source','crossref')
    if source=='crossref':
        return lookup({key:value for key,value in target.items() if key in {'query','doi','limit'}})
    from backend.security.policy import open_public as urlopen
    from urllib.parse import urlencode
    import defusedxml.ElementTree as ET
    query=target.get('query','').strip()
    limit=target.get('limit',5)
    if not query or type(limit) is not int or not 1<=limit<=20 or target.get('doi'):
        raise ValueError('arxiv/pubmed查询需query与1到20的limit，不接收doi')
    if source=='arxiv':
        url='https://export.arxiv.org/api/query?'+urlencode({'search_query':query,'max_results':limit})
        with urlopen(url,timeout=30) as response:
            root=ET.fromstring(response.read(4*1024*1024))
        ns={'a':'http://www.w3.org/2005/Atom'}
        return [{'id':entry.findtext('a:id',default='',namespaces=ns),'title':entry.findtext('a:title',default='',namespaces=ns),'abstract':entry.findtext('a:summary',default='',namespaces=ns),'source_url':url} for entry in root.findall('a:entry',ns)]
    if source=='pubmed':
        base='https://eutils.ncbi.nlm.nih.gov/entrez/eutils/'
        url=base+'esearch.fcgi?'+urlencode({'db':'pubmed','term':query,'retmax':limit,'retmode':'json'})
        with urlopen(url,timeout=30) as response:
            identifiers=json.load(response)['esearchresult']['idlist']
        if not identifiers: return []
        summary_url=base+'esummary.fcgi?'+urlencode({'db':'pubmed','id':','.join(identifiers),'retmode':'json'})
        with urlopen(summary_url,timeout=30) as response:
            result=json.load(response)['result']
        return [dict(result[identifier],source_url=summary_url) for identifier in identifiers]
    raise ValueError('不支持的公开文献源')


def load_records(path):
    if path.suffix.lower() == '.bib':
        import bibtexparser
        library = bibtexparser.parse_file(str(path))
        if library.failed_blocks:
            raise ValueError('BibTeX有无法解析的块，未进行有损转换')
        return [{'id': entry.key, 'type': entry.entry_type, **{key:field.value for key,field in entry.fields_dict.items()}} for entry in library.entries]
    if path.suffix.lower() in {'.ris','.nbib'}:
        import rispy
        if path.suffix.lower()=='.nbib':
            blocks=re.split(r'\n\s*\n(?=PMID\s*-)',path.read_text(encoding='utf-8'))
            rows=rispy.loads('\n'.join(source_converters().convert_from_medline(block,'ris') for block in blocks if block.strip()))
        else:
            with path.open(encoding='utf-8') as stream:
                rows=rispy.load(stream)
        return [{'id':row.get('id',str(i+1)), 'type':'article', 'title':row.get('title',''), 'author':' and '.join(row.get('authors',[])), 'year':row.get('year',''), 'doi':row.get('doi',''), 'journal':row.get('journal_name',''), 'url':next(iter(row.get('urls',[])), '')} for i,row in enumerate(rows)]
    rows=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(rows,list) or any(not isinstance(row,dict) for row in rows):
        raise ValueError('JSON应为文献对象数组')
    return rows


def export_records(records, path):
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.suffix.lower()=='.bib':
        import bibtexparser
        from bibtexparser.model import Entry, Field
        library=bibtexparser.Library()
        for i,row in enumerate(records):
            library.add(Entry(str(row.get('type','article')),str(row.get('id',f'ref{i+1}')),[Field(key,str(value)) for key,value in row.items() if key not in {'id','type'} and value is not None]))
        text=bibtexparser.write_string(library)
    elif path.suffix.lower()=='.ris':
        import rispy
        rows=[{'type_of_reference':'JOUR','id':str(row.get('id',i+1)), 'title':row.get('title',''), 'authors':str(row.get('author','')).split(' and '), 'year':str(row.get('year','')), 'doi':row.get('doi',''), 'journal_name':row.get('journal',''), 'urls':[row['url']] if row.get('url') else []} for i,row in enumerate(records)]
        text=rispy.dumps(rows)
    elif path.suffix.lower()=='.json':
        text=json.dumps(records,ensure_ascii=False,indent=2)
    else:
        raise ValueError('output扩展名为.bib/.ris/.json')
    path.write_text(text,encoding='utf-8')


def literature(targets, base_dir=None):
    def process(target):
        if target['operation']=='lookup':
            from datetime import datetime, timezone
            return json.dumps({'records':public_lookup(target),'retrieved_at':datetime.now(timezone.utc).isoformat(),'source_level':'metadata_or_abstract_only'},ensure_ascii=False)
        records=load_records(resolve_file(target['path'],base_dir))
        original=len(records)
        if target['operation']=='deduplicate':
            seen=set(); unique=[]
            for row in records:
                doi=str(row.get('doi','')).lower().removeprefix('https://doi.org/').strip()
                title=re.sub(r'\W+','',str(row.get('title','')).casefold())
                key=('doi',doi) if doi else ('title',title) if title else None
                if key is None or key not in seen:
                    unique.append(row)
                    if key is not None: seen.add(key)
            records=unique
        output=resolve_file(target['output'],base_dir)
        export_records(records,output)
        return json.dumps({'output':str(output),'input_count':original,'output_count':len(records),'scope':'本地字段转换，未核验语义或文献存在性'},ensure_ascii=False)
    return run_targets(process,targets,workers=1,base_dir=base_dir)


def run(targets, base_dir=None):
    return literature(targets, base_dir)

if __name__ == '__main__':
    from backend.utils.skills import run_script_cli
    raise SystemExit(run_script_cli(run, __doc__))
