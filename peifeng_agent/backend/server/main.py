"""FastAPI 应用工厂：装配应用状态、挂路由、托管前端产物、统一错误形状。

前端产物只有 ``frontend/dist``（Vite 构建结果）：没构建过时 ``/`` 返回 503 并提示先
``npm run build``，``/api/*`` 不受影响。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from peifeng_agent.backend.server import api
from peifeng_agent.backend.server.app_state import AppEntry
from peifeng_agent.config import Settings

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
        description="基于 Planner/Executor/Critic 线性协作的本地办公文档助手",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.engine = engine
    app.include_router(api.router)
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
                detail="前端尚未构建：先执行 cd peifeng_agent/frontend && npm run build",
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
