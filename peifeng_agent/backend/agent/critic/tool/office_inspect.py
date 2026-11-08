"""本角色的只读结构查看入口，共享实现保持一致。

核对版式/字号/页数这类改动时用它：比 `read` 读成纯文本准确，
也不必去翻备份原文逐段对比（实测那样一次评审要 14 次 read）。
"""
from peifeng_agent.backend.utils.office_inspect import information as information
from peifeng_agent.backend.utils.office_inspect import office_inspect as office_inspect
