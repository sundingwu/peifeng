import json
import tempfile
import unittest
import shutil
import subprocess
import sys
from pathlib import Path

from backend.utils.paths import Roots
from backend.tools.write import write


class TraceToolFixTests(unittest.TestCase):
    def test_generated_summary_reproduction_runs_without_installed_skills(self):
        from backend.utils.skills import load_skill_script
        module = load_skill_script('engineer', 'data-analysis', 'summarize_table')
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'original'
            source.mkdir()
            (source / 'data.csv').write_text('class,score\nA,80\nA,90\nB,70\nB,80\nB,90\n', encoding='utf-8')
            receipt = json.loads(module.summarize({'path': 'data.csv', 'group': 'class', 'value': 'score',
                         'metrics': ['count', 'median'], 'precision': 2,
                         'output': 'nested/summary.csv', 'reproduce': 'nested/reproduce.py'}, base_dir=source))
            from backend.tools.run_code import run_one
            self.assertIn('records', run_one('nested/reproduce.py', ['--base-dir', '.'], 30, source))
            expected = (source / 'nested/summary.csv').read_bytes()
            fresh = Path(folder) / 'fresh'
            (fresh / 'nested').mkdir(parents=True)
            shutil.copy2(source / 'data.csv', fresh / 'data.csv')
            shutil.copy2(source / 'nested/reproduce.py', fresh / 'nested/reproduce.py')
            result = subprocess.run([sys.executable, '-X', 'utf8', str(fresh / 'nested/reproduce.py')],
                                    cwd=fresh, capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((fresh / 'nested/summary.csv').read_bytes(), expected)
            self.assertEqual((fresh / 'data.csv').read_bytes(), (source / 'data.csv').read_bytes())
            self.assertNotIn('from agent', (fresh / 'nested/reproduce.py').read_text(encoding='utf-8'))

    def test_structured_json_preserves_unicode_escapes_and_nested_values(self):
        with tempfile.TemporaryDirectory() as folder:
            payload = {'说明': '首行\n次行 "引号" \\反斜杠', 'items': [0, False, None, {'x': 2.5}]}
            result = write([{'path': 'input.json', 'json_data': payload}], base_dir=folder)
            self.assertIn('已写入', result)
            self.assertEqual(json.loads((Path(folder) / 'input.json').read_text(encoding='utf-8')), payload)

    def test_conflicting_json_mode_does_not_overwrite_existing_file(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'input.json'
            target.write_text('original', encoding='utf-8')
            for extra in [{'content': 'bad'}, {'append': True}, {'edits': []}, {'facts': 'facts.json'}]:
                with self.assertRaises(ValueError):
                    write([{'path': 'input.json', 'json_data': {'x': 1}, **extra}], base_dir=folder)
                self.assertEqual(target.read_text(encoding='utf-8'), 'original')

    def test_final_response_misrouted_to_write_is_not_written(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, '直接作为最终回复'):
                write([{'content': json.dumps({'status': 'completed', 'summary': 'done'})}], base_dir=folder)
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_structured_json_cannot_bypass_project_path_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'project'
            root.mkdir()
            with self.assertRaises(RuntimeError):
                write([{'path': '../outside.json', 'json_data': {'x': 1}}],
                      base_dir=root, roots=Roots(root))
            self.assertFalse((Path(folder) / 'outside.json').exists())
