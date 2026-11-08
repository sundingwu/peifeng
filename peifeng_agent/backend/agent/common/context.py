"""分层上下文：system 固定层（提示 + 工具 schema）+ 运行环境 suffix + 会话层。

会话层没有滑动窗口，但**可以压缩**：当整体占用逼近模型窗口时，先截断单条超长内容
（工具结果与正文），再**成组**丢弃最旧的往返——成组是硬要求：``assistant`` 的
``tool_calls`` 与紧随其后的 ``tool`` 结果必须配对，单独丢会让请求直接 400。
"""
from __future__ import annotations

import json
from typing import Any, List, Optional

#: 压缩时单条**工具结果**保留的上限（字，头尾各留一半）
COMPRESS_TOOL_CHARS = 6_000
#: 压缩时单条**正文**保留的上限（字）
COMPRESS_TEXT_CHARS = 8_000
#: 压缩后至少保留最近几个「assistant + 它的工具结果」回合
COMPRESS_KEEP_ROUNDS = 4
#: 估算 token 的粗略系数：字符数 / 该值（与 base.complete 里的口径一致）
CHARS_PER_TOKEN = 3


class MessageContext:
    """分层上下文：system 固定层（提示/工具/技能）+ 会话层。"""

    def __init__(self, system_prompt: str = "", suffix: str = "") -> None:
        self.system_prompt = system_prompt
        self.suffix = suffix
        self.tool_defs: List[dict] = []
        self.conversation: List[dict] = []

    def reset(self, system_prompt: Optional[str] = None) -> None:
        if system_prompt is not None:
            self.system_prompt = system_prompt
        self.conversation = []

    def set_fixed_layer(self, tool_defs: Optional[List[dict]] = None) -> None:
        """固定层只有工具 schema；技能正文由模型用 read 直接读技能文件，不进 system。"""
        if tool_defs is not None:
            self.tool_defs = list(tool_defs)

    def add(self, role: str, content: str, **fields: Any) -> None:
        message: dict[str, Any] = {"role": role, "content": content}
        if fields:
            message.update({key: value for key, value in fields.items() if value is not None})
        self.conversation.append(message)

    def to_llm_messages(self) -> List[dict]:
        segments = [self.system_prompt]
        if self.suffix:
            segments.append(self.suffix)
        system_content = "\n\n".join(segment for segment in segments if segment)
        messages: List[dict] = []
        if system_content:
            messages.append({"role": "system", "content": system_content})
        messages.extend(self.conversation)
        return messages

    # ---------------------------------------------------------------- 占用与压缩
    def size(self) -> int:
        """会话层的**字节量**（UTF-8，含工具调用参数）：压缩判断与压缩效果都看它。

        用字节而不是字符：中文一个字占 3 字节，按字符算会把占用低估 3 倍——
        那样 80% 的阈值永远达不到，自动压缩等于没接上。
        """
        total = 0
        for message in self.conversation:
            total += len(str(message.get("content") or "").encode("utf-8"))
            calls = message.get("tool_calls")
            if calls:
                total += len(json.dumps(calls, ensure_ascii=False).encode("utf-8"))
        return total

    def estimated_tokens(self) -> int:
        """整份请求（含 system 与工具 schema）的 token 估算，口径与 ``BaseAgent.complete`` 一致。"""
        encoded = json.dumps(
            {"messages": self.to_llm_messages(), "tools": self.tool_defs}, ensure_ascii=False
        )
        return (len(encoded.encode("utf-8")) + 2) // CHARS_PER_TOKEN

    def rounds(self) -> List[tuple[int, int]]:
        """把会话切成「一个 assistant + 它触发的 tool 结果」的区间 ``(start, end)``。

        成组是压缩的前提：``tool_calls`` 与 ``tool`` 消息必须配对出现。
        """
        units: List[tuple[int, int]] = []
        index = 0
        while index < len(self.conversation):
            if self.conversation[index].get("role") == "assistant":
                end = index + 1
                while end < len(self.conversation) and self.conversation[end].get("role") == "tool":
                    end += 1
                units.append((index, end))
                index = end
                continue
            index += 1
        return units

    def compress(self, budget: int, keep_rounds: int = COMPRESS_KEEP_ROUNDS) -> bool:
        """把会话层压到 ``budget`` 字符以内，返回是否改动过。

        两步：① 单条超长内容截成「头 + 省略说明 + 尾」（工具结果最容易撑爆上下文）；
        ② 仍超预算就**成组**丢掉最旧的往返，只保留最近 ``keep_rounds`` 个。
        头部（通常是任务请求）与最后几个往返一定保留——丢了任务请求模型就不知道在干什么。
        """
        changed = self.truncate_long_messages()
        if self.size() <= budget:
            return changed

        units = self.rounds()
        if len(units) <= keep_rounds:
            return changed  # 剩的都是必要往返，再丢就丢任务本身了
        head_end = units[0][0]  # 第一个 assistant 之前的内容（任务请求等）
        dropped = units[:-keep_rounds]
        drop_indexes = {index for start, end in dropped for index in range(start, end)}
        kept = [
            message
            for index, message in enumerate(self.conversation)
            if index < head_end or index not in drop_indexes
        ]
        if len(kept) == len(self.conversation):
            return changed
        # 在保留段前面留一句说明，模型才不会把"历史缺失"当成自己记错
        note = {
            "role": "user",
            "content": (
                f"（上下文已压缩：更早的 {sum(end - start for start, end in dropped)} 条工具往返已省略。"
                "需要那些内容时重新用工具读取，不要凭记忆补写。）"
            ),
        }
        self.conversation = kept[:head_end] + [note] + kept[head_end:]
        return True

    def truncate_long_messages(self) -> bool:
        """只做"单条截断"：把超长工具结果/正文截成头 + 省略说明 + 尾。"""
        changed = False
        for message in self.conversation:
            content = str(message.get("content") or "")
            limit = COMPRESS_TOOL_CHARS if message.get("role") == "tool" else COMPRESS_TEXT_CHARS
            if len(content) <= limit:
                continue
            half = limit // 2
            message["content"] = (
                f"{content[:half]}\n…（此处省略 {len(content) - limit} 字，内容过长已压缩）…\n{content[-half:]}"
            )
            changed = True
        return changed
