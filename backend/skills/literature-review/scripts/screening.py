from pathlib import Path
import sys
for _parent in Path(__file__).resolve().parents:
    if (_parent / 'backend').is_dir():
        sys.path.insert(0, str(_parent))
        break
"""Validate single-review screening decisions and produce reproducible counts."""
import json
from collections import Counter
from backend.utils.skills import tool_schema, resolve_file
from backend.utils.web import run_targets
from backend.memory.store import write_json

information=tool_schema('screening','验证本地单评筛选记录并汇总计数；不代替资格判断，不宣称双盲筛选。',{'path':{'type':'string'},'output':{'type':'string'}},['path','output'])


def summarize(records):
    if not isinstance(records,list):
        raise ValueError('筛选记录必须为数组')
    keys=set()
    for row in records:
        if not isinstance(row,dict) or not row.get('id'):
            raise ValueError('每条记录必须有id')
        stage=row.get('stage')
        if stage not in {'title_abstract','full_text'}:
            raise ValueError('stage为title_abstract/full_text')
        key=(str(row['id']),stage)
        if key in keys:
            raise ValueError('同一记录同阶段存在重复决策')
        keys.add(key)
        if row.get('decision') not in {'include','exclude','uncertain'}:
            raise ValueError('decision为include/exclude/uncertain')
        if row['decision']=='exclude' and not row.get('reason'):
            raise ValueError('排除必须记录理由')
        if stage=='full_text' and row['decision']=='exclude' and not row.get('locator'):
            raise ValueError('全文排除必须有来源定位')
    title_decisions={str(row['id']):row['decision'] for row in records if row['stage']=='title_abstract'}
    for row in records:
        if row['stage']=='full_text' and title_decisions.get(str(row['id']))=='exclude':
            raise ValueError('全文筛选与题录排除冲突，先修订原决策并保留版本')
    counts={stage:dict(Counter(row['decision'] for row in records if row['stage']==stage)) for stage in ['title_abstract','full_text']}
    return {'review_mode':'single_review_with_rechecks','counts':counts,'records':records,'scope':'仅核对输入决策及计数；完整PRISMA还需检索、去重、未获取全文等数量，不自动补零'}


def compare_decisions(records, reference):
    """在给定人工标签上计算逐阶段推进/排除一致性，不创建第二评审。"""
    summarize(records)
    summarize(reference)
    by_key=lambda rows:{(str(row['id']),row['stage']):row['decision']!='exclude' for row in rows}
    actual=by_key(records);expected=by_key(reference)
    reports={}
    for stage in ['title_abstract','full_text']:
        keys=sorted(key for key in actual.keys() & expected.keys() if key[1]==stage)
        counts={'both_advance':0,'actual_advance_reference_exclude':0,
                'actual_exclude_reference_advance':0,'both_exclude':0}
        for key in keys:
            a,b=actual[key],expected[key]
            label=('both_advance' if a and b else 'both_exclude' if not a and not b else
                   'actual_advance_reference_exclude' if a else 'actual_exclude_reference_advance')
            counts[label]+=1
        n=len(keys);agree=(counts['both_advance']+counts['both_exclude'])/n if n else None
        if n:
            a=(counts['both_advance']+counts['actual_advance_reference_exclude'])/n
            b=(counts['both_advance']+counts['actual_exclude_reference_advance'])/n
            chance=a*b+(1-a)*(1-b)
        else:chance=None
        reports[stage]={'n':n,**counts,'agreement':agree,
                        'kappa':(agree-chance)/(1-chance) if chance is not None and chance<1 else None,
                        'pabak':2*agree-1 if agree is not None else None}
    return {'stages':reports,
            'actual_only':[{'id':key[0],'stage':key[1]} for key in sorted(actual.keys()-expected.keys())],
            'reference_only':[{'id':key[0],'stage':key[1]} for key in sorted(expected.keys()-actual.keys())],
            'scope':'仅比较给定标签；推进=include/uncertain，不证明评审独立性或标签正确性'}


def screening(targets,base_dir=None):
    def process(target):
        records=json.loads(resolve_file(target['path'],base_dir).read_text(encoding='utf-8'))
        result=summarize(records)
        if target.get('reference'):
            reference=json.loads(resolve_file(target['reference'],base_dir).read_text(encoding='utf-8'))
            result['comparison']=compare_decisions(records,reference)
        output=resolve_file(target['output'],base_dir)
        write_json(output,result)
        return json.dumps({'output':str(output),'review_mode':result['review_mode'],'counts':result['counts']},ensure_ascii=False)
    return run_targets(process,targets,workers=1,base_dir=base_dir)


def run(targets, base_dir=None):
    return screening(targets, base_dir)

if __name__ == '__main__':
    from backend.utils.skills import run_script_cli
    raise SystemExit(run_script_cli(run, __doc__))
