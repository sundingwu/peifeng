"""抓取网页正文：普通页面走 DeepSeek Fetch，PDF 走本地下载 + 文本提取。

- 普通页面：DeepSeek Fetch API 返回 Markdown
- **PDF**（``.pdf`` 链接，或 Fetch 取不到正文的地址）：本地下载后逐页提取文字——
  培养方案、招生简章、通知公告这类关键材料基本都是 PDF，之前只能让模型自己 ``curl``，
  它还会用 ``type`` 把二进制刷进上下文
- 支持一次抓多个 URL（``urls``）：并发请求、逐个返回
"""
from __future__ import annotations

import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from peifeng_agent.backend.utils.deepseek_web import request_web
from peifeng_agent.backend.utils.text import as_text_list, pdf_lines

#: 一次最多抓几个页面：0 = 不限（请求按下面的并发数排队）
MAX_BATCH = 0
#: 同时在飞的请求数。**实测**（probe_web_limit）：Fetch 端点并发 16 全部成功；
#: 并发 32 时 32 个里有 5 个返回 429 —— 所以上限取安全值的一半
MAX_WORKERS = 8
#: 单页正文上限：批量抓 8 个长网页能到几十万字，必须在返回前截住
MAX_CONTENT = 30_000
#: 整批返回的总字数上限
MAX_TOTAL = 60_000
#: PDF 下载体积上限（字节）：培养方案通常几 MB，超过就不是正常文档了
MAX_PDF_BYTES = 40 * 1024 * 1024
#: PDF 下载超时（秒）
PDF_TIMEOUT = 60

information = {
    "name": "fetch_web",
    "description": (
        "抓取 HTTP/HTTPS 内容，返回标题/正文与原始 URL。普通网页走 DeepSeek Fetch；"
        "**PDF 会自动下载并提取文字**（不用自己 curl，也不要用 type 打印二进制）。"
        "用于核对搜索结果中的关键事实；访问失败不表示已验证正文。"
        f"要核对多个页面时用 urls 一次抓多个（个数不限，内部按 {MAX_WORKERS} 个并发排队），不要一个网址一轮"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string", "maxLength": 8192,
                "description": "单个网址（与 urls 二选一）；.pdf 会自动下载并提取文字",
            },
            "urls": {
                "type": "array",
                "items": {"type": "string", "maxLength": 8192},
                "description": f"一次抓多个网址（与 url 二选一）：结果按 url 分段；"
                               f"请求按 {MAX_WORKERS} 个并发排队（实测 16 并发安全、32 会触发 429）",
            },
        },
        "required": [],
    },
}


def fetch_web(url: str = "", urls: list | None = None) -> str:
    """抓一个页面或一批页面；单个返回原形状，批量返回按 url 分段的数组。"""
    batch = as_text_list(urls, "urls", MAX_BATCH)
    if batch:
        return fetch_many(batch)
    if not isinstance(url, str) or not 1 <= len(url) <= 8192:
        raise RuntimeError("url 必须是不超过 8192 字符的地址（或用 urls 一次抓多个）")
    return json.dumps(fetch_one(url), ensure_ascii=False, indent=2)


def fetch_many(urls: list[str]) -> str:
    """并发抓多个页面：单个失败只记在它自己那一段，不影响整批。"""
    def one(target: str) -> dict:
        try:
            return fetch_one(target)
        except Exception as error:  # noqa: BLE001 - 一个页面失败不该让整批作废
            return {"url": target, "error": str(error)}

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(urls)), thread_name_prefix="fetch") as pool:
        results = list(pool.map(one, urls))
    return json.dumps({"urls": urls, "results": trim(results)}, ensure_ascii=False, indent=2)


