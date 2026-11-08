"""会话目录存储：``sessions/<项目>/<会话名>/`` 下的两个文件。

- ``summary.json``（``SummaryStore``）：会话元信息 + 各任务摘要（跨任务可见）；会话的列出 /
  重命名 / 归档也在这里
- ``detail.json``（``DetailStore``）：按**任务号**索引的完整上下文（``{"version": 1, "tasks":
  {"1": {...}}}``）；模型需要细节时用 ``read`` 的 ``json_path`` 取某任务那一段，不做全量读取

归档的会话移到 ``sessions/<项目>/archive/<会话名>/``：数据保留，但不再进入会话列表。
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any, List, Optional

from pydantic import BaseModel, Field

from peifeng_agent.backend.memory.json_store import read_json, write_json
from peifeng_agent.backend.utils.contracts import TaskDetail, TaskSummary

logger = logging.getLogger(__name__)

#: 会话目录内的固定文件名
SUMMARY_FILE = "summary.json"
DETAIL_FILE = "detail.json"

#: 会话内放中间产物的目录名
INTERNAL_DIR = "internal"

#: 归档会话的子目录名（在会话根下，不进会话列表）
ARCHIVE_DIR = "archive"

#: 没给名字时用的会话名
UNNAMED_SESSION = "未命名会话"

#: 明细文件的布局版本（结构变化时据此迁移）
DETAIL_VERSION = 1

#: 明细文件是"读-改-写"：同一会话并发保存时用一个锁串行化（任务本身是串行的，这里只是兜底）
_DETAIL_LOCK = threading.Lock()

#: 会话名里不允许出现的字符：会话名就是一层目录名
_PATH_CHARS = ("/", "\\")


def safe_name(name: str) -> str:
    """把会话名规整成"一层目录名"；为空或非法（含路径分隔符、``.`` / ``..``）时返回空串。

    会话名直接当目录名用：不校验的话 ``../../x`` 就能读写会话根之外的位置。
    """
    target = (name or "").strip()
    if target.endswith(SUMMARY_FILE):
        target = target[: -len(SUMMARY_FILE)].strip()
    if not target or target in {".", ".."} or any(char in target for char in _PATH_CHARS):
        return ""
    return target


class SessionInfo(BaseModel):
    """会话元信息。"""

    created_at: int = 0
    session_id: str
    title: str
    file_name: str = ""


class SessionData(BaseModel):
    """`summary.json` 的结构：meta + 任务摘要列表。"""

    meta: SessionInfo
    tasks: List[TaskSummary] = Field(default_factory=list)


class SummaryStore:
    """一个项目的会话目录：读写 ``summary.json``（列表 / 重命名 / 归档也在这里）。"""

    def __init__(self, session_dir) -> None:
        self.session_root = Path(session_dir)
        self.session_root.mkdir(parents=True, exist_ok=True)

    def folder(self, name: str) -> Path:
        """会话目录（按会话名，不带扩展名）。"""
        target = (name or "").strip()
        if target.endswith(SUMMARY_FILE):
            target = target[: -len(SUMMARY_FILE)]
        return self.session_root / target

    def summary_path(self, name: str) -> Path:
        """摘要文件路径。"""
        return self.folder(name) / SUMMARY_FILE

    def archive_dir(self) -> Path:
        """归档会话的目录。"""
        return self.session_root / ARCHIVE_DIR

    def resolve(self, name: str) -> Optional[Path]:
        """定位已存在的会话摘要文件；不存在返回 None。"""
        path = self.summary_path(name)
        return path if path.exists() else None

    def list(self) -> List[SessionInfo]:
        return [meta for meta, _ in self.list_overview()]

    def list_overview(self) -> list[tuple[SessionInfo, int]]:
        """Read sidebar metadata once per file version, without validating task bodies."""
        entries = []
        for folder in sorted(self.session_root.iterdir(), reverse=True):
            if not folder.is_dir() or folder.name == ARCHIVE_DIR:
                continue
            path = folder / SUMMARY_FILE
            try:
                info = path.stat()
                entry = session_overview(str(path), info.st_mtime_ns, info.st_size)
                if entry is not None:
                    meta, count = entry
                    entries.append((meta.model_copy(update={"created_at": meta.created_at or folder.stat().st_ctime_ns}), count))
            except OSError:
                continue
        entries.sort(key=lambda entry: (entry[0].created_at, entry[0].title))
        return entries

    def rename(self, old: str, new: str) -> Optional[SessionData]:
        """重命名会话：改目录名与摘要标题；源不存在、目标已存在或名字非法返回 None。"""
        previous = safe_name(old)
        title = safe_name(new)
        if not previous or not title:
            return None
        source = self.folder(previous)
        target = self.folder(title)
        if not source.is_dir() or target.exists():
            return None
        try:
            source.rename(target)
        except OSError:
            return None
        session = self.load(title)
        if session is None:
            return None
        session.meta.title = title
        session.meta.file_name = title
        self.save(session)
        return session

    def archive(self, name: str) -> Optional[Path]:
        """把会话目录移入 ``archive/``，返回归档后的路径；不存在或名字非法返回 None。"""
        title = safe_name(name)
        if not title:
            return None
        folder = self.folder(title)
        if not folder.is_dir():
            return None
        target_root = self.archive_dir()
        target = target_root / folder.name
        index = 2
        while target.exists():
            target = target_root / f"{folder.name}-{index}"
            index += 1
        try:
            target_root.mkdir(parents=True, exist_ok=True)
            shutil.move(str(folder), str(target))
        except OSError:
            return None
        return target

    def archived(self) -> List[str]:
        """归档区里的会话名（目录名倒序，即新的在前）。

        只列目录名、不解析 ``summary.json``：归档区可能很长，而页面只需要名字。
        """
        root = self.archive_dir()
        if not root.is_dir():
            return []
        try:
            return sorted((folder.name for folder in root.iterdir() if folder.is_dir()), reverse=True)
        except OSError:
            return []

    def restore(self, name: str) -> Optional[Path]:
        """把归档会话移回会话列表；不存在或名字非法返回 None，重名时加序号后缀。"""
        title = safe_name(name)
        if not title:
            return None
        source = self.archive_dir() / title
        if not source.is_dir():
            return None
        target = self.session_root / title
        index = 2
        while target.exists():
            target = self.session_root / f"{title}-{index}"
            index += 1
        try:
            shutil.move(str(source), str(target))
        except OSError:
            return None
        if target.name != title:
            # 目录名因重名而变：摘要标题与文件名要跟着改，否则列表里显示的还是旧名
            session = self.load(target.name)
            if session is not None:
                session.meta.title = target.name
                session.meta.file_name = target.name
                self.save(session)
        return target

    def delete(self, name: str, archived: bool = False) -> bool:
        """彻底删除会话目录（``archived=True`` 表示删归档区里的那个），不可恢复。"""
        title = safe_name(name)
        if not title:
            return False
        target = (self.archive_dir() if archived else self.session_root) / title
        if not target.is_dir():
            return False
        try:
            shutil.rmtree(target)
        except OSError:
            return False
        return True

    def create(self, name: str) -> Optional[SessionData]:
        """创建会话目录与摘要文件；同名已存在或名字非法返回 None（空名用默认名）。"""
        title = safe_name(name) if (name or "").strip() else UNNAMED_SESSION
        if not title:
            return None
        folder = self.folder(title)
        if folder.exists():
            return None
        try:
            folder.mkdir(parents=True)
        except Exception:  # noqa: BLE001
            return None
        session = SessionData(meta=SessionInfo(created_at=time.time_ns(), session_id=uuid.uuid4().hex[:8], title=title, file_name=title))
        self.save(session)
        return session

    def save(self, session: SessionData) -> Path:
        """写入摘要文件（meta + 任务摘要）。"""
        return write_json(
            self.summary_path(session.meta.file_name or session.meta.title),
            session.model_dump(exclude_none=True),
        )

    def load(self, name: str) -> Optional[SessionData]:
        """读取会话；缺失或损坏返回 None。"""
        path = self.resolve(name)
        if path is None:
            return None
        payload = read_json(path)
        if payload is None:
            return None
        try:
            return SessionData.model_validate(payload)
        except Exception:  # noqa: BLE001
            return None


class DetailStore:
    """任务明细文件（``detail.json``）：按任务号索引的完整上下文。

    结构：``{"version": 1, "tasks": {"1": {...TaskDetail...}, "2": {...}}}``——一个任务一个键，
    所以"取某个任务的明细"就是按键取，不需要行号，也不需要解析整个文件。写入走临时文件 +
    原子替换（见 :func:`write_json`）。
    """

    def __init__(self, session_dir, file_name: str = DETAIL_FILE) -> None:
        self.session_root = Path(session_dir)
        self.file_name = file_name

    def path_for(self, session_name: str) -> Path:
        """指定会话的明细文件路径。"""
        return self.session_root / session_name / self.file_name

    def save(self, session_name: str, detail: TaskDetail) -> int:
        """写入一个任务的明细（同号覆盖），返回其任务号。"""
        path = self.path_for(session_name)
        with _DETAIL_LOCK:
            payload = read_detail_payload(path)
            payload["tasks"][str(detail.task_seq)] = detail.model_dump(exclude_none=True)
            write_json(path, payload)
        return detail.task_seq

    def load(self, session_name: str, task: int) -> Optional[dict]:
        """读取指定任务号的明细；不存在返回 None。"""
        return read_detail_payload(self.path_for(session_name))["tasks"].get(str(int(task)))

    def tasks(self, session_name: str) -> List[int]:
        """该会话已有明细的任务号（升序）。"""
        return sorted(int(key) for key in read_detail_payload(self.path_for(session_name))["tasks"])


def read_detail_payload(path) -> dict:
    """读取明细文件，统一成 ``{"version": int, "tasks": {任务号字符串: 记录}}``。

    兼容早先的 JSONL 布局（一行一个任务）：读到那种文件时按行解析成同样结构，
    于是 ``read_detail`` 工具与 ``DetailStore`` 只有一种内存形状。
    """
    target = Path(path)
    if not target.is_file():
        return {"version": DETAIL_VERSION, "tasks": {}}
    payload = read_json(target)
    if isinstance(payload, dict) and isinstance(payload.get("tasks"), dict):
        return {"version": int(payload.get("version") or DETAIL_VERSION), "tasks": dict(payload["tasks"])}
    tasks: dict[str, Any] = {}
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            logger.debug("跳过无法解析的明细行：%s", line[:80])
            continue
        if isinstance(record, dict) and record.get("task_seq") is not None:
            tasks[str(int(record["task_seq"]))] = record
    return {"version": DETAIL_VERSION, "tasks": tasks}


@lru_cache(maxsize=512)
def session_overview(path: str, modified: int, size: int) -> Optional[tuple[SessionInfo, int]]:
    """Cache only small metadata; complete task records are not retained here."""
    payload = read_json(path)
    if not isinstance(payload, dict) or "meta" not in payload:
        return None
    try:
        return SessionInfo.model_validate(payload["meta"]), len(payload.get("tasks", []))
    except (ValueError, TypeError):
        return None
