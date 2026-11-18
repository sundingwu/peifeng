"""网络与并发：一次处理多个目标的调度、免密公开检索与抓取。"""

from __future__ import annotations

import os
import io
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from html.parser import HTMLParser
from typing import Callable, Sequence, TypeVar
from urllib.parse import parse_qs, unquote, urlencode, urlparse
from urllib.request import Request
from backend.security.policy import open_public as urlopen
from backend.utils.tool_output import ToolOutput



# ---------------------------------------------------------------- web
# ---------------------------------------------------------------- concurrency
T = TypeVar("T")
R = TypeVar("R")


def run_targets(func: Callable[[dict], str], targets: list[dict], *, workers: int = 8, base_dir=None) -> str:
    """统一对象数组入口，逐项隔离错误，结果与输入顺序一致。"""
    if not isinstance(targets, list) or not targets:
        raise ValueError("targets 必须是非空对象数组")
    if any(not isinstance(target, dict) for target in targets):
        raise ValueError("targets 每项必须是参数对象")
    if len(targets) == 1:
        return func(targets[0])
    # 有重叠路径时保留输入顺序，涵盖同文件覆盖与父子目录移动/删除。
    claimed: list[str] = []
    for target in targets:
        paths = [os.path.normcase(os.path.abspath(os.path.join(base_dir or os.curdir, str(target[key])))) for key in ("path", "split_path", "file", "output", "target", "template") if target.get(key)]
        for path in paths:
            for previous in claimed:
                try:
                    if os.path.commonpath([path, previous]) in (path, previous):
                        workers = 1
                except ValueError:
                    pass  # 不同磁盘不存在父子路径关系。
        claimed.extend(paths)
    results = run_parallel_safe(func, targets, workers=workers)
    return ToolOutput("\n\n".join(
        f"===== {index} =====\n{body if error is None else '错误：' + str(error)}"
        for index, (body, error) in enumerate(results, 1)
    ), has_errors=any(error is not None or getattr(body, 'has_errors', False) for body, error in results))

#: 默认并发度：本地操作（读文件、删、移）用 8，够快也不会把磁盘打满
DEFAULT_WORKERS = 8


def run_parallel(
    func: Callable[[T], R],
    items: Sequence[T],
    *,
    workers: int = DEFAULT_WORKERS,
    thread_prefix: str = "batch",
) -> list[R]:
    """对 ``items`` 逐项调 ``func``，并发执行，结果**按输入顺序**返回。

    单项抛异常不会被吞：异常会原样冒给调用方——各工具需要"单项目失败记在它自己那段"，
    自己在 ``func`` 里 catch 即可，这样错误信息的措辞由工具决定。
    """
    if not items:
        return []
    if len(items) == 1:
        return [func(items[0])]
    # 每个任务复制一份当前上下文：工具靠 contextvars 拿本轮路径边界（见 backend.orchestration.orchestrate），
    # 线程池的工作线程默认看不到主线程的绑定，必须显式传播。
    jobs = [(copy_context(), item) for item in items]

    def call(job):
        context, item = job
        return context.run(func, item)

    with ThreadPoolExecutor(
        max_workers=min(workers, len(items)),
        thread_name_prefix=thread_prefix,
    ) as pool:
        return list(pool.map(call, jobs))


def run_parallel_safe(
    func: Callable[[T], R],
    items: Sequence[T],
    *,
    workers: int = DEFAULT_WORKERS,
    thread_prefix: str = "batch",
) -> list[tuple[R | None, Exception | None]]:
    """同 :func:`run_parallel`，但把异常收进结果里（不中断其余项）。

    返回 ``[(结果, None) 或 (None, 异常)]``，与 ``items`` 一一对应。
    """

    def guarded(item: T) -> tuple[R | None, Exception | None]:
        try:
            return func(item), None
        except Exception as error:  # noqa: BLE001 - 单项失败要能记在它自己那段
            return None, error

    return run_parallel(guarded, items, workers=workers, thread_prefix=thread_prefix)


# ---------------------------------------------------------------- public_web
class PageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {'script', 'style'}:
            self.hidden = max(0, self.hidden-1)

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.text.append(data.strip())


def retrieve(url: str) -> dict:
    if urlparse(url).scheme not in {'http', 'https'}:
        raise ValueError('仅支持公开HTTP/HTTPS来源')
    request = Request(url, headers={'User-Agent': 'PeiFeng/1.0 (public academic retrieval)'})
    with urlopen(request, timeout=40) as response:
        if urlparse(response.url).scheme not in {'http','https'}:
            raise ValueError('不支持的重定向')
        content = response.read(40*1024*1024+1)
        if len(content) > 40*1024*1024:
            raise ValueError('响应超过40MB')
        content_type = response.headers.get('Content-Type', '')
        encoding = response.headers.get_content_charset() or 'utf-8'
        actual_url = response.url
    if content.startswith(b'%PDF') or 'application/pdf' in content_type:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        text = '\n'.join(f'[page {i+1}]\n{page.extract_text() or ""}' for i,page in enumerate(reader.pages))
        level = 'extracted_pdf_text'
    else:
        parser = PageText()
        parser.feed(content.decode(encoding, errors='replace'))
        text = '\n'.join(parser.text)
        level = 'public_page_text'
    return {'url': actual_url, 'content': text[:30000], 'truncated': len(text)>30000, 'source_level': level}


def search_public(query: str, count: int = 5) -> list[dict]:
    """公开搜索页，优先本地连通性较好的Bing，失败再尝试DuckDuckGo。"""
    import base64
    class Links(HTMLParser):
        def __init__(self, source):
            super().__init__(); self.results=[]; self.current=None; self.heading=False; self.source=source
        def handle_starttag(self, tag, attrs):
            fields=dict(attrs)
            if tag=='h2':self.heading=True
            if tag=='a' and (self.heading or 'result__a' in fields.get('class','').split()):
                href=fields.get('href','')
                link=parse_qs(urlparse(href).query).get('uddg',[href])[0]
                # Bing也可能返回带base64的公开出站链接；解析后仍由fetch逐跳校验。
                if urlparse(link).path.startswith('/ck/'):
                    encoded=parse_qs(urlparse(link).query).get('u',[''])[0]
                    if encoded.startswith('a1'):
                        try:link=base64.urlsafe_b64decode(encoded[2:]+'='*((-len(encoded[2:]))%4)).decode('utf-8')
                        except (ValueError,UnicodeError):return
                if urlparse(link).scheme in {'http','https'}:
                    self.current={'title':'','url':unquote(link),'source_level':'search_result','search_source':self.source}
        def handle_data(self,data):
            if self.current is not None:self.current['title']+=data
        def handle_endtag(self,tag):
            if tag=='a' and self.current is not None:
                self.results.append(self.current);self.current=None
            if tag=='h2':self.heading=False
    errors=[]
    for source,endpoint in [('bing','https://www.bing.com/search?'),('duckduckgo','https://html.duckduckgo.com/html/?')]:
        try:
            with urlopen(Request(endpoint+urlencode({'q':query}),headers={'User-Agent':'Mozilla/5.0'}),timeout=12) as response:
                html=response.read(2*1024*1024).decode('utf-8',errors='replace')
            parser=Links(source);parser.feed(html)
            if not parser.results:raise RuntimeError('页面没有可解析结果，可能被限流或要求验证')
            results=parser.results[:max(1,min(int(count),20))]
            if errors:results[0]['retrieval_warnings']=errors
            return results
        except Exception as error:
            errors.append(source+': '+str(error))
    raise RuntimeError('公开搜索失败，不能视为没有相关文献；可改用paper或提供URL。'+ '; '.join(errors))
