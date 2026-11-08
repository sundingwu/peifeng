"""token 估算：**只在端点没有返回 ``usage`` 时兜底**。

准确的值应当来自端点响应里的 ``usage.prompt_tokens``（账单口径，见 ``llm.ResponseMessage.usage``）。
拿不到时用这里的字符换算，口径取自 DeepSeek 官方文档：

- 1 个英文字符 ≈ 0.3 token
- 1 个中文字符 ≈ 0.6 token

比"UTF-8 字节数 ÷ 3"准得多：那个算法把每个汉字（3 字节）算成 1 token，
而实际约 0.6 token，中文为主的文本会系统性高估 40% 以上。
"""

from __future__ import annotations

#: 按"中文字符"计价（0.6 token/字）的码位区间：汉字、扩展 A、兼容区、中日韩标点、全角字符
_CJK_RANGES = (
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0x3000, 0x303F),
    (0xFF00, 0xFFEF),
)


def _is_cjk(char: str) -> bool:
    """该字符是否按中文字符计价（更快：按码位比较，不用逐字符正则）。"""
    code = ord(char)
    return any(low <= code <= high for low, high in _CJK_RANGES)


def count_cjk(text: str) -> int:
    """统计文本里的中文字符数（含中文标点与全角字符）。"""
    return sum(1 for char in text if _is_cjk(char))


def estimate_tokens(text: str) -> int:
    """按字符构成估算 token 数（无 ``usage`` 时的回退）。

    中文字符 0.6 token、其余字符 0.3 token；空串为 0。
    结果向下取整，至少为 1（非空文本不可能真的是 0 token）。
    """
    if not text:
        return 0
    cjk = count_cjk(text)
    return max(1, int(cjk * 0.6 + (len(text) - cjk) * 0.3))
