"""Search public webpages through the DeepSeek Search API.

支持一次搜多个词（``queries``）：并发请求、按 query 分段返回。这是把"检索 N 个角度"
从 N 轮压成 1 轮的关键——模型一轮只发一个调用时，轮次就是这么省下来的。
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from peifeng_agent.backend.utils.deepseek_web import request_web
from peifeng_agent.backend.utils.text import as_text_list

#: 一次最多搜几个词：0 = 不限（请求按下面的并发数排队）
MAX_BATCH = 0
#: 同时在飞的请求数。**实测**（probe_web_limit）：Search 端点并发 64、共 128 个请求
#: 全部成功、0 个 429，说明上限很高；这里取 16 留足余量（30 个目标两轮内跑完）
MAX_WORKERS = 16
#: 单个关键词的结果上限：6 个词 × 10 条 × 长摘要实测一次能到 9 万字符，必须截住
MAX_QUERY_CHARS = 8_000
#: 整批返回的总字数上限
MAX_TOTAL = 24_000

information = {
    "name": "search_web",
    "description": (
        "搜索实时网页，返回标题、链接、摘要、日期。"
        "用于找资料、核对事实；结果为空表示无匹配，不能据此编造。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string", "minLength": 1, "maxLength": 128,
                "description": "单个搜索关键词（与 queries 二选一）；支持 site:/after:/before:/lang: 语法",
            },
            "queries": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 128},
                "description": f"一次搜多个关键词（与 query 二选一）：结果按 query 分段；"
                               f"请求按 {MAX_WORKERS} 个并发排队（实测 Search 端点并发 64 仍无限流）",
            },
            "count": {
                "type": "integer", "minimum": 1, "maximum": 50,
                "description": "每个关键词返回多少条结果（默认 5，最多 50）；要交叉核对时调大到 10",
            },
            "need_content": {
                "type": "boolean",
                "description": "true 时请服务端同时返回结果正文（默认 false 只给标题+摘要）",
            },
            "ori_question": {
                "type": "string", "minLength": 1, "maxLength": 128,
                "description": "原始问题（可选）：帮助服务端按意图排序，一般填用户原话",
            },
        },
        "required": [],
    },
}


def search_web(
    query: str = "",
    count: int = 5,
    need_content: bool = False,
    ori_question: str | None = None,
    queries: list | None = None,
) -> str:
    """搜一个词或一批词；单次返回原始结果，批量返回按 query 分段的数组。"""
    batch = as_text_list(queries, "queries", MAX_BATCH)
    if batch:
        return search_many(batch, count, need_content, ori_question)
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 128:
        raise RuntimeError("query 必须是 1～128 字符的非空字符串（或用 queries 一次搜多个）")
    return json.dumps(fetch_one(query, count, need_content, ori_question), ensure_ascii=False, indent=2)


def search_many(queries: list[str], count: int, need_content: bool, ori_question: str | None) -> str:
    """并发搜多个词：单个失败只记在它自己那一段，不影响整批。"""
    def one(keyword: str) -> dict:
        try:
            payload = fetch_one(keyword, count, need_content, ori_question)
            return {"query": keyword, "results": payload.get("results") or []}
        except Exception as error:  # noqa: BLE001 - 一个词失败不该让整批作废
            return {"query": keyword, "error": str(error)}

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(queries)), thread_name_prefix="search") as pool:
        results = list(pool.map(one, queries))
    return json.dumps({"queries": queries, "results": trim(results)}, ensure_ascii=False, indent=2)


def trim(groups: list[dict]) -> list[dict]:
    """截断批量搜索结果：单关键词条目数与整批总量都设上限。

    实测 6 个关键词 × 10 条结果一次能返回 9 万字符（约 3 万 token），一次就把上下文吃掉
    一大块；而检索结果本来只需要挑几条去核对。截断只影响交给模型的这一份。
    """
    total = 0
    trimmed: list[dict] = []
    for group in groups:
        items = group.get("results")
        if not isinstance(items, list):
            trimmed.append(group)  # 出错的那一段原样保留（只有一句 error）
            continue
        kept: list[dict] = []
        size = 0
        for item in items:
            text = json.dumps(item, ensure_ascii=False)
            if size + len(text) > MAX_QUERY_CHARS or total + size + len(text) > MAX_TOTAL:
                break
            kept.append(item)
            size += len(text)
        total += size
        dropped = len(items) - len(kept)
        entry = {**group, "results": kept}
        if dropped:
            entry["note"] = f"另有 {dropped} 条结果未返回（本批已达字数上限）：需要更多请单独搜这一个关键词"
        trimmed.append(entry)
    return trimmed


def fetch_one(query: str, count: int, need_content: bool, ori_question: str | None) -> dict:
    """搜一个词，返回服务端原始 payload（已校验含 results）。"""
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 128:
        raise RuntimeError("query 必须是 1～128 字符的非空字符串")
    if type(count) is not int or not 1 <= count <= 50:
        raise RuntimeError("count 必须是 1～50 的整数")
    if type(need_content) is not bool:
        raise RuntimeError("need_content 必须是布尔值")
    payload = {"query": query.strip(), "count": count, "need_content": need_content}
    if ori_question is not None:
        if not isinstance(ori_question, str) or not 1 <= len(ori_question.strip()) <= 128:
            raise RuntimeError("ori_question 必须是 1～128 字符的非空字符串")
        payload["ori_question"] = ori_question.strip()
    result = request_web("search", payload)
    if not isinstance(result.get("results"), list):
        raise RuntimeError("Search 响应缺少 results 数组：" + json.dumps(result, ensure_ascii=False))
    return result
