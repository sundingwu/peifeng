"""模型输出的容错处理：把可能畸形的文本整理成可用的 JSON 对象。

**为什么需要它**（下面每条都在真实日志里出现过，不是假想）：

- 模型在 JSON 前后写解释、包 Markdown 围栏；
- 工具被关掉后仍按工具格式输出文本标记（``<|DSML|function_calls>``、``​```tool``）；
- 输出被截断（``finish_reason=length``），JSON 括号没闭合；
- 尾逗号、裸控制字符这类小毛病。

处理顺序是"从轻到重"：原样解析 → 去掉围栏与工具标记 → 修尾逗号 → 补全截断的括号。
全都不成时抛 :class:`ValueError`，消息里说明**卡在哪一步**，让上层能把它写进重试提示。
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

#: 工具调用标记：``<|DSML|function_calls>…</|DSML|function_calls>`` 一类（各家叫法不同）
_TOOL_MARKUP = re.compile(r"<\|[^|>]*\|[^>]*>.*?(?:</\|[^|>]*\|[^>]*>|$)", re.S)
#: ``` 围栏（含 ```tool / ```json 这类带语言标注的）
_FENCE = re.compile(r"```[a-zA-Z0-9_-]*\n(.*?)(?:```|$)", re.S)
#: 尾逗号：``{"a": 1,}`` / ``[1, 2,]``
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def strip_fences(text: str) -> str:
    """去掉 Markdown 代码围栏，只留里面的正文（没有围栏时原样返回）。"""
    body = (text or "").strip()
    if not body.startswith("```"):
        return body
    match = _FENCE.search(body)
    return (match.group(1) if match else body).strip()


def strip_tool_markup(text: str) -> str:
    """去掉模型误输出的工具调用标记（工具被关掉时它会这么写）。"""
    cleaned = _TOOL_MARKUP.sub("", text or "")
    # 语言标注为 tool 的围栏整块丢弃（那是"我想调工具"的文本形式，不是结果）
    cleaned = re.sub(r"```tool\b.*?(?:```|$)", "", cleaned, flags=re.S)
    return cleaned


def drop_trailing_commas(text: str) -> str:
    """删掉对象/数组末尾多余的逗号。"""
    return _TRAILING_COMMA.sub(r"\1", text)


def close_truncated(text: str) -> str:
    """尽力补全被截断的 JSON：从第一个 ``{`` 起补齐未闭合的括号。

    只在"确实是被截断"（引号配平、括号不配平）时生效；补完仍不合法就原样返回。
    """
    start = text.find("{")
    if start < 0:
        return text
    fragment = text[start:]
    if fragment.count('"') % 2 != 0:
        return text  # 停在字符串中间：补引号只会更糟
    stack: list[str] = []
    in_string = False
    escaped = False
    for char in fragment:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]" and stack:
            stack.pop()
    if not stack:
        return fragment
    return fragment.rstrip().rstrip(",") + "".join(reversed(stack))


def parse_json_object(text: str) -> dict:
    """从模型输出里取出一个 JSON 对象；四道兜底都失败时抛 ``ValueError``。

    候选按"解析跨度最大者优先、跨度相同取更靠后者"挑选——前者保证拿到最外层对象，
    后者保证模型"先给示例、再给结果"时取到结果。
    """
    return parse_json(text, want_dict=True)


def parse_json_value(text: str) -> Any:
    """取出文本里的任意 JSON 值（对象/数组/标量）——工具调用参数走这条。

    工具参数由模型自己写，同样会有围栏、尾逗号、被截断这些毛病，所以共用一套兜底。
    """
    return parse_json(text, want_dict=False)


def parse_json(text: str, *, want_dict: bool) -> Any:
    """按四道容错依次尝试解析；``want_dict`` 决定只认对象还是任意 JSON 值。"""
    steps = (
        ("原样", lambda value: value),
        ("去掉围栏", strip_fences),
        ("去掉工具标记", lambda value: strip_tool_markup(strip_fences(value))),
        ("修尾逗号", lambda value: drop_trailing_commas(strip_tool_markup(strip_fences(value)))),
        (
            "补全截断",
            lambda value: close_truncated(drop_trailing_commas(strip_tool_markup(strip_fences(value)))),
        ),
    )
    tried: list[str] = []
    scalar: Any = None
    for label, transform in steps:
        candidate = transform(text or "")
        parsed = largest_value(candidate, want_dict=want_dict)
        if parsed is None:
            tried.append(label)
            continue
        if want_dict or isinstance(parsed, (dict, list)):
            return parsed
        # 只拿到标量：记着，等后面几步（修尾逗号 / 补全）试完——坏掉的对象往往能救回来，
        # 而前面几步只能捞到里面的某个字符串（例如 {"items": [1, 2,]} 里的 "items"）
        scalar = parsed if scalar is None else scalar
        tried.append(label)
    if scalar is not None:
        return scalar
    raise ValueError(
        "输出里找不到可解析的 JSON（已尝试：" + "、".join(tried) + "）"
        "；请只输出一个 JSON 对象，不要解释、不要代码围栏、不要工具调用标记"
    )


def largest_value(source: str, *, want_dict: bool) -> Any:
    """在文本里挑一个跨度最大、最靠后的 JSON 值；没有则返回 ``None``。

    排序键依次是：**是否结构性值**（对象/数组）→ 跨度 → 靠后。结构性优先很重要：不这样
    的话，``{"items": [1, 2,]}`` 这种尾逗号坏对象会退化成取到里面那个字符串 ``"items"``。
    ``raw_decode`` 需要完整字面量，所以逐个起点试探是安全的（半截的 ``tru``、``12`` 会失败跳过）。
    """
    decoder = json.JSONDecoder(strict=False)
    best: Optional[tuple[tuple[bool, int, int], Any]] = None
    for index, char in enumerate(source):
        if char not in '{["-0123456789tfn':
            continue
        try:
            data, end = decoder.raw_decode(source, index)
        except ValueError:
            continue
        if want_dict and not isinstance(data, dict):
            continue
        rank = (isinstance(data, (dict, list)), end - index, index)
        if best is None or rank >= best[0]:
            best = (rank, data)
    return None if best is None else best[1]


def describe_failure(text: str) -> str:
    """给重试提示用的一句话：说明输出哪里不像 JSON（截断 / 带标记 / 无对象）。"""
    body = (text or "").strip()
    if not body:
        return "输出为空"
    if _TOOL_MARKUP.search(body) or "```tool" in body:
        return "输出里混入了工具调用标记（工具已关闭，不要再写调用格式）"
    if body.count("{") > body.count("}"):
        return "JSON 没有写完（括号不闭合），可能被长度上限截断"
    if not body.startswith("{"):
        return "输出不是以 { 开头的 JSON 对象（前面有解释或围栏）"
    return "结构不合法"
