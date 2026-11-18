"""文件写事务：影子副本、冲突检测、备份与子进程执行。

一个文件四块：
- 备份：覆盖/删除前把原文件另存到会话目录（可回溯）
- 文件锁：同一文件跨进程串行化，避免两个会话互相覆盖
- 事务：影子副本 → 提交前校验（size + SHA1）→ 替换；并发改动时用最新内容重做
- 子进程：把工具放到独立进程执行，超时/取消可强杀，回执走 IPC
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
import hashlib
import os
import tempfile
from contextlib import ExitStack, contextmanager
from typing import Iterable, Iterator
import filecmp
import inspect
import multiprocessing
import traceback


# ---------------------------------------------------------------- backup
#: 保留的备份批次数：更旧的按目录名顺序（即时间顺序）整批删除
MAX_BACKUPS = 100
#: 备份总大小上限：与批次数量任一超限，就从最旧的批次删起（默认 500 MB）
MAX_BACKUP_BYTES = 500 * 1024 * 1024
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
    target = Path(path).resolve()
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


def prune(batches: Path, limit: int = MAX_BACKUPS, max_bytes: int = MAX_BACKUP_BYTES) -> None:
    """只保留最近的若干批次：数量或总大小任一超限，就从最旧的批次删起。"""
    items = sorted((path for path in batches.iterdir() if path.is_dir()), key=lambda path: path.name, reverse=True)
    total = 0
    for index, item in enumerate(items):
        total += sum(file.stat().st_size for file in item.rglob("*") if file.is_file())
        if index + 1 > limit or total > max_bytes:
            for stale in items[index:]:
                shutil.rmtree(stale, ignore_errors=True)
            return


# ---------------------------------------------------------------- file_lock
#: 等待锁的上限（秒）：提交只有毫秒级，等这么久说明有人卡住了，报错比死等好
LOCK_TIMEOUT = 30.0
#: 重试间隔（秒）
LOCK_RETRY = 0.02


def lock_dir() -> Path:
    """锁文件目录：系统临时目录下，与工作区、会话都无关。"""
    return Path(tempfile.gettempdir()) / "peifeng-file-locks"


def lock_path(target) -> Path:
    """一个目标文件对应的锁文件路径。"""
    key = hashlib.sha1(str(Path(target).resolve()).lower().encode("utf-8")).hexdigest()[:24]
    return lock_dir() / f"{key}.lock"


@contextmanager
def file_lock(target) -> Iterator[None]:
    """独占一个目标文件；同一路径被别的线程/进程占着时等待，超时抛错。"""
    folder = lock_dir()
    folder.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path(target), "a+b")
    try:
        acquire(handle, target)
        yield
    finally:
        try:
            release(handle)
        finally:
            handle.close()


@contextmanager
def lock_files(targets: Iterable) -> Iterator[None]:
    """一次锁住一组文件：按路径排序逐个加锁，避免两个事务交叉等待。"""
    ordered = sorted({Path(item).resolve() for item in targets}, key=str)
    with ExitStack() as stack:
        for target in ordered:
            stack.enter_context(file_lock(target))
        yield


def acquire(handle, target) -> None:
    """阻塞式加锁（带超时）。"""
    if os.name == "nt":
        import msvcrt

        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"等待文件锁超时（{LOCK_TIMEOUT:.0f}s）：{target}（另一个任务正在写它）"
                    ) from None
                time.sleep(LOCK_RETRY)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def release(handle) -> None:
    """解锁；进程异常退出时操作系统也会自动释放。"""
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


# ---------------------------------------------------------------- file_stage
#: 提交时发现原文件被别人改过，就重做一遍；重做这么多轮仍冲突才判定失败
MAX_COMMIT_ATTEMPTS = 3

#: 需要事务保护的工具：它们会改工作区里的文件（run_code 不在内——它想改什么无法预知）
PROTECTED_TOOLS = frozenset({"write", "move", "delete"})


def file_version(path):
    info = path.stat()
    return (info.st_size, info.st_mtime_ns, info.st_mode)


def file_fingerprint(path):
    """内容指纹：大小 + SHA1。

    提交前的"有没有被别人改过"要按**内容**判定，不能按 mtime：桌面文件常被同步盘、
    杀毒、Word 之类的东西 touch 一下（内容没变），用 mtime 比会把正常提交误判成冲突
    （实测报过"原文件正在被另一个任务反复修改"，其实没有别的任务）。
    """
    info = path.stat()
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return (info.st_size, digest.hexdigest())


def file_versions(root):
    return {
        str(path.relative_to(root)): file_version(path)
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def reject_links(path: Path, root: Path) -> None:
    """Check lexical paths before resolve can hide a link or directory junction."""
    current = Path(os.path.abspath(path))
    boundary = Path(os.path.abspath(root))
    current.relative_to(boundary)
    while True:
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise RuntimeError(f"修改路径不能包含符号链接或目录联接：{current}")
        if current == boundary:
            return
        current = current.parent


def run_staged_transaction(function, payload, stopped, vision=None, *, execute, session_dir=None):
    """Stage supported file mutations; commit only after successful completion.

    This is a workspace transaction boundary, not an OS sandbox for arbitrary code.

    ``session_dir`` 给定时，被覆盖/删除前的原文件会另存一份到会话目录（可回溯）；
    没给（未绑定会话）就只保留事务语义。备份失败一律中止提交。
    """
    tool_name = function.__name__
    workspace = payload.get("base_dir")
    if tool_name not in PROTECTED_TOOLS or workspace is None:
        return execute(function, payload, stopped, vision)
    workspace = Path(os.path.abspath(workspace))
    reject_links(workspace, workspace)
    workspace = workspace.resolve()

    def check_cancel():
        if stopped and stopped():
            raise RuntimeError("工具调用已取消；未提交的副本已丢弃")

    check_cancel()
    for attempt in range(MAX_COMMIT_ATTEMPTS):
        with tempfile.TemporaryDirectory(prefix="peifeng-tool-") as folder:
            folder_path = Path(folder)
            stage = folder_path / "workspace"
            stage.mkdir()
            originals: dict = {}
            evidence_inputs: set[str] = set()
            path_keys = {"path", "split_path", "file", "target", "output", "template", "template_file", "facts", "svg_output"}

            def copy_file(source, target):
                check_cancel()
                source = Path(source)
                if source.is_symlink() or (hasattr(source, "is_junction") and source.is_junction()):
                    raise RuntimeError(f"副本不支持链接：{source}")
                relative = str(source.relative_to(workspace))
                originals[relative] = file_fingerprint(source)
                return shutil.copy2(source, target)

            def stage_path(value, key):
                check_cancel()
                candidate = Path(value)
                lexical = candidate if candidate.is_absolute() else workspace / candidate
                if key in {"template", "template_file"} and not lexical.resolve().is_relative_to(workspace):
                    # 模板只作为输入快照，存放在提交范围之外，不能覆盖原模板。
                    reject_links(lexical, Path(lexical.anchor))
                    if not lexical.is_file():
                        raise RuntimeError(f"模板文件不存在：{value}")
                    identity = hashlib.sha256(str(lexical.resolve()).encode()).hexdigest()[:12]
                    snapshot = folder_path / "inputs" / identity / lexical.name
                    snapshot.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(lexical, snapshot)
                    return str(snapshot)
                reject_links(lexical, workspace)
                original = lexical.resolve()
                try:
                    relative = original.relative_to(workspace)
                except ValueError:
                    raise RuntimeError(f"路径必须位于项目工作区内：{value}") from None
                target = stage / relative
                if original.is_dir():
                    if key in {"target", "output"}:
                        target.mkdir(parents=True, exist_ok=True)
                    else:

                        def check_entries(folder, names):
                            check_cancel()
                            for entry in names:
                                reject_links(Path(folder) / entry, workspace)
                            return []

                        shutil.copytree(
                            original, target, copy_function=copy_file, ignore=check_entries, dirs_exist_ok=True
                        )
                elif original.is_file() and not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    copy_file(original, target)
                if key == 'facts':
                    if not original.is_file():
                        raise RuntimeError(f'事实文件不存在：{value}')
                    try:
                        facts = json.loads(original.read_text(encoding='utf-8-sig'))
                    except (ValueError, OSError) as error:
                        raise RuntimeError(f'无法读取事实文件：{value}') from error
                    sources = facts.get('sources') if isinstance(facts,dict) else None
                    if not isinstance(sources,list) or not sources:
                        raise RuntimeError('事实文件缺少来源记录')
                    evidence_inputs.add(str(relative))
                    for row in sources:
                        if not isinstance(row,dict) or not isinstance(row.get('path'),str):
                            raise RuntimeError('事实来源记录无效')
                        dependency = original.parent / row['path']
                        reject_links(dependency,workspace)
                        dependency = dependency.resolve()
                        if not dependency.is_file():
                            raise RuntimeError(f'事实来源不存在：{row["path"]}')
                        stage_path(str(dependency),'path')
                        evidence_inputs.add(str(dependency.relative_to(workspace)))
                return str(target) if candidate.is_absolute() else str(relative)

            def translate(value):
                if isinstance(value, dict):
                    result = {}
                    for key, item in value.items():
                        if key in path_keys and isinstance(item, (str, Path)) and str(item):
                            result[key] = stage_path(item, key)
                        elif isinstance(item, (dict, list)):
                            result[key] = translate(item)
                        else:
                            result[key] = item
                    return result
                if isinstance(value, list):
                    return [translate(item) for item in value]
                return value

            staged_payload = translate(payload)
            staged_payload["base_dir"] = stage

            def include_conflicts(destination):
                reject_links(destination, workspace)
                destination = destination.resolve()
                relative = destination.relative_to(workspace)
                if not destination.exists():
                    return
                if destination.is_dir():
                    (stage / relative).mkdir(parents=True, exist_ok=True)
                else:
                    target = stage / relative
                    if not target.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        copy_file(destination, target)
                # Mirror only occupied candidates that the tool's rename policy probes.
                for index in range(1, 1000):
                    check_cancel()
                    candidate = destination.with_name(
                        f"{destination.name}-{index}"
                        if destination.is_dir()
                        else f"{destination.stem}-{index}{destination.suffix}"
                    )
                    reject_links(candidate, workspace)
                    if not candidate.exists():
                        break
                    target = stage / candidate.relative_to(workspace)
                    if candidate.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                    elif not target.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        copy_file(candidate, target)

            if tool_name == "move":
                for item in payload.get("targets", payload.get("items", [])):
                    source = Path(item["path"])
                    source = source if source.is_absolute() else workspace / source
                    destination = Path(item["target"])
                    destination = destination if destination.is_absolute() else workspace / destination
                    if destination.is_dir():
                        destination = destination / source.name
                    include_conflicts(destination)
            elif tool_name == "write":
                for item in payload.get("targets", [payload]):
                    if item.get("path"):
                        destination = Path(item["path"])
                        include_conflicts(destination if destination.is_absolute() else workspace / destination)
            before = file_versions(stage)
            original_directories = [path.relative_to(stage) for path in stage.rglob("*") if path.is_dir()]
            check_cancel()
            result = execute(function, staged_payload, stopped, vision)
            if stopped and stopped():
                raise RuntimeError("工具调用已取消；未提交的副本已丢弃")
            after = file_versions(stage)
            filecmp.clear_cache()
            changed = {
                name
                for name in before.keys() | after.keys()
                if before.get(name) != after.get(name)
                or (
                    name in before
                    and name in after
                    and (workspace / name).is_file()
                    and not filecmp.cmp(stage / name, workspace / name, shallow=False)
                )
            }
            # 提交是临界区：按文件的跨进程锁覆盖 CAS 校验 + 备份 + 替换。
            # 锁只覆盖提交（毫秒级），工具执行在锁外——写不同文件仍然并行。
            with lock_files(workspace / name for name in changed | evidence_inputs):
                stale = [
                    name
                    for name in changed | evidence_inputs
                    if (file_fingerprint(workspace / name) if (workspace / name).is_file() else None)
                    != originals.get(name)
                ]
                if stale:
                    if attempt + 1 < MAX_COMMIT_ATTEMPTS:
                        # 别人刚改过这个文件：作废本轮影子，用最新内容重做一遍
                        continue
                    raise RuntimeError(
                        f"原文件正在被另一个任务反复修改，重做 {MAX_COMMIT_ATTEMPTS} 次仍未提交成功："
                        f"{stale[0]}"
                    )
                # 回溯备份：即将被覆盖/删除的原文件先落一份到会话目录。备份失败会在这里抛错，
                # 于是本次提交根本不发生——不允许出现"没有备份的覆盖"。
                history = make_backup(
                    workspace,
                    session_dir,
                    tool_name,
                    [(item, "delete" if item not in after else "overwrite") for item in sorted(changed)],
                )
                # 回滚副本：只服务"提交本身失败"时的原地还原（与 history 分工不同，勿合并）
                rollback = folder_path / "rollback"
                rollback.mkdir()
                for name in changed:
                    original = workspace / name
                    if original.is_file():
                        saved = rollback / name
                        saved.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(original, saved)
                try:
                    # Commit is synchronous and not interrupted halfway by cancellation.
                    for name in sorted(changed):
                        target = workspace / name
                        if name not in after:
                            target.unlink()
                        else:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=".commit-")
                            os.close(handle)
                            try:
                                shutil.copy2(stage / name, temporary)
                                os.replace(temporary, target)
                            finally:
                                Path(temporary).unlink(missing_ok=True)
                    # Preserve empty-directory moves/deletions as well.
                    for relative in sorted(
                        original_directories, key=lambda path: len(path.parts), reverse=True
                    ):
                        original = workspace / relative
                        if original.is_dir() and not (stage / relative).exists():
                            if not any(original.iterdir()):
                                original.rmdir()
                    for directory in stage.rglob("*"):
                        if directory.is_dir():
                            (workspace / directory.relative_to(stage)).mkdir(parents=True, exist_ok=True)
                except OSError:
                    # Restore originals on ordinary commit failures. Hard process death is
                    # outside this cross-file transaction guarantee.
                    for name in changed:
                        target = workspace / name
                        saved = rollback / name
                        if saved.is_file():
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(saved, target)
                        elif target.is_file():
                            target.unlink()
                    for directory in original_directories:
                        (workspace / directory).mkdir(parents=True, exist_ok=True)
                    discard(history)  # 文件已还原：这份备份没有意义，别留在会话目录里
                    raise
                except BaseException:
                    # 取消、Ctrl+C、进程内异常：原文件没动，备份一并丢掉
                    discard(history)
                    raise
            note = history.note() if history is not None else ""
            if isinstance(result, str):
                body = result.replace(json.dumps(str(stage))[1:-1], json.dumps(str(workspace))[1:-1])
                body = body.replace(str(stage), str(workspace))
                from backend.utils.tool_output import ToolOutput
                return ToolOutput(f"{body}\n{note}" if note else body,
                                  has_errors=getattr(result, 'has_errors', False))
            return result
    raise RuntimeError("文件提交未能完成：请稍后重试")


# ---------------------------------------------------------------- process_runner
def child_main(connection, source, payload):
    from backend.orchestration.orchestrate import parse_tool
    vision_lock = threading.Lock()

    def vision(*args, **kwargs):
        # 多张图片的线程共享同一条 Pipe，完整请求/响应必须作为一个临界区。
        with vision_lock:
            connection.send(("vision", (args, kwargs)))
            kind, value = connection.recv()
        if kind == "error":
            raise RuntimeError(value)
        return value

    try:
        tool = parse_tool(Path(source))
        if "vision" in inspect.signature(tool["func"]).parameters:
            payload["vision"] = vision
        result = tool["func"](**payload)
        connection.send(("result", result))
    except (RuntimeError, ValueError, TypeError, KeyError) as error:
        # 工具的约定是"失败抛 RuntimeError"，消息是写给模型看的一句话，原样带回
        connection.send(("message", str(error)))
    except BaseException:  # noqa: BLE001 - transmit complete child failures
        connection.send(("error", traceback.format_exc()))
    finally:
        connection.close()


def kill_process(process):
    if os.name == "nt":
        import psutil
        try:
            parent = psutil.Process(process.pid)
            children = parent.children(recursive=True)
            for child in children:
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            parent.kill()
        except psutil.NoSuchProcess:
            pass
    elif process.is_alive():
        process.terminate()
    process.join(timeout=5)
    if process.is_alive():
        process.kill()
        process.join(timeout=2)
        if process.is_alive():
            raise RuntimeError("工具进程无法终止，不能确认清理完成")


PROCESS_EXIT_GRACE = 2.0


def wait_for_exit(process, stopped):
    deadline = time.monotonic() + PROCESS_EXIT_GRACE
    while process.is_alive():
        if stopped and stopped():
            kill_process(process)
            raise RuntimeError("工具调用已取消；退出等待已终止")
        if time.monotonic() >= deadline:
            kill_process(process)
            raise RuntimeError("工具返回结果后未正常退出，已终止进程树；副本不提交")
        process.join(timeout=0.05)
    if process.exitcode != 0:
        raise RuntimeError(f"工具退出异常：{process.exitcode}")


def run_process(function, payload, stopped, vision=None):
    if stopped and stopped():
        raise RuntimeError("工具调用已取消；未启动进程")
    source = inspect.getsourcefile(function)
    if not source:
        raise RuntimeError("工具必须来自可加载的 Python 文件")
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=child_main, args=(child, source, payload))
    process.start()
    child.close()
    try:
        while True:
            if stopped and stopped():
                kill_process(process)
                raise RuntimeError("工具调用已取消；未提交的副本已丢弃")
            if parent.poll(0.05):
                kind, value = parent.recv()
                if kind == "vision":
                    try:
                        if vision is None:
                            raise RuntimeError("未配置看图能力")
                        reply = {}

                        def describe():
                            try:
                                reply["value"] = vision(*value[0], **value[1])
                            except Exception:  # noqa: BLE001 - relay callback errors
                                reply["error"] = traceback.format_exc()

                        callback = threading.Thread(target=describe, daemon=True)
                        callback.start()
                        while callback.is_alive():
                            callback.join(0.05)
                            if stopped and stopped():
                                raise RuntimeError("工具调用已取消")
                        parent.send(("error", reply["error"]) if "error" in reply else ("result", reply["value"]))
                    except Exception:  # noqa: BLE001 - transmit callback failure
                        parent.send(("error", traceback.format_exc()))
                elif kind == "error":
                    raise RuntimeError(value)
                elif kind == "message":
                    # 子进程里的业务错误（RuntimeError）：消息本身就是给模型看的一句话
                    wait_for_exit(process, stopped)
                    raise RuntimeError(value)
                else:
                    wait_for_exit(process, stopped)
                    return value
            elif not process.is_alive():
                raise RuntimeError(f"工具进程异常退出：{process.exitcode}")
    finally:
        if process.is_alive():
            kill_process(process)
        parent.close()


def run_staged(function, payload, stopped, vision=None, *, session_dir=None):
    """Run a tool with scoped file staging when required.

    ``session_dir`` 透传给事务层：被覆盖/删除前的原文件按会话目录备份（可回溯）。
    """
    return run_staged_transaction(
        function, payload, stopped, vision, execute=run_process, session_dir=session_dir
    )
