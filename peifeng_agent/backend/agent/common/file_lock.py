"""按目标文件加的跨进程排他锁：同一路径在同一时刻只能有一个提交。

上一版的 ``COMMIT_LOCK`` 是进程内的全局锁——安全，但把"写不同文件"也串起来了。
这里换成**按文件路径**的锁，并且是**跨进程**的（工具跑在 spawn 子进程里，未来也可能
同时开多个服务实例指向同一个工作区）：

- 锁文件放在系统临时目录（不在用户工作区里落任何东西），文件名是目标绝对路径的哈希
- Windows 用 ``msvcrt.locking``，其它平台用 ``fcntl.flock``；拿不到就短间隔重试
- 多文件时**按路径排序后依次加锁**，保证任意两个事务的加锁顺序一致，不会互相等待成环

锁只保护"提交"这一段（CAS 校验 + 备份 + 替换，毫秒级）；工具真正干活的阶段在锁外，
所以并行度不受影响。
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterable, Iterator

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
