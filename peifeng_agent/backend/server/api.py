"""FastAPI 传输层：状态快照、SSE 事件流、文件下载与页面动作端点。

两个关键设计：

- **动作端点写成同步 ``def``**：FastAPI 会把同步端点丢进线程池，因此
  :class:`~peifeng_agent.backend.server.app_state.AppEntry` 与 ``TaskOrchestrator`` 那套
  "同步 + 线程 + 队列"的实现可以原样复用，不必为了异步再改一遍业务代码。
- **只有 SSE 端点必须是 ``async def``**：它用 ``asyncio.to_thread`` 把线程安全的
  ``queue.Queue`` 桥接到事件循环——既不阻塞循环，也不必改 ``EventStream``。

安全边界：不做身份认证（默认只监听 127.0.0.1）。写操作要求
``Content-Type: application/json``（由 FastAPI 的请求模型校验保证）。
"""

from __future__ import annotations

import asyncio
import json
import queue
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from peifeng_agent.backend.memory.project_store import default_parent
from peifeng_agent.backend.server import schemas
from peifeng_agent.backend.server.app_state import AppEntry
from peifeng_agent.backend.utils import capabilities, fs_browse, preferences, workspace_browse
from peifeng_agent.backend.utils.exceptions import PlanningError
from peifeng_agent.backend.workflow.presets import preset_catalog

router = APIRouter()


def get_app(request: Request) -> AppEntry:
    """取应用状态（由 :func:`create_app` 放进 ``app.state``）。"""
    return request.app.state.engine


def ok() -> schemas.OkResponse:
    return schemas.OkResponse()


def require(condition: bool, reason: str) -> None:
    """动作失败即 409：前端只需要知道"为什么没成"。"""
    if not condition:
        raise HTTPException(status_code=409, detail=reason)


# ---------------------------------------------------------------- 状态与事件流
@router.get("/api/state", response_model=schemas.Snapshot)
def read_state(app: AppEntry = Depends(get_app)) -> dict:
    """页面加载与断线重连时的状态快照。"""
    return app.snapshot()


@router.get("/api/events")
async def stream_events(
    request: Request,
    after: int = 0,
    session: str = "",
    app: AppEntry = Depends(get_app),
) -> StreamingResponse:
    """SSE 事件流：先补发缺失事件，再持续推送；带心跳，断连即退订。

    ``session`` 指定"只收哪个会话的事件"（多会话并发时两个任务同时在发事件，
    不过滤就会串台）；不给则跟随当前查看的会话。
    """
    last_id = max(after, as_int(request.headers.get("last-event-id")))
    missed, subscriber = app.events.subscribe(last_id, app.settings.event_backlog, session or None)
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


@router.get("/api/example")
def get_example(name: str = "") -> dict:
    """返回格式示例的内容（放在项目根 examples/ 下）。

    入口弹窗里展示它，用户照着一个格式写稿件就能被正确解析——
    离线排版的成败全在"输入格式对不对"，示例比文字说明有用。
    """
    from pathlib import Path as _Path

    clean = _Path((name or "").strip()).name
    if not clean:
        require(False, "需要 name（示例文件名）")
    folder = _Path(__file__).resolve().parents[3] / "examples"
    target = folder / clean
    if not target.is_file():
        require(False, f"示例不存在：{clean}")
    if target.suffix.lower() == ".xlsx":
        # 表格示例给出表头与首行，让人看清列结构
        from peifeng_agent.backend.utils.chart_kit import read_table

        rows = read_table(target)[:6]
        return {"name": clean, "text": "\n".join(" | ".join(str(c) for c in row) for row in rows)}
    return {"name": clean, "text": target.read_text(encoding="utf-8")}


@router.post("/api/offline", response_model=schemas.OfflineResultBody)
def run_offline(body: schemas.OfflineBody, app: AppEntry = Depends(get_app)) -> schemas.OfflineResultBody:
    """离线跑一个预设：**不调用任何模型**，直接按固定流程产出文件。

    给"断网 / 没有 API key / 不想花钱"的场景用。只接受**做得完整**的预设
    （读表画图、清洗汇总、按类型归档）——需要生成内容的任务没有离线版。
    """
    from peifeng_agent.backend.workflow.offline import RUNNERS, run_preset

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
    return schemas.OfflineResultBody(
        files=list(result.files), summary=result.summary, notes=list(result.notes)
    )


@router.post("/api/task", response_model=schemas.OkResponse)
def submit_task(body: schemas.SubmitTaskBody, app: AppEntry = Depends(get_app)) -> schemas.OkResponse:
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


