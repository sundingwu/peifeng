"""配置入口：Settings 字段、资源根与数据根、YAML 与 ``.env`` 两处覆盖。

- 只读资源 ``agent/``（注册表、工具、技能、提示词）与 ``frontend/`` 在项目根下。
- 可变数据集中在项目根的 ``data/``：项目清单与会话记录，整个目录拷走就能带走全部记录。
- 非密钥默认值写在 ``agent/config.yaml``；``.env``（项目根）是密钥与逐项覆盖的唯一入口。
  优先级：``.env`` > YAML > 字段默认值。
- 不读取进程环境变量，也不读旧的 ``settings.json``。
"""
from __future__ import annotations

import json

from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, ValidationError

#: agent 包根 ``agent/``：注册表、工具、技能、提示词与 config.yaml 都在它下面
AGENT_ROOT = Path(__file__).resolve().parent / "backend"
#: 项目根：资源与数据的基准
_PROJECT_ROOT = Path(__file__).resolve().parent
#: 只读资源根（agent 与 frontend 所在）
RESOURCE_ROOT = _PROJECT_ROOT
#: 项目清单与会话数据的存储根（项目根的 data/）
DATA_ROOT = _PROJECT_ROOT / "data"
#: 可选的外部配置文件：键名与 Settings 字段同名（位置见 :func:`env_file`）
ENV_FILE_NAME = ".env"
#: 平台无关的默认端点（DeepSeek 官方 Chat Completions）
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
#: 默认监听地址与端口（单点定义：Settings 与服务端都用它，避免两处默认值漂移）
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def env_file() -> Path:
    """``.env`` 的位置：项目根 ``.env``。"""
    return _PROJECT_ROOT / ENV_FILE_NAME


#: 配置的覆盖入口（除字段默认值与 YAML 外唯一来源）
ENV_FILE = env_file()


def load_env_file(path=None) -> dict[str, str]:
    """解析 ``.env``，返回 ``{字段名: 文本值}``。

    格式：一行一项 ``键=值``；``#`` 开头的行是注释；容忍空行、``export`` 前缀与成对引号。
    键名按 Settings 字段名匹配（大小写不敏感）；空值（如 ``model=``）视为未设置，
    文件缺失或不可读返回空字典——没有 ``.env`` 就是全部用字段默认值。
    """
    target = Path(path) if path is not None else ENV_FILE
    if not target.is_file():
        return {}
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        if entry.startswith("export "):
            entry = entry[len("export "):].strip()
        key, separator, value = entry.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and separator and value:
            values[key] = value
    return values


class Settings(BaseModel):
    """运行配置：字段默认值写在这里，可由 YAML 与 ``.env`` 逐字段覆盖。"""

    model_context_limit: Optional[int] = Field(default=None, gt=0)
    #: 运行中压缩的触发比例：mentor 单次调用内上下文占到窗口的这个比例就压缩
    #: （worker 不用：每步新建上下文，且单次工具输出有 200k 硬阀）
    context_compress_ratio: float = Field(default=0.8, ge=0.0, le=1.0)
    #: 单次模型调用的输出上限（token）：None = 用端点默认值。
    #: 长参数（整段源码塞进 write）容易被这个上限截断，截断时参数 JSON 不完整；
    #: 经常遇到就调大，比事后补括号可靠。
    max_output_tokens: Optional[int] = Field(default=None, gt=0)
    #: 单次工具输出的安全阀（字符）：超过就把完整内容落盘、回执给路径，不做上下文截断
    tool_output_limit: int = Field(default=200_000, ge=0)
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    #: 端点密钥；只从 ``.env`` 读取，不写进 YAML
    api_key: Optional[str] = None
    #: agent 包根：``agents.yaml`` + ``tools/`` + ``skills/`` + ``prompts/`` + ``config.yaml``
    agent_dir: Path = AGENT_ROOT
    #: 页面资源目录（前端）
    page_dir: Path = RESOURCE_ROOT / "frontend"
    #: 项目列表与会话数据的存储根
    data_dir: Path = DATA_ROOT
    #: 服务监听地址（默认只监听本机回环；要让局域网访问可改成 0.0.0.0）
    host: str = DEFAULT_HOST
    #: 服务端口（被占用时自动顺延）
    port: int = DEFAULT_PORT
    #: 启动后自动用默认浏览器打开页面
    open_browser: bool = True
    #: 每步最多尝试几次（含首次）：用完仍不通过就停止并出总结汇报
    step_max_retries: int = 3
    #: LLM 采样温度
    temperature: float = 0.2
    #: 单次 LLM 调用墙钟上限（秒）
    llm_timeout: float = 600.0
    #: 流式输出开关（端点不支持流式时自动回退一次普通请求）
    stream: bool = True
    #: Worker 单步的最大工具轮次
    max_tool_rounds: int = 45
    #: 规划与评审各自的工具轮次上限
    mentor_rounds: int = 15
    #: 计划步数上限
    plan_max_steps: int = 8
    #: 日志级别（DEBUG / INFO / WARNING / ERROR）
    log_level: str = "INFO"
    #: 事件流在内存里保留的可重放条数（浏览器断线重连时按它补齐）
    event_history: int = 500
    #: SSE 心跳间隔（秒）
    sse_heartbeat: float = 15.0
    #: 单个订阅者的待发事件上限；超出说明页面读得太慢，丢最旧的保住最新
    event_backlog: int = 200


