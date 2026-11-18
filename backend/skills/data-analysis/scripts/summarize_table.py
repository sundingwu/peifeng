"""Group local CSV/XLSX observations without silently dropping invalid values."""
from pathlib import Path
import sys
for parent in Path(__file__).resolve().parents:
    if (parent / 'backend').is_dir():
        sys.path.insert(0, str(parent))
        break

import csv
import hashlib
import io
import json
import math
import os
import statistics
import tempfile
import inspect
from backend.utils.skills import resolve_file, dispatch


def summarize(target, base_dir=None):
    source = resolve_file(target['path'], base_dir)
    if source.suffix.lower() == '.csv':
        with source.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.reader(stream))
    elif source.suffix.lower() == '.xlsx':
        from openpyxl import load_workbook
        book = load_workbook(source, read_only=True, data_only=True)
        try:
            sheet = book[target['sheet']] if target.get('sheet') else book.active
            rows = list(sheet.iter_rows(values_only=True))
        finally:
            book.close()
    else:
        raise ValueError('仅支持CSV/XLSX')
    if not rows:
        raise ValueError('输入没有表头')
    header = [str(value).strip() for value in rows[0]]
    group, value = target['group'], target['value']
    if header.count(group) != 1 or header.count(value) != 1:
        raise ValueError('分组列和数值列必须各存在且唯一')
    fields = target.get('metrics', ['count', 'mean', 'sample_stddev'])
    allowed = {'count', 'mean', 'sample_stddev', 'min', 'max', 'median'}
    if not isinstance(fields, list) or not fields or len(set(fields)) != len(fields) or set(fields) - allowed:
        raise ValueError('metrics需为不重复的count/mean/sample_stddev/min/max/median数组')
    precision = target.get('precision')
    if precision is not None and (type(precision) is not int or not 0 <= precision <= 12):
        raise ValueError('precision为0到12的整数；省略时保留浮点精度')
    groups = {}
    for number, row in enumerate(rows[1:], 2):
        if not any(item is not None and str(item).strip() for item in row):
            continue
        if len(row) <= max(header.index(group), header.index(value)):
            raise ValueError(f'第{number}行缺列')
        label, observation = row[header.index(group)], row[header.index(value)]
        if label is None or not str(label).strip():
            raise ValueError(f'第{number}行缺少分组')
        if observation is None or isinstance(observation, bool) or not str(observation).strip():
            raise ValueError(
                f'第{number}行数值为空或缺失：本技能不做清洗，请先补齐/剔除空值，或改为自行统计'
            )
        observation = float(observation)
        if not math.isfinite(observation):
            raise ValueError(f'第{number}行含非有限值')
        groups.setdefault(str(label), []).append(observation)
    if not groups:
        raise ValueError('没有有效观测')
    records = [{group: label, 'count': len(values), 'mean': statistics.mean(values), 'sample_stddev': statistics.stdev(values) if len(values) > 1 else None, 'min': min(values), 'max': max(values), 'median': statistics.median(values)} for label, values in sorted(groups.items())]
    output = None
    if target.get('output'):
        output = resolve_file(target['output'], base_dir)
        if output.suffix.lower() != '.csv' or output.resolve() == source.resolve():
            raise ValueError('output必须为与输入不同的CSV')
        buffer = io.StringIO(newline='')
        writer = csv.writer(buffer, lineterminator='\n')
        writer.writerow([group, *fields])
        for record in records:
            writer.writerow([record[group], *['' if record[field] is None else f'{record[field]:.{precision}f}' if field != 'count' and precision is not None else record[field] for field in fields]])
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=output.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as stream:
                stream.write(buffer.getvalue())
            os.replace(temporary, output)
        finally:
            Path(temporary).unlink(missing_ok=True)
    reproduction = write_reproduction(target, base_dir) if target.get('reproduce') else None
    command = None
    if reproduction:
        relative = Path(reproduction).relative_to(Path(base_dir or '.').resolve()).as_posix()
        command = f'python "{relative}" --base-dir .'
    return json.dumps({'source': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'observations': sum(len(values) for values in groups.values()), 'records': records, 'output': str(output) if output else None, 'reproduce': reproduction, 'reproduce_command': command, 'stddev_definition':'sample, ddof=1; null for n=1', 'unit':'not_provided'}, ensure_ascii=False, allow_nan=False)


def write_reproduction(target, base_dir):
    """将当前统计实现和相对路径参数写成独立复现入口。"""
    root = Path(base_dir or '.').resolve()
    destination = resolve_file(target['reproduce'], root)
    if destination.suffix.lower() != '.py' or not target.get('output'):
        raise ValueError('reproduce需要.py路径，并同时指定output')
    recipe = {key: value for key, value in target.items() if key != 'reproduce'}
    for key in ('path', 'output'):
        recipe[key] = resolve_file(recipe[key], root).relative_to(root).as_posix()
    depth = len(destination.parent.relative_to(root).parts)
    code = '''"""独立分组统计复现入口，不依赖培风安装目录。"""
from pathlib import Path
import argparse, csv, hashlib, io, json, math, os, statistics, tempfile

def resolve_file(name, base_dir=None):
    root = Path(base_dir or '.').resolve()
    path = (root / name).resolve()
    path.relative_to(root)
    return path

'''
    code += inspect.getsource(summarize)
    code += f'''\nif __name__ == '__main__':
    parser = argparse.ArgumentParser(description='重新生成分组统计CSV')
    parser.add_argument('--base-dir', default=str(Path(__file__).resolve().parents[{depth}]))
    args = parser.parse_args()
    recipe = json.loads({json.dumps(recipe, ensure_ascii=False)!r})
    source = resolve_file(recipe['path'], args.base_dir)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    print(summarize(recipe, args.base_dir))
    if hashlib.sha256(source.read_bytes()).hexdigest() != before:
        raise RuntimeError('复现期间输入发生变化')
'''
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(code, encoding='utf-8')
    return str(destination)


def run(targets, base_dir=None):
    return dispatch(summarize, targets, base_dir)


if __name__ == '__main__':
    from backend.utils.skills import run_script_cli
    raise SystemExit(run_script_cli(run, __doc__))
