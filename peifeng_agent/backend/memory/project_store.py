"""项目（工作区）清单：``projects.json`` 记录项目，会话数据放在 ``sessions/<项目>/``。

**目录即真相**：``sessions/`` 下存在的项目目录就是项目。清单文件只是加速与记录额外信息
（名称、工作文件夹、最近使用、用户移除过的项目）；一旦清单损坏或被误覆盖，启动时会从
``sessions/`` 把项目目录补回列表，不会出现"会话还在、项目却不见了"。

``path`` 为空表示"默认工作文件夹"，即**当前用户的桌面**——整个数据目录拷到另一台电脑后，
默认项目仍然指向那台机器的桌面。``slug`` 只在创建时确定、之后不变（重命名只改显示名）。
"""
from __future__ import annotations

import os
import re
import time
import uuid
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field

from peifeng_agent.backend.memory.json_store import read_json, write_json

#: 项目清单文件名、会话数据目录名、数据根下兜底的工作文件夹目录
PROJECTS_FILE = "projects.json"
SESSIONS_DIR = "sessions"
WORKSPACES_DIR = "workspaces"
#: 旧布局的目录名（会话曾放在 ``projects/<项目>/sessions/`` 下）
LEGACY_PROJECTS_DIR = "projects"


def xdg_desktop() -> Optional[Path]:
    """Linux 等平台从 ``user-dirs.dirs`` 读桌面目录。"""
    config_home = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    config = Path(config_home) / "user-dirs.dirs"
    if not config.is_file():
        return None
    try:
        for line in config.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("XDG_DESKTOP_DIR="):
                value = line.split("=", 1)[1].strip().strip('"').replace("$HOME", str(Path.home()))
                return Path(value) if value else None
    except OSError:
        return None
    return None


def registry_desktop() -> Optional[Path]:
    """Windows：从注册表读桌面目录（用户可能把桌面重定向到别处，如 OneDrive）。"""
    try:
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            value, _ = winreg.QueryValueEx(key, "Desktop")
    except Exception:  # noqa: BLE001 - 读不到就交给上层回退
        return None
    expanded = os.path.expandvars(str(value))
    return Path(expanded) if expanded else None


def windows_desktop() -> Optional[Path]:
    """Windows：优先用已知文件夹接口，失败再读注册表。"""
    try:
        import ctypes
        from ctypes import wintypes

        class Guid(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        # FOLDERID_Desktop {B4BFCC3A-DB2C-424C-B029-7FE99A87C641}
        folder_id = Guid(
            0xB4BFCC3A,
            0xDB2C,
            0x424C,
            (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41),
        )
        pointer = ctypes.c_wchar_p()
        status = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(folder_id), 0, None, ctypes.byref(pointer)
        )
        if status == 0 and pointer.value:
            path = Path(pointer.value)
            ctypes.windll.ole32.CoTaskMemFree(pointer)
            return path
    except Exception:  # noqa: BLE001 - 任何失败都退回注册表
        pass
    return registry_desktop()


def known_desktop() -> Optional[Path]:
    """问系统要桌面目录（不猜目录名）。"""
    return windows_desktop() if os.name == "nt" else xdg_desktop()


def choose_desktop(known: Optional[Path], home: Path) -> Optional[Path]:
    """按优先级挑桌面：系统给出的位置 → ``<主目录>/Desktop``（存在才用）→ None。"""
    for candidate in (known, home / "Desktop"):
        if candidate is not None and candidate.is_dir():
            return candidate
    return None


def desktop_dir() -> Optional[Path]:
    """当前系统的桌面目录；实在找不到返回 None。"""
    return choose_desktop(known_desktop(), Path.home())


def default_parent(data_dir) -> Path:
    """新建项目与目录选择框的默认位置：桌面 → 用户主目录 → 数据根的 ``workspaces/``。"""
    desktop = desktop_dir()
    if desktop is not None:
        return desktop
    home = Path.home()
    return home if home.is_dir() else Path(data_dir) / WORKSPACES_DIR


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

    def resolve_path(self, project: Project) -> Path:
        """项目的工作文件夹：``path`` 为空时用默认位置（当前用户桌面）。"""
        configured = (project.path or "").strip()
        return Path(configured).expanduser() if configured else default_parent(self.data_dir)

    # ---------------------------------------------------------------- 写
    def create(self, name: str, path=None) -> Optional[Project]:
        """新建项目：``path`` 为空表示用默认工作文件夹（桌面本身，不新建子目录）。"""
        title = (name or "").strip()
        if not title:
            return None
        payload = self.load()
        slug = unique_slug(title, payload.projects)
        workspace = Path(path).expanduser() if path else None
        try:
            if workspace is not None:
                workspace.mkdir(parents=True, exist_ok=True)
            self.sessions_root(slug).mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        project = Project(
            slug=slug,
            name=title,
            path=str(workspace) if workspace is not None else "",
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
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_dir():
                    target.rmdir()  # 由 create 建出的空目录，先让位
                source.rename(target)
            except OSError:
                continue
            moved += 1
        return moved

    # ---------------------------------------------------------------- 内部
    def session_folders(self) -> List[Path]:
        """``sessions/`` 下的项目目录（不存在时为空）。"""
        root = self.data_dir / SESSIONS_DIR
        if not root.is_dir():
            return []
        try:
            return [folder for folder in root.iterdir() if folder.is_dir()]
        except OSError:
            return []

    def guess_path(self, slug: str) -> str:
        """补回项目时猜它的工作文件夹：默认位置下有同名目录就用它，否则留空（默认位置）。"""
        candidate = default_parent(self.data_dir) / slug
        return str(candidate) if candidate.is_dir() else ""

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
        try:
            self.manifest.replace(self.manifest.with_name(self.manifest.name + ".broken"))
        except OSError:
            pass


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
