from pathlib import Path
import sys
for _parent in Path(__file__).resolve().parents:
    if (_parent / 'backend').is_dir():
        sys.path.insert(0, str(_parent))
        break
import json, math, statistics as descriptive, sys
from backend.utils.skills import resolve_file, dispatch
from backend.memory.store import write_json

def statistics_target(target: dict, base_dir=None) -> str:
    """Compute explicit analyses; never infer samples from manuscript prose."""
    from scipy import stats
    values = target.get('values')
    if target.get('path'):
        import pandas as pd
        frame = pd.read_csv(resolve_file(target['path'], base_dir))
        values = frame[target['column']].tolist()
    if not isinstance(values, list) or not values:
        raise ValueError('提供非空values或CSV path与column')
    if any(isinstance(value, bool) for value in values):
        raise ValueError('不接受布尔观测')
    numbers = [float(value) for value in values]
    if not all(math.isfinite(value) for value in numbers):
        raise ValueError('存在缺失或非有限值；请先明确处理规则')
    operation = target.get('operation', 'describe')
    result = {'operation': operation, 'n': len(numbers), 'mean': descriptive.mean(numbers), 'sample_sd': descriptive.stdev(numbers) if len(numbers) > 1 else None}
    if operation == 'describe' and len(numbers) > 1:
        interval = stats.t.interval(.95, len(numbers)-1, loc=result['mean'], scale=stats.sem(numbers)) if result['sample_sd'] else (result['mean'], result['mean'])
        result['mean_ci95'] = [float(value) for value in interval]
    elif operation in {'welch', 'paired'}:
        other = [float(value) for value in target.get('other', [])]
        if len(numbers) < 2 or len(other) < 2 or not all(math.isfinite(value) for value in other):
            raise ValueError('两组都需至少两个有限观测')
        if operation == 'paired' and len(numbers) != len(other):
            raise ValueError('配对数据长度必须相等，并由调用者确认顺序')
        test = stats.ttest_rel(numbers, other) if operation == 'paired' else stats.ttest_ind(numbers, other, equal_var=False)
        if not math.isfinite(float(test.statistic)) or not math.isfinite(float(test.pvalue)):
            raise ValueError('退化数据不能计算有效检验')
        result.update(statistic=float(test.statistic), pvalue=float(test.pvalue), n_other=len(other), mean_difference=descriptive.mean(numbers)-descriptive.mean(other))
    elif operation == 'meta':
        variances = [float(value) for value in target.get('variances', [])]
        if len(variances) != len(numbers) or any(not math.isfinite(value) or value <= 0 for value in variances):
            raise ValueError('每个效应量需要一个有限正方差')
        weights = [1/value for value in variances]
        pooled = sum(w*x for w,x in zip(weights,numbers))/sum(weights)
        standard_error = math.sqrt(1/sum(weights))
        q = sum(w*(x-pooled)**2 for w,x in zip(weights,numbers))
        result.update(model='fixed_effect_inverse_variance', pooled_effect=pooled, ci95=[pooled-1.95996398454*standard_error,pooled+1.95996398454*standard_error], Q=q, I2=max(0,(q-(len(numbers)-1))/q)*100 if q else 0)
    elif operation != 'describe':
        raise ValueError('operation支持describe/welch/paired/meta')
    if target.get('output'):
        write_json(resolve_file(target['output'], base_dir), result)
    return json.dumps(result, ensure_ascii=False, allow_nan=False)

def run(targets, base_dir=None):
    return dispatch(statistics_target, targets, base_dir)

if __name__ == '__main__':
    from backend.utils.skills import run_script_cli
    raise SystemExit(run_script_cli(run, __doc__))
