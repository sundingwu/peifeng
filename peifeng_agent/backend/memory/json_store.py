"""JSON 读写：会话记忆文件共用。

写入一律先落同目录临时文件再 ``os.replace``：进程中途退出也不会留下半截文件。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def write_json(path, payload: Any) -> Path:
    """写入 JSON（UTF-8、缩进 2）；父目录自动创建。

    先写临时文件再原子替换：中途失败不会破坏原有文件。

    替换在 Windows 上可能被短暂拒绝（目标文件正被另一个句柄打开，例如测试清理或杀毒扫描），
    因此做几次退避重试；仍失败才抛出。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        replace_with_retry(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return target


def replace_with_retry(source: str, target: Path, attempts: int = 5) -> None:
    """``os.replace`` 的退避重试（Windows 上目标文件可能被短暂占用）。"""
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def read_json(path, default: Any = None) -> Any:
    """读取 JSON；文件缺失或损坏时返回 default。"""
    target = Path(path)
    if not target.exists():
        return default
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 损坏的历史记录按缺失处理
        logger.debug("跳过无法解析的 JSON：%s", target, exc_info=True)
        return default
