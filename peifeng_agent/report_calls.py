"""统计各会话的工具调用效率：调用数 / 轮次 / 批量占比。

用来验证"模型是否一次取齐"这类提示词改动的实际效果。

用法（项目根目录）::

    runtime\\python.exe -m peifeng_agent.report_calls
    runtime\\python.exe -m peifeng_agent.report_calls --session 会话4

数据来源是会话目录下的 ``detail/``：每步一个 ``task{任务号}_step{步骤号}.json``，
这一步的全部尝试与工具调用都在里面。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from peifeng_agent.config import DATA_ROOT


def tool_calls_of(step: dict) -> list[dict]:
    """把一步里所有尝试的工具调用摊平。"""
    return [
        call
        for attempt in step.get("attempts") or []
        for call in (attempt.get("result") or {}).get("tool_calls") or []
    ]


def is_batch(call: dict) -> bool:
    """这次调用是否一次覆盖了多个目标。

    read 现在只有一个 ``targets`` 数组；其余工具的批量参数仍是各自的名字。
    """
    arguments = call.get("arguments") or {}
    if isinstance(arguments.get("targets"), list) and len(arguments["targets"]) > 1:
        return True
    return any(
        isinstance(arguments.get(name), list) and len(arguments[name]) > 1
        for name in ("queries", "urls", "commands", "items", "files", "slides", "sheets",
                     "blocks", "charts", "paths")
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="统计工具调用效率")
    parser.add_argument("--session", default="", help="只看某个会话名")
    args = parser.parse_args()

    root = Path(DATA_ROOT) / "sessions"
    sessions = sorted(p.parent for p in root.rglob("summary.json"))
    if args.session:
        sessions = [p for p in sessions if args.session in p.name]
    if not sessions:
        print(f"没有找到会话记录：{root}")
        return

    rows = []
    for session_dir in sessions:
        session = session_dir.name
        detail_dir = session_dir / "detail"
        if not detail_dir.is_dir():
            continue

        #: 任务号 → 该任务的每一步（每步是那一步的全部工具调用）
        by_task: dict[int, list[list[dict]]] = {}
        for path in sorted(detail_dir.glob("task*_step*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            seq = payload.get("task_seq")
            if not isinstance(seq, int):
                continue
            by_task.setdefault(seq, []).append(tool_calls_of(payload.get("step") or {}))

        for seq, steps in sorted(by_task.items()):
            calls = [call for step in steps for call in step]
            if not calls:
                continue
            # round 在每个步骤内从 0 开始，必须按步骤分别计数再相加
            rounds = sum(len({call.get("round") for call in step}) for step in steps)
            batch = [call for call in calls if is_batch(call)]
            rows.append(
                {
                    "session": session,
                    "task": seq,
                    "calls": len(calls),
                    "rounds": rounds,
                    "batch": len(batch),
                    "tools": len({call.get("name") for call in calls}),
                    # 固定往返：规划 1 次 + 每步执行/评审各 1 次
                    "fixed": 1 + len(steps) * 2,
                }
            )

    if not rows:
        print("没有带工具调用的任务记录。")
        return

    header = (
        f"{'会话':<14}{'任务':>4}{'调用':>6}{'工具轮':>7}{'固定':>6}{'总往返':>8}"
        f"{'批量':>6}{'占比':>7}{'每轮调用':>9}"
    )
    print(header)
    print("-" * 90)
    for row in rows:
        ratio = row["batch"] / row["calls"]
        per_round = row["calls"] / row["rounds"] if row["rounds"] else 0
        total = row["rounds"] + row["fixed"]
        print(
            f"{row['session']:<14}{row['task']:>4}{row['calls']:>6}{row['rounds']:>7}"
            f"{row['fixed']:>6}{total:>8}{row['batch']:>6}{ratio:>6.0%}{per_round:>9.2f}"
        )

    calls = sum(row["calls"] for row in rows)
    rounds = sum(row["rounds"] for row in rows)
    fixed = sum(row["fixed"] for row in rows)
    batch = sum(row["batch"] for row in rows)
    total = rounds + fixed
    print(f"\n合计：{len(rows)} 个任务、调用 {calls} 次、按每次往返 4 秒估算约 {total * 4 / 60:.1f} 分钟")
    print(f"  工具循环 {rounds} 轮（{rounds / total:.0%}）、固定往返 {fixed} 轮（{fixed / total:.0%}）")
    print(f"  批量 {batch} 次（{batch / calls:.0%}）")
    print("说明：工具轮按步骤分别计数（round 在每个步骤内从 0 开始）；固定往返 = 规划 1 + 每步执行/评审各 1。")


if __name__ == "__main__":
    main()
