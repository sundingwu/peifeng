"""配置入口：Settings 字段、资源根与数据根。

- 只读资源（``backend/agent/`` 与 ``frontend/``）就在包目录旁边，由
  :data:`RESOURCE_ROOT` 定位。
- 可变数据（项目清单与会话）集中在后端包内的 ``peifeng_agent/backend/data/``：
  与后端代码同层，整个包目录拷走就能带走全部会话记录。

配置来源只有两处：:class:`Settings` 的字段默认值，以及一个 ``peifeng_agent/.env``。
``.env`` 的键名与字段同名；不读取进程环境变量，也不读旧的 ``settings.json``。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, ValidationError

#: 包目录 peifeng_agent/
_PACKAGE_ROOT = Path(__file__).resolve().parent
#: 项目根：资源与数据的基准（包目录的上一级）
_PROJECT_ROOT = _PACKAGE_ROOT.parent
#: 只读资源根（backend/agent 与 frontend 所在）
RESOURCE_ROOT = _PROJECT_ROOT
#: 项目清单与会话数据的存储根（后端包内的 data/）
DATA_ROOT = _PACKAGE_ROOT / "backend" / "data"
#: 可选的外部配置文件：键名与 Settings 字段同名（位置见 :func:`env_file`）
ENV_FILE_NAME = ".env"
#: 平台无关的默认端点（DeepSeek 官方 Chat Completions）
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
#: 默认监听地址与端口（单点定义：Settings 与服务端都用它，避免两处默认值漂移）
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def env_file() -> Path:
    """``.env`` 的位置：``peifeng_agent/.env``。"""
    return _PACKAGE_ROOT / ENV_FILE_NAME


#: 配置的覆盖入口（除字段默认值外唯一来源）
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
    """运行配置：字段默认值写在这里，可用 ``.env`` 逐字段覆盖（键名与字段同名）。"""

    model_context_limit: Optional[int] = Field(default=None, gt=0)
    #: 上下文占用达到该比例时**自动压缩**会话层（截断超长工具输出 + 成组丢弃最旧的往返）。
    #: 0 表示关闭。压缩只动会话层，system 固定层与环境说明保持不变。
    context_compress_ratio: float = Field(default=0.8, ge=0.0, le=1.0)
    #: 单次工具输出的兜底上限（字符）：超过就把**完整内容**落盘、回执给路径，绝不截断内容。
    #: 实测：12 页 pptx 结构 1.7 万、5 万字文档 5.4 万、3000 行日志 14 万、1 万行 CSV 25 万，
    #: 所以取 30 万——常见任务不会被挡住；再大就会占满上下文，落盘 + read 分段反而更稳。
    tool_output_limit: int = Field(default=300_000, ge=0)
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    #: 端点密钥；DeepSeek 必填，本地 ollama 不校验可留空
    api_key: Optional[str] = None
    # 目录以资源根 RESOURCE_ROOT 导航
    #: agent 层根目录：``common/``（公用代码）+ ``planner/`` + ``critic/`` + ``executor/``；
    #: 每个 agent 自己目录下的 ``prompt.md`` / ``tool/`` / ``skill/`` 就是它的全部能力
    agent_dir: Path = RESOURCE_ROOT / "peifeng_agent" / "backend" / "agent"
    #: 页面资源目录（前端）
    page_dir: Path = RESOURCE_ROOT / "peifeng_agent" / "frontend"
    #: 项目列表与会话数据的存储根
    data_dir: Path = DATA_ROOT
    #: 服务监听地址（默认只监听本机回环；要让局域网访问可改成 0.0.0.0）
    host: str = DEFAULT_HOST
    #: 服务端口（被占用时自动顺延；浏览器按启动时打印的地址访问）
    port: int = DEFAULT_PORT
    #: 启动后自动用默认浏览器打开页面
    open_browser: bool = True
    #: 每步最多尝试几次（含首次执行）：用完仍不通过 → 任务就此停止并出总结汇报
    step_max_retries: int = 3
    #: LLM 采样温度
    temperature: float = 0.2
    #: 单次 LLM 调用墙钟上限（秒）
    llm_timeout: float = 600.0
    #: 流式输出开关（端点不支持流式时自动回退一次普通请求）
    stream: bool = True
    #: worker单步的最大工具轮次（多交付物、大数据这类重活的单步实测要 30 轮以上）
    max_tool_rounds: int = 45
    #: 规划与评审各自的工具轮次上限：两者都只是"读证据"，但长报告核对也需要余量
    planner_rounds: int = 15
    critic_rounds: int = 15
    #: 计划步数上限：超出即截断（单任务成本 ≈ 步数 × 轮次 × (执行 + 评审)，必须封顶）
    plan_max_steps: int = 8
    #: 日志级别（DEBUG / INFO / WARNING / ERROR；启动时应用到根 logger）
    log_level: str = "INFO"
    #: 事件流在内存里保留的可重放条数（浏览器断线重连时按它补齐）
    event_history: int = 500
    #: SSE 心跳间隔（秒）：保活，并让关闭的窗口尽快断开
    sse_heartbeat: float = 15.0
    #: 单个订阅者的待发事件上限；超出说明页面读得太慢，丢最旧的保住最新
    event_backlog: int = 200


def load_settings() -> Settings:
    """读取配置：字段默认值 + ``.env``（键名与字段同名，未知键忽略）。

    配置写错时给出可读的中文提示（指出是哪个键、期望什么），而不是抛裸的 pydantic 栈。
    """
    values = {key: value for key, value in load_env_file().items() if key in Settings.model_fields}
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
