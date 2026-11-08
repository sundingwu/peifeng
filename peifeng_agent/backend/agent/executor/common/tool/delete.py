from __future__ import annotations

import shutil
from pathlib import Path
from typing import List, Tuple

#: 条数不限（逐项回报成功/跳过/失败，结果本身很短）
#: 通配符：本工具只处理具体路径
WILDCARDS = ("*", "?")

information = {
    "name": "delete",
    "description": (
        "删除文件或目录，默认删文件与空目录，"
        "recursive=true 删除整棵目录树（清空清理）。"
        "用于：清理中间产物、清空临时目录时。删除不可逆，可先 dry_run=true 列出将删除的内容。"
        "逐项回报成功、跳过、失败"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "待删除项（条数不限）",
                "items": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "要删除的路径"}},
                    "required": ["path"],
                },
            },
            "recursive": {"type": "boolean", "description": "true 时删除非空目录及其内容（清空清理），默认 false"},
            "dry_run": {"type": "boolean", "description": "只列出将删除什么，不改动（删除前先跑一次）"},
        },
        "required": ["items"],
    },
}


def delete(items: List[dict], recursive: bool = False, dry_run: bool = False, base_dir=None) -> str:
    """按 items 批量删除，返回逐项结果；相对路径以 base_dir 为基准。"""
    planned = parse_items(items)

    lines: List[str] = []
    counts = {"ok": 0, "skip": 0, "fail": 0}
    for path in planned:
        try:
            mark, text = remove(path, bool(recursive), bool(dry_run), base_dir)
        except OSError as error:
            mark, text = "✗", f"{path}：{error}"
        counts[{"✓": "ok", "-": "skip", "✗": "fail"}[mark]] += 1
        lines.append(f"{mark} {text}")

    prefix = "预演（未改动）" if dry_run else "已"
    head = f"{prefix}删除：成功 {counts['ok']} 项、跳过 {counts['skip']} 项、失败 {counts['fail']} 项"
    return "\n".join([head, *lines])


def resolve(path: str, base_dir) -> Path:
    """相对路径以 base_dir（项目工作文件夹）为基准；绝对路径原样使用。"""
    target = Path(path)
    if target.is_absolute() or base_dir is None:
        return target
    return Path(base_dir) / target


def parse_items(items) -> List[str]:
    """校验 items 结构，返回路径列表。"""
    if not isinstance(items, list) or not items:
        raise RuntimeError("items 需要非空数组：[{path}]")
    paths: List[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or not str(item.get("path") or "").strip():
            raise RuntimeError(f'items[{index}] 需要 {{"path": ...}}：{item!r}')
        path = str(item["path"]).strip()
        if any(char in path for char in WILDCARDS):
            raise RuntimeError(f"不支持通配符：{path}（先用 read 列出目录，取具体路径）")
        paths.append(path)
    return paths


def remove(path: str, recursive: bool, dry_run: bool, base_dir) -> Tuple[str, str]:
    """删除单项，返回 (状态标记, 说明)：``✓`` 成功 / ``-`` 跳过 / ``✗`` 失败。"""
    target = resolve(path, base_dir)
    if not target.exists():
        return "-", f"{target} 不存在，跳过"

    if target.is_dir():
        try:
            empty = not any(target.iterdir())
        except OSError as error:
            return "✗", f"{target} 无法读取：{error}"
        if not empty and not recursive:
            return "✗", f"{target} 不是空目录：需要清空请传 recursive=true"
        if not dry_run:
            if empty:
                target.rmdir()
            else:
                shutil.rmtree(target)
        note = "（空目录）" if empty else "（整棵目录树，recursive=true）"
        return "✓", f"{target}{note}" + ("（将删除）" if dry_run else "")

    if not dry_run:
        target.unlink()
    return "✓", f"{target}" + ("（将删除）" if dry_run else "")
