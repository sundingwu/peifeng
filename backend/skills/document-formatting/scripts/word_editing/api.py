"""Word 编辑接口：直接组合 XML 操作，保持原文件和原子发布约定。"""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import zipfile

from .comment import add_comment
from .merge_runs import merge_runs
from .office.helpers import safe_extract, rezip
from .office.validators import DOCXSchemaValidator, RedliningValidator


def edit_package(source: Path, output: Path, *, text=None, author='培风', parent=None) -> str:
    with tempfile.TemporaryDirectory(dir=output.parent) as folder:
        unpacked = Path(folder)
        with zipfile.ZipFile(source) as archive:
            safe_extract(archive, unpacked)
        if text is not None:
            _, _, message = add_comment(unpacked, text, author=author, initials='培风', parent_id=parent)
        else:
            _, message = merge_runs(str(unpacked))
            if message.startswith('Error'):
                raise ValueError(message)
        rezip(unpacked, output)
    return message


def validate_package(source: Path, original: Path | None = None, author: str | None = None) -> str:
    if author is not None and original is None:
        raise ValueError('修订作者校验需要 original')
    with tempfile.TemporaryDirectory() as folder:
        unpacked = Path(folder)
        with zipfile.ZipFile(source) as archive:
            safe_extract(archive, unpacked)
        validators = [DOCXSchemaValidator(unpacked, original, verbose=False)]
        if author is not None:
            validators.append(RedliningValidator(unpacked, original, verbose=False))
        messages = StringIO()
        with redirect_stdout(messages):
            checks = [validator.validate() for validator in validators]
        log = messages.getvalue()
        if not all(checks):
            raise ValueError('Word XML校验失败：' + log[-12000:])
    return log + 'All validations PASSED!'
