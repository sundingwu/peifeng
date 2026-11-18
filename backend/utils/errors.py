"""编排层用于区分失败阶段的异常类型。"""

from __future__ import annotations
import json


def tool_error_hint(output: str) -> str:
    """从批次/CLI回执提取错误原因，避免把外部内容边界当成错误摘要。"""
    lines=output.strip().splitlines()
    for line in lines:
        if not line.lstrip().startswith('{'):
            continue
        try:
            payload=json.loads(line)
        except ValueError:
            continue
        if not isinstance(payload,dict):
            continue
        rows=payload.get('results')
        rows=rows if isinstance(rows,list) else []
        errors=[str(row['error']) for row in rows if isinstance(row,dict) and row.get('status')=='failed' and row.get('error')]
        if payload.get('error'):
            errors.insert(0,str(payload['error']))
        if errors:
            return '；'.join(errors)[:500]
    errors=[line.strip() for line in lines if line.strip().startswith(('错误：','✗ '))]
    return ('；'.join(errors) if errors else next((line for line in lines if not line.startswith('【外部内容')),''))[:500]



# ---------------------------------------------------------------- exceptions
class CancelledError(Exception):
    """用户在步骤边界中止了当前任务。"""


class PlanningError(Exception):
    """规划阶段无法产出合法结果（输出无法解析、缺少步骤、工作流名未知等）。"""


class ExecutionError(Exception):
    """执行层无法返回合法结构化结果（JSON 解析失败、模型调用失败等）。

    ``records`` 带着**抛出前已经执行过的工具调用**：解析失败不等于什么都没做，
    worker 很可能已经写了文件。编排层据此把这一步记成 partial，而不是留一片空白。
    """

    records: list = []


class ReviewError(Exception):
    """评审输出无法解析为合法评审结论。"""
