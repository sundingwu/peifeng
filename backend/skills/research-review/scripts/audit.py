from pathlib import Path
import sys
for _parent in Path(__file__).resolve().parents:
    if (_parent / 'backend').is_dir():
        sys.path.insert(0, str(_parent))
        break
import hashlib, json, sys
from backend.utils.skills import resolve_file, dispatch
def upstream_module(relative):
    from backend.orchestration.orchestrate import load_module
    return load_module(Path(__file__).parent / relative)

def audit_target(target: dict, base_dir=None) -> str:
    '仅进行确定性检查；语义支持由Mentor负责。'
    path = resolve_file(target['path'], base_dir)
    operation = target.get('operation', 'fingerprint')
    if operation == 'fingerprint':
        result = {'status': 'pass', 'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'bytes': path.stat().st_size}
    elif operation == 'quote':
        quote = target.get('quote', '')
        if not quote:
            raise ValueError('quote不能为空')
        if path.suffix.lower() == '.pdf':
            from pypdf import PdfReader
            matches = [i+1 for i,page in enumerate(PdfReader(path).pages) if quote in (page.extract_text() or '')]
        else:
            matches = [i+1 for i,line in enumerate(path.read_text(encoding='utf-8').splitlines()) if quote in line]
        result = {'status': 'pass' if matches else 'not_checked', 'locations': matches, 'scope': '仅字面匹配，不证明语义支持或扫描件中不存在'}
    elif operation == 'office':
        from backend.utils.office import office_inspect
        return office_inspect([{'path': str(path)}])
    elif operation == 'reader_math':
        from dataclasses import asdict
        module=upstream_module('validate_reader_math.py')
        source_map=resolve_file(target['source_map'],base_dir) if target.get('source_map') else None
        findings=module.run_validation(path,source_map)
        result={'status':'fail' if any(row.severity=='FAIL' for row in findings) else 'warn' if findings else 'pass','findings':[asdict(row) for row in findings],'scope':'原库公式与source_map规则；不证明公式推导正确'}
    elif operation == 'paper_card':
        module=upstream_module('audit_paper_card.py')
        bundle=json.loads(resolve_file(target['bundle'],base_dir).read_text(encoding='utf-8')) if target.get('bundle') else None
        mode=target.get('locator_mode','page-grounded')
        if mode not in module.LOCATOR_MODES:
            raise ValueError('locator_mode必须使用原库支持的定位模式')
        if mode=='page-grounded' and bundle is None:
            raise ValueError('page-grounded审查需要bundle')
        result=module.audit(path.read_text(encoding='utf-8'),bundle,mode)
    elif operation == 'response_package':
        from dataclasses import asdict
        module=upstream_module('check_package_consistency.py')
        clean=resolve_file(target['clean'],base_dir) if target.get('clean') else None
        marked=resolve_file(target['marked'],base_dir) if target.get('marked') else None
        if (clean is None)!=(marked is None):
            raise ValueError('clean和marked必须一起提供')
        findings=module.run_checks(path,resolve_file(target['response'],base_dir),clean,marked)
        result={'status':'fail' if findings else 'pass','findings':[asdict(row) for row in findings],'scope':'原库LaTeX/文本修订包一致性检查，不编译TeX'}
    elif operation == 'submission':
        import subprocess,tempfile,os
        script=Path(__file__).parent/'verify_submission_package.py'
        with tempfile.TemporaryDirectory(prefix='peifeng-audit-') as folder:
            report=Path(folder)/'report.json'
            command=[sys.executable,str(script),str(path.resolve()),'--report-out',str(report)]
            for field in ['passport','join_map','venue_profile']:
                if target.get(field):
                    command.extend(['--'+field.replace('_','-'),str(resolve_file(target[field],base_dir).resolve())])
            run=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=60,env={**os.environ,'PYTHONIOENCODING':'utf-8','PYTHONDONTWRITEBYTECODE':'1'})
            if not report.is_file() or run.returncode==2:
                raise RuntimeError('原库交付审计未完成：'+run.stderr[-2000:])
            result=json.loads(report.read_text(encoding='utf-8'))
            result['verifier_exit_code']=run.returncode
    else:
        raise ValueError('不支持的audit operation')
    return json.dumps(result, ensure_ascii=False)

def run(targets, base_dir=None):
    return dispatch(audit_target, targets, base_dir)

if __name__ == '__main__':
    from backend.utils.skills import run_script_cli
    raise SystemExit(run_script_cli(run, __doc__))
