"""Researcher统一公开检索/获取：不配置账号，不执行网页指令。"""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[4]))
import hashlib
import json
from datetime import datetime,timezone
from urllib.request import Request
from backend.security.policy import open_public,settings
from backend.utils.skill_runtime import project_path,atomic_output,run_cli
from backend.utils.paths import wrap_external
from backend.utils.skills import load_skill_script
from backend.utils.web import search_public,retrieve


def process(target: dict,base: Path) -> dict:
    action=target.get('action','search')
    allowed={'action','query','count','source','type','url','output','need_content','overwrite'}
    if set(target)-allowed:raise ValueError('不支持的检索参数：'+str(sorted(set(target)-allowed)))
    if action=='search' and target.get('type')=='paper':action='paper'
    files=[];level='search_result'
    if action in {'search','paper'}:
        query=target.get('query','').strip();count=target.get('count',5)
        if not query or type(count) is not int or not 1<=count<=20:raise ValueError('需要非空query和1到20的count')
        if action=='paper':
            records=load_skill_script('researcher','citation-management','references').public_lookup({'query':query,'limit':count,'source':target.get('source','crossref')})
            data={'query':query,'records':records};level='metadata_or_abstract_only'
        else:
            results=search_public(query,count)
            if target.get('need_content'):
                for row in results:
                    try:row['page']=retrieve(row['url'])
                    except Exception as error:row['retrieval_error']=str(error)
            data={'query':query,'results':results}
    elif action=='fetch':
        data=retrieve(target['url']);level=data['source_level']
    elif action=='download':
        output=project_path(target['output'],base)
        if output.exists() and not target.get('overwrite'):raise ValueError('输出已存在；覆盖需overwrite=true')
        with open_public(Request(target['url'],headers={'User-Agent':'PeiFeng research retrieval'}),timeout=40) as response:
            data_bytes=response.read(settings()['limits']['response_bytes']+1);actual=response.url
        if len(data_bytes)>settings()['limits']['response_bytes']:raise ValueError('响应超过下载限制')
        with atomic_output(output) as candidate:candidate.write_bytes(data_bytes)
        return {'files':[str(output)],'url':actual,'retrieved_at_utc':datetime.now(timezone.utc).isoformat(),'bytes':len(data_bytes),'sha256':hashlib.sha256(data_bytes).hexdigest(),'source_level':'downloaded_not_read'}
    else:raise ValueError('action支持search/paper/fetch/download')
    result={'action':action,'retrieved_at_utc':datetime.now(timezone.utc).isoformat(),'source_level':level,
            'evidence':wrap_external('research',json.dumps(data,ensure_ascii=False))}
    if target.get('output'):
        output=project_path(target['output'],base)
        if output.exists() and not target.get('overwrite'):raise ValueError('输出已存在；覆盖需overwrite=true')
        with atomic_output(output) as candidate:candidate.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        return {'files':[str(output)],'action':action,'source_level':level,'retrieved_at_utc':result['retrieved_at_utc']}
    return {'files':[],**result}


if __name__=='__main__':run_cli(process)
