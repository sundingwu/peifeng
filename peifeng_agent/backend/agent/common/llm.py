"""LLM 层：统一消息模型与 OpenAI 兼容 Provider（Chat Completions）。

Provider 只返回 ResponseMessage，不抛异常；失败原因保留在响应体里，所以上层能区分
"端点拒绝流式""连接中断""超时"等情形。接入的是 OpenAI 兼容的 /chat/completions，
换端点只改 model 与 base_url。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, List, Optional

from pydantic import BaseModel, Field

from peifeng_agent.backend.utils.model_output import parse_json_value

logger = logging.getLogger(__name__)

#: 端点专属附加体：思考模式**固定关闭**（随请求显式下发，行为不依赖端点默认值）。
#: 不开的理由：带 tools 的多轮必须把上一轮的 ``reasoning_content`` 原样回传，本项目不做这件事，
#: 开了整条工具链会 400；关掉思考换来的是稳定可跑的工具循环。
NO_THINKING_BODY: dict[str, Any] = {"thinking": {"type": "disabled"}}


class RequestMessage(BaseModel):
    """一次 /chat/completions 请求体。"""

    model: str
    messages: List[dict[str, Any]]
    tools: Optional[List[dict[str, Any]]] = None
    temperature: float = 0.2
    stream: bool = False
    #: 流式时让它顺带返回用量（``{"include_usage": True}``）：最后一个 chunk 带 ``usage``。
    #: 不是所有 OpenAI 兼容端点都认这个键，所以只在真的走流式时下发，端点拒绝时去掉重试。
    stream_options: Optional[dict[str, Any]] = None
    #: 端点专属的附加字段（如 DeepSeek 的 ``thinking`` 开关）：OpenAI SDK 不认识它，
    #: 要作为 ``extra_body`` 传下去，所以单独放一个字段、不混进 SDK 的已知参数
    extra_body: Optional[dict[str, Any]] = None


class ResponseMessage(BaseModel):
    """一次模型响应；错误信息编码在 ``error`` 中。"""

    status: str = "completed"
    #: 助手正文（流式时为增量拼接结果）
    content: str = ""
    #: 工具调用，统一形状：``{id, name, arguments, type}``
    tool_calls: List[dict[str, Any]] = Field(default_factory=list)
    #: 端点给出的结束原因：stop / length / tool_calls / content_filter
    finish_reason: str = "stop"
    #: 端点返回的用量（``prompt_tokens`` / ``completion_tokens`` / ``total_tokens``）：
    #: 这是账单口径的真实值，上下文占用应优先用它，而不是字符估算。端点不返回时为空。
    usage: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None

    @property
    def parsed(self) -> dict[str, Any]:
        """规范化为统一视图：content / tool_calls / has_tool_calls / stop_reason / error_text。"""
        return {
            "content": self.content,
            "tool_calls": self.tool_calls,
            "has_tool_calls": bool(self.tool_calls),
            "stop_reason": self.stop_reason(),
            "error_text": self.error_text(),
        }

    def stop_reason(self) -> str:
        if self.status == "failed":
            return "error"
        if self.tool_calls:
            return "tool_use"
        if self.finish_reason == "length":
            return "length"
        return "stop"

    def error_text(self) -> str:
        if isinstance(self.error, dict):
            return str(self.error.get("message") or self.error.get("code") or "")
        return str(self.error or "")


def parse_arguments(raw: Any) -> dict[str, Any]:
    """解析工具调用参数。

    工具参数是模型自己写的 JSON，同样会带围栏、尾逗号或被截断，所以走
    :func:`parse_json_value` 的兜底；实在解析不出来才保留原文，便于评审与排查。
    """
    if isinstance(raw, dict):
        return raw
    text = "{}" if raw is None else (raw if isinstance(raw, str) else str(raw))
    try:
        data = parse_json_value(text)
    except ValueError:
        return {"_raw": text}
    return data if isinstance(data, dict) else {"_raw": text}


def normalize_call(call: dict, name: str = "", arguments: Any = "") -> dict[str, Any]:
    """把工具调用规范化为内部统一形状（``arguments`` 允许是流式分片列表）。"""
    raw = call.get("arguments") if "arguments" in call else arguments
    if isinstance(raw, list):
        raw = "".join(str(part) for part in raw)
    return {
        "id": str(call.get("id") or ""),
        "name": str(call.get("name") or name or ""),
        "arguments": parse_arguments(raw),
        "type": "function",
    }


def image_data_uri(image_path, max_side: int = 1400) -> str:
    """把图片读成 data URI；超过 max_side 先等比缩小（省 token，也避开各家大小限制）。"""
    import base64
    import io

    from PIL import Image

    with Image.open(image_path) as image:
        if max(image.size) > max_side:
            image.thumbnail((max_side, max_side), Image.LANCZOS)
        buffer = io.BytesIO()
        ready = image.convert("RGB") if image.mode not in ("RGB", "L") else image
        ready.save(buffer, format="JPEG", quality=88)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


class LLMProvider(ABC):
    """LLM 后端统一接口：入参消息用 dict，返回 ResponseMessage，绝不抛异常。"""

    @abstractmethod
    def complete(
        self,
        messages: List[dict],
        tools: Optional[List[dict]] = None,
        temperature: float = 0.2,
        events: Optional[Callable[[str, dict[str, Any]], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> ResponseMessage:
        """发送一次完整对话；events 为可选流事件回调 (kind, data)。

        ``should_stop`` 在流式接收的每个分片之间检查：返回 True 就**就地停下**并返回
        已收到的内容（用户点了停止时不用再等整段输出）。
        """

    def describe_image(self, image_path, question: str, max_side: int = 1400) -> str:
        """看一张图并回答针对它的问题；默认后端不支持时抛错。"""
        raise NotImplementedError("当前模型后端不支持看图")


def to_chat_messages(messages: List[dict]) -> list[dict]:
    """把内部消息 dict 列表转换为 Chat Completions 的 messages。

    内部形状：``assistant`` 的 ``tool_calls`` 是 ``{id, name, arguments}``、
    工具结果用 ``role="tool"`` + ``tool_call_id``；这里补上协议要求的包装与
    ``arguments`` 的 JSON 字符串形式。
    """
    items: list[dict] = []
    for message in messages:
        role = message.get("role") or "user"
        content = str(message.get("content") or "")
        if role == "tool":
            items.append(
                {"role": "tool", "tool_call_id": message.get("tool_call_id") or "", "content": content}
            )
        elif role == "assistant" and message.get("tool_calls"):
            items.append(
                {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {
                            "id": call.get("id") or "",
                            "type": "function",
                            "function": {
                                "name": call.get("name", ""),
                                "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                            },
                        }
                        for call in message.get("tool_calls") or []
                    ],
                }
            )
        else:
            items.append({"role": role if role in ("user", "assistant", "system") else "user", "content": content})
    return items


def normalize_usage(raw: Any) -> Optional[dict[str, Any]]:
    """把端点返回的 ``usage`` 规整成固定三个整数字段；没有有效数据时返回 ``None``。

    端点之间字段名与空值写法不一致（有的给 ``null``、有的干脆不返回），统一在这里收口，
    上层只判断"有没有 prompt_tokens"。SDK 给的是 pydantic 对象、代理偶尔给普通对象或 dict，
    三种都要能读。
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = as_dict(raw)
        except TypeError:
            data = {key: getattr(raw, key, None) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    usage: dict[str, Any] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = data.get(key)
        try:
            usage[key] = int(value)
        except (TypeError, ValueError):
            continue
    return usage or None


def from_completion(payload: dict) -> ResponseMessage:
    """把 Chat Completions 响应体转换为 :class:`ResponseMessage`。"""
    choices = payload.get("choices") or []
    if not choices:
        return ResponseMessage(status="failed", error={"message": "响应中没有 choices"})
    choice = choices[0]
    message = choice.get("message") or {}
    calls = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        calls.append(
            normalize_call(
                {"id": call.get("id"), "name": function.get("name")},
                arguments=function.get("arguments"),
            )
        )
    return ResponseMessage(
        content=str(message.get("content") or ""),
        tool_calls=calls,
        finish_reason=str(choice.get("finish_reason") or "stop"),
        usage=normalize_usage(payload.get("usage")),
    )


def as_dict(obj: Any) -> dict:
    """把 OpenAI SDK 响应对象（或 dict）转成 dict。"""
    if isinstance(obj, dict):
        return obj
    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        return model_dump()
    raise TypeError(f"无法将 {type(obj).__name__} 转换为响应字典")


class OpenAIProvider(LLMProvider):
    """OpenAI 兼容端点（DeepSeek / Ollama / OpenAI 通用）。"""

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: Optional[str] = None,
        timeout: float = 120.0,
        max_retries: int = 2,
        retry_backoff: float = 5.0,
        stream: bool = True,
    ) -> None:
        from openai import OpenAI

        self.model = model
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.stream = stream
        #: 单次调用墙钟上限（秒）：流式无响应或保活占用时兜底中止，避免主流程永久阻塞
        self.timeout = timeout
        self.client = OpenAI(base_url=base_url, api_key=api_key or "not-set", timeout=timeout)

    @staticmethod
    def sdk_payload(request: RequestMessage) -> dict[str, Any]:
        """把请求体翻成 SDK 的调用参数：``extra_body`` 单独取出，作为关键字传给 SDK。"""
        payload = request.model_dump(exclude_unset=True)
        extra = payload.pop("extra_body", None)
        if extra:
            payload["extra_body"] = extra
        if payload.get("stream_options") is None:
            payload.pop("stream_options", None)  # 不需要时不要把这个键发给端点
        return payload

    def describe_image(self, image_path, question: str, max_side: int = 1400) -> str:
        """把图与问题发给模型，返回文字回答。"""
        request = RequestMessage(
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {"type": "image_url", "image_url": {"url": image_data_uri(image_path, max_side)}},
                    ],
                }
            ],
            temperature=0.2,
            extra_body=dict(NO_THINKING_BODY),
        )
        response = self.complete_once(request)
        if response.status == "error":
            detail = (response.error or {}).get("message") or "模型返回错误"
            raise RuntimeError(f"看图失败：{detail}")
        return response.content.strip()

    def build_request(
        self,
        messages: List[dict],
        tools: Optional[List[dict]] = None,
        temperature: float = 0.2,
    ) -> RequestMessage:
        """把消息 dict 流组装为 ``/chat/completions`` 请求体（system 保持为一条消息）。"""
        return RequestMessage(
            model=self.model,
            messages=to_chat_messages(messages),
            tools=tools or None,
            temperature=temperature,
            extra_body=dict(NO_THINKING_BODY),
        )

    def complete(
        self,
        messages: List[dict],
        tools: Optional[List[dict]] = None,
        temperature: float = 0.2,
        events: Optional[Callable[[str, dict[str, Any]], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> ResponseMessage:
        request = self.build_request(messages, tools, temperature)
        if not self.stream:
            return self.complete_once(request)
        # 流式时顺带要用量：端点会在最后一个 chunk 里给出 usage（账单口径的 prompt_tokens）
        stream_request = request.model_copy(
            update={"stream": True, "stream_options": {"include_usage": True}}
        )
        return self.complete_stream_with_deadline(stream_request, events, should_stop)

    # ------------------------------------------------------------ 流式
    def complete_stream_with_deadline(
        self,
        request: RequestMessage,
        events: Optional[Callable[[str, dict[str, Any]], None]],
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> ResponseMessage:
        """墙钟超时兜底：流式无响应或保活占用时中止本次调用，避免永久阻塞。

        读取在 daemon 线程进行，超时后主流程立即返回失败响应；该线程不阻止进程退出，
        其占用随进程结束释放。失败原因（异常文本）一并保留。
        """
        outcome: dict[str, Any] = {}

        def worker() -> None:
            try:
                outcome["message"] = self.complete_stream(request, events, should_stop)
            except BaseException as error:  # noqa: BLE001 - 转交主线程处理
                outcome["error"] = error

        thread = threading.Thread(target=worker, name="llm-stream", daemon=True)
        thread.start()
        thread.join(timeout=self.timeout)
        if "message" in outcome:
            return outcome["message"]
        if "error" in outcome:
            return self.failed(outcome["error"])
        return self.failed(f"流式请求超过 {self.timeout:.0f}s 未返回，已中止本次调用。")

    def complete_stream(
        self,
        request: RequestMessage,
        events: Optional[Callable[[str, dict[str, Any]], None]],
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> ResponseMessage:
        """流式接收：转发文本增量、累积分片工具调用；仅端点明确拒绝流式且未收到内容时回退一次。

        ``should_stop`` 命中时跳出接收循环，把**已收到的部分**作为结果返回——上层据
        :attr:`ResponseMessage.finish_reason` 为 ``"stopped"`` 判断这是被中止的内容，
        不会当成模型的完整输出。
        """
        received_text = False
        stopped = False
        stream = None
        try:
            stream = self.client.chat.completions.create(**self.sdk_payload(request))
            content_parts: list[str] = []
            calls: dict[int, dict] = {}
            finish_reason = ""
            usage: Optional[dict[str, Any]] = None
            for chunk in stream:
                if should_stop is not None and should_stop():
                    # 用户点了停止：保留已收到的内容就地返回，剩下的不再接收
                    stopped = True
                    break
                # 用量在最后一个 chunk 里（那一块通常没有 choices，所以要先取再跳过）
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    usage = normalize_usage(chunk_usage)
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                choice = choices[0]
                delta = getattr(choice, "delta", None)
                if delta is not None:
                    text = str(getattr(delta, "content", "") or "")
                    if text:
                        received_text = True
                        content_parts.append(text)
                        if events is not None:
                            try:
                                events("text_delta", {"delta": text})
                            except Exception:  # noqa: BLE001 - 展示层失败不影响接收
                                pass
                    for item in getattr(delta, "tool_calls", None) or []:
                        slot = calls.setdefault(
                            int(getattr(item, "index", 0) or 0),
                            {"id": "", "name": "", "arguments": []},
                        )
                        if getattr(item, "id", None):
                            slot["id"] = str(item.id)
                        function = getattr(item, "function", None)
                        if function is not None:
                            if getattr(function, "name", None):
                                slot["name"] = str(function.name)
                            if getattr(function, "arguments", None):
                                # 分片累积到列表，最后 join：字符串 += 是 O(n²)，长参数会明显放大
                                slot["arguments"].append(str(function.arguments))
                if getattr(choice, "finish_reason", None):
                    finish_reason = str(choice.finish_reason)
            return ResponseMessage(
                content="".join(content_parts),
                tool_calls=[
                    normalize_call(
                        slot,
                        name=slot.get("name") or "",
                        arguments="".join(slot.get("arguments") or []),
                    )
                    for _, slot in sorted(calls.items())
                ],
                # 被用户中止时明确标成 stopped：上层据此走收尾，而不是把半截正文当完整输出
                finish_reason="stopped" if stopped else (finish_reason or "stop"),
                usage=usage,
            )
        except Exception as error:  # noqa: BLE001
            if request.stream_options and self.rejects_stream_options(error, received_text):
                # 端点不认 stream_options：去掉它重试，宁可拿不到 usage 也要拿到内容
                logger.info("端点不支持 stream_options，去重后重试流式请求")
                return self.complete_stream(
                    request.model_copy(update={"stream_options": None}), events, should_stop
                )
            if self.rejects_streaming(error, received_text):
                try:
                    return self.complete_once(request.model_copy(update={"stream": False}))
                except Exception:  # noqa: BLE001
                    pass
            return self.failed(error)
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception:  # noqa: BLE001 - 连接清理不能覆盖原始结果
                    logger.debug("关闭模型流失败", exc_info=True)

    @staticmethod
    def rejects_streaming(error: Exception, received_text: bool) -> bool:
        """端点是否明确拒绝流式：4xx、未收到任何内容，且错误文本指向流式不受支持。"""
        status = getattr(error, "status_code", None)
        if status is None or not 400 <= status < 500 or received_text:
            return False
        message = str(error).lower()
        return any(word in message for word in ("stream", "unsupported", "not supported"))

    @staticmethod
    def rejects_stream_options(error: Exception, received_text: bool) -> bool:
        """端点是否因为 ``stream_options`` 报错：4xx、未收到内容，且文本指向这个参数。

        单独判断是为了"只去掉这个键重试"，而不是整条退化成非流式（那样连增量输出都没了）。
        """
        status = getattr(error, "status_code", None)
        if status is None or not 400 <= status < 500 or received_text:
            return False
        message = str(error).lower()
        return any(word in message for word in ("stream_options", "include_usage"))

    # ------------------------------------------------------------ 非流式
    def complete_once(self, request: RequestMessage) -> ResponseMessage:
        """非流式请求：429/5xx 线性退避重试，其余错误直接编码返回。"""
        last_error = ""
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.chat.completions.create(**self.sdk_payload(request))
                return from_completion(as_dict(response))
            except Exception as error:  # noqa: BLE001
                status = getattr(error, "status_code", None)
                last_error = str(error)
                if status is not None and (status == 429 or 500 <= status <= 599) and attempt < self.max_retries:
                    time.sleep(self.retry_backoff * (attempt + 1))
                    continue
                return self.failed(error)
        return self.failed(f"重试 {self.max_retries} 次后仍失败（{last_error}）")

    @staticmethod
    def failed(reason: Any) -> ResponseMessage:
        """把失败原因（异常对象或文本）编码为失败响应，保留原始信息。"""
        message = reason if isinstance(reason, str) else f"{type(reason).__name__}: {reason}"
        return ResponseMessage(status="failed", error={"message": message})
