"""Run file-backed tools in disposable child processes with cancellation."""

from __future__ import annotations

import inspect
import multiprocessing
import os
import subprocess
import threading
import time
import traceback
from pathlib import Path

from peifeng_agent.backend.agent.common.file_stage import run_staged as run_file_operation


def child_main(connection, source, payload):
    from peifeng_agent.backend.utils.loader import parse_tool

    def vision(*args, **kwargs):
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
    except RuntimeError as error:
        # 工具的约定是"失败抛 RuntimeError"，消息是写给模型看的一句话，原样带回
        connection.send(("message", str(error)))
    except BaseException:  # noqa: BLE001 - transmit complete child failures
        connection.send(("error", traceback.format_exc()))
    finally:
        connection.close()


def kill_process(process):
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
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
    return run_file_operation(
        function, payload, stopped, vision, execute=run_process, session_dir=session_dir
    )
