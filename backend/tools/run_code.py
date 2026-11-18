from __future__ import annotations

from backend.utils.web import run_targets

import os
import sys
from pathlib import Path
import subprocess
import tempfile
import json
from typing import Optional

from backend.utils.text import decode_text

#: 未指定 timeout（或给了 0 / 负数 / 非法值）时使用的默认超时（秒）
DEFAULT_TIMEOUT = 60
#: timeout 的上限（秒）：模型可能给出极大值，夹住以免线程被长期占用
MAX_TIMEOUT = 3600
#: 一次最多跑几条命令


#: 批量命令的并发度：跑脚本这类各占一个进程，4 路并行够快也不会打满机器


information = {
    "name": "run_code",
    "description": "用应用自带的 Python 运行脚本：只接受文件路径与参数数组。"
                   "脚本只能来自项目内相对路径或本角色 skill://<技能名>/scripts/<脚本>.py；"
                   "项目可写、服务资源只读，项目脚本默认不能出网。"
                   "不执行命令行、内联代码或 -m 模块；脚本先 write 落盘，收到成功回执后在下一轮执行。",
    "parameters": {
        "type": "object",
        "properties": {
            "targets": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "脚本路径：项目内相对路径（如 internal/check.py），"
                                           "或 skill://<技能名>/scripts/<脚本>.py。解释器由系统固定，不用写 python。",
                        },
                        "args": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "按顺序传给脚本的参数，例如 [\"--kind\", \"line\", \"--output\", \"trend.png\"]",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "超时秒数（默认 60；跑长脚本时调大到几百，上限 3600）",
                        },
                    },
                    "required": ["file_path"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["targets"],
        "additionalProperties": False,
    },
}


def runner_env(base_dir=None) -> dict:
    """只暴露本应用 Python 与最小环境：临时目录钉在项目内，路径边界之外的东西一概不给。"""
    env = {
        "PATH": str(Path(sys.executable).parent),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "MPLCONFIGDIR": os.environ.get("MPLCONFIGDIR", ""),
    }
    # Windows OpenSSL needs the system root to initialize its platform services.
    if os.name == "nt" and os.environ.get("SystemRoot"):
        env["SystemRoot"] = os.environ["SystemRoot"]
        env["WINDIR"] = os.environ["SystemRoot"]
    if base_dir:
        temp = Path(base_dir) / "internal" / "tmp"
        try:
            temp.mkdir(parents=True, exist_ok=True)
        except OSError:
            temp = Path(base_dir)
        env["TMP"] = env["TEMP"] = env["TMPDIR"] = str(temp)
    # 系统盘符与用户目录不传给子进程，减少脚本"顺手"读到边界外内容的机会
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        if key in os.environ:
            env[key] = env["TEMP"]
    return env


def looks_like_path(token: str) -> bool:
    """参数是不是路径：带分隔符或已知后缀就按路径校验（普通数值/开关不查）。"""
    value = str(token or "").strip()
    if not value or value.startswith("-"):
        return False
    if "/" in value or "\\" in value:
        return True
    return Path(value).suffix.lower() in {
        ".py", ".json", ".md", ".csv", ".txt", ".xlsx", ".docx", ".pptx",
        ".pdf", ".png", ".jpg", ".jpeg", ".svg", ".yaml", ".yml", ".bib",
    }


def resolve_script(file_path: str, base_dir=None) -> Path:
    """解析脚本路径：项目内相对路径，或本角色 ``skill://<技能名>/scripts/<脚本>.py``。

    只做路径边界校验——解释器由系统固定，不接受命令行、内联代码或其它运行时。
    """
    from backend.utils import paths

    raw = str(file_path or "").strip()
    if not raw:
        raise RuntimeError("file_path 不能为空")
    if raw.lower().endswith((".pyc", ".pyo")):
        raise RuntimeError("file_path 必须是源码 .py 脚本")
    roots = (
        paths.current()
        if (paths.current() is not None and paths.current().workspace == paths.real(base_dir))
        else paths.Roots(base_dir)
    )
    try:
        named = paths.parse_skill_uri(raw)
        if named is not None:
            relative = named[1].replace("\\", "/")
            if not relative.startswith("scripts/"):
                raise RuntimeError("技能脚本入口必须位于 scripts 目录")
            resolved = roots.resolve_skill(named[0], named[1])
        else:
            resolved = roots.resolve(raw, base_dir=base_dir)
    except paths.PathDenied as error:
        raise RuntimeError(
            f"{error}；技能自带脚本用 skill://<技能名>/scripts/<脚本>.py"
        ) from None
    if resolved.suffix.lower() != ".py":
        raise RuntimeError("file_path 必须是 .py 脚本")
    if not resolved.is_file():
        raise RuntimeError(f"脚本不存在：{raw}")
    return resolved


