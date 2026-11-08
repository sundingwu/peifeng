"""培风 Agent 启动入口：起本地服务，用浏览器访问。

后端（``peifeng_agent/backend``）是 FastAPI 应用：只提供页面与 ``/api/*`` 接口。
界面是 ``peifeng_agent/frontend`` 的 Vite 构建产物（``frontend/dist``，改前端后先
``npm run build``）。所以只有这一个启动文件，不需要分别起前后端。

    uv run peifeng
    uv run python -m peifeng_agent.start

本机浏览器直接访问打印出来的地址即可；服务**不做身份认证**，要让局域网里的其它机器访问就
把 ``.env`` 里的 ``host`` 改成 ``0.0.0.0``（那等于把接口交给同网段，只在可信网络里这么做）。
"""

from __future__ import annotations

import logging
import socket
import threading
import webbrowser

import uvicorn

from peifeng_agent.backend.server.main import create_app
from peifeng_agent.config import load_settings

#: 开浏览器前的等待时间（秒）：确保端口已经在监听
OPEN_DELAY = 0.8
#: 端口被占用时最多向后顺延多少个
PORT_TRIES = 10


def configure_logging(level: str) -> None:
    """按配置的级别初始化日志（默认 INFO；写成 DEBUG 才显示访问日志与内部细节）。"""
    logging.basicConfig(
        level=getattr(logging, str(level or "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def free_address(host: str, port: int) -> tuple[str, int]:
    """挑一个能监听的地址：端口被占用时向后顺延（uvicorn 自己不会顺延）。"""
    for offset in range(PORT_TRIES):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((host, port + offset))
            except OSError:
                continue
            return host, port + offset
    raise OSError(f"端口 {port}~{port + PORT_TRIES - 1} 都无法监听（可能都被占用）")


def main() -> None:
    """启动服务：打印访问地址（可选自动开浏览器），按 Ctrl+C 退出。"""
    settings = load_settings()
    configure_logging(settings.log_level)
    app = create_app(settings)
    host, port = free_address(settings.host, settings.port)
    url = f"http://{host}:{port}/"

    print(f"培风 Agent 已启动，请在浏览器打开：{url}", flush=True)
    print(f"（监听 {host}:{port}，无身份认证；按 Ctrl+C 退出）", flush=True)
    if settings.open_browser:
        threading.Timer(OPEN_DELAY, webbrowser.open, args=(url,)).start()

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level=str(settings.log_level or "INFO").lower(),
            access_log=False,  # 访问日志交给应用自己的 logger
        )
    )
    try:
        server.run()
    except KeyboardInterrupt:
        print("\n正在退出…")
    # 收尾由 lifespan 负责：它调用 AppEntry.shutdown()，等在跑的任务把会话落盘


if __name__ == "__main__":
    main()
