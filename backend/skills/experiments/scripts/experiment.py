from pathlib import Path
import sys
for _parent in Path(__file__).resolve().parents:
    if (_parent / 'backend').is_dir():
        sys.path.insert(0, str(_parent))
        break
import hashlib, json, platform, sys
from datetime import datetime, timezone
from backend.utils.skills import resolve_file, dispatch
from backend.memory.store import write_json

def experiment_target(target: dict, base_dir=None) -> str:
    """Synchronous bounded Python run with a durable provenance receipt."""
    from backend.tools.run_code import check_args, resolve_script, timeout_seconds
    script = resolve_file(target['path'], base_dir).resolve()
    if script.suffix.lower() != '.py' or not script.is_file():
        raise ValueError('path必须是已存在的Python脚本')
    output = resolve_file(target['output'], base_dir)
    receipt = {'script': str(script), 'script_sha256': hashlib.sha256(script.read_bytes()).hexdigest(), 'python': sys.version, 'platform': platform.platform(), 'started_at': datetime.now(timezone.utc).isoformat(), 'arguments': target.get('arguments', [])}
    try:
        # 路径与参数都过同一套白名单；子进程只起 [解释器, 脚本, *参数]，
        # 这正是审计钩子允许的形态（不能再嵌一层 runner，否则会被拒绝）。
        import subprocess
        checked = [sys.executable, str(resolve_script(target['path'], base_dir)),
                   *check_args(receipt['arguments'], base_dir)]
        result = subprocess.run(checked, cwd=str(base_dir), capture_output=True, text=True,
                                encoding='utf-8', errors='replace',
                                timeout=timeout_seconds(target.get('timeout', 60)))
        receipt['log'] = result.stdout + result.stderr
        if result.returncode:
            raise RuntimeError(f'实验退出码 {result.returncode}：' + receipt['log'])
        receipt['status'] = 'completed'
    except Exception as error:
        receipt.update(status='failed', error=str(error))
        raise
    finally:
        receipt['finished_at'] = datetime.now(timezone.utc).isoformat()
        write_json(output, receipt)
    return json.dumps(receipt, ensure_ascii=False)

def run(targets, base_dir=None):
    return dispatch(experiment_target, targets, base_dir)

if __name__ == '__main__':
    from backend.utils.skills import run_script_cli
    raise SystemExit(run_script_cli(run, __doc__))
