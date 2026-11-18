"""集中式正向白名单；模型的参数和工作区文件不能扩大它。"""
from functools import lru_cache
import ipaddress
import json
from pathlib import Path
import socket
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener, ProxyHandler


@lru_cache(maxsize=1)
def settings() -> dict:
    return json.loads(Path(__file__).with_name('policy.json').read_text(encoding='utf-8'))


def allowed_host(host: str) -> bool:
    host = host.rstrip('.').lower().encode('idna').decode('ascii')
    return any(host == value or host.endswith('.'+value) for value in settings()['hosts']) or any(host.endswith(value) for value in settings()['academic_suffixes'])


def check_url(url: str, *, resolve: bool = True) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username or parsed.password:
        raise PermissionError('只允许无凭据的公开HTTP(S)网址')
    host = parsed.hostname
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise PermissionError('检索不接受裸IP地址')
    if not allowed_host(host) or (parsed.port or (443 if parsed.scheme == 'https' else 80)) not in settings()['ports']:
        raise PermissionError(f'域名或端口不在检索白名单：{host}')
    if resolve:
        addresses = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise PermissionError('检索目标解析到了非公网地址')
    return url


class ApprovedRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        check_url(new_url)
        return super().redirect_request(request, response, code, message, headers, new_url)


def open_public(request, timeout=40):
    """逐跳校验；不自动使用环境代理或携带登录凭据。"""
    url = request.full_url if hasattr(request, 'full_url') else request
    check_url(url)
    if hasattr(request, 'get_method') and request.get_method() not in {'GET', 'HEAD'}:
        raise PermissionError('公开检索只允许GET或HEAD')
    response = build_opener(ProxyHandler({}), ApprovedRedirect()).open(request, timeout=timeout)
    try:
        check_url(response.url)
    except Exception:
        response.close()
        raise
    return response
