"""探测 DeepSeek Search/Fetch 的并发阈值，并可把结果写回工具的 ``MAX_WORKERS``。

**为什么要实测**：DeepSeek 官方只公开了 Chat Completions 的并发上限
（deepseek-flash 2500 / v4-pro 500，账号级、超限返回 429），**没有公开 Search / Fetch
端点的 QPS 或并发数字**。所以工具里那个 ``MAX_WORKERS = 8`` 是保守估计，不是测出来的。

用法（在本项目根目录下）::

    runtime\\python.exe -m peifeng_agent.probe_web_limit            # 只报告，不改代码
    runtime\\python.exe -m peifeng_agent.probe_web_limit --apply    # 把测得的 50% 写进工具常量

**会真实调用 API 并产生少量费用**（每个并发档位发 ``并发 × 轮数`` 个请求）。
遇到 429 立即停止并报告上一档为"安全并发"。
"""
from __future__ import annotations

import argparse
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from peifeng_agent.backend.utils.deepseek_web import request_web
from peifeng_agent.config import load_settings

#: 起始并发与上限（按倍数递增）
START = 8
CEILING = 32
#: 每个档位发几轮（每轮 = 并发个请求）
ROUNDS = 1
#: 目标文件：把结果写进这两个工具的 MAX_WORKERS
TARGETS = (
    Path(__file__).parent / "backend/agent/executor/web_search/tool/search_web.py",
    Path(__file__).parent / "backend/agent/executor/web_search/tool/fetch_web.py",
)


def main() -> None:
    parser = argparse.ArgumentParser(description="探测 Search/Fetch 的并发阈值")
    parser.add_argument("--apply", action="store_true", help="把安全并发的一半写进工具常量")
    parser.add_argument("--endpoint", default="search", choices=["search", "fetch"], help="探测哪个端点")
    args = parser.parse_args()

    settings = load_settings()
    if not settings.api_key or not settings.api_key.strip():
        print("需要先在 peifeng_agent/.env 里配好 api_key 才能实测。")
        raise SystemExit(1)

    limit = probe(args.endpoint)
    if limit is None:
        print("\n所有档位都没有触发 429：上限至少 " + str(START))
        return
    safe = max(START, limit // 2)
    print(f"\n建议把 MAX_WORKERS 设为 {safe}（测得无 429 的最大并发 {limit}，留一半余量）")
    if args.apply:
        applied = write_workers(safe)
        print("已写入：" + "、".join(applied))


def probe(endpoint: str) -> int | None:
    """逐档加压；返回"最后一个没有 429 的并发数"，全都没 429 时返回 None。"""
    concurrency = START
    last_ok = None
    while concurrency <= CEILING:
        total = concurrency * ROUNDS
        print(f"并发 {concurrency:2d}：发 {total} 个 {endpoint} 请求 …", end="", flush=True)
        ok, limited, other = fire(endpoint, concurrency, total)
        print(f" 成功 {ok}，429 {limited}，其它错误 {other}")
        if limited:
            print(f"→ 并发 {concurrency} 触发限流，上一档 {last_ok} 是安全并发")
            return last_ok
        if other and ok == 0:
            print("→ 全部失败且不是 429，可能是网络/额度问题，停止探测")
            return last_ok
        last_ok = concurrency
        concurrency *= 2
    return None


def fire(endpoint: str, concurrency: int, total: int) -> tuple[int, int, int]:
    """并发发 ``total`` 个请求，返回（成功, 429, 其它错误）计数。"""
    ok = limited = other = 0

    def one(index: int) -> str:
        try:
            if endpoint == "search":
                request_web("search", {"query": f"peifeng 并发探测 {index}", "count": 1})
            else:
                request_web("fetch", {"url": f"https://example.com/?probe={index}"})
            return "ok"
        except RuntimeError as error:
            return "limited" if ("429" in str(error) or "rate" in str(error).lower()) else "other"

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for result in pool.map(one, range(total)):
            if result == "ok":
                ok += 1
            elif result == "limited":
                limited += 1
            else:
                other += 1
    return ok, limited, other


def write_workers(value: int) -> list[str]:
    """把 value 写进两个工具的 MAX_WORKERS（幂等）。"""
    done = []
    for path in TARGETS:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        updated = re.sub(r"^MAX_WORKERS = \d+$", f"MAX_WORKERS = {value}", text, flags=re.M)
        if updated != text:
            path.write_text(updated, encoding="utf-8", newline="\r\n")
            done.append(path.name)
    return done


if __name__ == "__main__":
    sys.exit(main())
