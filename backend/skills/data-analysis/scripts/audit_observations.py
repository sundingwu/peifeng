"""Audit configured CSV/XLSX observations and install a portable reproduction entry.

Only declared columns/statuses are supported. Conflicts are quarantined, never
selected by input order. The copied reproduction helper uses Python libraries.
"""
from pathlib import Path
import argparse
import csv
import hashlib
import io
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import tempfile


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor,'w',encoding='utf-8',newline='') as stream:
            stream.write(text)
        os.replace(temporary,path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def csv_text(header, rows) -> str:
    stream = io.StringIO(newline='')
    writer = csv.writer(stream,lineterminator='\n')
    writer.writerow(header)
    writer.writerows(rows)
    return stream.getvalue()


def verify_output_order(outputs, groups, keys, method) -> None:
    for name,fields in [('cleaned',[*groups,method,*keys]),('metrics',[*groups,method]),('paired',[*groups,'comparison',*keys])]:
        with outputs[name].open(encoding='utf-8',newline='') as stream:
            records = list(csv.DictReader(stream))
        values = [tuple(row[field] for field in fields) for row in records]
        if values!=sorted(values):
            raise ValueError(f'{name}实际CSV排序核对失败')
    issues = json.loads(outputs['issues'].read_text(encoding='utf-8'))
    for name,fields in [('conflicting_keys',[*groups,method,*keys]),('failed_keys',[*groups,method,*keys]),('pairing_gaps',[*groups,'comparison'])]:
        values = [tuple(row[field] for field in fields) for row in issues[name]]
        if values!=sorted(values):
            raise ValueError(f'issues.{name}实际数组排序核对失败')
    for row in issues['pairing_gaps']:
        for name in ['candidate_only','baseline_only']:
            values = [tuple(item[field] for field in keys) if isinstance(item,dict) else (item,) for item in row[name]]
            if values!=sorted(values):
                raise ValueError(f'issues.pairing_gaps.{name}排序核对失败')


def audit_observations(target: dict, base_dir=None) -> dict:
    root = Path(base_dir or Path.cwd()).resolve()
    def resolve(value):
        path = Path(value)
        return (path if path.is_absolute() else root/path).resolve()
    groups = target.get('group_by',[])
    keys = target.get('keys')
    method, value, status = target.get('method','method'), target.get('value','score'), target.get('status','status')
    for label, fields, empty in [('group_by',groups,True),('keys',keys,False)]:
        if not isinstance(fields,list) or not fields and not empty or any(not isinstance(item,str) or not item for item in fields):
            raise ValueError(f'{label}必须是字段名数组，keys不能为空')
    columns = [*groups,method,*keys,value,*([status] if status is not None else [])]
    if any(not isinstance(name,str) or not name for name in columns) or len(columns)!=len(set(columns)):
        raise ValueError('所有列名必须非空且互不重复')
    accepted_status = target.get('accepted_status','ok')
    excluded = target.get('excluded_statuses',['failed'])
    if not isinstance(accepted_status,str) or not accepted_status or not isinstance(excluded,list) or not excluded or any(not isinstance(item,str) or not item for item in excluded) or accepted_status in excluded:
        raise ValueError('accepted_status与excluded_statuses必须是明确且不重叠的状态')
    candidate, controls = target.get('candidate'), target.get('controls')
    if not isinstance(candidate,str) or not candidate or not isinstance(controls,list) or not controls or any(not isinstance(item,str) or not item for item in controls) or len(controls)!=len(set(controls)) or candidate in controls:
        raise ValueError('candidate和controls必须是明确且互异的方法名')
    precision = target.get('precision',10)
    if type(precision) is not int or not 0 <= precision <= 12:
        raise ValueError('precision必须是0到12的整数')
    inputs = target.get('inputs')
    if not isinstance(inputs,list) or not inputs or any(not isinstance(item,dict) or not item.get('path') for item in inputs):
        raise ValueError('inputs必须为含path的非空对象数组；XLSX另需sheet')
    hashes, records = {}, []
    for entry in inputs:
        source = resolve(entry['path'])
        hashes[str(source)] = hashlib.sha256(source.read_bytes()).hexdigest()
        if source.suffix.lower()=='.csv':
            with source.open(encoding='utf-8-sig',newline='') as stream:
                rows = list(csv.reader(stream))
        elif source.suffix.lower()=='.xlsx':
            if not entry.get('sheet'):
                raise ValueError('XLSX必须明确指定sheet，不猜工作表')
            from openpyxl import load_workbook
            book = load_workbook(source,read_only=True,data_only=True)
            try:
                rows = list(book[entry['sheet']].iter_rows(values_only=True))
            finally:
                book.close()
        else:
            raise ValueError('inputs仅支持CSV/XLSX')
        if not rows:
            raise ValueError(f'输入没有表头：{source.name}')
        header = [str(item).strip() if item is not None else '' for item in rows[0]]
        if len(header)!=len(set(header)) or set(header)!=set(columns):
            raise ValueError(f'{source.name}表头与声明不一致；缺列{sorted(set(columns)-set(header))}，未声明列{sorted(set(header)-set(columns))}。材料无状态列时明确设置status:null，不制造状态列；其他列需明确配置')
        for number, cells in enumerate(rows[1:],2):
            if not any(item is not None and str(item).strip() for item in cells):
                continue
            if len(cells)!=len(header):
                raise ValueError(f'{source.name}第{number}行缺列或多列')
            row = dict(zip(header,cells))
            text = {name:str(row[name]).strip() if row[name] is not None else '' for name in columns if name!=value}
            state = text[status] if status is not None else accepted_status
            if any(not item for item in text.values()) or state not in [accepted_status,*excluded]:
                raise ValueError(f'{source.name}第{number}行键缺失或状态未声明')
            raw = row[value]
            observation = None if raw is None or str(raw).strip()=='' else float(raw)
            if isinstance(raw,bool) or observation is not None and not math.isfinite(observation) or state==accepted_status and observation is None:
                raise ValueError(f'{source.name}第{number}行有效观测缺值、为布尔或非有限值')
            records.append((tuple(text[name] for name in [*groups,method,*keys]),observation,state))
    unique = list(dict.fromkeys(records))
    by_key = {}
    for business,observation,state in unique:
        by_key.setdefault(business,[]).append((observation,state))
    conflicting = sorted(key for key,values in by_key.items() if len(values)>1)
    failed = sorted(key for key,values in by_key.items() if len(values)==1 and values[0][1] in excluded)
    clean = {key:values[0][0] for key,values in by_key.items() if len(values)==1 and values[0][1]==accepted_status}
    if not clean:
        raise ValueError('没有有效观测；当前流水线不伪造空统计结果，需单独报告全失败情况')
    number = lambda observation: f'{observation:.{precision}f}'
    grouped = {}
    for business,observation in sorted(clean.items()):
        grouped.setdefault(business[:len(groups)+1],[]).append(observation)
    metrics = [(*group,len(values),number(statistics.mean(values)),number(statistics.stdev(values)) if len(values)>1 else '') for group,values in sorted(grouped.items())]
    paired, gaps = [], []
    for group in sorted({business[:len(groups)] for business in clean}):
        left = {business[len(groups)+1:]:observation for business,observation in clean.items() if business[:len(groups)+1]==(*group,candidate)}
        for control in sorted(controls):
            right = {business[len(groups)+1:]:observation for business,observation in clean.items() if business[:len(groups)+1]==(*group,control)}
            comparison = f'{candidate}-{control}'
            for key in sorted(left.keys() & right.keys()):
                difference = left[key]-right[key]
                if not math.isfinite(difference):
                    raise ValueError('配对差溢出，未写输出')
                paired.append((*group,comparison,*key,repr(difference)))
            def display(items):
                return [key[0] if len(keys)==1 else dict(zip(keys,key)) for key in sorted(items)]
            gaps.append({**dict(zip(groups,group)),'comparison':comparison,'candidate_only':display(left.keys()-right.keys()),'baseline_only':display(right.keys()-left.keys())})
    business_columns = [*groups,method,*keys]
    issues = {'input_rows':len(records),'exact_duplicate_rows':len(records)-len(unique),
        'conflicting_keys':[dict(zip(business_columns,key)) for key in conflicting],
        'failed_keys':[dict(zip(business_columns,key)) for key in failed], 'accepted_rows':len(clean),'pairing_gaps':gaps}
    names = {'cleaned':'cleaned.csv','metrics':'metrics.csv','paired':'paired.csv','issues':'issues.json'}
    provided = target.get('outputs',{})
    if not isinstance(provided,dict) or set(provided)-set(names):
        raise ValueError('outputs只支持cleaned/metrics/paired/issues路径')
    outputs = {name:resolve(provided.get(name,path)) for name,path in names.items()}
    if len(set(outputs.values()))!=4 or any(str(path) in hashes for path in outputs.values()):
        raise ValueError('输出必须互异，不能覆盖输入')
    if any(path.suffix.lower()!=('.json' if name=='issues' else '.csv') for name,path in outputs.items()):
        raise ValueError('cleaned/metrics/paired输出必须为CSV，issues必须为JSON')
    helper, recipe = root/'internal/peifeng_audit_observations.py', root/'internal/peifeng_audit_recipe.json'
    entry = resolve(target['reproduce']) if target.get('reproduce') else None
    facts_path = resolve(target['facts']) if target.get('facts') else None
    if entry and (entry in outputs.values() or str(entry) in hashes or entry in [helper,recipe] or any(path in [helper,recipe] for path in outputs.values())):
        raise ValueError('reproduce或辅助源码路径与数据冲突')
    if facts_path and (facts_path.suffix.lower()!='.json' or facts_path in [*outputs.values(),entry,helper,recipe] or str(facts_path) in hashes):
        raise ValueError('facts必须为独立JSON，不能覆盖输入或其他产物')
    rendered = {'cleaned':csv_text([*business_columns,value],[(*key,repr(observation)) for key,observation in sorted(clean.items())]),
        'metrics':csv_text([*groups,method,'count','mean','sample_stddev'],metrics),
        'paired':csv_text([*groups,'comparison',*keys,'difference'],paired), 'issues':json.dumps(issues,ensure_ascii=False,indent=2)+'\n'}
    if any(hashlib.sha256(Path(path).read_bytes()).hexdigest()!=digest for path,digest in hashes.items()):
        raise ValueError('处理期间输入发生变化，未写输出')
    for name,path in outputs.items():
        atomic_text(path,rendered[name])
    verify_output_order(outputs,groups,keys,method)
    output_hashes = {name:hashlib.sha256(path.read_bytes()).hexdigest() for name,path in outputs.items()}
    result = {'outputs':{name:str(path) for name,path in outputs.items()},'output_sha256':output_hashes,'input_sha256':hashes,
        'status_information':'not_provided' if status is None else f'column:{status}',
        'counts':{name:issues[name] for name in ['input_rows','exact_duplicate_rows','accepted_rows']},'verification':{'inputs_unchanged':True,'sorting_checked':True}}
    if target.get('reproduce'):
        recipe_target = dict(target)
        recipe_target.pop('reproduce',None);recipe_target.pop('facts',None)
        recipe_target['inputs'] = [dict(item,path=os.path.relpath(resolve(item['path']),root)) for item in inputs]
        recipe_target['outputs'] = {name:os.path.relpath(path,root) for name,path in outputs.items()}
        helper.parent.mkdir(parents=True,exist_ok=True)
        if Path(__file__).resolve()!=helper:
            shutil.copy2(Path(__file__),helper)
        atomic_text(recipe,json.dumps(recipe_target,ensure_ascii=False,indent=2)+'\n')
        relative_root = os.path.relpath(root,entry.parent)
        source = Path(__file__).read_text(encoding='utf-8')
        source = source.rsplit("if __name__=='__main__':", 1)[0]
        program = (source
            + '\n# Self-contained reproduction: configuration travels with this file.\n'
            + "if __name__ == '__main__':\n"
            + f'    project = (Path(__file__).resolve().parent / {relative_root!r}).resolve()\n'
            + f'    recipe = json.loads({json.dumps(recipe_target, ensure_ascii=False)!r})\n'
            + '    audit_observations(recipe, project)\n')
        atomic_text(entry,program)
        result['reproduce'] = str(entry)
        rerun = subprocess.run([sys.executable,str(entry)],cwd=root,capture_output=True,encoding='utf-8',errors='replace',timeout=60)
        if rerun.returncode!=0:
            raise ValueError('生成的复现入口执行失败：'+rerun.stderr[-1000:])
        if any(hashlib.sha256(path.read_bytes()).hexdigest()!=output_hashes[name] for name,path in outputs.items()):
            raise ValueError('复现入口重生成结果不一致')
        if any(hashlib.sha256(Path(path).read_bytes()).hexdigest()!=digest for path,digest in hashes.items()):
            raise ValueError('复现入口修改了原始输入')
        result['verification'].update(reproduction_exit_code=0,reproduction_identical=True)
    if target.get('facts'):
        for parent in Path(__file__).resolve().parents:
            if (parent/'agent').is_dir():
                sys.path.insert(0,str(parent));break
        from backend.utils.skills import load_skill_script
        receipt = load_skill_script('engineer','data-analysis','paired_evidence').paired_evidence({'path':str(outputs['cleaned']),'output':target['facts'],'group_by':groups,'keys':keys,'method':method,'value':value,'candidate':candidate,'controls':controls},root)
        result['facts'] = json.loads(receipt)
    return result


def main() -> int:
    if hasattr(sys.stdout,'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--base-dir',type=Path,default=Path.cwd())
    args = parser.parse_args()
    try:
        payload = json.loads(args.input.read_text(encoding='utf-8-sig'))
        targets = payload.get('targets',[payload]) if isinstance(payload,dict) else payload
        if not isinstance(targets,list) or not targets or any(not isinstance(item,dict) for item in targets):
            raise ValueError('输入必须为目标对象或非空targets对象数组')
        results = []
        for target in targets:
            results.append(audit_observations(target,args.base_dir))
    except (OSError,ValueError,KeyError,TypeError,OverflowError,subprocess.TimeoutExpired) as error:
        print(json.dumps({'status':'failed','error':str(error)},ensure_ascii=False));return 1
    print(json.dumps({'status':'completed','results':results},ensure_ascii=False,allow_nan=False))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
