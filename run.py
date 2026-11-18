"""培风入口：起本地服务，用浏览器访问。

    runtime\\python.exe run.py            # 正常启动（首次会问 API key）
    runtime\\python.exe run.py --offline <预设id> --set key=value --dir <目录>

后端是 ``agent/server``（FastAPI，页面与 ``/api/*`` 接口）；界面是 ``frontend/dist``。
服务不做身份认证。
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent

#: 开浏览器前的等待时间（秒）：确保端口已经在监听
OPEN_DELAY = 0.8
#: 端口被占用时最多向后顺延多少个
PORT_TRIES = 10


def prepare() -> None:
    """固定运行环境：把项目根放进 sys.path，并把内置运行时排到 PATH 前面。"""
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    runtime = ROOT / "runtime"
    os.environ.pop("PYTHONHOME", None)
    os.environ.pop("PYTHONPATH", None)
    os.environ["PYTHONNOUSERSITE"] = "1"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PATH"] = os.pathsep.join([str(runtime), str(runtime / "Scripts"), os.environ.get("PATH", "")])
    data = ROOT / "data"
    data.mkdir(parents=True, exist_ok=True)
    os.environ["MPLCONFIGDIR"] = str(data / "cache" / "matplotlib")
    os.environ["PEIFENG_PORTABLE"] = "1"


def configure_logging(level: str) -> None:
    """按配置的级别初始化日志。"""
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


def run_offline(argv: list[str]) -> None:
    """离线保底：预设任务的流程是确定的，直接调工具跑，不碰 Mentor/Worker 的对话循环。"""
    from backend.scripts.offline import PRESETS, RUNNERS, run_preset

    position = argv.index("--offline")
    rest = argv[position + 1:]
    preset_id = next((item for item in rest if not item.startswith("--")), "")
    if not preset_id or preset_id not in RUNNERS:
        print("离线预设：")
        for preset in PRESETS:
            if preset["id"] in RUNNERS:
                print(f"  {preset['id']:<14} {preset['name']}")
                for spec in preset["fields"]:
                    default = spec.get("default") or spec.get("fallback") or ""
                    flag = "必填" if spec.get("required") else "可选"
                    print(f"      --set {spec['key']}=…  （{flag}）{default}")
        sys.exit(0 if not preset_id else 2)

    values: dict[str, str] = {}
    folder = ROOT
    index = 0
    while index < len(rest):
        if rest[index] == "--set" and index + 1 < len(rest):
            pair = rest[index + 1]
            key, value = pair.split("=", 1)
            values[key.strip()] = value.strip()
            index += 2
            continue
        if rest[index] == "--dir" and index + 1 < len(rest):
            folder = Path(rest[index + 1]).resolve()
            index += 2
            continue
        index += 1

    folder.mkdir(parents=True, exist_ok=True)
    print(f"离线执行：{preset_id}（不联网、不调用任何模型）")
    print(f"工作目录：{folder}")
    result = run_preset(preset_id, values, folder)
    print(f"\n{result.summary}")
    for note in result.notes:
        print(f"注意：{note}")
    print("\n产出：")
    for item in result.files:
        print(f"  {item}")
    sys.exit(0)


def ask_api_key() -> None:
    """首次使用：弹窗问 API key，写进项目根的 ``.env``（密钥永不进 YAML）。"""
    from config import ENV_FILE, load_settings

    if load_settings().api_key:
        return
    import tkinter as tk
    from tkinter import simpledialog

    window = tk.Tk()
    window.withdraw()
    key = simpledialog.askstring("培风 · 首次使用", "请输入自己的 DeepSeek API key：", show="*", parent=window)
    window.destroy()
    if not key or not key.strip():
        return
    value = key.strip()
    if "\n" in value or "\r" in value:
        raise ValueError("API key 不能包含换行")
    previous = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
    ENV_FILE.write_text(previous.rstrip() + f"\napi_key={value}\n", encoding="utf-8")


def serve() -> None:
    """启动服务：打印访问地址（可选自动开浏览器），按 Ctrl+C 退出。"""
    import uvicorn

    from config import load_settings
    from backend.server.server import create_app

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
            access_log=False,
        )
    )
    try:
        server.run()
    except KeyboardInterrupt:
        print("\n正在退出…")


def main() -> None:
    prepare()
    argv = sys.argv[1:]
    if "--offline" in argv:
        run_offline(sys.argv)
    ask_api_key()
    serve()


if __name__ == "__main__":
    main()