@router.post("/api/input", response_model=schemas.OkResponse)
def submit_input(body: schemas.InputBody, app: AppEntry = Depends(get_app)) -> schemas.OkResponse:
    """回传一次提问的回答。"""
    require(app.submit_input(body.id, body.value), "该请求已失效")
    return ok()


@router.post("/api/supplement", response_model=schemas.OkResponse)
def supplement(body: schemas.SupplementBody, app: AppEntry = Depends(get_app)) -> schemas.OkResponse:
    """执行期间追加要求；非执行期拒绝，避免它留到下一个任务被消费掉。"""
    require(app.supplement(body.text), "当前没有执行中的任务，无法追加要求")
    return ok()


@router.post("/api/cancel", response_model=schemas.OkResponse)
def cancel(app: AppEntry = Depends(get_app)) -> schemas.OkResponse:
    """中止当前任务（在步骤边界或节点等待期间生效）。"""
    require(app.cancel(), "当前没有执行中的任务")
    return ok()


# ---------------------------------------------------------------- 项目
@router.post("/api/project/open", response_model=schemas.OkResponse)
def open_project(body: schemas.OpenProjectBody, app: AppEntry = Depends(get_app)) -> schemas.OkResponse:
    require(app.open_project(body.slug, body.session), "该项目无法打开（可能正在执行任务）")
    return ok()


@router.post("/api/project/create", response_model=schemas.OkResponse)
def create_project(body: schemas.CreateProjectBody, app: AppEntry = Depends(get_app)) -> schemas.OkResponse:
    require(app.create_project(body.name, body.parent), "无法新建项目（项目名不能为空）")
    return ok()


@router.post("/api/project/open-folder", response_model=schemas.OkResponse)
def open_folder(body: schemas.PathBody, app: AppEntry = Depends(get_app)) -> schemas.OkResponse:
    """使用一个已有文件夹作为项目（路径由页面选择或粘贴）。"""
    require(app.open_folder(body.path), "该文件夹不可用")
    return ok()


@router.post("/api/project/rename", response_model=schemas.OkResponse)
def rename_project(body: schemas.RenameProjectBody, app: AppEntry = Depends(get_app)) -> schemas.OkResponse:
    require(app.rename_project(body.slug, body.name), "无法重命名项目（名称不能为空）")
    return ok()


@router.post("/api/project/remove", response_model=schemas.OkResponse)
def remove_project(body: schemas.RemoveProjectBody, app: AppEntry = Depends(get_app)) -> schemas.OkResponse:
    require(app.remove_project(body.slug), "该项目无法移除（可能正在执行任务）")
    return ok()


# ---------------------------------------------------------------- 会话
@router.post("/api/session/open", response_model=schemas.OkResponse)
def open_session(body: schemas.SessionBody, app: AppEntry = Depends(get_app)) -> schemas.OkResponse:
    """切换会话（任务执行中也可以切；每个会话各有自己的执行单元）。"""
    require(app.open_session(body.name, body.slug), "无法打开该会话（可能已被归档或删除）")
    return ok()


@router.post("/api/session/create", response_model=schemas.OkResponse)
def create_session(body: schemas.SessionBody, app: AppEntry = Depends(get_app)) -> schemas.OkResponse:
    require(app.create_session(body.name, body.slug), "无法新建会话")
    return ok()


@router.post("/api/session/rename", response_model=schemas.OkResponse)
def rename_session(body: schemas.RenameSessionBody, app: AppEntry = Depends(get_app)) -> schemas.OkResponse:
    require(app.rename_session(body.old, body.new, body.slug), "无法重命名会话（名称重复、为空或含路径分隔符）")
    return ok()


@router.post("/api/session/archive", response_model=schemas.OkResponse)
def archive_session(body: schemas.SessionBody, app: AppEntry = Depends(get_app)) -> schemas.OkResponse:
    """归档：目录移入 ``archive/``，数据保留但不再进会话列表。"""
    require(app.archive_session(body.name, body.slug), "无法归档该会话")
    return ok()


@router.post("/api/session/restore", response_model=schemas.OkResponse)
def restore_session(body: schemas.SessionBody, app: AppEntry = Depends(get_app)) -> schemas.OkResponse:
    """把归档会话移回会话列表（数据原样）。"""
    require(app.restore_session(body.name, body.slug), "无法恢复该会话")
    return ok()


@router.post("/api/session/delete", response_model=schemas.OkResponse)
def delete_session(body: schemas.DeleteSessionBody, app: AppEntry = Depends(get_app)) -> schemas.OkResponse:
    """彻底删除会话目录，不可恢复——页面对这个动作有二次确认。"""
    require(app.delete_session(body.name, body.slug, body.archived), "无法删除该会话")
    return ok()


