"""Portable entry point; no system Python or developer configuration is used."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> None:
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    runtime = ROOT / "runtime"
    os.environ.pop("PYTHONHOME", None)
    os.environ.pop("PYTHONPATH", None)
    os.environ["PYTHONNOUSERSITE"] = "1"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PATH"] = os.pathsep.join([
        str(runtime), str(runtime / "Scripts"), os.environ.get("PATH", ""),
    ])
    data = ROOT / "peifeng_agent/backend/data"
    data.mkdir(parents=True, exist_ok=True)
    os.environ["MPLCONFIGDIR"] = str(data / "cache/matplotlib")
    os.environ["PEIFENG_PORTABLE"] = "1"
    if "--check" in sys.argv:
        import ssl
        import tkinter

        from peifeng_agent.backend.workflow.assembly import build_registry
        from peifeng_agent.config import Settings

        registry = build_registry(Settings())
        if registry.errors:
            raise RuntimeError(registry.errors)
        print(f"Portable OK: {sys.executable}; agents={len(registry.nodes)}; SSL={ssl.OPENSSL_VERSION}; Tk={tkinter.TkVersion}")
        return
    from peifeng_agent.config import ENV_FILE, load_settings

    if not load_settings().api_key:
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
    from peifeng_agent.start import main as start_server

    start_server()


if __name__ == "__main__":
    main()
