"""技能公共文件事务：项目边界、原子发布、来源冲突检查与结构化回执。"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from backend.utils import paths
from backend.utils.files import file_lock, make_backup
_UNSET = object()

def project_path(value: str, base_dir=None) -> Path:
    if not str(value or '').strip():
        raise ValueError('路径不能为空')
    root = Path(base_dir or Path.cwd()).resolve()
    target = (root / value).resolve()
    if not target.is_relative_to(root):
        raise ValueError('文档路径必须位于当前项目内')
    return target

def file_digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None

@contextmanager
def atomic_output(target: Path, *, expected=_UNSET, sources=()):
    """先生成候选文件，来源未变且目标未被其他操作更新时才发布。"""
    target = target.resolve()
    expected = file_digest(target) if expected is _UNSET else expected
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.document-', dir=target.parent) as folder:
        candidate = Path(folder) / ('candidate' + target.suffix)
        yield candidate
        if not candidate.is_file() or not candidate.stat().st_size:
            raise ValueError('未生成有效的候选文件')
        with file_lock(target):
            if file_digest(target) != expected:
                raise RuntimeError('目标文件在制作期间改变，未覆盖，请重新读取后重试')
            for source, digest in sources:
                if file_digest(source) != digest:
                    raise RuntimeError('来源文件在制作期间改变，未发布结果')
            if target.exists():
                roots = paths.current()
                root = roots.workspace if roots and roots.workspace and target.is_relative_to(roots.workspace) else target.parent
                make_backup(root, root / 'internal', 'document-skill', [(str(target.relative_to(root)), 'overwrite')])
            candidate.replace(target)

def run_cli(process, fields: tuple[str, ...] = ()) -> None:
    """技能脚本的统一 CLI。

    两种用法：
    - ``--input x.json``：完整参数对象或 targets 数组（复杂用例、批量）；
    - 直给字段：``--kind line --output trend.png --series '[{"name":"华东","values":[1,2]}]'``。
      简单用例不必先写一个 JSON 文件——那层"填表"正是模型绕过技能的原因。

    ``fields`` 由各脚本声明自己能直给的键；值若像 JSON（以 ``[``/``{`` 开头）就解析成对象。
    """
    parser = argparse.ArgumentParser(description="技能脚本：--input JSON，或直给字段")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--base-dir", type=Path, default=Path.cwd())
    for name in fields:
        parser.add_argument("--" + name.replace("_", "-"), dest=name)
    args = parser.parse_args()

    results, files = ([], [])
    try:
        base = args.base_dir.resolve()
        paths.bind(paths.Roots(base))
        if args.input is not None:
            payload = json.loads(args.input.read_text(encoding="utf-8-sig"))
        else:
            payload = {}
            for name in fields:
                value = getattr(args, name, None)
                if value is None:
                    continue
                text_value = str(value).strip()
                if text_value[:1] in "[{":
                    try:
                        payload[name] = json.loads(text_value)
                        continue
                    except ValueError:
                        pass
                payload[name] = value
            if not payload:
                parser.error("需要 --input，或至少一个直给字段")
        targets = payload.get("targets", [payload]) if isinstance(payload, dict) else payload
        if not isinstance(targets, list) or not targets or any((not isinstance(row, dict) for row in targets)):
            raise ValueError("输入必须为一个参数对象或非空targets数组")
        for number, target in enumerate(targets, 1):
            try:
                result = process(dict(target), base)
                results.append({"index": number, "status": "completed", "result": result})
                if isinstance(result, dict):
                    files.extend(result.get("files", []))
            except Exception as error:
                results.append({"index": number, "status": "failed", "error": str(error)})
        failed = any((row["status"] == "failed" for row in results))
        print(json.dumps({"status": "partial_failure" if failed else "completed",
                          "files": list(dict.fromkeys(files)), "results": results}, ensure_ascii=False))
        if failed:
            raise SystemExit(1)
    except SystemExit:
        raise
    except Exception as error:
        print(json.dumps({"status": "failed", "error": str(error), "files": files}, ensure_ascii=False))
        raise SystemExit(1)
