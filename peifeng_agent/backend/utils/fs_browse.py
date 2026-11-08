"""目录浏览：给页面"选择文件夹"用的目录列表。

为什么不是服务端弹系统目录选择框：那需要 GUI，在无桌面的环境里会直接失败（而且它同样
只在服务端所在机器上有意义）。浏览器一侧也拿不到本机目录的绝对路径——``<input webkitdirectory>``
和 ``showDirectoryPicker()`` 都只给相对路径。所以做法是：服务端把某一层目录的**子目录**
列给页面，页面自己渲染浏览器。

因此本模块是只读的、只看目录：不返回文件内容，也没有任何写操作。
"""
from __future__ import annotations

import os
import string
from pathlib import Path
from typing import Optional


def list_dirs(path: str = "", default=None) -> Optional[dict]:
    """列出 ``path`` 下的子目录；``path`` 为空时用 ``default``（默认位置）。

    返回 ``None`` 表示该路径不可用（为空、不存在或读不到），由调用方转成错误响应——
    注意**不**把"给了路径但无效"回退成默认位置：那样页面会以为选中的是别的地方。
    """
    target = resolve_dir(path) if str(path or "").strip() else resolve_dir(default)
    if target is None:
        return None
    return {
        "path": str(target),
        # 到根（``C:\\`` / ``/``）就没有上一级：页面据此禁用「上一级」
        "parent": str(target.parent) if target.parent != target else "",
        "entries": subdirs(target),
        "roots": roots(),
        "shortcuts": shortcuts(default),
    }


def resolve_dir(value) -> Optional[Path]:
    """把用户给的路径解析成"存在且可访问的目录"；解析不了返回 ``None``。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        target = Path(text).expanduser()
        if not target.is_dir():
            return None
        # 规范化：同一目录的多种写法（``.`` / 链接 / 大小写）在页面里看起来是同一个
        return target.resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def subdirs(folder: Path) -> list[dict]:
    """``folder`` 下的子目录（按名字排序，读不到的条目跳过）；只列目录，不列文件。"""
    entries: list[dict] = []
    try:
        children = sorted(folder.iterdir(), key=lambda item: item.name.lower())
    except OSError:
        return entries
    for child in children:
        try:
            if not child.is_dir():
                continue
        except OSError:  # noqa: BLE001 - 权限不足、链接失效等情况跳过即可
            continue
        entries.append({"name": child.name, "path": str(child)})
    return entries


def roots() -> list[dict]:
    """可切换的顶层位置：Windows 是各盘符，其它平台是 ``/``。"""
    if os.name != "nt":
        return [{"name": "/", "path": "/"}]
    found: list[dict] = []
    for letter in string.ascii_uppercase:
        drive = Path(f"{letter}:\\")
        try:
            if drive.is_dir():
                found.append({"name": f"{letter}:", "path": str(drive)})
        except OSError:  # noqa: BLE001 - 空驱动器、未就绪的设备等，跳过
            continue
    return found


def shortcuts(default=None) -> list[dict]:
    """常用位置（默认位置、主目录），供页面一键跳转。"""
    found: list[dict] = []
    for label, candidate in (("默认位置", default), ("主目录", Path.home())):
        target = resolve_dir(candidate)
        if target is None or any(item["path"] == str(target) for item in found):
            continue
        found.append({"name": label, "path": str(target)})
    return found
