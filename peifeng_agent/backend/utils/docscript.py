"""文档脚本执行层：让 docx / xlsx / pptx 三个工具透传底层库的全部能力。

模型在脚本里拿到的是真正的库对象（``Document`` / ``Workbook`` / ``Presentation``），
库有什么方法就能用什么——不必等工具为每个能力单开参数。工具这边只负责三件事：

1. **执行**：给一个收窄的命名空间（库对象 + 常用单位与枚举 + 安全内置函数），
   拿掉 ``__import__`` / ``open`` 这类入口，脚本只能改文档，碰不到文件系统与网络。
2. **保存**：执行完统一落盘，异常时不动原文件。
3. **校验**：跑前跑后各取一次结构快照，回执里给出改动量，改坏了能被看见。
"""
from __future__ import annotations

import sys
import traceback
from typing import Any, Callable

#: 脚本能用的内置函数：够写逻辑，不含 import / open / eval / exec
SAFE_BUILTINS: dict[str, Any] = {
    "print": print,
    "len": len,
    "range": range,
    "enumerate": enumerate,
    "zip": zip,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list": list,
    "dict": dict,
    "set": set,
    "tuple": tuple,
    "sorted": sorted,
    "reversed": reversed,
    "min": min,
    "max": max,
    "sum": sum,
    "abs": abs,
    "round": round,
    "isinstance": isinstance,
    "getattr": getattr,
    "hasattr": hasattr,
    "setattr": setattr,
    "type": type,
    "None": None,
    "True": True,
    "False": False,
}

#: 脚本允许 import 的模块根：只放文档库自己，挡住 os / sys / subprocess / socket
ALLOWED_IMPORT_ROOTS = frozenset({"docx", "pptx", "openpyxl"})


def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    """受限的 ``__import__``：只允许导入文档库的子模块。

    **为什么需要**：模型写脚本时最自然的开头就是 ``from docx.shared import Pt``，
    完全禁掉 import 会让这类脚本必然失败（实测连错 2 次）。而文档库的辅助类型
    （Pt/Cm/RGBColor/枚举）分散在子模块里，靠 namespace 逐个注入也覆盖不全。
    白名单挡住 ``os``/``sys``/``subprocess`` 这类能碰文件系统与网络的模块。
    """
    root = str(name or "").split(".")[0]
    if root not in ALLOWED_IMPORT_ROOTS:
        raise ImportError(
            f"脚本里只能 import 文档库（docx / pptx / openpyxl），不能用 {name}；"
            "文件读写请用 read / write / bash 工具"
        )
    return __import__(name, globals, locals, fromlist, level)


SAFE_BUILTINS["__import__"] = safe_import


def run(
    script: str,
    namespace: dict[str, Any],
    save: Callable[[], None],
    snapshot: Callable[[], dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """执行脚本并保存，返回 ``(执行前快照, 执行后快照, 脚本输出)``。"""
    body = (script or "").strip()
    if not body:
        raise RuntimeError("script 不能为空：把要对文档做的操作写成 Python 代码")

    before = snapshot()
    output: list[str] = []
    scope = {"__builtins__": dict(SAFE_BUILTINS), **namespace}
    scope["print"] = lambda *args, **kwargs: output.append(" ".join(str(item) for item in args))
    try:
        exec(compile(body, "<脚本>", "exec"), scope)  # noqa: S102 - 收窄命名空间后的受控执行
    except SyntaxError as error:
        raise RuntimeError(f"脚本语法错误（第 {error.lineno} 行）：{error.msg}") from None
    except Exception as error:  # noqa: BLE001 - 把原始错误回给模型，它才改得对
        line = error_line()
        where = f"第 {line} 行" if line else "脚本执行中"
        raise RuntimeError(f"{where}出错：{type(error).__name__}: {error}") from None

    save()
    return before, snapshot(), "\n".join(output)


def diff(before: dict[str, Any], after: dict[str, Any]) -> str:
    """把前后快照的差异写成一句话（页数/对象数这类结构性变化）。"""
    changes: list[str] = []
    for key, value in after.items():
        old = before.get(key)
        if old == value:
            continue
        if isinstance(value, int) and isinstance(old, int):
            changes.append(f"{key} {old} → {value}")
        elif isinstance(value, list) and isinstance(old, list) and len(old) == len(value):
            moved = [f"第 {index + 1} 项 {old[index]} → {value[index]}" for index in range(len(value)) if old[index] != value[index]]
            changes.append(f"{key}：" + "；".join(moved[:6]) + ("…" if len(moved) > 6 else ""))
        else:
            changes.append(f"{key}：{old} → {value}")
    return "；".join(changes) if changes else "结构未变（可能只改了文字或格式）"


def error_line() -> int:
    """从最近一次 traceback 里取出错行号（脚本本身的行，不是调用栈）。"""
    for frame in traceback.extract_tb(sys.exc_info()[2]):
        if frame.filename == "<脚本>":
            return frame.lineno
    return 0
