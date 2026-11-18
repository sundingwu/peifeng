"""word_markdown.status — 退出码。

技能与脚本靠这几个数字判断结果（`SKILL.md` §8 把它们列在冻结面里），
所以在代码里只写一次：改数字等于改契约。
"""
from __future__ import annotations
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_PDF_SKIPPED = 3
