"""直接检查PPTX包、媒体、字体与动画；不渲染、不修改源文件。"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent))
from presentation.pptx_delivery_check import audit_pptx_delivery


def check_file(path: Path, expected_pages: int | None = None) -> dict:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    report=audit_pptx_delivery(path)
    errors=list(report['errors'])
    count = report['slides']['count']
    if expected_pages is not None and count != expected_pages:
        errors.append({'message': f'实际页数{count}不等于要求{expected_pages}'})
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        errors.append({'message': '检查期间源文件发生变化'})
    warnings = report.get('advisories', [])
    return {'status': 'fail' if errors else 'warn' if warnings else 'pass', 'pages': count,
            'errors': errors, 'warnings': warnings, 'source_sha256': digest, 'report': report,
            'scope': '原生包、媒体、字体、隐藏页及动画检查；不等于渲染或视觉验收。'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path)
    parser.add_argument('--base-dir', type=Path, default=Path.cwd())
    parser.add_argument('--path', type=Path)
    parser.add_argument('--expected-pages', type=int)
    args = parser.parse_args()
    try:
        options = json.loads(args.input.read_text(encoding='utf-8-sig')) if args.input else {}
        path = args.path or Path(options['path'])
        if not path.is_absolute():
            path = args.base_dir / path
        result = check_file(path, args.expected_pages if args.expected_pages is not None else options.get('expected_pages'))
        print(json.dumps(result, ensure_ascii=False))
        if result['errors']:
            raise SystemExit(1)
    except Exception as error:
        print(json.dumps({'status': 'failed', 'error': str(error)}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
