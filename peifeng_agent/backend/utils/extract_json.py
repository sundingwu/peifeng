"""从模型输出中容错提取 JSON 对象（薄封装，能力在 :mod:`model_output`）。

保留本模块是为了让调用点与历史测试不用改：真正的容错（去围栏、去工具标记、修尾逗号、
补全截断）都在 ``model_output.parse_json_object`` 里。
"""
from __future__ import annotations

from typing import Any

from peifeng_agent.backend.utils.model_output import parse_json_object, strip_fences


def extract_json(text: str) -> Any:
    """返回文本中可解析的 JSON 对象；找不到时抛 :class:`ValueError`。"""
    return parse_json_object(text)


def strip_code_fence(text: str) -> str:
    """去掉包裹正文的 Markdown 代码围栏（若存在）。"""
    return strip_fences(text)
