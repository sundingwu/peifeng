"""move 工具（搬）：复制、移动、重命名文件或目录。

一次处理一批，逐项回报成功、跳过、失败；单项失败不影响其余项。
`dry_run=true` 先预演；`on_conflict` 统一冲突策略（rename 加后缀 / skip 跳过 / overwrite 覆盖）。
不接受通配符——路径必须来自 `read` 列出来的真实条目。

工具约定：文件头 information 元数据 + 与 name 同名的业务函数；成功 return 文本，失败抛 RuntimeError。
"""
from __future__ import annotations

from backend.utils.web import run_targets

import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Tuple


#: 支持的动作与冲突策略
ACTIONS = ("copy", "move", "rename")
CONFLICTS = ("rename", "skip", "overwrite")
#: 单批上限（再多就没法在一次上下文里核对）
#: 条数不限（逐项回报成功/跳过/失败，结果本身很短）
#: 通配符：本工具只处理具体路径
WILDCARDS = ("*", "?")
#: 动作动词（用在结果标题里）
VERBS = {"copy": "复制", "move": "移动", "rename": "重命名"}

#: 文件操作并发度：不同源/目标互不影响，4 路足够
FILE_WORKERS = 4

information = {'name': 'move',
 'description': '搬动文件：复制、移动、重命名。用于归档到子目录、批量改名、复制留底。逐项回报成功、跳过、失败。',
 'parameters': {'type': 'object',
                'properties': {'targets': {'type': 'array',
                                           'minItems': 1,
                                           'items': {'type': 'object',
                                                     'properties': {'action': {'type': 'string',
                                                                               'enum': ['copy',
                                                                                        'move',
                                                                                        'rename'],
                                                                               'description': 'copy '
                                                                                              '复制（保留原文件）/ '
                                                                                              'move 移动 / '
                                                                                              'rename '
                                                                                              '同目录内改名'},
                                                                    'dry_run': {'type': 'boolean',
                                                                                'description': '只预演不改动（批量改名或覆盖之前先跑一次）'},
                                                                    'on_conflict': {'type': 'string',
                                                                                    'enum': ['rename',
                                                                                             'skip',
                                                                                             'overwrite'],
                                                                                    'description': '目标已存在时的处理方式，默认 '
                                                                                                   'rename'},
                                                                    'path': {'type': 'string',
                                                                             'description': '源路径'},
                                                                    'target': {'type': 'string',
                                                                               'description': '目标路径或目标目录'}},
                                                     'required': ['path', 'action', 'target'],
                                                     'additionalProperties': False}}},
                'required': ['targets'],
                'additionalProperties': False}}


def move_target(action: str, path: str, target: str, dry_run: bool = False, on_conflict: str = "rename", base_dir=None) -> str:
    """校验并执行一次复制、移动或重命名。"""
    action = str(action).strip().lower()
    if action not in ACTIONS:
        raise RuntimeError(f"action 只支持 {ACTIONS}")
    if on_conflict not in CONFLICTS:
        raise RuntimeError(f"on_conflict 只支持 {CONFLICTS}")
    if not all(isinstance(value, str) and value.strip() and not any(char in value for char in WILDCARDS) for value in (path, target)):
        raise RuntimeError("path 与 target 必须是具体路径，不支持通配符")
    mark, message = apply(action, path.strip(), target.strip(), dry_run, on_conflict, base_dir)
    from backend.utils.tool_output import ToolOutput
    return ToolOutput(f"{mark} {message}",has_errors=mark=='✗')


def resolve(path: str, base_dir) -> Path:
    """相对路径以 base_dir（项目工作目录）为基准；绝对路径必须落在白名单内。"""
    from backend.utils.paths import resolve_path

    return resolve_path(path, base_dir, write=True)




