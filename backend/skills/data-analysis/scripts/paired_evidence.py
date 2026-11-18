"""Compute reusable paired facts from a CSV; never infer metric direction."""
from pathlib import Path
import sys

for parent in Path(__file__).resolve().parents:
    if (parent / 'backend').is_dir():
        sys.path.insert(0, str(parent))
        break

import csv
import hashlib
import json
import math
import os
import statistics
from backend.memory.store import write_json
from backend.utils.skills import resolve_file, dispatch


def field_names(value, label, allow_empty=False):
    if not isinstance(value, list) or (not value and not allow_empty) or any(not isinstance(item, str) or not item for item in value) or len(value) != len(set(value)):
        raise ValueError(f'{label}必须为不重复字段名数组')
    return value


def calculate_evidence(target: dict, base_dir=None) -> dict:
    source = resolve_file(target['path'], base_dir).resolve()
    output = resolve_file(target['output'], base_dir).resolve()
    if source.suffix.lower() != '.csv' or output.suffix.lower() != '.json' or source == output:
        raise ValueError('path必须是CSV，output必须是不同的JSON文件')
    groups = field_names(target.get('group_by', []), 'group_by', allow_empty=True)
    keys = field_names(target.get('keys'), 'keys')
    method, value = target.get('method', 'method'), target.get('value', 'score')
    if not isinstance(method, str) or not method or not isinstance(value, str) or not value:
        raise ValueError('method/value必须是非空列名')
    columns = [*groups, *keys, method, value]
    if len(columns) != len(set(columns)):
        raise ValueError('分组、配对键、方法和数值列必须互不重复')
    candidate, controls = target.get('candidate'), target.get('controls')
    if not isinstance(candidate, str) or not candidate:
        raise ValueError('candidate必须为非空方法名')
    field_names(controls, 'controls')
    if candidate in controls:
        raise ValueError('candidate不能同时是control')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    observations = {}
    with source.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        header = reader.fieldnames or []
        if len(header) != len(set(header)) or any(name not in header for name in columns):
            raise ValueError('表头重复或缺少指定列')
        for number, row in enumerate(reader, 2):
            if not any(item is not None and str(item).strip() for item in row.values()):
                continue
            if None in row or any(row.get(name) is None or not row[name].strip() for name in columns):
                raise ValueError(f'第{number}行存在缺失字段或额外列')
            observation = float(row[value])
            if not math.isfinite(observation):
                raise ValueError(f'第{number}行不是有限数值')
            group = tuple(row[name] for name in groups)
            key = tuple(row[name] for name in keys)
            values = observations.setdefault(group, {}).setdefault(row[method], {})
            if key in values:
                raise ValueError(f'第{number}行重复(group, method, key)，需先按协议清洗')
            values[key] = observation
    if not observations or not any(candidate in methods for methods in observations.values()):
        raise ValueError('没有观测或未找到candidate方法')
    facts, comparisons = {}, []
    def key_records(items):
        return [dict(zip(keys, item)) for item in sorted(items)]
    for group, methods in sorted(observations.items()):
        for control in sorted(controls):
            left, right = methods.get(candidate, {}), methods.get(control, {})
            shared = sorted(left.keys() & right.keys())
            differences = [left[key] - right[key] for key in shared]
            counts = {'negative':sum(item < 0 for item in differences), 'zero':sum(item == 0 for item in differences), 'positive':sum(item > 0 for item in differences)}
            if any(not math.isfinite(item) for item in differences):
                raise ValueError('配对差溢出，未导出事实')
            candidate_mean = statistics.mean(left.values()) if left else None
            control_mean = statistics.mean(right.values()) if right else None
            overall_difference = candidate_mean-control_mean if left and right else None
            if overall_difference is not None and not math.isfinite(overall_difference):
                raise ValueError('整体均值差溢出，未导出事实')
            row = {'group':dict(zip(groups, group)), 'candidate':candidate, 'control':control,
                'candidate_mean':candidate_mean,'control_mean':control_mean,'overall_mean_difference':overall_difference,
                'candidate_count':len(left), 'control_count':len(right), 'paired_count':len(shared),
                'candidate_only_keys':key_records(left.keys() - right.keys()),
                'control_only_keys':key_records(right.keys() - left.keys()),
                'complete_pairing':bool(shared) and left.keys() == right.keys(),
                'pairs':[{'key':dict(zip(keys,key)), 'difference':difference} for key,difference in zip(shared,differences)],
                'difference_counts':counts, 'mean_difference':statistics.mean(differences) if differences else None}
            comparisons.append(row)
            identifier = f'comparison_{len(comparisons)}'
            label = json.dumps(row['group'], ensure_ascii=False) if groups else '全部观测'
            facts[identifier] = (f'{label}：{candidate}有效观测{len(left)}条，{control}有效观测{len(right)}条，共同键配对{len(shared)}对。'
                f'{candidate}独有键（{control}无对应观测）：{json.dumps(row["candidate_only_keys"],ensure_ascii=False)}；'
                f'{control}独有键（{candidate}无对应观测）：{json.dumps(row["control_only_keys"],ensure_ascii=False)}。'
                f'差值定义为{candidate}减{control}；负值{counts["negative"]}个、零值{counts["zero"]}个、正值{counts["positive"]}个；'
                f'配对差均值为{row["mean_difference"] if differences else "未计算（没有共同键）"}。'
                f'两侧全部有效观测均值分别为{candidate_mean if left else "未计算（该侧无观测）"}和{control_mean if right else "未计算（该侧无观测）"}，'
                f'整体均值差（{candidate}减{control}）为{overall_difference if overall_difference is not None else "未计算"}。'
                '整体均值差使用两侧各自全部有效观测，配对差均值只使用共同键；键集合不同时不得混称完整配对效应。'
                '以上仅为描述性事实，不推断指标方向、独立性、显著性或因果关系。')
    if hashlib.sha256(source.read_bytes()).hexdigest() != digest:
        raise ValueError('读取期间输入变化，未导出事实')
    return {'format':'peifeng.evidence.v1', 'producer':'paired_evidence', 'producer_version':2,
        'recipe':{'group_by':groups,'keys':keys,'method':method,'value':value,'candidate':candidate,'controls':controls},
        'sources':[{'path':os.path.relpath(source, output.parent), 'sha256':digest}],
        'facts':facts, 'comparisons':comparisons, 'scope':'按指定键配对；浮点差值符号精确比较零；不判断优劣'}


def paired_evidence(target: dict, base_dir=None) -> str:
    payload = calculate_evidence(target, base_dir)
    output = resolve_file(target['output'], base_dir).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json(output, payload)
    return json.dumps({'output':str(output), 'source_sha256':payload['sources'][0]['sha256'], 'facts':payload['facts']}, ensure_ascii=False, allow_nan=False)


def run(targets, base_dir=None):
    return dispatch(paired_evidence, targets, base_dir)


if __name__ == '__main__':
    from backend.utils.skills import run_script_cli
    raise SystemExit(run_script_cli(run, __doc__))
