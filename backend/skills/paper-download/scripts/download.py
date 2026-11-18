'下载一份公开文档，并可选地验证 DOI 注册表元数据。'
from pathlib import Path
import sys
for parent in Path(__file__).resolve().parents:
    if (parent / 'backend').is_dir():
        sys.path.insert(0, str(parent))
        break

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.request import Request
from backend.security.policy import open_public as urlopen
from backend.utils.skills import resolve_file, dispatch
from backend.utils.skills import load_skill_script


def download(target, base_dir=None):
    url = target['url']
    if urlparse(url).scheme not in {'http', 'https'}:
        raise ValueError('仅支持公开HTTP/HTTPS下载，不使用账号或授权')
    metadata = None
    if target.get('doi'):
        metadata = load_skill_script('researcher', 'paper-download', 'doi').lookup({'doi': target['doi']})
        if not metadata:
            raise ValueError('DOI未返回元数据，未开始下载')
    output = resolve_file(target['output'], base_dir)
    with urlopen(Request(url, headers={'User-Agent':'PeiFeng/1.0'}), timeout=60) as response:
        data = response.read(40*1024*1024+1)
        final_url = response.url
    if len(data)>40*1024*1024 or not data:
        raise ValueError('文档为空或超过40MB')
    if output.suffix.lower()=='.pdf' and not data.startswith(b'%PDF'):
        raise ValueError('返回内容不是PDF，可能是登录页或错误页；未保存')
    if output.suffix.lower()=='.pdf':
        import io
        from pypdf import PdfReader
        if not PdfReader(io.BytesIO(data)).pages:
            raise ValueError('PDF没有页面')
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=output.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return json.dumps({'output':str(output),'source_url':final_url,'retrieved_at_utc':datetime.now(timezone.utc).isoformat(),'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'doi_metadata':metadata,'document_identity':'not_checked','scope':'DOI注册记录不证明下载文件与该论文相同；需read核对标题作者和正文'}, ensure_ascii=False)


def run(targets, base_dir=None):
    return dispatch(download, targets, base_dir)


if __name__ == '__main__':
    from backend.utils.skills import run_script_cli
    raise SystemExit(run_script_cli(run, __doc__))
