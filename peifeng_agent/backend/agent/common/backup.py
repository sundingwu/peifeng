"""覆盖/删除前的原文件备份：给"改稿"留一条回溯路。

与 :mod:`file_stage` 的分工（两者互补，不要合并）：

- **副本**（``file_stage`` 的 ``stage/``）：防"被打断留下半成品"，工具返回即删
- **备份**（本模块）：防"提交之后想反悔"，落在会话目录里长期保留

放置位置：``<会话目录>/backup/<时间戳>-<序号>-<工具>/``。批次内按**工作区相对路径**还原原文件，
另附一份 ``manifest.json``（时间、工具、每个文件的相对路径/动作/大小）——用户按名字就能找回。

**不允许无备份的覆盖**：备份失败时直接抛错，调用方据此中止提交
（见 :func:`peifeng_agent.backend.agent.common.file_stage.run_staged`）。
被打断或提交失败时由调用方 :func:`discard` 整批丢弃——那时原文件还没动，备份没有意义。
"""
from __future__ import annotations

import itertools
import json
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

#: 保留的备份批次数：更旧的按目录名顺序（即时间顺序）整批删除
MAX_BACKUPS = 100
#: 会话目录下的备份子目录名
BACKUP_DIR = "backup"
#: 批次内的清单文件名（只是索引，坏了不影响文件本身）
MANIFEST_FILE = "manifest.json"

#: 批次名里的序号：同一秒内多次备份也不会撞名（备份只发生在主进程）
_LOCK = threading.Lock()
_SEQ = itertools.count()


@dataclass
class Backup:
    """一批已落盘的备份：``batch`` 是批次目录，``files`` 是工作区相对路径。"""

    batch: Path
    files: list[str] = field(default_factory=list)

    def note(self) -> str:
        """给工具回执用的一句话：告诉模型/用户回溯去哪找（相对 <会话目录> 表达，短且够用）。"""
        if not self.files:
            return ""
        return (
            f"（{len(self.files)} 个原文件已备份到 <会话目录>/{BACKUP_DIR}/{self.batch.name}，"
            "需要回溯时从这里取回）"
        )


def make_backup(
    workspace,
    session_dir,
    tool: str,
    entries: Iterable[tuple[str, str]],
) -> Optional[Backup]:
    """把**即将被覆盖或删除**的原文件复制到会话目录，返回该批次；无内容可备份时返回 None。

    ``entries`` 是 ``(工作区相对路径, 动作)``，动作为 ``overwrite``（内容将被改写）或
    ``delete``（文件将被删除）。只备份工作区里**当前确实存在**的文件：新建文件没有"原版"可存。

    复制失败直接抛错、并清掉这批半成品——调用方据此中止提交，绝不允许"没有备份的覆盖"。
    """
    if not session_dir or not str(session_dir).strip() or workspace is None:
        return None
    source_root = Path(workspace)
    guarded = guarded_roots(session_dir, workspace)
    pairs = [
        (name, action)
        for name, action in entries
        if (source_root / name).is_file() and not inside(source_root / name, guarded)
    ]
    if not pairs:
        return None

    batches = Path(session_dir) / BACKUP_DIR
    batches.mkdir(parents=True, exist_ok=True)
    batch = new_batch(batches, tool)
    files: list[str] = []
    records: list[dict] = []
    try:
        for name, action in pairs:
            source = source_root / name
            target = batch / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)  # copy2 保留时间戳：回溯后仍能看出原文件的年龄
            files.append(name)
            records.append({"path": name, "action": action, "size": source.stat().st_size})
        write_manifest(batch, tool, source_root, records)
    except BaseException:
        shutil.rmtree(batch, ignore_errors=True)
        raise
    prune(batches)
    return Backup(batch=batch, files=files)


def discard(backup: Optional[Backup]) -> None:
    """丢弃一批备份（提交失败或被打断时调用）：原文件没被改动，这份备份没有意义。"""
    if backup is not None:
        shutil.rmtree(backup.batch, ignore_errors=True)


def guarded_roots(session_dir, workspace=None) -> list[Path]:
    """不该被备份的目录：会话目录本身，以及它所在的项目会话区。

    工作区默认就是"桌面"，而会话数据（``桌面/program/.../sessions/桌面/<会话>``）也在
    桌面之内。不排除的话，删一个会话内的临时目录就会把里面的图片一起复制进备份——
    备份复制备份、会话数据复制会话数据，越滚越深（实测一个会话的 backup 里躺了 13 MB）。

    两条护栏：只在**确实位于工作区之内**时才排除；且排除项不能是工作区本身或它的祖先
    （否则会把用户产物一并挡掉）。
    """
    folder = Path(session_dir).resolve()
    root = Path(workspace).resolve() if workspace else None
    roots: list[Path] = []
    for candidate in (folder, folder.parent):
        if root is not None:
            try:
                root.relative_to(candidate)  # 候选是工作区或其祖先 → 会误伤
                continue
            except ValueError:
                pass
            try:
                candidate.relative_to(root)  # 不在工作区内 → 备份本来也不会碰它
            except ValueError:
                continue
        if candidate != folder and candidate in roots:
            continue
        roots.append(candidate)
    return roots


def inside(path, roots: list[Path]) -> bool:
    """路径是否落在这些目录之下（框架自己的数据，不算用户产物）。"""
    try:
        target = Path(path).resolve()
    except OSError:
        return False
    for root in roots:
        try:
            target.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def new_batch(batches: Path, tool: str) -> Path:
    """创建批次目录：``<YYYYmmdd-HHMMSS>-<序号>-<工具>``，按名字排序即按时间排序。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    label = "".join(char for char in str(tool or "") if char.isalnum() or char in "_-") or "tool"
    for _ in range(1000):
        with _LOCK:
            sequence = next(_SEQ) % 1000
        candidate = batches / f"{stamp}-{sequence:03d}-{label}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError(f"无法在 {batches} 下创建备份批次（同名目录过多）")


def write_manifest(batch: Path, tool: str, workspace: Path, records: list[dict]) -> None:
    """写批次清单：用户翻备份时不用猜这批是哪个工具、改了哪些文件。"""
    payload = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tool": tool,
        "workspace": str(workspace),
        "files": records,
    }
    (batch / MANIFEST_FILE).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def prune(batches: Path, limit: int = MAX_BACKUPS) -> None:
    """只保留最近 ``limit`` 个批次：超出部分整批删除（备份目录不能无限长）。"""
    if limit <= 0:
        return
    try:
        items = sorted((path for path in batches.iterdir() if path.is_dir()), key=lambda path: path.name)
    except OSError:
        return
    for stale in items[:-limit]:
        shutil.rmtree(stale, ignore_errors=True)
