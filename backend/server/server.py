"""服务层：应用状态机 + HTTP 路由 + 请求响应模型 + SSE。

一个文件四块：
- ``EventStream``：线程安全的事件序列（发布/回放/订阅/未决提问）
- ``SessionRunner`` / ``AppEntry``：每个会话一个执行单元，项目与任务生命周期
- 路由与模型：``/api/*`` 的请求响应模型、项目管理、会话、文件、能力面板、偏好
- SSE：``/api/events`` 的推送与心跳；``create_app`` 负责静态页面与错误形状
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from backend.memory.store import Project, ProjectStore, default_parent
from backend.memory.store import INTERNAL_DIR, DetailStore, SessionData, SummaryStore
from backend.agents.base import CORE_ROLES, Clarifier, TaskResult, TaskSummary
from backend.utils.errors import CancelledError, ExecutionError, PlanningError, ReviewError
from backend.utils.text import estimate_tokens
from backend.orchestration.orchestrate import build_environment, build_registry, copy_artifacts
from backend.orchestration.orchestrate import TaskOrchestrator, build_session_summary
from backend.scripts.offline import build_plan
from config import Settings
from contextlib import asynccontextmanager
from typing import AsyncIterator
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from typing import Any
from pydantic import BaseModel, ConfigDict, Field
import asyncio
from typing import AsyncIterator, Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from backend.orchestration.orchestrate import create_worker, delete_worker, list_all
from backend.orchestration.orchestrate import (
    drop_skill,
    drop_tool,
    upload_skill,
    upload_tool,
)
from backend.orchestration.orchestrate import (
    read_prompt,
    read_skill_source,
    read_tool_source,
    save_tool_source,
    save_skill_source,
    save_prompt,
)
# 与下面的路由函数同名：取别名，避免递归调到自己
from backend.orchestration.orchestrate import save_agent_config as grant_capabilities
from config import list_workspace
from backend.utils.errors import PlanningError
from backend.scripts.offline import preset_catalog


# ---------------------------------------------------------------- app
# ---------------------------------------------------------------- app_state
# ================================================================ 事件流

#: 内存中保留的可重放事件条数
HISTORY = 500
#: 单个订阅者的待发事件上限（默认值；实际值来自 Settings.event_backlog）
SUBSCRIBER_BACKLOG = 200
#: 退出时等待在跑任务收尾的时间（秒）
SHUTDOWN_WAIT = 5.0
#: 轨迹级事件：单独留一份用于重放（流式文本不进轨迹，避免重放体积与噪音）
TRACE_KINDS = frozenset(
    {
        "task_received",
        "plan_ready",
        "step_start",
        "step_review",
        "agent_tool_execution_start",
        "agent_tool_execution_end",
        "task_result",
        "task_failed",
        "task_cancelled",
        "task_idle",
        "input_timeout",
        "supplement_queued",
        "orchestrator_aborted",
        "orchestrator_failed",
        "artifacts",
        "await_input",
    }
)
#: 轨迹事件保留上限（够放若干个长任务；超出丢最旧的）
TRACE_LIMIT = 2000

logger = logging.getLogger(__name__)


class EventStream:
    """线程安全的事件序列：发布、回放、订阅与未决输入登记。

    **多会话并发的前提**：事件必须知道"它属于哪个会话"，不能靠一个可变的"当前会话"
    字段打标——两个会话同时跑任务时，那会让两条流互相串台。所以真正的打标入口是
    :meth:`publish_for`（会话名随事件走），:meth:`publish` 只是"发到当前查看会话"的简写。
    订阅也按会话过滤：一个页面连接只收它正在看的那个会话的事件（外加会话无关的全局事件）。
    """

    def __init__(self, history: int = HISTORY) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._history = history
        self._events: list[dict] = []
        #: 轨迹级事件（不含流式文本）：页面切换会话或刷新后据此重建轨迹
        self._trace: list[dict] = []
        #: 页面当前在看的会话（决定"发到哪个会话"与"回放哪个会话的轨迹"）
        self._session = ""
        self._project = ""
        #: 订阅者 → 它只接收哪个会话的事件（None 表示全部）
        self._viewers: dict[queue.Queue, Optional[tuple[str, str]]] = {}
        self._pending: dict[str, dict] = {}

    def set_session(self, name: str) -> None:
        """标记页面当前查看的会话：``publish`` 与 ``trace`` 缺省作用在它上面。"""
        with self._lock:
            self._session = name

    def set_project(self, project: str) -> None:
        with self._lock:
            self._project = project

    @property
    def session(self) -> str:
        """页面当前查看的会话名。"""
        with self._lock:
            return self._session

    # ------------------------------------------------------------ 事件
    def publish(self, kind: str, data: dict) -> dict:
        """广播一条事件到**当前查看的会话**（单个会话的旧路径用这个）。"""
        with self._lock:
            return self.emit(kind, data, self._session)

    def publish_for(self, session: str, kind: str, data: dict) -> dict:
        """广播一条**属于指定会话**的事件（并发执行时用这个，不依赖全局状态）。"""
        with self._lock:
            return self.emit(kind, data, session)

    def publish_global(self, kind: str, data: dict) -> dict:
        """广播一条**与会话无关**的事件（项目列表变化、启动失败等），所有页面都该收到。"""
        with self._lock:
            return self.emit(kind, data, "")

    def emit(self, kind: str, data: dict, session: Optional[str] = None) -> dict:
        """锁内发布：编号、入缓冲、推给订阅者。

        ``session`` 为空表示"会话无关的全局事件"（启动失败、能力加载错误等），
        这种事件推给所有订阅者。
        """
        self._seq += 1
        event = {"seq": self._seq, "kind": kind, "data": data,
                 "session": self._session if session is None else session, "project": self._project}
        self._events.append(event)
        if len(self._events) > self._history:
            del self._events[: len(self._events) - self._history]
        if kind in TRACE_KINDS:
            self._trace.append(event)
            if len(self._trace) > TRACE_LIMIT:
                del self._trace[: len(self._trace) - TRACE_LIMIT]
        owner = event["session"]
        for subscriber, only in self._viewers.items():
            if only is not None and owner and only != (event['project'], owner):
                continue  # 别的会话的事件不进这条连接
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                # 订阅者读得太慢：丢最旧的一条保住最新状态（页面靠跳号检测重新对账）
                subscriber.get_nowait()
                subscriber.put_nowait(event)
        return event

    @property
    def seq(self) -> int:
        """当前已发布的事件编号。"""
        with self._lock:
            return self._seq

    def trace(self, session: Optional[str] = None) -> list[dict]:
        """某个会话最近一个任务的轨迹事件（不含流式文本）。

        页面首次加载、刷新或切换会话时用它重建轨迹：从最近一条 ``task_received``
        起截取；更早任务的轨迹不再回放——它们已经作为历史任务摘要展示。
        ``session`` 缺省为页面当前查看的会话。
        """
        with self._lock:
            target = self._session if session is None else session
            events = [event for event in self._trace if event.get("session") == target
                      and event.get('project', '') == self._project]
        for index in range(len(events) - 1, -1, -1):
            if events[index]["kind"] == "task_received":
                return events[index:]
        return []

    def subscribe(
        self, after_seq: int = 0, backlog: int = SUBSCRIBER_BACKLOG, session: Optional[str] = None,
        project: Optional[str] = None,
    ) -> tuple[list[dict], queue.Queue]:
        """订阅某会话的事件流：返回 ``(缺失事件, 新事件队列)``，两者衔接无空档。

        ``session`` 为空表示"跟随当前查看的会话"——页面切换会话时应当带上明确的会话名
        （或重连），否则它会一直只收最初那个会话的流。
        每个订阅者的待发队列有上限：页面读得太慢时丢最旧的事件保住最新，
        避免关闭不掉的连接把内存撑爆（页面会重新拉一次状态快照对账）。
        """
        with self._lock:
            only = self._session if session is None else session
            scope = self._project if project is None else project
            missed = [
                event for event in self._events
                if event["seq"] > after_seq and (not event['session'] or
                    (event['session'] == only and event.get('project', '') == scope))
            ]
            subscriber: queue.Queue = queue.Queue(maxsize=max(1, backlog))
            self._viewers[subscriber] = (scope, only) if only else None
        return missed, subscriber

    def unsubscribe(self, subscriber: queue.Queue) -> None:
        """取消订阅。"""
        with self._lock:
            self._viewers.pop(subscriber, None)

    # ------------------------------------------------------------ 未决输入
    def request_input(self, request: dict, session: Optional[str] = None) -> str:
        """登记一次等待用户回答的请求并广播，返回其 id。"""
        with self._lock:
            owner = self._session if session is None else session
            request_id = f"ask-{self._seq + 1}"
            entry = {**request, "id": request_id, "session": owner, 'project': self._project}
            self._pending[request_id] = entry
            self.emit("await_input", entry, owner)
        return request_id

    def resolve_input(self, request_id: str, value: Any) -> bool:
        """页面回传后撤销未决状态，返回是否命中。"""
        with self._lock:
            entry = self._pending.pop(request_id, None)
            if entry is None:
                return False
            self.emit("input_resolved", {"id": request_id, "value": value}, entry.get("session", None))
            return True

    def pending(self, session: Optional[str] = None) -> list[dict]:
        """当前仍待回答的请求（可按会话过滤）。"""
        with self._lock:
            items = list(self._pending.values())
        if session is None:
            return items
        return [item for item in items if item.get("session", "") in ("", session)
                and item.get('project', '') == self._project]


DEFAULT_PROJECT = "默认项目"
NEW_SESSION = "会话1"
#: 等待页面回答的墙钟上限（秒）
ASK_TIMEOUT = 1800.0


#: 导入上限：单文件 64MB、合计 256MB（浏览器上传，写进当前项目目录）
IMPORT_FILE_LIMIT = 64 * 1024 * 1024
IMPORT_TOTAL_LIMIT = 256 * 1024 * 1024


@dataclass
class SessionRunner:
    """一个会话的执行单元：编排器 + 后台线程 + 取消信号 + 待发补充输入。

    多会话并发的**最小单位**：每个会话各持一个，卡住的任务不会拖住别的会话，
    取消 / 追加要求也只作用于自己那一个。``AppEntry`` 上那些同名字段是"当前查看会话"
    的镜像，方便状态快照读取；**执行路径一律用 runner 显式传参**，不能回头读镜像
    （否则用户切个会话就会把正在跑的任务指向别的会话）。
    """

    title: str
    session: Any
    session_dir: Path
    work_dir: Path
    #: 该会话独占的 agent 实例（agent 内部有可变对话上下文，绝不能跨会话共用）
    registry: Any = None
    orchestrator: Any = None
    running: bool = False
    worker: Optional[threading.Thread] = None
    cancel: threading.Event = field(default_factory=threading.Event)
    waiting: dict = field(default_factory=dict)
    supplements: Any = field(default_factory=queue.Queue)
    accepting_input: bool = True
    #: 用户在执行期间追加的要求：任务收尾后据此自动接续
    restart_requirement: str = ""
    restart_reservation: Any = None
    interruption_reason: str = ""
    #: 该会话最后一次模型调用的上下文占用（并发时各会话各算各的）
    context_usage: dict = field(default_factory=dict)

    def reset_for_task(self) -> None:
        """开始一个新任务前清掉上一轮的信号与排队输入。"""
        self.cancel = threading.Event()
        self.accepting_input = True
        self.interruption_reason = ""
        self.restart_requirement = ""
        self.restart_reservation = None


# 按裸文件名回查项目目录时跳过的目录：备份、中间产物、渲染缓存。
SKIP_DIRS = {INTERNAL_DIR, "backup", "render"}


class AppEntry:
    """应用状态机：一个当前项目、一个当前查看的会话，**每个会话各有一个执行单元**。

    同一时刻允许多个会话并行跑任务（彼此独立）；同一个会话内仍然串行——
    并发只在会话边界上发生，这样"切走再切回"看到的仍是那条完整、有序的轨迹。
    """

    def __init__(self, settings: Settings, provider: Any = None) -> None:
        self.settings = settings
        self.provider = provider
        self.events = EventStream(settings.event_history)
        self.projects = ProjectStore(settings.data_dir)
        self.project: Optional[Project] = None
        self.workspace: Optional[Path] = None
        self.store: Optional[SummaryStore] = None
        self.detail_store: Optional[DetailStore] = None
        #: 页面当前查看的会话（下面这些字段都是它的镜像，执行逻辑一律用 runner 显式传参）
        self.session: Optional[SessionData] = None
        self.session_dir: Optional[Path] = None
        self.work_dir: Optional[Path] = None
        self.orchestrator: Optional[TaskOrchestrator] = None
        #: 会话名 → 执行单元：**并发的最小单位**，每个会话各持一份编排器与工作线程
        self.runners: dict[str, "SessionRunner"] = {}
        #: 当前会话执行单元的键：``<项目 slug>/<会话名>``（不同项目可以有同名会话）
        self._runner_key: str = ""
        self.running = False
        #: 当前会话的任务线程（退出时用来等它收尾）
        self._worker: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._waiting: dict[str, list] = {}
        self._supplements: queue.Queue = queue.Queue()
        self._lock = threading.Lock()


    def assemble(self) -> None:
        """后台装配：打开最近使用的项目；失败时把原因推给页面。"""
        try:
            self.prepare()
        except Exception as error:  # noqa: BLE001 - 启动失败要显示在界面上
            self.events.publish_global("startup_failed", {"message": f"{type(error).__name__}: {error}"})

    def prepare(self) -> None:
        """打开最近使用的项目；一个项目都没有时建立默认项目。"""
        self.projects.migrate_legacy()
        projects = self.projects.list()
        if not projects:
            created = self.create_default_project()
            if created is None:
                raise RuntimeError("无法建立默认项目")
            projects = [created]
        last = self.projects.last()
        chosen = next((project for project in projects if project.slug == last), projects[0])
        self.open_project(chosen.slug)

    def create_default_project(self):
        """建立默认项目：工作文件夹就是默认位置（桌面），不新建子目录、不写绝对路径。"""
        return self.projects.create(DEFAULT_PROJECT)

    # ---------------------------------------------------------------- 项目
    def open_project(self, slug: str, session: str = "") -> bool:
        """切换项目（可指定会话，缺省打开最近更新的一个）。

        切项目会换掉全局的工作目录与会话存储，**只要还有会话在跑任务就拒绝**——
        那个任务的成果交付依赖当前的 workspace，换了就会投递到错误的位置。
        """
        project = self.projects.get(slug)
        if project is None or self.busy_sessions():
            return False
        self.project = project
        self.events.set_project(project.slug)
        # 工作文件夹：默认项目是"当前用户桌面"，交给系统解析（换电脑后仍然有效）
        self.workspace = self.projects.resolve_path(project)
        self.workspace.mkdir(parents=True, exist_ok=True)
        root = self.projects.sessions_root(slug)
        self.store = SummaryStore(root)
        self.detail_store = DetailStore(root)
        self.projects.mark_last(slug)
        if not self.open_session(session):
            return False
        self.events.publish_global(
            "project_opened",
            {"slug": project.slug, "name": project.name, "path": project.path},
        )
        return True

    def create_project(self, name: str, parent: str = "") -> bool:
        """新建项目：**名字必填**，工作区固定在 ``data/projects/<名字>/``（不再挂载外部目录）。"""
        title = (name or "").strip()
        if not title:
            return False
        created = self.projects.create(title)
        if created is None:
            return False
        self.events.publish_global("project_changed", {})
        return self.open_project(created.slug)

    def import_files(self, uploads, slug: str = "") -> Optional[list[str]]:
        """把页面选中的文件复制进项目（唯一的外部内容入口）。

        ``uploads`` 是 ``[(文件名, 字节), ...]``；``slug`` 缺省表示当前项目，
        显式给出时直接写进那个项目（侧栏项目菜单里的"导入文件"用它，不必先切项目）。
        文件名只取 basename，重名加序号，单文件上限见 :data:`IMPORT_FILE_LIMIT`。
        """
        key = str(slug or "").strip()
        if key:
            project = self.projects.get(key)
            if project is None:
                raise ValueError(f"项目不存在：{key}")
            root = self.projects.resolve_path(project)
        elif self.workspace is not None:
            root = Path(self.workspace)
        else:
            return None
        root.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []
        total = 0
        for raw_name, payload in uploads:
            name = Path(str(raw_name or "")).name.strip()
            if not name or name in {".", ".."}:
                continue
            total += len(payload)
            if len(payload) > IMPORT_FILE_LIMIT or total > IMPORT_TOTAL_LIMIT:
                raise ValueError(f"导入超限：单文件 {IMPORT_FILE_LIMIT // (1024 * 1024)}MB、合计 {IMPORT_TOTAL_LIMIT // (1024 * 1024)}MB")
            target = root / name
            if target.is_file():
                # 内容完全一样就直接算"已在项目里"：重复导入同一份材料不该堆出 name-2、name-3
                if target.read_bytes() == payload:
                    saved.append(target.relative_to(root).as_posix())
            continue
            index = 2
            while target.exists():
                target = root / f"{Path(name).stem}-{index}{Path(name).suffix}"
                index += 1
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            saved.append(target.relative_to(root).as_posix())
        return saved or None

    def rename_project(self, slug: str, name: str) -> bool:
        """重命名项目：只改显示名（标识、工作文件夹与会话数据都不动）。"""
        if self.busy_sessions():
            return False
        renamed = self.projects.rename(slug, name)
        if renamed is None:
            return False
        if self.project is not None and self.project.slug == slug:
            self.project = renamed
        self.events.publish_global("project_changed", {})
        return True

    def remove_project(self, slug: str) -> bool:
        """从清单移除项目（保留工作文件夹与数据）；移除当前项目时切到剩下的第一个。"""
        if self.busy_sessions() or not self.projects.remove(slug):
            return False
        if self.project is not None and self.project.slug != slug:
            self.events.publish_global("project_changed", {})
            return True
        remaining = self.projects.list()
        if remaining:
            return self.open_project(remaining[0].slug)
        created = self.create_default_project()
        return bool(created) and self.open_project(created.slug)


    # ---------------------------------------------------------------- 会话
    def is_current(self, slug: str) -> bool:
        """当前打开的项目是不是它。"""
        return self.project is not None and self.project.slug == slug

    def store_for(self, slug: str) -> Optional[tuple[Project, SummaryStore]]:
        """定位某个项目的会话存储：``slug`` 为空（或就是当前项目）时用当前项目。

        跨项目操作只借用对方的 ``SummaryStore``，不改变当前打开的项目与会话——
        页面因此可以直接管理别的项目下的会话。
        """
        if not slug or self.is_current(slug):
            if self.project is None or self.store is None:
                return None
            return self.project, self.store
        project = self.projects.get(slug)
        if project is None:
            return None
        return project, SummaryStore(self.projects.sessions_root(project.slug))

    def open_session(self, name: str = "", slug: str = "") -> bool:
        """切换会话（可指定所属项目；缺省用当前项目里最近更新的一个，没有会话就新建）。

        **任务执行中也允许切换**：每个会话各有自己的执行单元，切走只是换"页面在看谁"，
        正在跑的会话继续在后台推进；切回来能看到它的完整轨迹。
        """
        if slug and (self.project is None or self.project.slug != slug):
            # 点的是别的项目下的会话：先切到那个项目
            if not self.open_project(slug):
                return False
        if self.store is None:
            return False
        target = (name or "").strip()
        session = self.store.load(target) if target else None
        if session is None:
            metas = self.store.list()
            session = self.store.load(metas[0].title) if metas else None
        if session is None:
            session = self.store.create(NEW_SESSION)
        if session is None:
            return False
        self.attach_session(session)
        self.events.publish_for(
            session.meta.title,
            "session_opened",
            {
                "project": self.project.slug if self.project else "",
                "session": session.meta.title,
            },
        )
        return True

    def create_session(self, name: str = "", slug: str = "") -> bool:
        """在指定项目（缺省当前项目）下新建会话并切换过去。

        新建会话只增一个目录，不动全局工作目录，所以**任务执行中也允许**——
        这正是"另起一个会话并行干活"的入口。
        """
        if slug and (self.project is None or self.project.slug != slug):
            if not self.open_project(slug):
                return False
        if self.store is None:
            return False
        title = (name or "").strip() or self.next_session_name()
        if self.store.create(title) is None:
            return False
        self.events.publish_global("project_changed", {})
        return self.open_session(title)

    @staticmethod
    def runner_key(slug: str, title: str) -> str:
        """执行单元的键：项目 + 会话名（不同项目允许同名会话）。"""
        return f"{slug}/{title}"

    def _busy(self, title: str, slug: str = "") -> bool:
        """该会话是否正在执行任务（并发下"忙"要按会话判断，不能看全局）。"""
        project = slug or (self.project.slug if self.project else "")
        runner = self.runners.get(self.runner_key(project, title))
        return bool(runner and runner.running)

    def busy_sessions(self) -> list[str]:
        """所有正在执行任务的会话名（切项目这类全局操作据此决定能不能做）。"""
        return [name for name, runner in self.runners.items() if runner.running]

    def rename_session(self, old: str, new: str, slug: str = "") -> bool:
        """重命名某个项目的会话：目录与摘要标题一起改，会话数据不动。"""
        if self._busy(old):
            return False
        located = self.store_for(slug)
        if located is None:
            return False
        project, store = located
        renamed = store.rename(old, new)
        if renamed is None:
            return False
        self.runners.pop(self.runner_key(project.slug, old), None)  # 执行单元跟着换 key（此时它没有在跑）
        self.events.publish_global("session_renamed", {"session": renamed.meta.title, "project": project.slug})
        self.events.publish_global("project_changed", {})
        # 改的是当前会话：跟着切到新名字，否则页面标题还停在旧名
        if self.is_current(project.slug) and self.session is not None and self.session.meta.title == old:
            return self.open_session(renamed.meta.title)
        return True

    def archive_session(self, name: str = "", slug: str = "") -> bool:
        """归档会话：目录移入 ``sessions/<项目>/archive/``，数据保留但不再进列表。"""
        located = self.store_for(slug)
        if located is None:
            return False
        project, store = located
        title = (name or "").strip()
        if not title:
            # 不给名字＝归档当前会话
            if not self.is_current(project.slug) or self.session is None:
                return False
            title = self.session.meta.file_name or self.session.meta.title
        if self._busy(title) or self._busy(self.session.meta.title if self.session else ""):
            return False
        if store.archive(title) is None:
            return False
        self.runners.pop(self.runner_key(project.slug, title), None)
        self.events.publish_global("session_archived", {"session": title, "project": project.slug})
        self.events.publish_global("project_changed", {})
        # 归档的若是当前会话，切到剩下的第一个（没有则新建）
        if self.is_current(project.slug) and self.session is not None and self.session.meta.title == title:
            return self.open_session()
        return True

    def restore_session(self, name: str, slug: str = "") -> bool:
        """把归档会话移回会话列表（数据原样）。"""
        located = self.store_for(slug)
        if located is None:
            return False
        project, store = located
        restored = store.restore(name)
        if restored is None:
            return False
        self.events.publish_global("session_restored", {"session": restored.name, "project": project.slug})
        self.events.publish_global("project_changed", {})
        return True

    def delete_session(self, name: str, slug: str = "", archived: bool = False) -> bool:
        """彻底删除会话目录（不可恢复）；删掉的若是当前会话，就切到剩下的第一个。"""
        if self._busy(name):
            return False
        located = self.store_for(slug)
        if located is None:
            return False
        project, store = located
        if not store.delete(name, archived=archived):
            return False
        self.runners.pop(self.runner_key(project.slug, name), None)
        self.events.publish_global("session_deleted", {"session": name, "project": project.slug})
        self.events.publish_global("project_changed", {})
        if (
            not archived
            and self.is_current(project.slug)
            and self.session is not None
            and self.session.meta.title == name
        ):
            return self.open_session()
        return True

    def next_session_name(self) -> str:
        """Return the first free numbered session name."""
        taken = {meta.title for meta in self.store.list()} if self.store else set()
        index = 1
        while f"会话{index}" in taken:
            index += 1
        return f"会话{index}"


    def attach_session(self, session: SessionData) -> SessionRunner:
        """把某个会话设为"当前查看"，返回它的执行单元（没有就建一个）。

        **这里是并发的边界**：执行单元一旦建好就只属于这个会话，切走再切回来复用同一个
        （编排器、工作线程、取消信号都还在），所以切换会话不会打断正在跑的任务。
        """
        title = session.meta.title
        key = self.runner_key(self.project.slug if self.project else "", title)
        session_dir = self.store.folder(session.meta.file_name or title) if self.store else Path(".")
        # 中间目录放在**项目内**：提示词告诉模型"中间目录放临时脚本"，而工具白名单只允许项目目录，
        # 两者必须一致，否则每次写中间文件都会撞墙。
        work_dir = Path(self.workspace or session_dir) / INTERNAL_DIR
        work_dir.mkdir(parents=True, exist_ok=True)
        runner = self.runners.get(key)
        if runner is None:
            runner = SessionRunner(title=title, session=session, session_dir=session_dir, work_dir=work_dir)
            runner.orchestrator = self.build_orchestrator(runner)
            runner.context_usage = self.estimate_context(runner)
            self.runners[key] = runner
        elif not runner.running:
            # 空闲会话：接受重新载入的数据（任务摘要更新了），并让编排器跟着换绑定。
            # 正在跑的时候不能换——那会把跑到一半的会话状态冲掉。
            runner.session = session
            if runner.orchestrator is not None:
                runner.orchestrator.bind_session(session)
        # 镜像：页面快照与只读路径（workspace_files 等）读这些字段
        self.session = runner.session
        self.session_dir = session_dir
        self.work_dir = work_dir
        self.orchestrator = runner.orchestrator
        self.running = runner.running
        self._worker = runner.worker
        self._cancel = runner.cancel
        self._waiting = runner.waiting
        self._supplements = runner.supplements
        self._runner_key = key
        self.events.set_session(title)
        return runner

    def build_orchestrator(self, runner: SessionRunner) -> TaskOrchestrator:
        """给一个会话建编排器：**它自己的 agent 实例 + 它自己的事件出口**。

        agent 内部持有可变的对话上下文与取消检查，不能跨会话共用——每个会话各装配一份
        （工具模块本身按 (路径, mtime) 缓存，重复装配只是重读若干文件）。
        """
        if runner.registry is None:
            runner.registry = build_registry(self.settings, provider=self.provider)
            for error in runner.registry.errors:
                self.events.publish_for(runner.title, "capability_error", {"message": error})
        registry = runner.registry
        title = runner.title
        workers = {name: node for name, node in registry.nodes.items() if name not in CORE_ROLES}
        orchestrator = TaskOrchestrator(
            mentor=registry.nodes["mentor"],
            workers=workers,
            worker_specs=registry.specs,
            clarifier=PanelClarifier(self, title),
            step_max_retries=self.settings.step_max_retries,
            # 事件显式带上会话名：并发时不能依赖"当前会话"这种可变全局
            listener=lambda kind, data: self.events.publish_for(title, kind, data),
            summary_store=self.store,
            detail_store=self.detail_store,
            cancelled=lambda: runner.cancel.is_set(),
            artifact_dir=self.workspace,
        )
        orchestrator.runtime = {
            "environment": build_environment(
                self.settings, self.workspace, runner.session_dir, runner.work_dir
            ),
            "workspace": str(self.workspace or ""),
            "session_dir": str(runner.session_dir),
        }
        orchestrator.bind_session(runner.session)
        orchestrator.agent_listener = lambda kind, data: self.on_agent_event(title, kind, data)
        orchestrator.poll_input = lambda: self.poll_supplement(runner)
        orchestrator.close_input = lambda: self.close_input(runner)
        orchestrator.interruption_reason = lambda: runner.interruption_reason
        return orchestrator

    def estimate_context(self, runner: SessionRunner) -> dict:
        """估算该会话开跑前的上下文占用（拿不到端点 usage 时的起点值）。"""
        nodes = getattr(runner.registry, "nodes", {}) if runner.registry else {}
        mentor = nodes.get("mentor")
        runtime = getattr(runner.orchestrator, "runtime", {}) or {}
        stored = json.dumps(
            {
                "system": getattr(mentor, "system_prompt", ""),
                "environment": runtime.get("environment", ""),
                "history": build_session_summary(runner.orchestrator.session) if runner.orchestrator else "",
                "tools": getattr(mentor, "tool_defs", []),
            },
            ensure_ascii=False,
        )
        return {
            "characters": len(stored),
            "estimated_tokens": estimate_tokens(stored),
            "source": "estimate",
        }

    def on_agent_event(self, session: str, kind: str, data: dict) -> None:
        """Agent 事件归属到它自己的会话，加 ``agent_`` 前缀后并入同一事件流。"""
        runner = next((item for item in self.runners.values() if item.title == session), None)
        if kind == "context_usage" and runner is not None:
            runner.context_usage = data
        self.events.publish_for(session, f"agent_{kind}", data)

    def poll_supplement(self, runner: SessionRunner) -> Optional[str]:
        """执行期间取一次页面插入的补充要求（只取该会话自己的）。"""
        try:
            return runner.supplements.get_nowait()
        except queue.Empty:
            return None

    def close_input(self, runner: SessionRunner) -> list[str]:
        """关闭该会话的追加通道，并取走剩下还没消费的补充要求。"""
        with self._lock:
            runner.accepting_input = False
            remaining = []
            while True:
                try:
                    remaining.append(runner.supplements.get_nowait())
                except queue.Empty:
                    return remaining

    def cancelled(self, runner: Optional[SessionRunner] = None) -> bool:
        """是否已请求中止（缺省看当前查看的会话）。"""
        if runner is None:
            runner = self.runners.get(self.events.session)
        return bool(runner and runner.cancel.is_set())

    # ---------------------------------------------------------------- 页面接口
    def snapshot(self) -> dict:
        """页面加载时的状态快照（全部围绕**当前查看的会话**）。"""
        tasks = [
            {
                "seq": task.task_seq,
                "requirement": task.requirement,
                "original_request": task.original_request or task.requirement,
                "status": task.status,
                "content": task.content,
                "files": self.artifact_paths(task.files, self.workspace),
                # 历史任务也能看到当时的计划：刷新或切会话后，轨迹里只剩最近一个任务
                "plan": {
                    "requirement": task.plan.requirement if task.plan else "",
                    "output_name": task.plan.output_name if task.plan else "",
                    "steps": [
                        {"index": index, "goal": step.goal, "worker": step.worker}
                        for index, step in enumerate(task.plan.steps if task.plan else [])
                    ],
                },
            }
            for task in (self.session.tasks if self.session else [])
        ]
        files = self.workspace_files()
        session_name = self.events.session
        runner = self.runners.get(self._runner_key)
        return {
            "context_usage": {
                **((runner.context_usage if runner else {}) or {}),
                "model": self.settings.model,
                "limit": self.settings.model_context_limit or (1048576 if self.settings.model == "deepseek-flash" else None),
            },
            "projects": self.project_summaries(),
            "project": self.project.slug if self.project else "",
            "session": self.session.meta.title if self.session else "",
            "workspace": str(self.workspace or ""),
            "default_parent": str(default_parent(self.settings.data_dir)),
            # running 指"你正在看的这个会话"是否在跑；running_sessions 是全部在跑的会话
            "running": bool(runner and runner.running),
            "running_sessions": [item.title for item in self.runners.values() if item.running],
            "tasks": tasks,
            "files": files,
            "pending": self.events.pending(session_name),
            "seq": self.events.seq,
            "trace": self.events.trace(session_name),
        }

    def project_summaries(self) -> list:
        """项目列表（含各自会话、任务数与运行状态），供侧边栏渲染。"""
        summaries = []
        for project in self.projects.list():
            sessions = []
            archived: list[str] = []
            root = self.projects.sessions_root(project.slug)
            if root.is_dir():
                store = SummaryStore(root)
                for meta, task_count in store.list_overview():
                    sessions.append(
                        {
                            "name": meta.title,
                            "tasks": task_count,
                            "running": bool(
                                self.runners.get(self.runner_key(project.slug, meta.title))
                                and self.runners[self.runner_key(project.slug, meta.title)].running
                            ),
                            "current": bool(
                                self.is_current(project.slug)
                                and self.session is not None
                                and self.session.meta.title == meta.title
                            ),
                        }
                    )
                archived = store.archived()
            folder = self.projects.resolve_path(project)
            summaries.append(
                {
                    "slug": project.slug,
                    "name": project.name,
                    "path": project.path,
                    "folder": str(folder),
                    "missing": not folder.is_dir(),
                    "current": self.is_current(project.slug),
                    "sessions": sessions,
                    "archived": archived,
                }
            )
        return summaries

    def submit_task(self, text: str, preset: str = "", values: Optional[dict] = None, *, _reservation=None) -> None:
        """接收页面提交的任务并在后台线程执行（计划直接执行，无需确认）。

        ``preset`` 非空时走**快捷模板**：用模板 + 表单值生成现成计划，跳过 LLM 规划。
        模板字段不合法会抛 :class:`PlanningError`，由 API 层转成页面能看懂的提示。
        """
        plan = None
        if preset:
            plan = build_plan(preset, values or {})
            requirement = plan.requirement
        else:
            requirement = text.strip()
        if not requirement:
            return
        # 占位与启动放在同一把锁内：**同一个会话**并发提交时只有一个能进入。
        # 不同会话各有自己的 runner，所以可以同时在跑。
        with self._lock:
            runner = self.runners.get(self._runner_key)
            if runner is None or getattr(self, "_shutting_down", False) or runner.orchestrator is None:
                return
            if _reservation is not None and runner.restart_reservation is not _reservation:
                return
            owns_reservation = _reservation is not None and runner.restart_reservation is _reservation
            if runner.running and not owns_reservation:
                return
            runner.restart_reservation = None
            runner.reset_for_task()
            runner.running = True
            self._sync_view(runner)
        self.events.publish_for(runner.title, "task_received", {"requirement": requirement})
        runner.worker = threading.Thread(
            target=self.run_task,
            args=(runner, requirement),
            kwargs={"plan": plan},
            daemon=True,
        )
        self._sync_view(runner)
        runner.worker.start()

    def _sync_view(self, runner: SessionRunner) -> None:
        """把执行单元的运行态镜像到 AppEntry（仅当它就是当前查看的会话）。"""
        if self.events.session != runner.title:
            return
        self.session = runner.session
        self.running = runner.running
        self._worker = runner.worker
        self._cancel = runner.cancel
        self._waiting = runner.waiting
        self._supplements = runner.supplements
        self.orchestrator = runner.orchestrator

    def submit_input(self, request_id: str, value: Any) -> bool:
        """页面回传一次回答；命中未决请求时唤醒等待中的执行线程。"""
        with self._lock:
            waiting = self._waiting.get(request_id)
            if waiting is None:
                # 并发时未决请求可能属于别的会话的 runner
                waiting = next(
                    (item.waiting.get(request_id) for item in self.runners.values() if request_id in item.waiting),
                    None,
                )
            if waiting is None:
                return False
            waiting[1] = value
        if not self.events.resolve_input(request_id, value):
            return False
        waiting[0].set()
        return True

    def ask(self, kind: str, payload: dict, session: str = "") -> dict:
        """推送一次等待用户回答的请求并阻塞，直到页面回传（按会话定位执行线程）。"""
        title = session or self.events.session
        request_id = self.events.request_input({"kind": kind, **payload}, title)
        runner = next((item for item in self.runners.values() if item.title == title), None)
        waiting: list = [threading.Event(), {}]
        with self._lock:
            self._waiting[request_id] = waiting
            if runner is not None:
                runner.waiting[request_id] = waiting
        try:
            deadline = time.monotonic() + ASK_TIMEOUT
            while True:
                if self.cancelled(runner):
                    raise CancelledError("用户中止了任务")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.events.publish_for(title, "input_timeout", {"id": request_id})
                    break
                if waiting[0].wait(min(0.05, remaining)):
                    if self.cancelled(runner):
                        raise CancelledError("用户中止了任务")
                    break
        finally:
            with self._lock:
                self._waiting.pop(request_id, None)
                if runner is not None:
                    runner.waiting.pop(request_id, None)
            self.events.resolve_input(request_id, None)
        value = waiting[1]
        return value if isinstance(value, dict) else {}

    def supplement(self, text: str) -> bool:
        """把执行期间的补充要求排入**当前查看会话**的队列，当前步骤结束后并入任务。

        非执行期（空闲、或任务刚结束）拒绝入队：否则它会留在队列里，
        被下一个任务的第一个节点当成"用户补充"消费掉。
        """
        with self._lock:
            runner = self.runners.get(self._runner_key)
            if runner is None or not runner.running or not runner.accepting_input or not text.strip():
                return False
            runner.supplements.put(text.strip())
            runner.restart_requirement = text.strip()
            runner.interruption_reason = "用户补充"
            runner.cancel.set()
            title = runner.title
        self.events.publish_for(title, "supplement_queued", {"text": text.strip()})
        return True

    def cancel(self) -> bool:
        """请求中止**当前查看会话**正在执行的任务（在当前步骤边界生效）。"""
        title = self.events.session
        with self._lock:
            runner = self.runners.get(self._runner_key)
            if runner is None or not runner.running:
                return False
            reserved = runner.restart_reservation is not None
            runner.restart_reservation = None
            runner.restart_requirement = ""
            runner.interruption_reason = "用户停止"
            runner.cancel.set()
            if reserved:
                runner.running = False
            self._sync_view(runner)
        self.events.publish_for(title, "task_cancelling", {"reason": "用户停止"})
        if reserved:
            self.events.publish_for(title, "task_idle", {})
        return True

    @staticmethod
    def artifact_paths(files, workspace) -> list[str]:
        """Expose existing artifacts by their full workspace-relative path."""
        if not workspace:
            return []
        root = Path(workspace).resolve()
        result = []
        for name in files or []:
            candidate = Path(name)
            if not candidate.is_absolute():
                candidate = root / candidate
            resolved = candidate.resolve()
            if not resolved.is_relative_to(root) or not resolved.is_file():
                continue
            relative = resolved.relative_to(root).as_posix()
            if relative not in result:
                result.append(relative)
        return result

    def resolve_file(self, name: str) -> Optional[Path]:
        """定位工作区里的产出文件（供页面下载）；不存在返回 None。

        页面只传文件名，所以裸名在项目目录内按名查找（跳过备份与中间目录，
        同名取最近修改的一份）；带目录的相对路径照旧按路径解析。
        """
        if self.workspace is None or not name:
            return None
        relative = Path(name)
        if relative.is_absolute() or relative.drive or ".." in relative.parts:
            return None
        root = self.workspace.resolve()
        target = (root / relative).resolve()
        if target.is_file() and target.is_relative_to(root):
            return target
        if len(relative.parts) > 1:
            return None
        best: Optional[tuple] = None
        for current, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            if relative.name not in files:
                continue
            candidate = Path(current) / relative.name
            try:
                stamp = candidate.stat().st_mtime
            except OSError:
                continue
            if best is None or stamp > best[1]:
                best = (candidate, stamp)
        return best[0] if best else None

    def workspace_files(self) -> list[str]:
        """List top-level files and declared artifacts without walking the workspace."""
        if self.workspace is None or not self.workspace.is_dir():
            return []
        root = self.workspace.resolve()
        files = {path.name for path in root.iterdir() if path.is_file() and self.resolve_file(path.name)}
        for task in self.session.tasks if self.session else []:
            for name in task.files:
                candidate = Path(name)
                if candidate.is_absolute():
                    candidate = candidate.resolve()
                    if not candidate.is_relative_to(root):
                        continue
                    candidate = candidate.relative_to(root)
                relative = candidate.as_posix()
                if self.resolve_file(relative) is not None:
                    files.add(relative)
        return sorted(files)

    # ---------------------------------------------------------------- 任务执行
    def run_task(self, runner: SessionRunner, requirement: str, plan=None) -> None:
        """在某个会话的执行单元里跑一轮任务（后台线程入口）。

        **全程只用 runner**，不碰 `self.orchestrator` / `self._cancel` 这些"当前查看会话"
        的镜像——用户随时可能切到别的会话，读镜像就会把结果写错地方。
        """
        orchestrator = runner.orchestrator
        title = runner.title
        orchestrator._finalized = False
        try:
            final = orchestrator.run_task(requirement, workflow="plan_and_execute", plan=plan)
        except CancelledError:
            self.events.publish_for(title, "task_cancelled", {"message": "已按你的要求中止，未完成的步骤不会交付。"})
            self.finish_failure(runner, "用户中止", status="aborted")
        except (PlanningError, ExecutionError, ReviewError) as error:
            self.events.publish_for(title, "task_failed", {"message": str(error)})
            self.finish_failure(runner, str(error))
        except Exception as error:  # noqa: BLE001 - 任何异常都要让界面恢复可用
            self.events.publish_for(title, "task_failed", {"message": f"{type(error).__name__}: {error}"})
            self.finish_failure(runner, f"{type(error).__name__}: {error}")
        else:
            self.events.publish_for(title, "task_result", {"status": final.status, "content": final.content})
            try:
                self.deliver(runner, final)
            except Exception as error:  # noqa: BLE001 - 交付失败也要让界面知道，不能只留线程栈
                logger.debug("交付成果文件失败", exc_info=True)
                self.events.publish_for(title, "task_failed", {"message": f"成果文件交付失败：{error}"})
        finally:
            with self._lock:
                # 编排器的 SessionState 持有的是任务列表的**拷贝**：把最新摘要回写到执行单元，
                # 否则页面快照（读 runner.session）会停在这个任务开始前的样子
                state = getattr(orchestrator, "session", None)
                if state is not None and state.info is not None:
                    runner.session = SessionData(meta=state.info, tasks=list(state.tasks))
                restart = runner.restart_requirement
                runner.restart_requirement = ""
                persisted = bool(getattr(orchestrator, "_finalized", False))
                should_restart = bool(restart) and persisted and not getattr(self, "_shutting_down", False)
                reservation = object() if should_restart else None
                runner.restart_reservation = reservation
                runner.running = should_restart
                runner.worker = None
                self._sync_view(runner)
            if should_restart:
                # 自动接续只对"刚刚跑完的那个会话"生效：把当前查看的会话临时切回去提交
                self._resume_in_session(runner, restart, reservation)
            else:
                if restart and not persisted:
                    self.events.publish_for(
                        title, "task_failed", {"message": "中断日志未保存成功，已停止自动接续；请先处理存储错误。"}
                    )
                self.events.publish_for(title, "task_idle", {})

    def _resume_in_session(self, runner: SessionRunner, requirement: str, reservation) -> None:
        """把"用户补充后自动接续"的任务提交回它自己的会话（不动页面正在看的会话）。"""
        with self._lock:
            current = self.events.session
            self.events.set_session(runner.title)
        try:
            self.submit_task(requirement, _reservation=reservation)
        finally:
            with self._lock:
                self.events.set_session(current)

    def shutdown(self, timeout: float = SHUTDOWN_WAIT) -> None:
        """退出前收尾：请求中止**所有**在跑的任务，并等它们把会话落盘。

        任务线程是 daemon，进程退出不会等它；这里给它们一点时间走完 ``finally`` 里的收尾，
        避免 Ctrl+C 正好落在写 summary.json / detail/ 的中间。
        """
        self._shutting_down = True
        workers = []
        with self._lock:
            for runner in self.runners.values():
                runner.interruption_reason = "服务退出"
                runner.restart_requirement = ""
                if runner.running:
                    runner.cancel.set()
                if runner.worker is not None:
                    workers.append(runner.worker)
        for worker in workers:
            worker.join(timeout)

    def deliver(self, runner: SessionRunner, final: TaskResult) -> None:
        """把成果文件复制到工作区，并推给页面（按会话发事件）。"""
        workspace = Path(runner.orchestrator.runtime.get('workspace') or runner.work_dir.parent)
        copied = copy_artifacts(final, runner.work_dir, workspace)
        declared = [name for result in final.step_results for name in result.files]
        files = self.artifact_paths([*declared, *copied], workspace)
        self.events.publish_for(runner.title, "artifacts", {"files": files})

    def record_failure(self, runner: SessionRunner, reason: str, *, status: str = "failed") -> None:
        """兜底记录：编排层已不可用（或还没建任务）时，只在会话摘要里留一条。"""
        if runner.session is None or self.store is None:
            return
        orchestrator = runner.orchestrator
        requirement = orchestrator.task.requirement if orchestrator and orchestrator.task else ""
        runner.session.tasks.append(
            TaskSummary(
                task_seq=orchestrator.session.next_task_seq() if orchestrator else len(runner.session.tasks) + 1,
                requirement=requirement,
                status=status,
                content=reason,
            )
        )
        self.store.save(runner.session)

    def finish_failure(self, runner: SessionRunner, reason: str, *, status: str = "failed") -> None:
        """失败 / 中止也留一份**总结汇报**（写 detail/ 与 summary.json），而不是只有一条摘要。

        编排层还在时交给它的 ``fail_task``：那里知道每一步做到哪、产出哪些文件、卡在哪，
        生成的汇报下一次规划也能读到；只有编排层不可用时才退化成 :meth:`record_failure`。
        """
        orchestrator = runner.orchestrator
        if orchestrator is not None and getattr(orchestrator, "task", None) is not None:
            if status == "failed":
                orchestrator.fail_task(reason)
            else:
                orchestrator.finalize_task(orchestrator.stop_content(None), status="aborted")
            return
        self.record_failure(runner, reason, status=status)


class PanelClarifier(Clarifier):
    """澄清提问由页面回答（计划直接执行，不再走确认）。

    带上了会话名：并发时"等的是哪个会话的提问"必须明确，否则用户切到别的会话后
    这个等待会被挂到错误的任务上。
    """

    def __init__(self, app: AppEntry, session: str = "") -> None:
        self.app = app
        self.session = session

    def ask_clarification(self, questions: list) -> list:
        """推送澄清问题并等待页面回答（只发给发起提问的那个会话）。"""
        answer = self.app.ask("clarify", {"questions": questions}, session=self.session)
        return [str(value) for value in (answer.get("answers") or [])]


# ---------------------------------------------------------------- main
#: 前端构建产物目录名（Vite 默认）
DIST_DIR = "dist"


def create_app(settings: Settings) -> FastAPI:
    """创建应用：状态、路由、静态资源与错误形状。"""
    engine = AppEntry(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # 后台装配：打开最近使用的项目；失败时把原因推给页面
        import threading

        threading.Thread(target=engine.assemble, name="assemble", daemon=True).start()
        try:
            yield
        finally:
            engine.shutdown()

    app = FastAPI(
        title="培风 Agent",
        description="基于 Mentor–Worker 协作的高校科研学习助手",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.engine = engine
    app.include_router(router)
    mount_pages(app, settings)
    unify_errors(app)
    return app


def mount_pages(app: FastAPI, settings: Settings) -> None:
    """托管前端构建产物（``frontend/dist``，由 ``npm run build`` 生成）。

    ``index.html`` 不缓存（重新构建后刷新即生效）；``dist/assets`` 的名字带内容哈希，
    可以长缓存。没构建过时给出明确提示，而不是一个难以理解的 404。
    """
    pages = Path(settings.page_dir) / DIST_DIR
    assets = pages / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        page = pages / "index.html"
        if not page.is_file():
            raise HTTPException(
                status_code=503,
                detail="前端尚未构建：先执行 cd frontend && npm run build",
            )
        return FileResponse(page, media_type="text/html", headers={"Cache-Control": "no-store"})

    # 页面对应的静态文件（构建产物）由它提供；/api/* 的路由优先级更高
    if pages.is_dir():
        app.mount("/", StaticFiles(directory=pages, html=False), name="pages")


def unify_errors(app: FastAPI) -> None:
    """把 HTTPException 统一成 ``{"ok": false, "error": ...}``，与动作端点同形状。"""

    @app.exception_handler(HTTPException)
    async def handle_http_error(_: Request, error: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content={"ok": False, "error": str(error.detail)},
        )


# ---------------------------------------------------------------- api
# ---------------------------------------------------------------- schemas
class SubmitTaskBody(BaseModel):
    """提交任务（计划直接执行，无需确认）。

    ``preset`` 非空时走**快捷模板**：用模板 + ``values`` 生成现成计划，跳过 LLM 规划；
    为空时 ``text`` 就是任务描述，照旧交给 Mentor。
    """

    text: str = ""
    preset: str = ""
    values: dict[str, str] = Field(default_factory=dict)


class SupplementBody(BaseModel):
    """执行期间追加要求（非执行期返回 409）。"""

    text: str = ""


class InputBody(BaseModel):
    """回传一次提问的回答。"""

    id: str = ""
    value: Any = None


class OpenProjectBody(BaseModel):
    slug: str = ""
    session: str = ""


class CreateProjectBody(BaseModel):
    """新建项目：只要名字，工作区固定在 data/projects/<名字>/。"""

    name: str = ""


class SourceBody(BaseModel):
    """保存能力源码（工具 / SKILL.md / 提示词）时的请求体。"""

    kind: str = ""
    name: str = ""
    content: str = ""


class SourceResponse(BaseModel):
    """读能力源码的响应：正文 + 落盘路径 + 归属。"""

    content: str = ""
    path: str = ""
    name: str = ""
    agent: str = ""


class AgentConfigBody(BaseModel):
    """角色栏保存：这个角色能用哪些工具与技能。"""

    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)


class ImportResult(BaseModel):
    """导入结果：落盘后的项目内相对路径。"""

    files: list[str] = Field(default_factory=list)


class RenameProjectBody(BaseModel):
    slug: str = ""
    name: str = ""


class RenameSessionBody(BaseModel):
    """会话重命名：``from`` / ``to`` 都是 Python 关键字或易混名，用别名映射。

    ``slug`` 指会话所属项目（页面可以对任意项目下的会话操作，不必先切过去）。
    """

    model_config = ConfigDict(populate_by_name=True)

    old: str = Field(default="", alias="from")
    new: str = Field(default="", alias="to")
    slug: str = ""


class SessionBody(BaseModel):
    """定位会话：``slug`` 是所属项目，``name`` 是会话目录名。"""

    name: str = ""
    slug: str = ""


class DeleteSessionBody(BaseModel):
    """彻底删除会话（不可恢复）：``archived=true`` 表示删归档区里的那一个。"""

    name: str = ""
    slug: str = ""
    archived: bool = False


class RemoveProjectBody(BaseModel):
    slug: str = ""


class OkResponse(BaseModel):
    """动作成功。"""

    ok: bool = True


class SessionSummary(BaseModel):
    """侧边栏里的一个会话。"""

    name: str = ""
    tasks: int = 0
    current: bool = False
    #: 这个会话是否有任务正在执行（并发时侧边栏据此画"运行中"标记）
    running: bool = False


class ProjectSummary(BaseModel):
    """侧边栏里的一个项目。"""

    slug: str = ""
    name: str = ""
    path: str = ""
    folder: str = ""
    missing: bool = False
    current: bool = False
    sessions: list[SessionSummary] = Field(default_factory=list)
    #: 归档区里的会话名（数据保留；页面可恢复或彻底删除）
    archived: list[str] = Field(default_factory=list)


class TaskItem(BaseModel):
    """对话历史里的一条任务。"""

    seq: int = 0
    #: mentor 整合后的完整需求
    requirement: str = ""
    #: 用户实际输入的那句话（历史列表显示它）
    original_request: str = ""
    status: str = ""
    summary: str = ""
    content: str = ""
    files: list[str] = Field(default_factory=list)
    #: 当时的计划（steps: index/goal/worker/expected_output）；刷新或切会话后历史任务也能回看
    plan: dict[str, Any] = Field(default_factory=dict)


class OfflineBody(BaseModel):
    """离线执行一个预设：不调模型，直接跑固定流程。"""

    preset: str = ""
    values: dict[str, str] = Field(default_factory=dict)
    #: 工作目录（相对项目目录或绝对路径）；空表示用当前会话的工作区
    folder: str = ""


class OfflineResultBody(BaseModel):
    """离线执行结果：产出文件、说明与注意事项。"""

    files: list[str] = Field(default_factory=list)
    summary: str = ""
    notes: list[str] = Field(default_factory=list)


class Snapshot(BaseModel):
    """页面加载与断线重连时拉取的状态快照。"""

    context_usage: dict[str, Any] = Field(default_factory=dict)
    projects: list[ProjectSummary] = Field(default_factory=list)
    project: str = ""
    session: str = ""
    workspace: str = ""
    #: 新建项目时的默认位置（父目录），供页面提示用
    default_parent: str = ""
    #: 当前查看的会话是否在跑任务
    running: bool = False
    #: 所有正在跑任务的会话名（并发时页面据此提示"另有 N 个会话在跑"）
    running_sessions: list[str] = Field(default_factory=list)
    tasks: list[TaskItem] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    pending: list[dict[str, Any]] = Field(default_factory=list)
    seq: int = 0
    #: 当前会话最近一个任务的轨迹事件（不含流式文本）：切换会话或刷新后据此重建轨迹
    trace: list[dict[str, Any]] = Field(default_factory=list)


class WorkspaceItem(BaseModel):
    """项目工作目录里的一项：路径一律相对项目目录，可直接填进任务描述。"""

    name: str = ""
    path: str = ""
    size: int = 0


class WorkspaceListing(BaseModel):
    """项目内浏览的一屏：当前相对路径 + 子目录 + 文件（给快捷模板点选素材）。"""

    path: str = ""
    #: 上一级（相对路径）；已在项目根上时为空串
    parent: str = ""
    dirs: list[WorkspaceItem] = Field(default_factory=list)
    files: list[WorkspaceItem] = Field(default_factory=list)


class CreateWorkerBody(BaseModel):
    """新建worker：名字即角色名，职责描述是 Mentor 的分派依据。"""

    name: str = ""
    description: str = ""
    prompt: str = ""


class UploadToolBody(BaseModel):
    """上传一个工具：``filename`` 决定工具名，``content`` 是 .py 全文。"""

    agent: str = ""
    filename: str = ""
    content: str = ""


class UploadSkillBody(BaseModel):
    """上传一个技能：``name`` 是技能目录名，``content`` 是 SKILL.md 全文。"""

    agent: str = ""
    name: str = ""
    content: str = ""


# ---------------------------------------------------------------- api
router = APIRouter()


def get_app(request: Request) -> AppEntry:
    """取应用状态（由 :func:`create_app` 放进 ``app.state``）。"""
    return request.app.state.engine


def ok() -> OkResponse:
    return OkResponse()


def require(condition: bool, reason: str) -> None:
    """动作失败即 409：前端只需要知道"为什么没成"。"""
    if not condition:
        raise HTTPException(status_code=409, detail=reason)


# ---------------------------------------------------------------- 状态与事件流
@router.get("/api/state", response_model=Snapshot)
def read_state(app: AppEntry = Depends(get_app)) -> dict:
    """页面加载与断线重连时的状态快照。"""
    return app.snapshot()


@router.get("/api/events")
async def stream_events(
    request: Request,
    after: int = 0,
    session: str = "",
    project: Optional[str] = None,
    app: AppEntry = Depends(get_app),
) -> StreamingResponse:
    """SSE 事件流：先补发缺失事件，再持续推送；带心跳，断连即退订。

    ``session`` 指定"只收哪个会话的事件"（多会话并发时两个任务同时在发事件，
    不过滤就会串台）；不给则跟随当前查看的会话。
    """
    last_id = max(after, as_int(request.headers.get("last-event-id")))
    missed, subscriber = app.events.subscribe(last_id, app.settings.event_backlog, session or None, project=project)
    heartbeat = app.settings.sse_heartbeat

    async def frames() -> AsyncIterator[bytes]:
        try:
            for event in missed:
                yield frame(event)
            while True:
                try:
                    # 队列来自线程池一侧，用 to_thread 取，避免阻塞事件循环
                    event = await asyncio.to_thread(subscriber.get, True, heartbeat)
                except queue.Empty:
                    yield b": ping\n\n"
                    continue
                yield frame(event)
        finally:
            app.events.unsubscribe(subscriber)

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


def frame(event: dict) -> bytes:
    """一条 SSE 帧：``id`` 让浏览器重连时能带上 Last-Event-ID。"""
    return f"id: {event['seq']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")


# ---------------------------------------------------------------- 任务动作
@router.get("/api/presets")
def list_presets() -> dict:
    """快捷模板清单：页面据此渲染入口按钮与表单字段（不含 Plan 模板本身）。"""
    return {"presets": preset_catalog()}


@router.post("/api/offline", response_model=OfflineResultBody)
def run_offline(body: OfflineBody, app: AppEntry = Depends(get_app)) -> OfflineResultBody:
    """离线跑一个预设：**不调用任何模型**，直接按固定流程产出文件。

    给"断网 / 没有 API key / 不想花钱"的场景用。只接受**做得完整**的预设
    （读表画图、清洗汇总、按类型归档）——需要生成内容的任务没有离线版。
    """
    from backend.scripts.offline import RUNNERS, run_preset

    preset = (body.preset or "").strip()
    if preset not in RUNNERS:
        require(False, f"这个入口没有离线版：{preset or '（空）'}（可用：{'、'.join(sorted(RUNNERS))}）")
    # 没选项目时 app.workspace 是 None：退到进程工作目录（就是项目根），
    # 否则 Path(None) 会抛 TypeError，页面只能看到 500
    base = Path(app.workspace) if app.workspace else Path.cwd()
    folder = (body.folder or "").strip()
    workspace = base if not folder else (Path(folder) if Path(folder).is_absolute() else base / folder)
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        result = run_preset(preset, dict(body.values or {}), workspace)
    except Exception as error:  # noqa: BLE001 - 离线失败要把原因直接回给页面
        raise HTTPException(status_code=400, detail=str(error)) from error
    return OfflineResultBody(
        files=list(result.files), summary=result.summary, notes=list(result.notes)
    )


@router.post("/api/task", response_model=OkResponse)
def submit_task(body: SubmitTaskBody, app: AppEntry = Depends(get_app)) -> OkResponse:
    """在当前查看的会话里提交一个任务；**该会话**已有任务在跑时静默忽略。

    不同会话各有自己的执行单元，可以同时跑（并发只在会话之间发生）。
    带 ``preset`` 时用快捷模板直接生成计划（跳过 LLM 规划）；字段不合法返回 400 并点名
    缺哪一项，页面据此提示用户补填。
    """
    try:
        app.submit_task(body.text.strip(), preset=body.preset.strip(), values=body.values)
    except PlanningError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return ok()


@router.post("/api/input", response_model=OkResponse)
def submit_input(body: InputBody, app: AppEntry = Depends(get_app)) -> OkResponse:
    """回传一次提问的回答。"""
    require(app.submit_input(body.id, body.value), "该请求已失效")
    return ok()


@router.post("/api/supplement", response_model=OkResponse)
def supplement(body: SupplementBody, app: AppEntry = Depends(get_app)) -> OkResponse:
    """执行期间追加要求；非执行期拒绝，避免它留到下一个任务被消费掉。"""
    require(app.supplement(body.text), "当前没有执行中的任务，无法追加要求")
    return ok()


@router.post("/api/cancel", response_model=OkResponse)
def cancel(app: AppEntry = Depends(get_app)) -> OkResponse:
    """中止当前任务（在步骤边界或节点等待期间生效）。"""
    require(app.cancel(), "当前没有执行中的任务")
    return ok()


# ---------------------------------------------------------------- 项目
@router.post("/api/project/open", response_model=OkResponse)
def open_project(body: OpenProjectBody, app: AppEntry = Depends(get_app)) -> OkResponse:
    require(app.open_project(body.slug, body.session), "该项目无法打开（可能正在执行任务）")
    return ok()


@router.post("/api/project/create", response_model=OkResponse)
def create_project(body: CreateProjectBody, app: AppEntry = Depends(get_app)) -> OkResponse:
    require(app.create_project(body.name), "无法新建项目（项目名不能为空）")
    return ok()


@router.post("/api/project/import", response_model=ImportResult)
async def import_file(
    request: Request,
    name: str = "",
    slug: str = "",
    app: AppEntry = Depends(get_app),
) -> dict:
    """把页面选中的文件导入当前项目（系统外部内容进入项目的唯一入口）。

    浏览器只能给文件内容、给不了本机绝对路径——这正好符合"工作区固定在项目内"的边界。
    一次一个文件：请求体就是文件字节，文件名走查询参数（避免 multipart 依赖）。
    """
    require(bool(str(name or "").strip()), "缺少文件名")
    try:
        payload = await request.body()
        saved = app.import_files([(name, payload)], slug)
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    require(saved is not None, "导入失败（没有当前项目或文件为空）")
    return {"files": saved}


@router.post("/api/project/rename", response_model=OkResponse)
def rename_project(body: RenameProjectBody, app: AppEntry = Depends(get_app)) -> OkResponse:
    require(app.rename_project(body.slug, body.name), "无法重命名项目（名称不能为空）")
    return ok()


@router.post("/api/project/remove", response_model=OkResponse)
def remove_project(body: RemoveProjectBody, app: AppEntry = Depends(get_app)) -> OkResponse:
    require(app.remove_project(body.slug), "该项目无法移除（可能正在执行任务）")
    return ok()


# ---------------------------------------------------------------- 会话
@router.post("/api/session/open", response_model=OkResponse)
def open_session(body: SessionBody, app: AppEntry = Depends(get_app)) -> OkResponse:
    """切换会话（任务执行中也可以切；每个会话各有自己的执行单元）。"""
    require(app.open_session(body.name, body.slug), "无法打开该会话（可能已被归档或删除）")
    return ok()


@router.post("/api/session/create", response_model=OkResponse)
def create_session(body: SessionBody, app: AppEntry = Depends(get_app)) -> OkResponse:
    require(app.create_session(body.name, body.slug), "无法新建会话")
    return ok()


@router.post("/api/session/rename", response_model=OkResponse)
def rename_session(body: RenameSessionBody, app: AppEntry = Depends(get_app)) -> OkResponse:
    require(app.rename_session(body.old, body.new, body.slug), "无法重命名会话（名称重复、为空或含路径分隔符）")
    return ok()


@router.post("/api/session/archive", response_model=OkResponse)
def archive_session(body: SessionBody, app: AppEntry = Depends(get_app)) -> OkResponse:
    """归档：目录移入 ``archive/``，数据保留但不再进会话列表。"""
    require(app.archive_session(body.name, body.slug), "无法归档该会话")
    return ok()


@router.post("/api/session/restore", response_model=OkResponse)
def restore_session(body: SessionBody, app: AppEntry = Depends(get_app)) -> OkResponse:
    """把归档会话移回会话列表（数据原样）。"""
    require(app.restore_session(body.name, body.slug), "无法恢复该会话")
    return ok()


@router.post("/api/session/delete", response_model=OkResponse)
def delete_session(body: DeleteSessionBody, app: AppEntry = Depends(get_app)) -> OkResponse:
    """彻底删除会话目录，不可恢复——页面对这个动作有二次确认。"""
    require(app.delete_session(body.name, body.slug, body.archived), "无法删除该会话")
    return ok()


# ---------------------------------------------------------------- 能力管理
@router.get("/api/capabilities/source", response_model=SourceResponse)
def read_source(kind: str, name: str, app: AppEntry = Depends(get_app)) -> dict:
    """读能力源码：kind=tool|skill|prompt，name 是工具名 / 技能名 / 角色名。"""
    readers = {"tool": read_tool_source, "skill": read_skill_source, "prompt": read_prompt}
    reader = readers.get(kind)
    if reader is None:
        raise HTTPException(status_code=400, detail=f"kind 只能是 {'/'.join(readers)}，当前 {kind!r}")
    try:
        return reader(app.settings.agent_dir, name)
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api/capabilities/source", response_model=OkResponse)
def save_source(body: SourceBody, app: AppEntry = Depends(get_app)) -> OkResponse:
    """保存能力源码：工具先解析再落盘，SKILL.md 与提示词校验名字与内容。"""
    writers = {"tool": save_tool_source, "skill": save_skill_source, "prompt": save_prompt}
    writer = writers.get(body.kind)
    if writer is None:
        raise HTTPException(status_code=400, detail=f"kind 只能是 {'/'.join(writers)}，当前 {body.kind!r}")
    try:
        writer(app.settings.agent_dir, body.name, body.content)
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return ok()


@router.get("/api/capabilities")
def list_capabilities(app: AppEntry = Depends(get_app)) -> dict:
    """能力面板三栏数据：工具（含被哪些角色注册）、技能（按角色）、角色与提示词。"""
    return list_all(app.settings.agent_dir)


@router.post("/api/capabilities/worker", response_model=OkResponse)
def create_worker(body: CreateWorkerBody, app: AppEntry = Depends(get_app)) -> OkResponse:
    """新建worker：生成 角色注册表 + 提示词（重启后生效）。"""
    try:
        create_worker(app.settings.agent_dir, body.name, body.description, body.prompt)
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return ok()


@router.delete("/api/capabilities/worker/{name}", response_model=OkResponse)
def delete_worker(name: str, app: AppEntry = Depends(get_app)) -> OkResponse:
    """删除worker：注册表与提示词一起删；核心角色（mentor）拒绝删除。"""
    try:
        delete_worker(app.settings.agent_dir, name)
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return ok()


@router.post("/api/capabilities/tool", response_model=OkResponse)
def upload_tool(body: UploadToolBody, app: AppEntry = Depends(get_app)) -> OkResponse:
    """上传工具：先解析校验（元数据 + 同名函数）再落盘，坏文件不进目录。"""
    try:
        upload_tool(app.settings.agent_dir, body.agent, body.content, body.filename)
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return ok()


@router.delete("/api/capabilities/tool/{name}", response_model=OkResponse)
def drop_tool(name: str, app: AppEntry = Depends(get_app)) -> OkResponse:
    """从所有角色移除这个工具并删除文件（工具栏的"删除"）。"""
    try:
        drop_tool(app.settings.agent_dir, name)
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return ok()


@router.delete("/api/capabilities/skill/{name}", response_model=OkResponse)
def drop_skill(name: str, app: AppEntry = Depends(get_app)) -> OkResponse:
    """从所有角色移除这个技能并删除目录（技能栏的"删除"）。"""
    try:
        drop_skill(app.settings.agent_dir, name)
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return ok()


@router.post("/api/capabilities/agent/{name}/config", response_model=OkResponse)
def save_agent_config(name: str, body: AgentConfigBody, app: AppEntry = Depends(get_app)) -> OkResponse:
    """保存角色能用哪些工具与技能（只改注册表，不动文件）。"""
    try:
        grant_capabilities(app.settings.agent_dir, name, body.tools, body.skills)
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return ok()


@router.post("/api/capabilities/skill", response_model=OkResponse)
def upload_skill(body: UploadSkillBody, app: AppEntry = Depends(get_app)) -> OkResponse:
    """上传技能：frontmatter 的 name 必须与技能目录名一致。"""
    try:
        upload_skill(app.settings.agent_dir, body.agent, body.content, body.name)
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return ok()


# ---------------------------------------------------------------- 目录浏览
@router.get("/api/workspace/entries", response_model=WorkspaceListing)
def list_workspace_entries(path: str = "", app: AppEntry = Depends(get_app)) -> dict:
    """列出**项目工作目录**里某一层的子目录与文件，供快捷模板点选素材。

    路径一律相对项目目录（worker的工具就是按相对路径读的）；``../`` 越界返回 404。
    与 ``/api/fs/dirs`` 的分工：那个用来选"项目放在哪"（整块磁盘），这个只浏览项目内部。
    """
    listing = list_workspace(app.workspace, path)
    if listing is None:
        raise HTTPException(status_code=404, detail="该目录不可用（或不在项目目录内）")
    return listing


# 目录浏览只剩 "项目内"（``/api/workspace/entries``）：系统其它位置已隔离，
# 不再提供整盘目录选择。


# ---------------------------------------------------------------- 产出文件
@router.get("/api/file")
def download_file(name: str, app: AppEntry = Depends(get_app)) -> FileResponse:
    """把工作文件夹里的产出文件作为附件下载（接受工作区内相对路径，拒绝越界）。"""
    target: Optional[Path] = app.resolve_file(name)
    if target is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    # FileResponse 的 filename 会生成带 UTF-8 编码的 Content-Disposition
    return FileResponse(target, media_type="application/octet-stream", filename=target.name)


def as_int(value) -> int:
    """宽松取整：非法值按 0 处理。"""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
