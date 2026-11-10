"""工具箱：agent 装配出的工具集合；运行期按当前项目绑定路径。
"""
from __future__ import annotations

import inspect
import json
import traceback
from pathlib import Path
from typing import Any, Callable, List, Optional

from peifeng_agent.backend.utils.contracts import ToolRunner


#: 单次工具输出兜底上限（字符）。超过就把**完整内容**落盘并把路径给模型——不做截断：
#: 截断会让模型以为"文件里没有这段"，然后反复缩小范围重试直到轮次耗尽（实测空转 40 多轮）。
#: 30 万的依据（实测）：12 页 pptx 结构 1.7 万、5 万字文档 5.4 万、3000 行日志 14 万、
#: 1 万行 CSV 25 万——常见输出都在这条线以内；更大的内容落盘 + ``read`` 分段反而更省上下文。
#: 可由 ``Settings.tool_output_limit`` 覆盖（装配时注入）。
OUTPUT_LIMIT = 300_000

#: 溢出内容的落盘位置：系统临时目录下按会话/工具分文件，不污染工作区
_SPILL_PREFIX = "peifeng-output-"


def spill_output(tool: str, output: str) -> str:
    """把超长输出写到临时文件，返回路径（失败返回空串，调用方退回简短提示）。"""
    try:
        import tempfile

        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".txt",
            prefix=f"{_SPILL_PREFIX}{tool}-",
            delete=False,
        )
        with handle:
            handle.write(output)
        return handle.name
    except OSError:
        return ""


def guard_output(tool: str, output: str, limit: int = OUTPUT_LIMIT) -> str:
    """输出兜底：没超就原样返回；超了就把全文落盘，只回一条能定位到文件的提示。"""
    if len(output) <= limit:
        return output
    saved = spill_output(tool, output)
    size = f"{len(output):,}"
    if not saved:
        # 写不出来时也只能提示（不能静默砍内容）
        return (
            f"（工具 {tool} 的输出有 {size} 字符，超过 {limit:,} 上限，且无法落盘；"
            "请改小参数或分段调用）"
        )
    hint = (
        f"（工具 {tool} 的输出有 {size} 字符，超过 {limit:,} 上限：**完整内容已存到 {saved}**，"
        f"没有截断。用 `read(targets=[{{\"path\": \"{saved}\", \"offset\": ..., \"limit\": ...}}])` 分段查看，"
        "或在 targets 里加 \"grep\": \"关键词\" 直接定位）"
    )
    return hint


class ToolBox:
    """某个 agent 可见的工具（由 ``tools/`` 的目录层级决定，见 ``shared/loader.load_agent_tools``）。"""

    def __init__(self, tools: List[dict], output_limit: Optional[int] = None) -> None:
        self.tools: List[dict] = list(tools)
        self._by_name: dict[str, dict] = {tool["name"]: tool for tool in self.tools}
        #: 单次输出兜底上限（None = 用模块默认 OUTPUT_LIMIT）
        self.output_limit: int = int(output_limit) if output_limit else OUTPUT_LIMIT

    def __bool__(self) -> bool:
        return bool(self.tools)

    @property
    def names(self) -> List[str]:
        """工具名（按装配顺序）。"""
        return [tool["name"] for tool in self.tools]

    def schemas(self) -> List[dict]:
        """转为 Chat Completions 的 function schema。

        并发的表达方式只有一种：工具自己的**批量参数**（``paths`` / ``queries`` / ``urls`` /
        ``commands`` / ``items``）。所以这里不需要再往每个工具上挂调度参数。
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["parameters"],
                },
            }
            for tool in self.tools
        ]

    def index(self) -> str:
        """工具清单保留完整描述，返回格式由各工具自身定义。"""
        if not self.tools:
            return ""
        lines = [f"- {tool['name']}：{tool['description']}" for tool in self.tools]
        return "\n\n【可用工具】\n" + "\n".join(lines)

    def runner(
        self,
        *,
        workspace=None,
        session_dir=None,
        vision=None,
        stopped=None,
    ) -> ToolRunner:
        """绑定本轮路径，返回给模型用的工具执行入口。

        ``workspace``（项目工作文件夹）、``session_dir``（当前会话目录）由宿主注入，
        不出现在工具的 parameters 里：即便模型在参数里塞了同名键，也以这里注入的为准。
        """
        context = {
            "session_dir": session_dir,
            "base_dir": Path(workspace) if workspace else None,
            # 看图能力由 agent 注入：工具签名里写 vision 就能用
            "vision": vision,
            "_stopped": stopped,
        }
        return lambda name, arguments: self.run(name, arguments, context)

    def run(self, name: str, arguments: Any, context: dict) -> tuple[str, bool]:
        """调用一个工具：正常返回即成功，异常记为失败（作为结果回给模型）。"""
        tool = self._by_name.get(name)
        if tool is None:
            return f"错误：未知工具 {name}（可用：{'、'.join(self.names) or '无'}）", True
        payload = dict(arguments) if isinstance(arguments, dict) else {}
        # 只注入工具签名接受的上下文：不收 session_dir 的工具不会因此 TypeError
        payload.update(accepted_context(tool["func"], {key: value for key, value in context.items() if value}))
        try:
            from peifeng_agent.backend.agent.common.process_runner import run_staged

            payload.pop("_stopped", None)
            if tool.get("source"):
                payload.pop("vision", None)
                # session_dir 不注入 payload（它不是工具参数），单独传给事务层：
                # 覆盖/删除前的原文件备份按会话目录落盘，模型看不到也不该控制这个路径
                result = run_staged(
                    tool["func"],
                    payload,
                    context.get("_stopped"),
                    context.get("vision"),
                    session_dir=context.get("session_dir"),
                )
            else:
                # Explicitly injected in-memory tools are used by embedders/tests.
                result = tool["func"](**payload)
        except RuntimeError as error:
            # 工具的约定是"失败抛 RuntimeError"，消息本身就是写给模型看的一句话；
            # 再贴 traceback 只会浪费 token、还可能把模型带偏
            return f"错误：{error}", True
        except Exception as error:  # noqa: BLE001 - 未预期的异常才带栈，便于排查
            return f"错误：{type(error).__name__}: {error}\n{traceback.format_exc()}", True
        body = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        # 统一兜底：任何工具的输出超长都落盘给路径，不截断、也不让它撑爆上下文
        return guard_output(name, body, self.output_limit), False


def accepted_context(func: Callable, injected: dict) -> dict:
    """只保留 ``func`` 签名接受的注入项；签名不可读时宁可不注入。"""
    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return {}
    if any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values()):
        return dict(injected)
    return {key: value for key, value in injected.items() if key in parameters}
