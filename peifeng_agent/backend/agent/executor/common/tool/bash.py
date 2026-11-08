"""bash 工具：执行 Windows cmd 能做的所有非管理员操作（只给worker）。

**什么时候用**：仅在专用工具无法完成时——需要真正跑程序、跑脚本、查环境、装包或做系统操作。
读文件内容走 ``read``（有分段与 grep，不会被截断），写文件走 ``write``，不要用 ``python -c`` 打印片段。
输出按 UTF-8 / GBK 自动解码。

参数：command（要执行的 shell 命令）、timeout（可选，超时秒数，默认 60，上限 3600）。
超时连同已捕获的输出一起报错，并按进程树终止。成功 return 输出文本，失败抛 RuntimeError。

**只给worker**：Planner / Critic 是只读角色，探查用 read，不拿执行类工具。

**跑在机器上**：子进程沿用系统的 PATH 与环境（``python`` 就是你机器上那个 python，含你自己
装的包）。只有项目自带的工具（``docx`` / ``xlsx`` / ``pptx`` / 图表 / ``run_code`` 之外的
工具实现）才依赖自带环境，那些由进程内的解释器直接提供，不需要在这里准备。

超时值会被夹到 1..MAX_TIMEOUT：``timeout=0`` / 负数 / 非法值都退回默认值，
不会退化成"永不超时"。超时后连同已捕获的输出一起报错，并按进程树终止。

命令在 ``base_dir``（项目工作文件夹）下执行；未提供时沿用进程工作目录。
"""
from __future__ import annotations

import os
import subprocess
from typing import List, Optional

from peifeng_agent.backend.utils.text import as_text_list, decode_text

#: 未指定 timeout（或给了 0 / 负数 / 非法值）时使用的默认超时（秒）
DEFAULT_TIMEOUT = 60
#: timeout 的上限（秒）：模型可能给出极大值，夹住以免线程被长期占用
MAX_TIMEOUT = 3600
#: 一次最多跑几条命令
MAX_BATCH = 0  #: 0 = 不限条数（输出量另有上限保护）
#: 输出不截断：截断会让模型以为"文件里没有"，然后反复缩小范围重试直到轮次耗尽。
#: 上下文压力由 agent 层的 80% 自动压缩兜底，不在这里砍内容。

information = {
    "name": "bash",
    "description": (
        "执行 Windows cmd 能做的所有非管理员操作（cmd 语法：dir / type / cd /d / findstr / copy / mkdir；"
        "可跑 python、node、pip、git、uv 等命令）。"
        "**按类别分派，能用专用工具就别用它**："
        "读取用 `read`（支持 offset/limit 分段、grep 定位、json_path 取字段）；"
        "写入用 `write`；删除用 `delete`；搬动/改名用 `move`；"
        "Office 文档用 `docx`/`xlsx`/`pptx`；出图用 `bar_chart`/`line_chart`/`pie_chart`/`scatter_chart`；"
        "检索与抓网页用 `search_web`/`fetch_web`；看图片用 `image_describe`；看 Office 结构用 `office_inspect`。"
        "这些都比开子进程稳，输出也不会被截断。"
        "真正只有它能干的：跑脚本、装包、查环境与已装依赖、推算日期、格式转换、系统操作。"
        "**例外：从网页 / 日志这类超长行文件里精确提取内容，用自己的脚本更合适**——"
        "`read` 是按行给的，压缩后的 HTML 常是一整行几十万字符，切不出可用的片段；"
        "这时写 `python` 正则提取是正当做法。普通文件、文档、JSON 取值仍然优先用 `read`。"
        "命令跑在这台机器上（`python` 就是你机器上的 python 与已装包）。"
        "command 或 commands 必填其一；timeout 可选（秒，默认 60，长任务调大到几百，上限 3600），"
        "超时连同已产生的输出报错并终止进程树。命令在项目工作文件夹下执行，输出按 UTF-8 / GBK 自动解码"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的 shell 命令（Windows 下用 cmd 语法）"},
            "commands": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    f"一次跑多条命令（与 command 二选一，条数不限）：各自独立执行、按顺序、"
                    "各自的输出分段返回；不要一条命令一次调用"
                ),
            },
            "timeout": {"type": "integer", "description": "超时秒数（默认 60；装包、跑长脚本这类调大到几百，上限 3600）"},
        },
        "required": [],
    },
}