def check_args(args: list[str], base_dir=None) -> list[str]:
    """参数里的路径也要在项目内：只校验像路径的 token，普通开关与数值放行。"""
    from backend.utils import paths

    values = []
    for token in args or []:
        value = str(token)
        values.append(value)
        if not looks_like_path(value):
            continue
        roots = (
            paths.current()
            if (paths.current() is not None and paths.current().workspace == paths.real(base_dir))
            else paths.Roots(base_dir)
        )
        try:
            roots.resolve(value, base_dir=base_dir)
        except paths.PathDenied as error:
            raise RuntimeError(str(error)) from None
    return values


def run_code_target(
    file_path: str = '',
    args: Optional[list] = None,
    timeout: Optional[int] = 60,
    base_dir=None
) -> str:
    """跑一个脚本（可带参数）：脚本与参数各自独立校验，执行走同一套沙箱。"""
    output=run_one(file_path, args, timeout, base_dir)
    return '[run_code] exit_code=0\n' + output


def run_one(file_path: str, args: Optional[list], timeout: Optional[int], base_dir) -> str:
    """跑一个脚本，返回合并后的输出；非零退出与超时都抛 RuntimeError。"""
    script = resolve_script(file_path, base_dir)
    script_args = check_args(args, base_dir)
    seconds = timeout_seconds(timeout)
    from backend.utils import paths
    from backend.security.policy import settings
    roots=paths.current()
    if roots is None or roots.workspace!=paths.real(base_dir):
        roots=paths.Roots(base_dir)
    workspace=Path(base_dir or Path.cwd()).resolve()
    backend=Path(__file__).resolve().parents[1]
    if backend.is_relative_to(workspace):
        raise RuntimeError('不能把服务资源根目录作为脚本工作区，请使用独立项目目录')
    skill_names=roots.skills if roots.skills is not None else {p.name for p in paths.SKILLS_DIR.iterdir() if p.is_dir()}
    from uuid import uuid4
    temp=workspace/'internal'/'.execution'/uuid4().hex
    temp.mkdir(parents=True,exist_ok=True)
    limits=settings()
    policy={'workspace':str(workspace),'temp':str(temp),'write':roots.access!='read-only',
            'session':str(roots.session_dir) if roots.session_dir else None,
            'skill_roots':[str(paths.SKILLS_DIR/name) for name in sorted(skill_names)],
            'vendor_execution':bool(skill_names & {'research-figures','citation-management'}),
            'network_scripts':[str(paths.SKILLS_DIR/name) for name in limits['network_scripts'] if name.split('/')[0] in skill_names],
            'hosts':limits['hosts'],'academic_suffixes':limits['academic_suffixes'],'ports':limits['ports'],
            'blocked_names':limits['blocked_names'],
            'fonts':[str(Path(os.environ.get('SystemRoot','C:/Windows'))/'Fonts'),'/usr/share/fonts','/usr/local/share/fonts']}
    policy_directory=tempfile.TemporaryDirectory(prefix='peifeng-policy-')
    policy_file=Path(policy_directory.name)/'policy.json'
    policy_file.write_text(json.dumps(policy),encoding='utf-8')
    bootstrap=backend/'security'/'runner.py'
    environment=runner_env(base_dir)
    for key in ['TMP','TEMP','TMPDIR','HOME','USERPROFILE','LOCALAPPDATA','APPDATA','XDG_CACHE_HOME']:
        environment[key]=str(temp)
    environment['MPLCONFIGDIR']=str(temp/'matplotlib')
    command=[sys.executable,'-I','-B',str(bootstrap),str(policy_file),str(script),*script_args]
    proc = subprocess.Popen(command,shell=False,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                            env=environment,cwd=str(workspace),**isolate_kwargs())
    try:
        out, err = proc.communicate(timeout=seconds)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        out, err = proc.communicate()
        seen = decode(out) + decode(err)
        detail = f"\n已产生的输出：\n{seen.strip()}" if seen.strip() else ""
        policy_directory.cleanup()
        raise RuntimeError(f"命令超时（>{seconds}s），已终止该进程树：{command}{detail}") from None
    policy_directory.cleanup()
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
        import psutil
        try:
            parent = psutil.Process(proc.pid)
            children = parent.children(recursive=True)
            for child in children:
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            parent.kill()
            psutil.wait_procs(children + [parent], timeout=5)
        except psutil.NoSuchProcess:
            pass
        return
    try:
        os.killpg(os.getpgid(proc.pid), 9)
    except (ProcessLookupError, PermissionError):
        proc.kill()


def run_code(targets: list[dict], base_dir=None, roots=None) -> str:
    """统一批处理入口，每项独立参数。"""
    from backend.utils import paths

    paths.bind(roots or paths.Roots(base_dir))

    return run_targets(lambda target: run_code_target(**target, base_dir=base_dir), targets, workers=1, base_dir=base_dir)