def apply(action: str, path: str, target: str, dry_run: bool, on_conflict: str, base_dir) -> Tuple[str, str]:
    """执行单项，返回 (状态标记, 说明)：``✓`` 成功 / ``-`` 跳过 / ``✗`` 失败。"""
    source = resolve(path, base_dir)
    if not source.exists():
        return "✗", f"{source} 源不存在"

    destination = resolve(target, base_dir)
    if action == "rename":
        if destination.is_dir() or target.endswith(("/", "\\")):
            destination = destination / source.name
        if destination.parent != source.parent:
            return "✗", f"重命名只换名字不换目录：{source} → {destination}（跨目录请用 move）"
    elif destination.is_dir():
        destination = destination / source.name

    if destination == source:
        return "-", f"{source} 源与目标相同，跳过"
    if on_conflict == "rename":
        # rename 策略全程串行：挑空闲名与写入之间只要有一丝空隙，两个线程就会挑到同一个名字，
        # 后写的把前写的覆盖掉（实测 8 个同名副本只落地 3~5 个）。
        # 锁的代价远小于一次模型往返，正确性优先。
        with _NAME_LOCK:
            if destination.exists():
                destination = next_free(destination)
            return transfer(action, source, destination, dry_run)

    if destination.exists():
        if on_conflict == "skip":
            return "-", f"{source} → {destination} 已存在，跳过"
        if destination.is_dir():
            return "✗", f"{destination} 是已存在的目录：不覆盖目录，请换目标或先清空"

    return transfer(action, source, destination, dry_run, overwrite=on_conflict == "overwrite")


def transfer(
    action: str,
    source: Path,
    destination: Path,
    dry_run: bool,
    *,
    overwrite: bool = False,
) -> Tuple[str, str]:
    """按 action 把 source 搬到 destination，返回 (状态标记, 说明)。"""
    if dry_run:
        return "✓", f"{source} → {destination}（将{VERBS[action]}）"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if overwrite and destination.exists():
        if source.is_dir():
            return "✗", f"不能用目录覆盖文件：{source} → {destination}"
        handle, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".replace-")
        os.close(handle)
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, destination)
            if action in ("move", "rename"):
                source.unlink()
        finally:
            Path(temporary).unlink(missing_ok=True)
    elif action in ("move", "rename"):
        shutil.move(str(source), str(destination))
    elif source.is_dir():
        shutil.copytree(source, destination)
    else:
        shutil.copy2(source, destination)
    return "✓", f"{source} → {destination}"


#: 并发下"挑空闲名 + 实际搬动"必须整体串行；用 RLock 因为 apply 会套住 next_free
_NAME_LOCK = threading.RLock()


def next_free(target: Path) -> Path:
    """目标已存在时依次尝试 ``name-1.ext``、``name-2.ext``…

    移动的目标重名只能另存（不能覆盖用户的既有文件）；这与 ``write`` 的"整体写入直接覆盖"
    是两回事——后者覆盖前会把旧版备份到会话目录。

    整个查找加锁：批量移动是并发的，两个线程同时挑"下一个空闲名"会挑到同一个。
    """
    with _NAME_LOCK:
        if not target.exists():
            return target
        if target.is_dir():
            for index in range(1, 1000):
                candidate = target.with_name(f"{target.name}-{index}")
                if not candidate.exists():
                    return candidate
            raise RuntimeError(f"同名目录过多，请换一个目标名：{target.name}")
        stem, suffix = target.stem, target.suffix
        for index in range(1, 1000):
            candidate = target.with_name(f"{stem}-{index}{suffix}")
            if not candidate.exists():
                return candidate
        raise RuntimeError(f"同名文件过多，请换一个目标名：{target.name}")


def move(targets: list[dict], base_dir=None, roots=None) -> str:
    """统一批处理入口，重叠路径自动串行。"""
    from backend.utils import paths

    paths.bind(roots or paths.Roots(base_dir))

    return run_targets(lambda target: move_target(**target, base_dir=base_dir), targets, workers=8, base_dir=base_dir)