def load_settings() -> Settings:
    """读取配置：字段默认值 + ``.env`` 逐字段覆盖（没有 YAML）。

    配置写错时给出可读的中文提示（指出是哪个键、期望什么），而不是抛裸的 pydantic 栈。
    """
    values: dict = {key: value for key, value in load_env_file().items() if key in Settings.model_fields}
    try:
        return Settings(**values)
    except ValidationError as error:
        raise ValueError(f"{ENV_FILE_NAME} 配置有误：\n{describe(error)}") from error


def describe(error: ValidationError) -> str:
    """把 pydantic 的校验错误整理成"键：原因"的多行文本。"""
    lines = []
    for item in error.errors():
        field = ".".join(str(part) for part in item.get("loc") or ()) or "（未知项）"
        lines.append(f"  {field}：{item.get('msg', '取值无效')}")
    return "\n".join(lines)


# ---------------------------------------------------------------- workspace_browse
#: 单层最多列多少条：目录再大也不至于把页面拖死
MAX_ENTRIES = 500


def resolve_inside(root: Path, relative: str = "") -> Optional[Path]:
    """把相对路径解析成 ``root`` 内的目录；越界或不存在返回 ``None``。"""
    base = Path(root).resolve()
    text = str(relative or "").strip().replace("\\", "/").strip("/")
    target = (base / text).resolve() if text else base
    if target != base and base not in target.parents:
        return None  # 跳出工作目录：拒绝
    return target if target.is_dir() else None


def relative_to_root(root: Path, target: Path) -> str:
    """相对路径用正斜杠，跨平台显示一致（worker的工具也认这个写法）。

    项目根目录返回空串（``Path.relative_to`` 对自身会给 ``"."``，页面上不该显示成子目录）。
    """
    base = Path(root).resolve()
    resolved = target.resolve()
    if resolved == base:
        return ""
    try:
        return resolved.relative_to(base).as_posix()
    except ValueError:
        return ""


def list_workspace(root: Optional[Path], relative: str = "") -> Optional[dict]:
    """列出工作目录里 ``relative`` 这一层的子目录与文件；越界或不可用返回 ``None``。

    ``path`` 回到上一级时给 ``parent``（已经在根上则给空串，页面据此禁用「上一级」）。
    ``dirs`` / ``files`` 的 ``path`` 都相对工作目录，可以直接填进任务描述。
    """
    if root is None:
        return None
    base = resolve_inside(root, relative)
    if base is None:
        return None
    current = relative_to_root(root, base)
    folders: list[dict] = []
    files: list[dict] = []
    children = sorted(base.iterdir(), key=lambda item: item.name.lower())
    for child in children[:MAX_ENTRIES]:
        if child.name.startswith("."):
            continue
        path = relative_to_root(root, child)
        if child.is_dir():
            folders.append({"name": child.name, "path": path})
        elif child.is_file():
            files.append({"name": child.name, "path": path, "size": child.stat().st_size})
    parent = ""
    if current:
        parent = "/".join(current.split("/")[:-1])
    return {"path": current, "parent": parent, "dirs": folders, "files": files}
