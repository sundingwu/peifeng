"""应用状态：事件序列、项目与会话管理、任务生命周期（不含任何 HTTP 细节）。

- ``EventStream``：线程安全的事件序列（发布 / 回放 / 订阅 / 未决提问），HTTP 层只做桥接
- ``AppEntry``：项目与会话管理、任务生命周期、供路由调用的动作方法
- ``PanelClarifier``：把澄清提问交给页面回答（计划不再需要用户确认）

传输层已迁到 FastAPI（见 ``backend/server/api.py``）；本模块不 import 任何 Web 框架，
因此可以脱离 HTTP 单独测试与复用。

服务端不碰桌面：不弹系统选择框、不在本机打开文件——目录由页面自己浏览选择
（`GET /api/fs/dirs` 只列目录名，见 :mod:`peifeng_agent.backend.utils.fs_browse`），产出文件由页面下载。
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from peifeng_agent.backend.memory.project_store import Project, ProjectStore, default_parent
from peifeng_agent.backend.memory.session_store import INTERNAL_DIR, DetailStore, SessionData, SummaryStore
from peifeng_agent.backend.utils.contracts import CORE_ROLES, Clarifier, TaskResult, TaskSummary
from peifeng_agent.backend.utils.exceptions import CancelledError, ExecutionError, PlanningError, ReviewError
from peifeng_agent.backend.utils.tokens import estimate_tokens
from peifeng_agent.backend.workflow.assembly import AgentRegistry, build_environment, build_registry, copy_artifacts
from peifeng_agent.backend.workflow.forward import TaskOrchestrator, build_session_summary
from peifeng_agent.backend.workflow.presets import build_plan
from peifeng_agent.config import Settings

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
        #: 订阅者 → 它只接收哪个会话的事件（None 表示全部）
        self._viewers: dict[queue.Queue, Optional[str]] = {}
        self._pending: dict[str, dict] = {}

    def set_session(self, name: str) -> None:
        """标记页面当前查看的会话：``publish`` 与 ``trace`` 缺省作用在它上面。"""
        with self._lock:
            self._session = name

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
                 "session": self._session if session is None else session}
        self._events.append(event)
        if len(self._events) > self._history:
            del self._events[: len(self._events) - self._history]
        if kind in TRACE_KINDS:
            self._trace.append(event)
            if len(self._trace) > TRACE_LIMIT:
                del self._trace[: len(self._trace) - TRACE_LIMIT]
        owner = event["session"]
        for subscriber, only in self._viewers.items():
            if only is not None and owner and only != owner:
                continue  # 别的会话的事件不进这条连接
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                # 订阅者读得太慢：丢最旧的一条保住最新状态（页面靠跳号检测重新对账）
                try:
                    subscriber.get_nowait()
                    subscriber.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass
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
            events = [event for event in self._trace if event.get("session") == target]
        for index in range(len(events) - 1, -1, -1):
            if events[index]["kind"] == "task_received":
                return events[index:]
        return []

    def subscribe(
        self, after_seq: int = 0, backlog: int = SUBSCRIBER_BACKLOG, session: Optional[str] = None
    ) -> tuple[list[dict], queue.Queue]:
        """订阅某会话的事件流：返回 ``(缺失事件, 新事件队列)``，两者衔接无空档。

        ``session`` 为空表示"跟随当前查看的会话"——页面切换会话时应当带上明确的会话名
        （或重连），否则它会一直只收最初那个会话的流。
        每个订阅者的待发队列有上限：页面读得太慢时丢最旧的事件保住最新，
        避免关闭不掉的连接把内存撑爆（页面会重新拉一次状态快照对账）。
        """
        with self._lock:
            only = self._session if session is None else session
            missed = [
                event for event in self._events
                if event["seq"] > after_seq and (event["session"] in ("", only))
            ]
            subscriber: queue.Queue = queue.Queue(maxsize=max(1, backlog))
            self._viewers[subscriber] = only or None
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
            entry = {**request, "id": request_id, "session": owner}
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
        return [item for item in items if item.get("session", "") in ("", session)]


DEFAULT_PROJECT = "桌面"
NEW_SESSION = "会话1"
#: 等待页面回答的墙钟上限（秒）
ASK_TIMEOUT = 1800.0


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
        """新建空白项目：在父目录（缺省为桌面的默认位置）下创建同名文件夹。"""
        title = (name or "").strip()
        if not title:
            return False
        base = (parent or "").strip()
        folder = (Path(base).expanduser() if base else default_parent(self.settings.data_dir)) / title
        existing = self.project_at(folder)
        if existing is not None:
            return self.open_project(existing.slug)
        created = self.projects.create(title, folder)
        if created is None:
            return False
        self.events.publish_global("project_changed", {})
        return self.open_project(created.slug)

    def open_folder(self, path: str) -> bool:
        """把已有文件夹登记为项目（路径由页面选择或粘贴）。"""
        target = Path((path or "").strip()).expanduser()
        if not target.is_dir():
            return False
        existing = self.project_at(target)
        if existing is not None:
            return self.open_project(existing.slug)
        created = self.projects.create(target.name, target)
        if created is None:
            return False
        self.events.publish_global("project_changed", {})
        return self.open_project(created.slug)

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

    def project_at(self, target: Path) -> Optional[Project]:
        """按工作文件夹定位已有项目（同一文件夹不重复添加）。"""
        needle = Path(target)
        return next(
            (project for project in self.projects.list() if self.projects.resolve_path(project) == needle),
            None,
        )

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

    def _busy(self, title: str) -> bool:
        """该会话是否正在执行任务（并发下"忙"要按会话判断，不能看全局）。"""
        runner = self.runners.get(title)
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
        self.runners.pop(old, None)  # 执行单元跟着换 key（此时它没有在跑）
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
        self.runners.pop(title, None)
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
        self.runners.pop(name, None)
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

    def setup_orchestrator(self) -> None:
        """重建当前会话的执行单元（重命名、删除等改动会话身份后重新绑定用）。

        切会话**不走这里**：那个路径见 :meth:`attach_session`——它复用已有执行单元，
        不会把正在跑的任务重建掉。
        """
        if self.session is None or self.store is None:
            return
        self.runners.pop(self.session.meta.title, None)
        self.attach_session(self.session)

    def attach_session(self, session: SessionData) -> SessionRunner:
        """把某个会话设为"当前查看"，返回它的执行单元（没有就建一个）。

        **这里是并发的边界**：执行单元一旦建好就只属于这个会话，切走再切回来复用同一个
        （编排器、工作线程、取消信号都还在），所以切换会话不会打断正在跑的任务。
        """
        title = session.meta.title
        session_dir = self.store.folder(session.meta.file_name or title) if self.store else Path(".")
        work_dir = session_dir / INTERNAL_DIR
        work_dir.mkdir(parents=True, exist_ok=True)
        runner = self.runners.get(title)
        if runner is None:
            runner = SessionRunner(title=title, session=session, session_dir=session_dir, work_dir=work_dir)
            runner.orchestrator = self.build_orchestrator(runner)
            runner.context_usage = self.estimate_context(runner)
            self.runners[title] = runner
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
            planner=registry.nodes["planner"],
            workers=workers,
            critic=registry.nodes["critic"],
            worker_specs=registry.specs,
            default_executor=registry.default_executor,
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
        planner = nodes.get("planner")
        runtime = getattr(runner.orchestrator, "runtime", {}) or {}
        stored = json.dumps(
            {
                "system": getattr(planner, "system_prompt", ""),
                "environment": runtime.get("environment", ""),
                "history": build_session_summary(runner.orchestrator.session) if runner.orchestrator else "",
                "tools": getattr(planner, "tool_defs", []),
            },
            ensure_ascii=False,
        )
        return {
            "characters": len(stored),
            "estimated_tokens": estimate_tokens(stored),
            "source": "estimate",
        }

    def assemble_agents(self) -> None:
        """（兼容旧调用）确保当前会话有执行单元；装配已下沉到 :meth:`build_orchestrator`。"""
        if self.session is not None and self.store is not None:
            self.attach_session(self.session)

    def refresh_runtime(self) -> None:
        """把**当前会话**的运行环境挂到它的编排器上（切换项目后调用）。

        路径按会话走：每个会话的 session_dir 不同，所以这里只刷新当前查看的那个。
        """
        runner = self.runners.get(self.events.session)
        if runner is None or runner.orchestrator is None:
            return
        runner.orchestrator.runtime = {
            "environment": build_environment(
                self.settings, self.workspace, runner.session_dir, runner.work_dir
            ),
            "workspace": str(self.workspace or ""),
            "session_dir": str(runner.session_dir),
        }

    def on_agent_event(self, session: str, kind: str, data: dict) -> None:
        """Agent 事件归属到它自己的会话，加 ``agent_`` 前缀后并入同一事件流。"""
        runner = self.runners.get(session)
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
                "files": list(task.files or []),
            }
            for task in (self.session.tasks if self.session else [])
        ]
        files = self.workspace_files()
        session_name = self.events.session
        runner = self.runners.get(session_name)
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
            "running_sessions": [name for name, item in self.runners.items() if item.running],
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
                                self.runners.get(meta.title) and self.runners[meta.title].running
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
            runner = self.runners.get(self.events.session)
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
        runner = self.runners.get(title)
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
            runner = self.runners.get(self.events.session)
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
            runner = self.runners.get(title)
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

    def resolve_file(self, name: str) -> Optional[Path]:
        """定位工作区里的产出文件（供页面下载）；不存在返回 None。"""
        if self.workspace is None or not name:
            return None
        relative = Path(name)
        if relative.is_absolute() or relative.drive or ".." in relative.parts:
            return None
        try:
            root = self.workspace.resolve()
            target = (root / relative).resolve()
            target.relative_to(root)
            return target if target.is_file() else None
        except (OSError, ValueError):
            return None

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
                    try:
                        candidate = candidate.resolve().relative_to(root)
                    except (OSError, ValueError):
                        continue
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
        copied = copy_artifacts(final, runner.work_dir, self.workspace)
        self.events.publish_for(runner.title, "artifacts", {"files": [path.name for path in copied]})

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
            try:
                if status == "failed":
                    orchestrator.fail_task(reason)
                else:
                    orchestrator.finalize_task(orchestrator.stop_content(None), status="aborted")
                return
            except Exception:  # noqa: BLE001 - 总结本身失败不该让页面卡住
                logger.debug("生成任务总结失败", exc_info=True)
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
