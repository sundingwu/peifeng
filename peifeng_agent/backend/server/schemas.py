"""HTTP 契约：FastAPI 用它生成 OpenAPI，前端再据此生成 TypeScript 类型。

字段名与前端一一对应——这里改了，前端在编译期就会报错。所有动作端点返回
:class:`OkResponse`，失败统一为 :class:`ErrorResponse`（由 main 的异常处理器产出）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SubmitTaskBody(BaseModel):
    """提交任务（计划直接执行，无需确认）。

    ``preset`` 非空时走**快捷模板**：用模板 + ``values`` 生成现成计划，跳过 LLM 规划；
    为空时 ``text`` 就是任务描述，照旧交给 Planner。
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
    name: str = ""
    parent: str = ""


class PathBody(BaseModel):
    """使用现有文件夹：路径由页面内置的目录浏览器选择，也可以直接粘贴。"""

    path: str = ""


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


class ErrorResponse(BaseModel):
    """动作失败：前端只需看 ok 与 error。"""

    ok: bool = False
    error: str = ""


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
    #: planner 整合后的完整需求
    requirement: str = ""
    #: 用户实际输入的那句话（历史列表显示它）
    original_request: str = ""
    status: str = ""
    summary: str = ""
    content: str = ""
    files: list[str] = Field(default_factory=list)


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


class DirEntry(BaseModel):
    """目录浏览器里的一个位置（子目录 / 盘符 / 常用位置）。"""

    name: str = ""
    path: str = ""


class DirListing(BaseModel):
    """目录浏览器的一屏：当前目录 + 它的子目录，以及可跳转的盘符与常用位置。"""

    path: str = ""
    #: 上一级；已在根目录时为空串（页面据此禁用「上一级」）
    parent: str = ""
    entries: list[DirEntry] = Field(default_factory=list)
    roots: list[DirEntry] = Field(default_factory=list)
    shortcuts: list[DirEntry] = Field(default_factory=list)


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


class CreateExecutorBody(BaseModel):
    """新建worker：目录名即worker名，职责描述是 Planner 的分派依据。"""

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


class PreferencesBody(BaseModel):
    """用户个性化规则（追加进 system prompt 的规则层）。"""

    system_append: str = ""
