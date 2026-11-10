"""批量任务的并发执行：一次调用处理多个目标时统一走这里。

为什么需要：``describe_many``（看图）每张要过一次模型、``bash`` 的批量命令可能各跑几十秒，
串行就是简单相加。并发只在这类"互相独立、结果按输入顺序拼"的场合用——
文件写同一路径这类有副作用的操作仍由各自的实现保证顺序或去重。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Sequence, TypeVar

T = TypeVar("T")
R = TypeVar("R")

#: 默认并发度：本地操作（读文件、删、移）用 8，够快也不会把磁盘打满
DEFAULT_WORKERS = 8


def run_parallel(
    func: Callable[[T], R],
    items: Sequence[T],
    *,
    workers: int = DEFAULT_WORKERS,
    thread_prefix: str = "batch",
) -> list[R]:
    """对 ``items`` 逐项调 ``func``，并发执行，结果**按输入顺序**返回。

    单项抛异常不会被吞：异常会原样冒给调用方——各工具需要"单项目失败记在它自己那段"，
    自己在 ``func`` 里 catch 即可，这样错误信息的措辞由工具决定。
    """
    if not items:
        return []
    if len(items) == 1:
        return [func(items[0])]
    with ThreadPoolExecutor(
        max_workers=min(workers, len(items)),
        thread_name_prefix=thread_prefix,
    ) as pool:
        return list(pool.map(func, items))


def run_parallel_safe(
    func: Callable[[T], R],
    items: Sequence[T],
    *,
    workers: int = DEFAULT_WORKERS,
    thread_prefix: str = "batch",
) -> list[tuple[R | None, Exception | None]]:
    """同 :func:`run_parallel`，但把异常收进结果里（不中断其余项）。

    返回 ``[(结果, None) 或 (None, 异常)]``，与 ``items`` 一一对应。
    """

    def guarded(item: T) -> tuple[R | None, Exception | None]:
        try:
            return func(item), None
        except Exception as error:  # noqa: BLE001 - 单项失败要能记在它自己那段
            return None, error

    return run_parallel(guarded, items, workers=workers, thread_prefix=thread_prefix)
