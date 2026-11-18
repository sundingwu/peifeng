"""word_markdown.config — 配置与资源路径解析器。

提供预设目录、CSL 目录、基础模板等的统一定位逻辑。
"""
from __future__ import annotations
from pathlib import Path
from typing import Optional
PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_PRESET_ZH = 'zh-academic'
DEFAULT_PRESET_EN = 'en-nature'
WORKSPACE_DIR = PACKAGE_DIR.parent.parent

def get_presets_dir() -> Path:
    return PACKAGE_DIR.parent.parent / 'assets' / 'markdown/presets'

def get_resources_dir() -> Path:
    return PACKAGE_DIR.parent.parent / 'assets' / 'markdown'

def get_csl_dir() -> Path:
    """获取 CSL 样式文件目录 resources/csl/。"""
    return get_resources_dir() / 'csl'

def get_base_template_path() -> Path:
    """获取纯净现代底模 pure_modern_base.docx。"""
    return get_resources_dir() / 'pure_modern_base.docx'

def resolve_preset_path(preset_name: str) -> Optional[Path]:
    """根据预设名称（如 zh-academic、en-nature）返回预设文件路径。"""
    clean_name = preset_name.strip().removesuffix('.txt')
    presets_dir = get_presets_dir()
    target = presets_dir / f'{clean_name}.txt'
    if target.is_file():
        return target
    return None
DEFAULT_CSL_ZH = 'chinese-gb7714-2015-numeric.csl'
DEFAULT_CSL_EN = 'apa.csl'

def looks_chinese(text: str, threshold: float=0.05) -> bool:
    """正文是不是中文写的。

    只数**正文**里的汉字占比：`%% 表题`、`%% 图片题注` 这些样式名是词表的一部分，
    英文稿里也会出现，拿它们判断语种会把英文稿认成中文稿。行首 `%%` 的行整行跳过。
    """
    lines = [line for line in (text or '').split('\n') if not line.lstrip().startswith('%%')]
    dense = [ch for ch in ''.join(lines) if not ch.isspace()]
    if not dense:
        return False
    cjk = sum((1 for ch in dense if '一' <= ch <= '鿿'))
    return cjk / len(dense) > threshold

def default_csl_path(text: str) -> Path:
    """按稿件的字种挑默认 CSL：中文稿走 GB/T 7714 数字制，其余走 APA。"""
    return get_csl_dir() / (DEFAULT_CSL_ZH if looks_chinese(text) else DEFAULT_CSL_EN)

def default_crossref_prefixes(chinese: bool) -> dict[str, str]:
    """交叉引用前缀的默认值：`@eq-a` 该长成「式1」还是「Equation 1」。"""
    if chinese:
        return {'eq-prefix': '式', 'fig-prefix': '图', 'tbl-prefix': '表'}
    return {'eq-prefix': 'Equation ', 'fig-prefix': 'Figure ', 'tbl-prefix': 'Table '}
