"""统计各会话的工具调用效率：调用数 / 轮次 / 批量占比 / with_content 使用。

用来验证"模型是否一次取齐"这类提示词改动的实际效果。

用法（项目根目录）::

    runtime\\python.exe -m peifeng_agent.report_calls
    runtime\\python.exe -m peifeng_agent.report_calls --session 会话4
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from peifeng_agent.config import DATA_ROOT

#: 批量参数名（出现任意一个是数组，就算"一次覆盖多个目标"）
BATCH_KEYS = ("paths", "queries", "urls", "commands", "items", "files", "documents", "charts")


def main() -> None:
    parser = argparse.ArgumentParser(description="统计工具调用效率")
    parser.add_argument("--session", default="", help="只看某个会话名")
    args = parser.parse_args()

    root = Path(DATA_ROOT) / "sessions"
    files = sorted(root.rglob("detail.json"))
    if not files:
        print(f"没有找到会话记录：{root}")
        return

    rows = []
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        session = path.parent.name
        if args.session and session != args.session:
            continue
        for key, task in (payload.get("tasks") or {}).items():
            calls = []
            step_rounds = 0
            steps_with_tools = 0
            for step in task.get("steps") or []:
                step_calls = [
                    call
                    for attempt in step.get("attempts") or []
                    for call in (attempt.get("result") or {}).get("tool_calls") or []
                ]
                if not step_calls:
                    continue
                calls.extend(step_calls)
                steps_with_tools += 1
                # round 在每个步骤内从 0 开始，必须按步骤分别计数再相加（不能跨步骤去重）
                step_rounds += len({call.get("round") for call in step_calls})
            if not calls:
                continue
            batch = [
                call for call in calls
                if any(isinstance(call.get("arguments", {}).get(name), list) for name in BATCH_KEYS)
            ]
            content = [call for call in calls if call.get("arguments", {}).get("with_content")]
            rows.append(
                {
                    "session": session,
                    "task": int(key),
                    "calls": len(calls),
                    "rounds": step_rounds,
                    "batch": len(batch),
                    "with_content": len(content),
                    "tools": len({call.get("name") for call in calls}),
                    # 固定往返：规划 1 次 + 每步执行/评审各 1 次
                    "fixed": 1 + steps_with_tools * 2,
                }
            )

    if not rows:
        print("没有带工具调用的任务记录。")
        return

    header = (
        f"{'会话':<10}{'任务':>4}{'调用':>6}{'工具轮':>7}{'固定':>6}{'总往返':>8}"
        f"{'批量':>6}{'占比':>7}{'每轮调用':>9}"
    )
    print(header)
    print("-" * 80)
    for row in rows:
        ratio = row["batch"] / row["calls"]
        per_round = row["calls"] / row["rounds"] if row["rounds"] else 0
        total_rounds = row["rounds"] + row["fixed"]
        print(
            f"{row['session']:<10}{row['task']:>4}{row['calls']:>6}{row['rounds']:>7}"
            f"{row['fixed']:>6}{total_rounds:>8}{ratio:>6.0%}{per_round:>9.2f}"
        )

    total_calls = sum(row["calls"] for row in rows)
    total_tool_rounds = sum(row["rounds"] for row in rows)
    total_fixed = sum(row["fixed"] for row in rows)
    total_batch = sum(row["batch"] for row in rows)
    total_rounds = total_tool_rounds + total_fixed
    print(
        f"\n合计：{len(rows)} 个任务、调用 {total_calls} 次、按每次往返 4 秒估算约 "
        f"{total_rounds * 4 / 60:.1f} 分钟"
    )
    print(f"  其中工具循环 {total_tool_rounds} 轮（{total_tool_rounds / total_rounds:.0%}）、"
          f"固定往返 {total_fixed} 轮（{total_fixed / total_rounds:.0%}）")
    print(f"  批量 {total_batch} 次（{total_batch / total_calls:.0%}）")
    print("说明：工具轮按步骤分别计数（round 在每个步骤内从 0 开始）；固定往返 = 规划 1 + 每步执行/评审各 1。")


if __name__ == "__main__":
    sys.exit(main())
