'复用技能自有的分组实现来进行绘图准备。'
from pathlib import Path
import sys
for parent in Path(__file__).resolve().parents:
    if (parent / 'backend').is_dir():
        sys.path.insert(0, str(parent))
        break
from backend.utils.skills import load_skill_script, run_script_cli


def run(targets, base_dir=None):
    return load_skill_script('engineer', 'data-analysis', 'summarize_table').run(targets, base_dir)


if __name__ == '__main__':
    raise SystemExit(run_script_cli(run, __doc__))