# ---------------------------------------------------------------- 能力管理
@router.get("/api/capabilities")
def list_capabilities(app: AppEntry = Depends(get_app)) -> dict:
    """看现在有哪些角色，各自的工具与技能（含来源路径与是否共享）。"""
    return {"roles": capabilities.list_capabilities(app.settings.agent_dir)}


@router.post("/api/capabilities/executor", response_model=schemas.OkResponse)
def create_executor(body: schemas.CreateExecutorBody, app: AppEntry = Depends(get_app)) -> schemas.OkResponse:
    """新建worker：生成 spec.py + prompt.md（重启后生效）。"""
    try:
        capabilities.create_executor(app.settings.agent_dir, body.name, body.description, body.prompt)
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return ok()


@router.delete("/api/capabilities/executor/{name}", response_model=schemas.OkResponse)
def delete_executor(name: str, app: AppEntry = Depends(get_app)) -> schemas.OkResponse:
    """删除worker目录；核心角色（planner/critic）拒绝删除。"""
    try:
        capabilities.delete_executor(app.settings.agent_dir, name)
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return ok()


@router.post("/api/capabilities/tool", response_model=schemas.OkResponse)
def upload_tool(body: schemas.UploadToolBody, app: AppEntry = Depends(get_app)) -> schemas.OkResponse:
    """上传工具：先解析校验（元数据 + 同名函数）再落盘，坏文件不进目录。"""
    try:
        capabilities.upload_tool(app.settings.agent_dir, body.agent, body.content, body.filename)
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return ok()


@router.delete("/api/capabilities/tool/{agent}/{name}", response_model=schemas.OkResponse)
def delete_tool(agent: str, name: str, app: AppEntry = Depends(get_app)) -> schemas.OkResponse:
    """删除角色自己目录下的工具（共享工具不走这个入口）。"""
    try:
        capabilities.delete_tool(app.settings.agent_dir, agent, name)
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return ok()


@router.post("/api/capabilities/skill", response_model=schemas.OkResponse)
def upload_skill(body: schemas.UploadSkillBody, app: AppEntry = Depends(get_app)) -> schemas.OkResponse:
    """上传技能：frontmatter 的 name 必须与技能目录名一致。"""
    try:
        capabilities.upload_skill(app.settings.agent_dir, body.agent, body.content, body.name)
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return ok()


@router.delete("/api/capabilities/skill/{agent}/{name}", response_model=schemas.OkResponse)
def delete_skill(agent: str, name: str, app: AppEntry = Depends(get_app)) -> schemas.OkResponse:
    try:
        capabilities.delete_skill(app.settings.agent_dir, agent, name)
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return ok()


@router.get("/api/preferences")
def read_preferences(app: AppEntry = Depends(get_app)) -> dict:
    """读用户个性化规则（追加进 system prompt 的那一段）。"""
    return preferences.load_preferences(app.settings.data_dir)


@router.post("/api/preferences", response_model=schemas.OkResponse)
def write_preferences(body: schemas.PreferencesBody, app: AppEntry = Depends(get_app)) -> schemas.OkResponse:
    """保存个性化规则：重启生效；超长直接拒绝（不静默截断）。"""
    try:
        preferences.save_preferences(app.settings.data_dir, body.system_append)
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return ok()


# ---------------------------------------------------------------- 目录浏览
@router.get("/api/workspace/entries", response_model=schemas.WorkspaceListing)
def list_workspace_entries(path: str = "", app: AppEntry = Depends(get_app)) -> dict:
    """列出**项目工作目录**里某一层的子目录与文件，供快捷模板点选素材。

    路径一律相对项目目录（worker的工具就是按相对路径读的）；``../`` 越界返回 404。
    与 ``/api/fs/dirs`` 的分工：那个用来选"项目放在哪"（整块磁盘），这个只浏览项目内部。
    """
    listing = workspace_browse.list_workspace(app.workspace, path)
    if listing is None:
        raise HTTPException(status_code=404, detail="该目录不可用（或不在项目目录内）")
    return listing


@router.get("/api/fs/dirs", response_model=schemas.DirListing)
def list_dirs(path: str = "", app: AppEntry = Depends(get_app)) -> dict:
    """列出某个目录下的子目录，供页面"选择文件夹"（服务端不弹系统选择框）。

    ``path`` 为空时用默认位置（当前用户桌面）；不可用的路径返回 404，页面据此提示。
    """
    listing = fs_browse.list_dirs(path, default=default_parent(app.settings.data_dir))
    if listing is None:
        raise HTTPException(status_code=404, detail="该文件夹不可用")
    return listing


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
