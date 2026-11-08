"""DeepSeek Search/Fetch transport, using the existing configured API key."""
from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from peifeng_agent.config import load_settings

API_ROOT = "https://api.deepseek.com/v1/tools"


def request_web(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    if endpoint not in {"search", "fetch"}:
        raise ValueError("Unsupported DeepSeek web endpoint")
    settings = load_settings()
    if not settings.api_key or not settings.api_key.strip():
        raise RuntimeError("网页搜索需要配置 DeepSeek API Key（peifeng_agent/.env 的 api_key）")
    base = urlsplit(settings.base_url)
    if base.scheme != "https" or base.hostname != "api.deepseek.com":
        raise RuntimeError("网页搜索需要 DeepSeek 官方 API Key；当前 base_url 非官方接口，未发送密钥")
    request = Request(
        f"{API_ROOT}/{endpoint}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {settings.api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=min(max(float(settings.llm_timeout), 1), 60)) as response:
            body = response.read().decode("utf-8")
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace").replace(settings.api_key, "[REDACTED]")
        raise RuntimeError(f"DeepSeek {endpoint} HTTP {error.code}\n{body}") from None
    except URLError as error:
        raise RuntimeError(f"DeepSeek {endpoint} 网络错误：{error.reason}") from None
    try:
        result = json.loads(body)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"DeepSeek {endpoint} 返回非 JSON：{error}\n{body.replace(settings.api_key, '[REDACTED]')}") from None
    if not isinstance(result, dict):
        raise RuntimeError(f"DeepSeek {endpoint} 返回格式错误：{body}")
    return result
