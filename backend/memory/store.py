"""数据落盘：JSON 读写、项目清单、会话日志与明细。

一个文件三块：
- JSON：先写同目录临时文件再 ``os.replace``，进程中断不留半截文件
- 项目：``<data>/projects/<项目>/`` 清单与工作区解析
- 会话：一个会话一份 ``summary.json``（``/tasks`` 摘要 + ``/records/<任务号>`` 明细 + ``/outputs`` 正文）
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any
import re
import uuid
from typing import List, Optional
from pydantic import BaseModel, Field
import shutil
import threading
from functools import lru_cache
from typing import Any, List, Optional
from backend.agents.base import TaskSummary


# ---------------------------------------------------------------- json_store
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


# ---------------------------------------------------------------- project_store
#: 项目清单文件名、会话数据目录名、项目工作区目录名
PROJECTS_FILE = "projects.json"
SESSIONS_DIR = "sessions"
PROJECTS_DIR = "projects"
#: 旧布局的目录名（会话曾放在 ``projects/<项目>/sessions/`` 下）
LEGACY_PROJECTS_DIR = "projects"














def default_parent(data_dir) -> Path:
    """项目工作区根：``<data>/projects``。工作区只在这里，不再指向用户桌面或任意目录。"""
    return Path(data_dir) / PROJECTS_DIR


class Project(BaseModel):
    """一个项目：标识、显示名与工作文件夹。

    ``path`` 为空表示用默认工作文件夹（当前用户桌面），每次启动动态解析；有值则是绝对路径。
    """

    slug: str
    name: str
    path: str = ""
    created_at: str = ""


class ProjectList(BaseModel):
    """``projects.json`` 的结构：项目清单 + 最近使用 + 用户移除过的项目。"""

    projects: List[Project] = Field(default_factory=list)
    last: str = ""
    #: 用户主动移除过的项目：目录还在也不再自动补回列表
    removed: List[str] = Field(default_factory=list)


class ProjectStore:
    """项目清单读写与项目数据目录定位。"""

    def __init__(self, data_dir) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.manifest = self.data_dir / PROJECTS_FILE

    # ---------------------------------------------------------------- 读
    def list(self) -> List[Project]:
        """全部项目（清单顺序在前，``sessions/`` 里多出来的目录补在后面）。

        清单损坏或丢失时，项目目录本身仍是真相——据此把项目补回列表。
        """
        payload = self.load()
        projects = list(payload.projects)
        known = {project.slug for project in projects}
        missing = [
            Project(slug=folder.name, name=folder.name, path=self.guess_path(folder.name))
            for folder in sorted(self.session_folders())
            if folder.name not in known and folder.name not in set(payload.removed)
        ]
        if missing:
            payload.projects = projects + missing
            self.save(payload)
            projects = payload.projects
        return list(projects)

    def last(self) -> str:
        """最近使用的项目 slug。"""
        return self.load().last

    def get(self, slug: str) -> Optional[Project]:
        """按 slug 取项目。"""
        return next((project for project in self.list() if project.slug == slug), None)

    def workspace_root(self, slug: str) -> Path:
        """项目工作区（唯一真相）：``<data>/projects/<slug>/``，与外部目录无关。"""
        return self.data_dir / PROJECTS_DIR / slug

    def resolve_path(self, project: Project) -> Path:
        """项目的工作文件夹：固定在 ``<data>/projects/<slug>/``，忽略历史清单里的外部路径。"""
        return self.workspace_root(project.slug)

    # ---------------------------------------------------------------- 写
    def create(self, name: str, path=None) -> Optional[Project]:
        """新建项目：工作区固定在 ``<data>/projects/<slug>/``（``path`` 参数已废弃、被忽略）。"""
        title = (name or "").strip()
        if not title:
            return None
        payload = self.load()
        slug = unique_slug(title, payload.projects)
        workspace = self.workspace_root(slug)
        workspace.mkdir(parents=True, exist_ok=True)
        self.sessions_root(slug).mkdir(parents=True, exist_ok=True)
        project = Project(
            slug=slug,
            name=title,
            path=str(workspace),
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        payload.projects.append(project)
        payload.last = slug
        payload.removed = [item for item in payload.removed if item != slug]
        self.save(payload)
        return project

    def rename(self, slug: str, name: str) -> Optional[Project]:
        """重命名项目：只改显示名，标识、工作文件夹与会话数据都不动。"""
        title = (name or "").strip()
        payload = self.load()
        project = next((item for item in payload.projects if item.slug == slug), None)
        if project is None or not title:
            return None
        project.name = title
        self.save(payload)
        return project

    def remove(self, slug: str) -> bool:
        """从清单移除项目（保留工作文件夹与 ``sessions/`` 数据，并记下"不再自动补回"）。"""
        payload = self.load()
        remaining = [project for project in payload.projects if project.slug != slug]
        if len(remaining) == len(payload.projects):
            return False
        payload.projects = remaining
        if slug not in payload.removed:
            payload.removed.append(slug)
        if payload.last == slug:
            payload.last = remaining[0].slug if remaining else ""
        self.save(payload)
        return True

    def mark_last(self, slug: str) -> None:
        """记录最近使用的项目。"""
        payload = self.load()
        payload.last = slug
        self.save(payload)

    def sessions_root(self, slug: str) -> Path:
        """项目的会话数据目录（每层一个项目，归档也在其下）。"""
        return self.data_dir / SESSIONS_DIR / slug

    def migrate_legacy(self) -> int:
        """把旧布局（``projects/<项目>/sessions``）的会话搬到 ``sessions/<项目>``。

        返回迁移的项目数；目标目录已有数据时跳过，不做覆盖。
        """
        legacy = self.data_dir / LEGACY_PROJECTS_DIR
        if not legacy.is_dir():
            return 0
        moved = 0
        for folder in sorted(legacy.iterdir()):
            source = folder / "sessions"
            if not source.is_dir():
                continue
            target = self.sessions_root(folder.name)
            if target.is_dir() and any(target.iterdir()):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_dir():
                target.rmdir()  # 由 create 建出的空目录，先让位
            source.rename(target)
            moved += 1
        return moved

    # ---------------------------------------------------------------- 内部
    def session_folders(self) -> List[Path]:
        """``sessions/`` 下的项目目录（不存在时为空）。"""
        root = self.data_dir / SESSIONS_DIR
        if not root.is_dir():
            return []
        return [folder for folder in root.iterdir() if folder.is_dir()]

    def guess_path(self, slug: str) -> str:
        """补回项目时定位它的工作文件夹：固定根下的同名目录。"""
        return str(self.workspace_root(slug))

    def load(self) -> ProjectList:
        payload = read_json(self.manifest)
        if not isinstance(payload, dict):
            return ProjectList()
        try:
            return ProjectList.model_validate(payload)
        except Exception:  # noqa: BLE001 - 清单损坏时留档，避免下次启动把旧内容覆盖掉
            self.quarantine()
            return ProjectList()

    def save(self, payload: ProjectList) -> None:
        write_json(self.manifest, payload.model_dump())

    def quarantine(self) -> None:
        """把损坏的清单改名留档（``projects.json.broken``），便于人工检查。"""
        self.manifest.replace(self.manifest.with_name(self.manifest.name + ".broken"))


#: 目录名里唯一需要替换的字符（Windows 非法字符），空格与中文都保留
_ILLEGAL = re.compile(r'[\\/:*?"<>|]+')


def unique_slug(name: str, projects: List[Project]) -> str:
    """由项目名生成唯一的目录名：只清理非法字符，保留空格与中文。"""
    base = _ILLEGAL.sub("-", name).strip(" .") or uuid.uuid4().hex[:8]
    taken = {project.slug for project in projects}
    slug = base
    index = 2
    while slug in taken:
        slug = f"{base}-{index}"
        index += 1
    return slug


# ---------------------------------------------------------------- session_store
#: 会话目录内的固定文件名
SUMMARY_FILE = "summary.json"
#: 明细目录名：每个任务每个步骤一个文件（task{任务号}_step{步骤号}.json）
DETAIL_DIR = "detail"

#: 会话内放中间产物的目录名
INTERNAL_DIR = "internal"

#: 归档会话的子目录名（在会话根下，不进会话列表）
ARCHIVE_DIR = "archive"

#: 没给名字时用的会话名
UNNAMED_SESSION = "未命名会话"

#: 明细文件是"读-改-写"：同一会话并发保存时用一个锁串行化（任务本身是串行的，这里只是兜底）
_DETAIL_LOCK = threading.RLock()

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
            except OSError:
                # 列举与 stat 之间被删/改名（界面上删会话、并发建新会话）是正常竞态：
                # 跳过这一条即可，不能让整个状态接口 500。
                continue
            entry = session_overview(str(path), info.st_mtime_ns, info.st_size)
            if entry is not None:
                meta, count = entry
                if not meta.created_at:
                    try:
                        meta = meta.model_copy(update={"created_at": folder.stat().st_ctime_ns})
                    except OSError:
                        pass
                entries.append((meta, count))
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
        source.rename(target)
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
        target_root.mkdir(parents=True, exist_ok=True)
        shutil.move(str(folder), str(target))
        return target

    def archived(self) -> List[str]:
        """归档区里的会话名（目录名倒序，即新的在前）。

        只列目录名、不解析 ``summary.json``：归档区可能很长，而页面只需要名字。
        """
        root = self.archive_dir()
        if not root.is_dir():
            return []
        return sorted((folder.name for folder in root.iterdir() if folder.is_dir()), reverse=True)

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
        shutil.move(str(source), str(target))
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
        shutil.rmtree(target)
        return True

    def create(self, name: str) -> Optional[SessionData]:
        """创建会话目录与摘要文件；同名已存在或名字非法返回 None（空名用默认名）。"""
        title = safe_name(name) if (name or "").strip() else UNNAMED_SESSION
        if not title:
            return None
        folder = self.folder(title)
        if folder.exists():
            return None
        folder.mkdir(parents=True)
        session = SessionData(meta=SessionInfo(created_at=time.time_ns(), session_id=uuid.uuid4().hex[:8], title=title, file_name=title))
        self.save(session)
        return session

    def save(self, session: SessionData) -> Path:
        """写入摘要文件（meta + 任务摘要）。"""
        target = self.summary_path(session.meta.file_name or session.meta.title)
        with _DETAIL_LOCK:
            payload = read_session_log(target)
            payload.update(session.model_dump(exclude_none=True))
            return write_json(target, payload)

    def load(self, name: str) -> Optional[SessionData]:
        """读取会话；缺失或损坏返回 None。"""
        path = self.resolve(name)
        if path is None:
            return None
        payload = read_json(path)
        if payload is None:
            return None
        return SessionData.model_validate(payload)


def read_session_log(path: Path) -> dict:
    """Load a complete log; never silently replace a corrupt existing file."""
    payload = read_json(path)
    if payload is None:
        if path.exists():
            raise ValueError(f'会话日志损坏，拒绝覆盖：{path}')
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError(f'会话日志必须是对象：{path}')
    payload.setdefault('records', {})
    return payload


class DetailStore:
    """Compatibility facade: all records now live in the session's single log."""

    def __init__(self, session_dir, folder_name: str = DETAIL_DIR) -> None:
        self.session_root = Path(session_dir)

    def folder_for(self, session_name: str) -> Path:
        if not safe_name(session_name):
            raise ValueError('非法会话名')
        return self.session_root / session_name

    def save(self, session_name: str, detail: Any) -> int:
        target = self.folder_for(session_name) / SUMMARY_FILE
        number = str(detail.task_seq)
        with _DETAIL_LOCK:
            payload = read_session_log(target)
            task = payload['records'].setdefault(number, {})
            outputs = task.get('outputs', {})
            task.clear()
            task.update(detail.model_dump(exclude_none=True))
            valid = {str(step.index) for step in detail.steps}
            task['outputs'] = {key: value for key, value in outputs.items() if key in valid}
            write_json(target, payload)
        return int(number)

    def save_output(self, session_name: str, task_seq: int, index: int, result: Any) -> str:
        target = self.folder_for(session_name) / SUMMARY_FILE
        with _DETAIL_LOCK:
            payload = read_session_log(target)
            task = payload['records'].setdefault(str(task_seq), {'task_seq': task_seq})
            task.setdefault('outputs', {})[str(index)] = result.model_dump(exclude_none=True)
            write_json(target, payload)
        return f'{target}#/records/{task_seq}/outputs/{index}'

    def load_step(self, session_name: str, task_seq: int, index: int) -> Optional[dict]:
        target = self.folder_for(session_name) / SUMMARY_FILE
        with _DETAIL_LOCK:
            task = read_session_log(target)['records'].get(str(task_seq), {})
        step = next((item for item in task.get('steps', []) if item['index'] == index), None)
        return {'task_seq': task_seq, 'requirement': task.get('requirement', ''), 'step': step} if step else None


@lru_cache(maxsize=512)
def session_overview(path: str, modified: int, size: int) -> Optional[tuple[SessionInfo, int]]:
    """Cache only small metadata; complete task records are not retained here."""
    payload = read_json(path)
    if not isinstance(payload, dict) or "meta" not in payload:
        return None
    return SessionInfo.model_validate(payload["meta"]), len(payload.get("tasks", []))
