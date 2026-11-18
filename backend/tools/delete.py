from __future__ import annotations

from backend.utils.web import run_targets

import shutil
from pathlib import Path
from typing import Tuple


#: 条数不限（逐项回报成功/跳过/失败，结果本身很短）
#: 通配符：本工具只处理具体路径
WILDCARDS = ("*", "?")

#: 文件操作并发度：不同路径互不影响，4 路足够
FILE_WORKERS = 4

information = {'name': 'delete',
 'description': '删除文件或目录。用于清理中间产物、清空临时目录。删除不可逆，逐项回报成功、跳过、失败。',
 'parameters': {'type': 'object',
                'properties': {'targets': {'type': 'array',
                                           'minItems': 1,
                                           'items': {'type': 'object',
                                                     'properties': {'recursive': {'type': 'boolean',
                                                                                  'description': 'true '
                                                                                                 '时删除非空目录及其内容（清空清理），默认 '
                                                                                                 'false'},
                                                                    'dry_run': {'type': 'boolean',
                                                                                'description': '只列出将删除什么，不改动（删除前先跑一次）'},
                                                                    'path': {'type': 'string',
                                                                             'description': '要删除的路径'}},
                                                     'required': ['path'],
                                                     'additionalProperties': False}}},
                'required': ['targets'],
                'additionalProperties': False}}


def delete_target(path: str, recursive: bool = False, dry_run: bool = False, base_dir=None) -> str:
    """校验并删除一个具体路径。"""
    if not isinstance(path, str) or not path.strip() or any(char in path for char in WILDCARDS):
        raise RuntimeError("path 必须是具体路径，不支持通配符")
    mark, message = remove(path.strip(), recursive, dry_run, base_dir)
    from backend.utils.tool_output import ToolOutput
    return ToolOutput(f"{mark} {message}",has_errors=mark=='✗')


def resolve(path: str, base_dir) -> Path:
    """相对路径以 base_dir（项目工作目录）为基准；绝对路径必须落在白名单内。"""
    from backend.utils.paths import resolve_path

    return resolve_path(path, base_dir, write=True)




def remove(path: str, recursive: bool, dry_run: bool, base_dir) -> Tuple[str, str]:
    """删除单项，返回 (状态标记, 说明)：``✓`` 成功 / ``-`` 跳过 / ``✗`` 失败。"""
    target = resolve(path, base_dir)
    if not target.exists():
        return "-", f"{target} 不存在，跳过"
    # Compression notes and oversized tool receipts retain these paths for rereads.
    workspace = Path(base_dir).resolve() if base_dir else None
    if workspace:
        archive = workspace / 'internal' / 'spill'
        evidence = list(archive.glob('peifeng-output-*.txt')) if archive.is_dir() else []
        if any(target == item.resolve() or target in item.resolve().parents for item in evidence):
            raise RuntimeError('不能删除系统证据归档或其父目录；压缩上下文仍依赖 internal/spill 中的回读路径。仅清理自己的临时文件。')

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


def delete(targets: list[dict], base_dir=None, roots=None) -> str:
    """统一批处理入口，重叠路径自动串行。"""
    from backend.utils import paths

    paths.bind(roots or paths.Roots(base_dir))

    return run_targets(lambda target: delete_target(**target, base_dir=base_dir), targets, workers=8, base_dir=base_dir)