def trim(results: list[dict]) -> list[dict]:
    """截断批量返回：单页正文与整批总量都设上限，避免一次把上下文灌满。

    截断只影响"交给模型的这一份"——需要更多内容时单独抓那一个 URL。
    """
    total = 0
    trimmed: list[dict] = []
    for item in results:
        content = item.get("content")
        if isinstance(content, str) and len(content) > MAX_CONTENT:
            item = {**item, "content": content[:MAX_CONTENT] + f"\n…（正文超过 {MAX_CONTENT} 字已截断）"}
        size = len(str(item.get("content") or ""))
        if total + size > MAX_TOTAL:
            # 先判断再累加：整批不越过上限，被跳过的页只留提示（需要时单独抓那一个 URL）
            trimmed.append({
                "url": item.get("url", ""),
                "note": f"本批正文已达 {MAX_TOTAL} 字上限，此页未返回正文；需要时单独抓取这一个 URL",
            })
            continue
        total += size
        trimmed.append(item)
    return trimmed


def fetch_one(url: str) -> dict:
    """抓一个页面（或 PDF），返回 {…响应字段, url}。"""
    check_url(url)
    if is_pdf_url(url):
        return fetch_pdf(url)
    try:
        result = request_web("fetch", {"url": url})
        content = result.get("content")
        if isinstance(content, str) and content.strip():
            return {**result, "url": url}
        reason = "Fetch 响应缺少正文"
    except RuntimeError as error:
        reason = str(error)
    # 取不到正文的常见原因：这个地址返回的是 PDF/附件——本地下载再看
    try:
        return fetch_pdf(url, reason)
    except RuntimeError as error:
        raise RuntimeError(f"{reason}；本地按 PDF 下载也失败：{error}") from None


def is_pdf_url(url: str) -> bool:
    """地址路径是否以 .pdf 结尾（忽略 query 与大小写）。"""
    try:
        return urlsplit(url).path.lower().endswith(".pdf")
    except ValueError:
        return False


def fetch_pdf(url: str, reason: str = "") -> dict:
    """下载 PDF 并提取文字；``reason`` 是"为什么改走本地下载"的说明（会写进返回）。"""
    target = download_pdf(url)
    try:
        lines = pdf_lines(target)
        text = "\n".join(line for line in lines if line.strip()).strip()
        if not text:
            raise RuntimeError("PDF 里没有可提取的文字（可能是扫描件或纯图片版）")
        truncated = len(text) > MAX_CONTENT
        payload = {
            "url": url,
            "type": "pdf",
            "content": text[:MAX_CONTENT] + (f"\n…（正文超过 {MAX_CONTENT} 字已截断）" if truncated else ""),
            "chars": len(text),
        }
        if reason:
            payload["note"] = f"DeepSeek Fetch 未取到正文（{reason}），已本地下载解析"
        return payload
    finally:
        target.unlink(missing_ok=True)


def download_pdf(url: str) -> Path:
    """把 PDF 下载到临时文件并确认它真是 PDF；任何失败都清掉临时文件。"""
    handle, temporary = tempfile.mkstemp(prefix="peifeng-pdf-", suffix=".pdf")
    os.close(handle)
    target = Path(temporary)
    try:
        request = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; PeiFeng)"})
        with urlopen(request, timeout=PDF_TIMEOUT) as response, open(target, "wb") as stream:
            size = 0
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_PDF_BYTES:
                    raise RuntimeError(f"PDF 超过 {MAX_PDF_BYTES // 1024 // 1024} MB 上限，未下载")
                stream.write(chunk)
        if size == 0:
            raise RuntimeError("下载到的内容为空")
        with open(target, "rb") as probe:
            head = probe.read(8)
        if not head.startswith(b"%PDF"):
            raise RuntimeError(f"该地址返回的不是 PDF（开头字节 {head[:4]!r}）")
        return target
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def check_url(url: str) -> None:
    """校验地址形状：必须 http/https、有主机名、不带账号密码。"""
    if not isinstance(url, str) or not 1 <= len(url) <= 8192:
        raise RuntimeError("url 必须是长度不超过 8192 的 HTTP/HTTPS 地址")
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        valid = False
    if not valid:
        raise RuntimeError("url 必须包含主机名，且不能携带用户名或密码")
