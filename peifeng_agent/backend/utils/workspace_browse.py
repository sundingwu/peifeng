"""项目工作目录内的浏览：给页面的"选文件 / 选目录"用。

**为什么单独一套**：页面选项目里的素材，不该让人在整块磁盘上找——路径要相对项目目录
（worker的工具就是按相对路径读的），也不能越出工作目录（``../`` 一律拒绝）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

#: 单层最多列多少条：目录再大也不至于把页面拖死
MAX_ENTRIES = 500


def resolve_inside(root: Path, relative: str = "") -> Optional[Path]:
    """把相对路径解析成 ``root`` 内的目录；越界或不存在返回 ``None``。"""
    try:
        base = Path(root).resolve()
    except (OSError, RuntimeError):
        return None
    text = str(relative or "").strip().replace("\\", "/").strip("/")
    try:
        target = (base / text).resolve() if text else base
    except (OSError, RuntimeError):
        return None
    if target != base and base not in target.parents:
        return None  # 跳出工作目录：拒绝
    return target if target.is_dir() else None


def relative_to_root(root: Path, target: Path) -> str:
    """相对路径用正斜杠，跨平台显示一致（worker的工具也认这个写法）。

    项目根目录返回空串（``Path.relative_to`` 对自身会给 ``"."``，页面上不该显示成子目录）。
    """
    base = Path(root).resolve()
    try:
        resolved = target.resolve()
    except (OSError, RuntimeError):
        return ""
    if resolved == base:
        return ""
    try:
        return resolved.relative_to(base).as_posix()
    except ValueError:
        return ""


def list_workspace(root: Optional[Path], relative: str = "") -> Optional[dict]:
    """列出工作目录里 ``relative`` 这一层的子目录与文件；越界或不可用返回 ``None``。

    ``path`` 回到上一级时给 ``parent``（已经在根上则给空串，页面据此禁用「上一级」）。
    ``dirs`` / ``files`` 的 ``path`` 都相对工作目录，可以直接填进任务描述。
    """
    if root is None:
        return None
    base = resolve_inside(root, relative)
    if base is None:
        return None
    current = relative_to_root(root, base)
    folders: list[dict] = []
    files: list[dict] = []
    try:
        children = sorted(base.iterdir(), key=lambda item: item.name.lower())
    except OSError:
        return None
    for child in children[:MAX_ENTRIES]:
        try:
            if child.name.startswith("."):
                continue
            path = relative_to_root(root, child)
            if child.is_dir():
                folders.append({"name": child.name, "path": path})
            elif child.is_file():
                files.append({"name": child.name, "path": path, "size": child.stat().st_size})
        except OSError:
            continue
    parent = ""
    if current:
        parent = "/".join(current.split("/")[:-1])
    return {"path": current, "parent": parent, "dirs": folders, "files": files}
