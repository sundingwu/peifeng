"""技能脚本运行时：按技能名加载 scripts、CLI 入口，以及研究工具的目标分发。"""

from __future__ import annotations

from pathlib import Path

from backend.orchestration.orchestrate import load_module
from backend.utils.web import run_targets



# ---------------------------------------------------------------- skill_runtime
def load_skill_script(agent: str, skill: str, module: str):
    """按技能名定位脚本：``backend/skills/<技能>/scripts/<模块>.py``。

    技能平铺后与角色无关，``agent`` 参数只为兼容既有调用点保留，不参与路径解析。
    """
    path = Path(__file__).resolve().parents[1] / "skills" / skill / "scripts" / (module + ".py")
    return load_module(path)


def run_script_cli(function, description=None):
    """Run skill targets independently and expose partial failures as nonzero exit."""
    import argparse
    import json
    import sys
    for stream in [sys.stdout, sys.stderr]:
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('--input', type=Path, required=True, help='UTF-8 JSON target, array or targets object')
    parser.add_argument('--base-dir', type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        payload = json.loads(args.input.read_text(encoding='utf-8'))
        targets = payload.get('targets', [payload]) if isinstance(payload, dict) else payload
        if not isinstance(targets, list) or not targets or any(not isinstance(target, dict) for target in targets):
            raise ValueError('输入必须包含非空目标对象数组')
    except (OSError, ValueError) as error:
        print(json.dumps({'status':'failed','error':str(error)},ensure_ascii=False))
        return 2
    results = []
    for index, target in enumerate(targets, 1):
        try:
            value = function([target], args.base_dir)
            if getattr(value, 'has_errors', False):
                raise RuntimeError(str(value))
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                pass
            results.append({'index':index,'status':'completed','result':value})
        except Exception as error:
            results.append({'index':index,'status':'failed','error':str(error)})
    failed = any(row['status']=='failed' for row in results)
    print(json.dumps({'status':'partial_failure' if failed else 'completed','results':results},ensure_ascii=False))
    return 1 if failed else 0


# ---------------------------------------------------------------- research_tools
def tool_schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {'name': name, 'description': description, 'parameters': {'type': 'object', 'properties': {'targets': {'type': 'array', 'minItems': 1, 'items': {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}}}, 'required': ['targets'], 'additionalProperties': False}}

def resolve_file(value: str, base_dir=None) -> Path:
    path = Path(value)
    return path if path.is_absolute() else Path(base_dir or '.') / path

def dispatch(function, targets, base_dir=None):
    return run_targets(lambda target: function(target, base_dir), targets, workers=1, base_dir=base_dir)

def statistics_target(target, base_dir=None):
    from backend.utils.skills import load_skill_script
    return load_skill_script('engineer', 'data-analysis', 'analyze_statistics').statistics_target(target, base_dir)


def experiment_target(target, base_dir=None):
    from backend.utils.skills import load_skill_script
    return load_skill_script('engineer', 'experiments', 'experiment').experiment_target(target, base_dir)


def paper_target(target, base_dir=None):
    from backend.utils.skills import load_skill_script
    return load_skill_script('researcher', 'paper-reading', 'extract_pdf').paper_target(target, base_dir)


def audit_target(target, base_dir=None):
    from backend.utils.skills import load_skill_script
    return load_skill_script('mentor', 'research-review', 'audit').audit_target(target, base_dir)
