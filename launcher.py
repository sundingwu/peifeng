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
    if "--offline" in sys.argv:
        # 离线保底：预设任务的流程是确定的，直接调工具跑，不碰 planner/worker 的对话循环。
        # 用法：launcher.py --offline <预设id> [--set key=value ...] [--dir <工作目录>]
        from peifeng_agent.backend.workflow.offline import RUNNERS, run_preset
        from peifeng_agent.backend.workflow.presets import PRESETS

        args = [item for item in sys.argv[1:]]
        position = args.index("--offline")
        rest = args[position + 1:]
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
                if "=" not in pair:
                    print(f"--set 需要 key=value（收到：{pair}）")
                    sys.exit(2)
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
        try:
            result = run_preset(preset_id, values, folder)
        except Exception as error:  # noqa: BLE001 - 离线入口要把原因直接给人看
            print(f"失败：{error}")
            sys.exit(1)
        print(f"\n{result.summary}")
        for note in result.notes:
            print(f"注意：{note}")
        print("\n产出：")
        for item in result.files:
            print(f"  {item}")
        sys.exit(0)

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
