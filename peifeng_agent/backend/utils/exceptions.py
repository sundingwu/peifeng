"""异常类型：编排层按类型区分失败阶段（规划 / 执行 / 评审 / 用户中止），据此决定任务终态。

- ``PlanningError``：规划阶段失败（输出无法解析、轮次耗尽）
- ``ExecutionError``：执行阶段无法产出合法结果
- ``ReviewError``：评审阶段无法产出合法结论
- ``CancelledError``：用户在步骤边界中止
"""
from __future__ import annotations


class CancelledError(Exception):
    """用户在步骤边界中止了当前任务。"""


class PlanningError(Exception):
    """规划阶段无法产出合法结果（输出无法解析、缺少步骤、工作流名未知等）。"""


class ExecutionError(Exception):
    """执行层无法返回合法结构化结果（JSON 解析失败、模型调用失败等）。"""


class ReviewError(Exception):
    """评审输出无法解析为合法评审结论。"""
