"""word_markdown.validator.check — 稿件导前校验门禁。

按后果分两级，判据只有一条：

- **阻断（error）**：产物会静默出错，而且错了看不出来——内容丢字、变成乱码、格式静默不生效。
- **提醒（warning）**：产物看得见，只是不合规——多出一页、宽度被钳、题注离得远、标题跳级。

提醒不阻塞导出：作者可能是有意为之，也可能打算在 Word 里收尾。把这类情况做成硬失败，
只会把人逼去用 `--no-check`，连真正的阻断也一起关掉。
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import yaml
from word_markdown.config import get_csl_dir
from word_markdown.i18n import t
from word_markdown.model.reader import inline_residue
from word_markdown.spec.constants import PAGE_NUMBER_FMT, ROW_NAME_EN
from word_markdown.spec.models import TemplateSpec
from word_markdown.spec.parser import parse_format
from word_markdown.preprocessor.blocks import split_style_line, style_line_issues, style_line_names
from word_markdown.preprocessor.blocks import resolve_style_name
from word_markdown.template.generator import PAGE_SIZES_CM, max_doc_grid_lines, missing_styles_for_body
from word_markdown.utils.pdf_figure import PDF_SUFFIX, RASTER_SUFFIXES, PdfFigureError, page_count
from word_markdown.utils.skill_utils import block_marker_issues, soft_wrap_issues, citation_key_issues, citations_without_bibliography, dialect_issues, has_declared_image_width, heading_issues, image_width_issues, malformed_table_issues, mask_code_fences, parse_bib_keys, resolve_manuscript_path, section_pagebreak_issues, split_frontmatter, unbalanced_math_issues, math_comment_issues, undefined_crossref_issues, unknown_shortcode_issues
_GROUP_LIMIT = 8

def _group_warn(report: ValidationReport, tag: str, items: list[tuple[int, str]], lang: str) -> None:
    """同一类提醒合并成一条：一处照旧逐行说，多处只列行号。

    门禁的提醒是按行产生的，一稿折行三十处就会刷三十行，真正要紧的东西被冲掉。
    合并后每类只有一条，行号照给，改起来一样清楚。
    """
    if not items:
        return
    if len(items) == 1:
        report.warnings.append(t('gate.line', lang, line=items[0][0], tag=tag, why=items[0][1]))
        return
    sep = t('gate.list_sep', lang)
    lines = sep.join((str(line_no) for line_no, _ in items[:_GROUP_LIMIT]))
    if len(items) > _GROUP_LIMIT:
        lines += t('gate.line_more', lang, count=len(items) - _GROUP_LIMIT)
    report.warnings.append(t('gate.line_grouped', lang, tag=tag, count=len(items), lines=lines, why=items[0][1]))

@dataclass
class ValidationReport:
    """导前校验结果报告。"""
    valid: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

def caption_adjacency_issues(md_text: str, lang: str='zh') -> list[tuple[int, str]]:
    """题注是否紧邻其实体（表格/图片）——孤立题注在成稿里很明显，提醒即可。

    题注是独立段落（`%% 表题 …` / `%% 图片题注 …`），所以"紧邻"要跨过空行与表注：
    中间只要夹了一句正文，就认为断开。
    """
    masked = mask_code_fences(md_text)
    cleaned = re.sub('<!--.*?-->', lambda m: '\n' * m.group(0).count('\n'), masked, flags=re.DOTALL)
    lines = cleaned.split('\n')

    def is_table_row(line: str) -> bool:
        s = line.strip()
        return s.startswith('|') and s.endswith('|') or s.startswith('|---')

    def is_image_entity(line: str) -> bool:
        return bool(re.search('!\\[[^\\]]*\\]\\([^)]*\\)', line))

    def kind_of(line: str) -> str | None:
        split = split_style_line(line)
        if split is None:
            return None
        resolved = resolve_style_name(split[0])
        return {'Table Caption': 'table', 'Image Caption': 'image', 'Table Note': 'note'}.get(resolved)

    def scan(start: int, step: int) -> tuple[bool, bool]:
        """从 start 出发单向扫描，遇到正文就停；返回 (见到表格, 见到图片)。"""
        table = image = False
        i = start
        while 0 <= i < len(lines):
            line = lines[i].strip()
            if not line:
                i += step
                continue
            if kind_of(line) == 'note':
                i += step
                continue
            if is_table_row(line):
                return (True, image)
            if is_image_entity(line):
                return (table, True)
            if kind_of(line) is not None:
                i += step
                continue
            break
        return (table, image)
    issues: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        kind = kind_of(line)
        if kind not in ('table', 'image'):
            continue
        table_near = scan(i - 1, -1)[0] or scan(i + 1, 1)[0]
        image_near = scan(i - 1, -1)[1] or scan(i + 1, 1)[1]
        if kind == 'table' and (not table_near):
            issues.append((i + 1, t('gate.caption.table_far', lang)))
        elif kind == 'image' and (not image_near):
            issues.append((i + 1, t('gate.caption.image_far', lang)))
    return issues

def latex_tag_issues(md_text: str) -> list[tuple[int, str]]:
    """`\\tag{}` 不是本工具支持的编号写法，整条公式会退化成字面文本。

    公式编号只有 `{#eq-xxx}` 一种写法；写错就拦下，不做自动转换。
    """
    issues: list[tuple[int, str]] = []
    for i, line in enumerate(mask_code_fences(md_text).split('\n'), 1):
        if re.search('\\\\tag\\s*\\{', line):
            issues.append((i, line.strip()[:80]))
    return issues
_RESIDUE_EMPHASIS_RE = re.compile('\\*(?=[^\\s*])[^\\s*]+?(?<=[^\\s*])\\*|\\*\\*(?=[^\\s*])[^\\s*]+?(?<=[^\\s*])\\*\\*')

def _residue_hit(residue: str, spans: list[tuple[int, int]]) -> Optional[tuple[int, int]]:
    """残符里可疑的星号区间：形态像强调的配对，或紧贴着已解析标记的孤星。

    孤星那一条专治「三颗星连写切不开」：`*a **b***` 里解析器吃掉 `**b**`，剩下的星号贴在
    它两侧——形态上就是没解析完的标记，不能无声印出去。
    """
    hit = _RESIDUE_EMPHASIS_RE.search(residue)
    if hit:
        return hit.span()
    for i, ch in enumerate(residue):
        if ch == '*' and any((i == end or i + 1 == start for start, end in spans)):
            return (i, i + 1)
    return None

def literal_asterisk_issues(md_text: str) -> list[tuple[int, str]]:
    """形态像强调、却没被解析成强调的星号 → [(行号, 片段)]。

    这些星号会原样印进正文。判据只用解析器自己那份（`unparsed_stars`）：已经被当作语法
    的星号、`\\*` 转义、代码与公式里的星号都留不下来。所以符号注 `* P<0.05，** P<0.01`
    （开符后就是空格）与 `3*4` 这种孤立星号不会被报；报的是 `2*3*4`、`a*b*c` 这类词内
    乘号，以及历史上 `(1*S*,2*R*)` 那类漏解析。

    只提醒、不阻断：词内乘号的本意就是字面星号，写成 `\\*` 即可消掉本条。
    """
    issues: list[tuple[int, str]] = []
    masked = mask_code_fences(md_text)
    masked = re.sub('<!--.*?-->', lambda m: '\n' * m.group(0).count('\n'), masked, flags=re.DOTALL)
    for line_no, line in enumerate(masked.split('\n'), 1):
        if '*' not in line:
            continue
        hit = _residue_hit(*inline_residue(line))
        if hit:
            snippet = re.sub('\\s+', ' ', line[max(0, hit[0] - 6):hit[1] + 6]).strip()
            issues.append((line_no, snippet))
    return issues

def content_width_cm(spec: TemplateSpec) -> Optional[float]:
    """版心宽度（cm）= 纸张宽 − 左页边距 − 右页边距 − 装订线。取不到页边距时返回 None。"""
    if not spec.margins:
        return None
    width = PAGE_SIZES_CM.get(spec.page_size, PAGE_SIZES_CM['a4'])[0]
    return width - spec.margins['left'] - spec.margins['right'] - (spec.gutter_cm or 0)

def required_style_issues(md_text: str, spec: TemplateSpec, fm_data, lang: str='zh') -> list[tuple[int, str]]:
    """稿件用到了结构、要求文件却没声明对应样式 → 导出会静默回退默认样式。

    这是本工具最要紧的一道门禁：产物外观正常，但格式根本没映射上。
    """
    declared = {s.get('style_name') for s in spec.styles}
    if spec.table:
        declared.add('__ROW_table__')
    if spec.table_header:
        declared.add('__ROW_table_header__')
    _, body = split_frontmatter(md_text)
    start_line = md_text.count('\n') - body.count('\n') + 1
    issues: list[tuple[int, str]] = []
    reported: set[str] = set()
    for line_no, feat, cn, en in missing_styles_for_body(mask_code_fences(body), declared, start_line=start_line, lang=lang):
        reported.add(en)
        label = t('gate.label.row_settings', lang, name=cn if lang != 'en' else ROW_NAME_EN.get(cn, cn)) if en.startswith('__ROW_') else t('gate.label.style_def', lang, cn=cn, en=en)
        issues.append((line_no, t('gate.style.required_missing', lang, feat=feat, label=label)))
    if isinstance(fm_data, dict):
        for key, cn, en, feat_key in (('bibliography', '参考文献', 'Bibliography', 'gate.feat.bibliography_yaml'),):
            if fm_data.get(key) and en not in declared and (en not in reported):
                reported.add(en)
                issues.append((1, t('gate.bib.required_missing', lang, cn=cn, en=en, feat=t(feat_key, lang))))
    return issues

def validate_manuscript(md_path: Path | str, *, spec: Optional[TemplateSpec]=None, spec_path: Optional[Path | str]=None, csl_dir: Optional[Path]=None, lang: str='zh') -> ValidationReport:
    """对稿件进行全套严格校验，返回 ValidationReport。

    `lang` 决定报错语言（"zh"/"en"），默认中文——中文串是既有测试与既有用户的契约，
    英文只是新增的一条路径。判据、阈值、阻断/提醒的划分与语言无关。
    """
    md_path = Path(md_path)
    report = ValidationReport()
    if not md_path.exists():
        report.valid = False
        report.errors.append(t('gate.manuscript.missing', lang, path=md_path))
        return report
    try:
        md_text = md_path.read_text(encoding='utf-8')
    except Exception as e:
        report.valid = False
        report.errors.append(t('gate.manuscript.unreadable', lang, path=md_path, err=e))
        return report
    csl_dir = csl_dir or get_csl_dir()
    if spec is None and spec_path is not None:
        fmt_file = Path(spec_path)
        if not fmt_file.is_file():
            report.valid = False
            report.errors.append(t('gate.format.missing', lang, path=fmt_file))
            return report
        try:
            spec = parse_format(fmt_file.read_text(encoding='utf-8'))
        except Exception as e:
            report.valid = False
            report.errors.append(t('gate.format.parse_failed', lang, path=fmt_file, err=e))
            return report
    fm_raw, body_text = split_frontmatter(md_text)
    fm_offset = md_text.count('\n') - body_text.count('\n')
    fm_data = {}
    if fm_raw:
        try:
            fm_data = yaml.safe_load(fm_raw) or {}
        except Exception as e:
            report.valid = False
            report.errors.append(t('gate.yaml.parse_failed', lang, err=e))
    if isinstance(fm_data, dict):
        bib_value = fm_data.get('bibliography')
        if isinstance(bib_value, list) and len(bib_value) > 1:
            report.valid = False
            report.errors.append(t('gate.bib.multiple', lang, count=len(bib_value)))
        elif bib_value:
            single = bib_value[0] if isinstance(bib_value, list) else bib_value
            bib_path, how = resolve_manuscript_path(str(single), md_path)
            if not how:
                report.valid = False
                report.errors.append(t('gate.bib.missing', lang, path=single))
        if fm_data.get('csl'):
            csl_path, how = resolve_manuscript_path(str(fm_data['csl']), md_path, csl_dir)
            if not how:
                report.valid = False
                report.errors.append(t('gate.csl.missing', lang, path=fm_data['csl']))
        for key, form_key in (('title', 'gate.yaml.form.title'), ('subtitle', 'gate.yaml.form.subtitle'), ('author', 'gate.yaml.form.author'), ('date', 'gate.yaml.form.date'), ('abstract', 'gate.yaml.form.abstract'), ('keywords', 'gate.yaml.form.keywords'), ('reference-section-title', 'gate.yaml.form.reference_section_title')):
            if fm_data.get(key):
                message = t('gate.yaml.key_unused', lang, key=key, form=t(form_key, lang))
                if key == 'reference-section-title':
                    report.warnings.append(message)
                else:
                    report.valid = False
                    report.errors.append(message)
        if fm_data.get('bibliography') and (not fm_data.get('csl')):
            from word_markdown.config import default_csl_path
            report.warnings.append(t('gate.csl.default', lang, name=default_csl_path(md_text).name))
        for line_no, line in enumerate(mask_code_fences(md_text).split('\n'), 1):
            if re.search('\\[-@[\\w:.-]+', line):
                report.valid = False
                report.errors.append(t('gate.citation.suppress_author', lang, line=line_no))
        known = {'bibliography', 'csl', 'crossref', 'link-citations'}
        blocked = {'title', 'subtitle', 'author', 'date', 'abstract', 'keywords'}
        unknown = [key for key in fm_data or {} if key not in known and key not in blocked and (not str(key).startswith('reference-section'))]
        if unknown:
            report.warnings.append(t('gate.yaml.unknown', lang, keys=', '.join(sorted(map(str, unknown)))))
        if fm_data.get('bibliography'):
            bib_path, how = resolve_manuscript_path(str(fm_data['bibliography']), md_path)
            if how:
                try:
                    bib_text = Path(bib_path).read_text(encoding='utf-8')
                except Exception:
                    bib_text = ''
                known_keys = parse_bib_keys(bib_text)
                if not known_keys:
                    bare = citations_without_bibliography(body_text, fm_offset)
                    if bare:
                        report.valid = False
                        report.errors.append(t('gate.bib.empty', lang, name=Path(bib_path).name, count=len(bare)))
                cite_issues = citation_key_issues(body_text, known_keys, fm_offset, lang)
                if cite_issues:
                    report.valid = False
                    for line_no, why in cite_issues:
                        report.errors.append(t('gate.line', lang, line=line_no, tag=t('gate.tag.citation_key', lang), why=why))
        else:
            bare_cites = citations_without_bibliography(body_text, fm_offset)
            if bare_cites:
                report.valid = False
                sep = t('gate.list_sep', lang)
                shown = sep.join((t('gate.line_item', lang, line=n) for n in bare_cites[:5]))
                report.errors.append(t('gate.citation.no_bibliography', lang, count=len(bare_cites), shown=shown))
    for attr, name_key in (('equation_number_format', 'spec.line.equation_format'), ('appendix_equation_number_format', 'spec.line.appendix_equation_format')):
        eq_format = getattr(spec, attr, '') or ''
        if '{c}' not in eq_format:
            continue
        crossref = fm_data.get('crossref') if isinstance(fm_data, dict) else None
        by_chapter = isinstance(crossref, dict) and bool(crossref.get('chapters'))
        if not by_chapter:
            report.valid = False
            report.errors.append(t('gate.equation.chapters', lang, fmt=eq_format, name=t(name_key, lang)))
    if spec and spec.doc_grid_lines:
        limit = max_doc_grid_lines(spec)
        if limit and spec.doc_grid_lines > limit:
            report.warnings.append(t('gate.doc_grid.unreachable', lang, want=spec.doc_grid_lines, limit=limit))
    in_code = False
    for i, line in enumerate(md_text.split('\n'), 1):
        stripped = line.strip()
        if stripped.startswith('```') or stripped.startswith('~~~'):
            in_code = not in_code
        if in_code:
            continue
        for m in re.finditer('!\\[(.*?)\\]\\((.*?)\\)', line):
            raw_path = m.group(2).strip()
            if raw_path.startswith(('http://', 'https://', 'data:')):
                report.valid = False
                report.errors.append(t('gate.image.remote', lang, line=i, path=raw_path[:60]))
                continue
            if ' ' in raw_path and (not raw_path.startswith('<')) and (not re.search('["\\\'].*["\\\']', raw_path)):
                report.valid = False
                report.errors.append(t('gate.image.path_space', lang, line=i, path=raw_path))
            clean_path = raw_path.split()[0].strip('<>"\'')
            img_file = md_path.parent / clean_path
            if not img_file.exists():
                report.valid = False
                report.errors.append(t('gate.image.missing', lang, line=i, path=clean_path))
                continue
            suffix = img_file.suffix.lower()
            if suffix == PDF_SUFFIX:
                try:
                    pages = page_count(img_file)
                except PdfFigureError as exc:
                    report.valid = False
                    report.errors.append(t('gate.image.pdf_unreadable', lang, line=i, path=clean_path, err=exc))
                else:
                    if pages > 1:
                        report.warnings.append(t('gate.image.pdf_multipage', lang, line=i, path=clean_path, pages=pages))
            elif suffix not in RASTER_SUFFIXES:
                report.valid = False
                report.errors.append(t('gate.image.unsupported', lang, line=i, path=clean_path, ext=suffix or '（无扩展名）'))
    for i, line in enumerate(md_text.split('\n'), 1):
        stripped = line.strip()
        if not re.search('!\\[.*?\\]\\(', line) or stripped.startswith('```'):
            continue
        if not re.fullmatch('!\\[[^\\]]*\\]\\([^)]*\\)(?:\\{[^}]*\\})?', stripped):
            report.valid = False
            report.errors.append(t('gate.image.inline', lang, line=i))
    defined = set(re.findall('^\\s*\\[\\^([^\\]]+)\\]:', md_text, re.M))
    for i, line in enumerate(md_text.split('\n'), 1):
        for label in re.findall('\\[\\^([^\\]]+)\\](?!:)', line):
            if label not in defined:
                report.valid = False
                report.errors.append(t('gate.footnote.undefined', lang, line=i, label=label))
    for i, line in enumerate(md_text.split('\n'), 1):
        stripped = line.strip()
        if stripped.startswith('|') and (not stripped.endswith('|')):
            report.valid = False
            report.errors.append(t('gate.table.row_unclosed', lang, line=i))
    for i, line in enumerate(md_text.split('\n'), 1):
        if line.startswith('%%') and (not line.startswith('%% ')):
            report.valid = False
            report.errors.append(t('gate.style_line.no_space', lang, line=i))
    for i, line in enumerate(md_text.split('\n'), 1):
        m = re.search('\\{\\{<\\s*section\\b([^>]*)>\\}\\}', line)
        if not m:
            continue
        for key, value in re.findall('([^\\s=]+)\\s*=\\s*([^\\s>]+)', m.group(1)):
            if key in ('页码', 'page_number', 'format'):
                if value not in PAGE_NUMBER_FMT:
                    report.valid = False
                    report.errors.append(t('gate.section.page_format', lang, line=i, value=value))
            elif key in ('起始', 'start') and (not value.isdigit()):
                report.valid = False
                report.errors.append(t('gate.section.start', lang, line=i, value=value))
    for i, line in enumerate(mask_code_fences(md_text).split('\n'), 1):
        if re.search('!\\[\\[.*?\\]\\]|\\[\\[.*?\\]\\]', line):
            report.valid = False
            report.errors.append(t('gate.wikilink', lang, line=i))
        elif re.search('\\\\(?:newpage|pagebreak|clearpage)', line):
            report.valid = False
            report.errors.append(t('gate.latex.pagebreak', lang, line=i))
    for line_no, why in dialect_issues(md_text, lang):
        report.valid = False
        report.errors.append(t('gate.line', lang, line=line_no, tag=t('gate.tag.unsupported_syntax', lang), why=why))
    for line_no, why in block_marker_issues(md_text, lang):
        report.valid = False
        report.errors.append(t('gate.line', lang, line=line_no, tag=t('gate.tag.block_marker', lang), why=why))
    for line_no, why in unknown_shortcode_issues(md_text):
        report.valid = False
        report.errors.append(t('gate.line', lang, line=line_no, tag=t('gate.tag.unknown_shortcode', lang), why=why))
    _group_warn(report, t('gate.tag.section_marker', lang), section_pagebreak_issues(md_text, lang), lang)
    _group_warn(report, t('gate.tag.heading', lang), heading_issues(md_text, lang), lang)
    for line_no, why in undefined_crossref_issues(md_text, lang=lang):
        report.valid = False
        report.errors.append(t('gate.line', lang, line=line_no, tag=t('gate.tag.crossref', lang), why=why))
    for line_no, why in malformed_table_issues(md_text, lang):
        report.valid = False
        report.errors.append(t('gate.line', lang, line=line_no, tag=t('gate.tag.table', lang), why=why))
    labels_seen: dict[str, int] = {}
    for i, line in enumerate(md_text.split('\n'), 1):
        for label in re.findall('\\{#(eq-[\\w:.\\-]+)\\}', line):
            if label in labels_seen:
                report.valid = False
                report.errors.append(t('gate.equation.duplicate_label', lang, label=label, first=labels_seen[label], line=i))
            else:
                labels_seen[label] = i
    crossref = fm_data.get('crossref') if isinstance(fm_data, dict) else None
    if isinstance(crossref, dict) and crossref.get('chapters'):
        seen_heading = False
        for i, line in enumerate(md_text.split('\n'), 1):
            if re.match('^#\\s+', line):
                seen_heading = True
            elif not seen_heading and re.search('\\$\\$.*\\{#(eq-[\\w:.\\-]+)\\}', line):
                report.warnings.append(t('gate.equation.before_chapter', lang, line=i))
    _group_warn(report, t('gate.tag.caption_adjacency', lang), caption_adjacency_issues(md_text, lang), lang)
    _group_warn(report, t('gate.tag.soft_wrap', lang), [(line_no, t('gate.soft_wrap', lang, snippet=snippet)) for line_no, snippet in soft_wrap_issues(md_text)], lang)
    for line_no, snippet in math_comment_issues(md_text):
        report.valid = False
        report.errors.append(t('gate.line', lang, line=line_no, tag=t('gate.tag.math_comment', lang), why=t('gate.math.comment', lang, snippet=snippet)))
    for line_no, snippet in unbalanced_math_issues(md_text):
        report.valid = False
        report.errors.append(t('gate.line', lang, line=line_no, tag=t('gate.tag.equation_brace', lang), why=t('gate.equation.brace', lang, snippet=snippet)))
    _group_warn(report, t('gate.tag.literal_asterisk', lang), [(line_no, t('gate.literal_asterisk', lang, snippet=snippet)) for line_no, snippet in literal_asterisk_issues(md_text)], lang)
    for line_no, snippet in latex_tag_issues(md_text):
        report.valid = False
        report.errors.append(t('gate.line', lang, line=line_no, tag=t('gate.tag.equation_number', lang), why=t('gate.latex.tag', lang, snippet=snippet)))
    if spec is not None:
        width_cm = content_width_cm(spec)
        if width_cm:
            _group_warn(report, t('gate.tag.image_width', lang), image_width_issues(md_text, width_cm, lang), lang)
        elif has_declared_image_width(md_text):
            report.warnings.append(t('gate.image_width.no_margins', lang))
        for line_no, why in required_style_issues(md_text, spec, fm_data, lang):
            report.valid = False
            report.errors.append(t('gate.line', lang, line=line_no, tag=t('gate.tag.style_missing', lang), why=why))
    for line_no, line in enumerate(mask_code_fences(md_text).split('\n'), 1):
        if re.match('^\\s*:::', line):
            report.valid = False
            report.errors.append(t('gate.line', lang, line=line_no, tag=t('gate.tag.style_line', lang), why=t('gate.style.triple_colon', lang)))
    for line_no, line in enumerate(mask_code_fences(md_text).split('\n'), 1):
        m = re.search('!\\[([^\\]]+)\\]\\(', line)
        if m and re.match('^\\s*(?:图|表|Figure|Table)\\s*\\d', m.group(1)):
            report.valid = False
            report.errors.append(t('gate.line', lang, line=line_no, tag=t('gate.tag.caption_image', lang), why=t('gate.caption.in_alt', lang, text=m.group(1).strip())))
    if isinstance(fm_data, dict) and fm_data.get('bibliography'):
        has_style_title = any((resolved == 'References Title' for _, resolved in style_line_names(md_text)))
        has_heading = bool(re.search('^#{1,6}\\s*(?:参考文献|参考书目|References|Bibliography)\\s*$', md_text, re.M))
        if not (has_style_title or has_heading):
            report.warnings.append(t('gate.bib.title_missing', lang))
    for line_no, why in style_line_issues(md_text, lang):
        report.valid = False
        report.errors.append(t('gate.line', lang, line=line_no, tag=t('gate.tag.style_line', lang), why=why))
    return report