def runner_env() -> dict:
    """子进程环境：沿用系统环境，只补上 UTF-8 编码设置。

    刻意**不**改 PATH：``bash`` 的语义是"在这台机器上执行命令"，用用户自己装的 python 与包；
    项目自带的依赖只服务于项目自己的工具（那些在进程内直接 import，不走子进程）。
    """
    return {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}


def bash(
    command: str = "",
    timeout: Optional[int] = 60,
    commands: Optional[List[str]] = None,
    base_dir=None,
) -> str:
    """跑一条命令，或按顺序跑一批（``commands``）；每条独立执行、独立超时。"""
    batch = as_text_list(commands, "commands", MAX_BATCH)
    if batch:
        return run_many(batch, timeout, base_dir)
    return run_one(command, timeout, base_dir)


def run_many(commands: List[str], timeout: Optional[int], base_dir) -> str:
    """按顺序跑多条命令：每条结果独立成段，单条失败或超时不影响后面的。

    **输出不截断**：截断会让模型以为"文件里没有这段"，然后反复缩小切片范围重试直到轮次耗尽
    （实测有任务在一个 HTML 上空转 40 多轮）。上下文压力交给 agent 层的自动压缩处理。
    """
    blocks: List[str] = []
    for index, cmd in enumerate(commands, start=1):
        head = f"### [{index}/{len(commands)}] {cmd}"
        try:
            body = run_one(cmd, timeout, base_dir)
        except RuntimeError as error:
            body = f"错误：{error}"
        blocks.append(f"{head}\n{body}")
    return "\n\n".join(blocks)


def run_one(command: str, timeout: Optional[int], base_dir) -> str:
    """跑一条命令，返回合并后的输出；非零退出与超时都抛 RuntimeError。"""
    cmd = (command or "").strip()
    if not cmd:
        raise RuntimeError("command 不能为空")
    seconds = timeout_seconds(timeout)
    proc = subprocess.Popen(
        cmd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=runner_env(),
        # 显式指定工作目录：不依赖进程 cwd，切换项目也不会把命令跑在别处
        cwd=str(base_dir) if base_dir else None,
        **isolate_kwargs(),
    )
    try:
        out, err = proc.communicate(timeout=seconds)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        out, err = proc.communicate()
        seen = decode(out) + decode(err)
        detail = f"\n已产生的输出：\n{seen.strip()}" if seen.strip() else ""
        raise RuntimeError(f"命令超时（>{seconds}s），已终止该进程树：{cmd}{detail}") from None
    text = decode(out) + decode(err)
    if proc.returncode != 0:
        raise RuntimeError(f"退出码 {proc.returncode}\n{text}")
    return text or "（命令无输出）"


def decode(raw) -> str:
    """解码命令输出：交给 utils.text.decode_text（UTF-8 → GBK/OEM 回退）。

    cmd 的内置命令（dir / type / echo）按 OEM 代码页输出，硬按 UTF-8 解会全是 `�`，
    模型据此拿不到任何信息（一次调用就白花了）。
    """
    if not raw:
        return ""
    data = raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode("utf-8", "replace")
    return decode_text(bytes(data))[0]


def timeout_seconds(timeout: Optional[int]) -> int:
    """把 timeout 夹到 1..MAX_TIMEOUT；未给、0、负数或非法值都用默认值。"""
    try:
        value = int(timeout) if timeout else DEFAULT_TIMEOUT
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT
    return max(1, min(value, MAX_TIMEOUT))


def isolate_kwargs() -> dict:
    """让子进程自成进程组，超时后能连同其后代一起终止。"""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_tree(proc: "subprocess.Popen") -> None:
    """终止整个进程树：Windows 用 ``taskkill /T``，其它平台杀进程组。"""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
        return
    try:
        os.killpg(os.getpgid(proc.pid), 9)
    except (ProcessLookupError, PermissionError):
        proc.kill()
