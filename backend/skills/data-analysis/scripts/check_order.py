"""Verify declared CSV/JSON ordering without changing the artifact."""
from pathlib import Path
import sys

for parent in Path(__file__).resolve().parents:
    if (parent / 'backend').is_dir():
        sys.path.insert(0, str(parent))
        break

import csv
import json
from backend.utils.skills import resolve_file, dispatch


def check_order(target: dict, base_dir=None) -> str:
    source = resolve_file(target['path'], base_dir)
    keys = target.get('keys')
    if source.suffix.lower() == '.csv':
        if target.get('array'):
            raise ValueError('CSV不支持array字段')
        with source.open(encoding='utf-8-sig', newline='') as stream:
            records = list(csv.DictReader(stream))
    elif source.suffix.lower() == '.json':
        records = json.loads(source.read_text(encoding='utf-8-sig'))
        selector = target.get('array') or ''
        if not isinstance(selector, str):
            raise ValueError('array必须为字段路径字符串')
        for part in selector.split('.'):
            if part:
                if isinstance(records,list):
                    if not part.isdigit() or int(part)>=len(records):
                        raise ValueError(f'数组下标无效：{part}')
                    records = records[int(part)]
                elif isinstance(records,dict) and part in records:
                    records = records[part]
                else:
                    raise ValueError(f'数组路径不存在：{selector}')
    else:
        raise ValueError('仅支持CSV和JSON')
    if not isinstance(records, list):
        raise ValueError('目标必须是数组')
    objects = source.suffix.lower() == '.csv' or bool(keys) and not records or bool(records) and all(isinstance(row,dict) for row in records)
    if objects:
        if not isinstance(keys,list) or not keys or any(not isinstance(key,str) or not key for key in keys) or len(keys)!=len(set(keys)):
            raise ValueError('对象数组keys必须为非空且不重复字段名数组')
    elif any(isinstance(row,(dict,list)) or row is None or isinstance(row,bool) for row in records):
        raise ValueError('不能混用对象与标量；标量只支持数值或字符串')
    elif keys:
        raise ValueError('标量数组直接比较值，省略keys或设为空数组')
    try:
        values = [tuple(row[key] for key in keys) for row in records] if objects else records
        expected = sorted(values)
    except (KeyError, TypeError) as error:
        raise ValueError('排序字段缺失或类型不可比较，不做隐式类型转换') from error
    if values != expected:
        index = next(index for index, (actual, wanted) in enumerate(zip(values, expected), 1) if actual != wanted)
        raise ValueError(f'排序不符：第{index}条实际{values[index-1]!r}，应为{expected[index-1]!r}；修复生成源码后重跑')
    return json.dumps({'status':'pass','path':str(source),'array':target.get('array'),'keys':keys,'records':len(records),'input_modified':False}, ensure_ascii=False)


def run(targets, base_dir=None):
    return dispatch(check_order, targets, base_dir)


if __name__ == '__main__':
    from backend.utils.skills import run_script_cli
    raise SystemExit(run_script_cli(run, __doc__))
