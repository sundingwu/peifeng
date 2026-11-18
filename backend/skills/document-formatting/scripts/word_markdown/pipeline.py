"""word_markdown.pipeline — 导出总编排。

顺序固定：读稿 → 预检 → 生成样式模板 → 读成文档模型 → 写出 OOXML → 防丢数 → 对账 → 可选 PDF。
每一步失败都有明确的退出码与可读原因；跨平台，不依赖任何外部二进制。
"""
from __future__ import annotations
import hashlib
import logging
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from word_markdown.audit.inspector import audit_docx_against_spec
from word_markdown.audit.postflight import verify_chinese_integrity, verify_numeric_integrity
from word_markdown.config import DEFAULT_PRESET_EN, DEFAULT_PRESET_ZH, default_csl_path, get_csl_dir, resolve_preset_path
from word_markdown.i18n import resolve_lang, t
from word_markdown.preprocessor.cleaner import strip_html_comments
from word_markdown.spec.parser import parse_format
from word_markdown.template.builder import build_template
from word_markdown.status import EXIT_ERROR, EXIT_OK, EXIT_PDF_SKIPPED
from word_markdown.template.generator import TemplateError
from word_markdown.validator.check import validate_manuscript
from word_markdown.visual_qa.word_pdf import render_pdf_via_word
from word_markdown.utils.skill_utils import split_frontmatter, resolve_manuscript_path
logger = logging.getLogger('md2word')

@dataclass
class ExportOptions:
    """导出配置选项。"""
    input_path: Path | str
    output_path: Optional[Path | str] = None
    format_path: Optional[Path | str] = None
    preset: Optional[str] = None
    check_md: bool = True
    postflight: bool = True
    audit: bool = True
    make_pdf: bool = False
    lang: Optional[str] = None
    base_template: Optional[Path | str] = None
    cover_sections: int = 0
    keep_pages: int = 0
    keep_until: str = ''
    requirements_path: Optional[Path | str] = None

    def __post_init__(self):
        if self.format_path is None and self.requirements_path is not None:
            self.format_path = self.requirements_path

@dataclass
class ExportResult:
    """流水线执行结果。"""
    returncode: int
    output_docx: Optional[Path] = None
    output_pdf: Optional[Path] = None
    messages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    lang: str = 'zh'
    pdf_skip_reason: str = ''

    @property
    def ok(self) -> bool:
        """docx 是否已经写出来了（`--pdf` 跳过不算失败）。"""
        return self.returncode in (EXIT_OK, EXIT_PDF_SKIPPED)

def _inline_text(inline) -> str:
    """行内节点里真正的字。加粗、斜体是包着一层的容器，只读 `.value` 会漏掉整段加粗的开头。"""
    text = getattr(inline, 'text', '') or getattr(inline, 'value', '')
    if text:
        return text
    return ''.join((_inline_text(child) for child in getattr(inline, 'children', ()) or ()))

def _first_texts(blocks, limit: int=5) -> list[str]:
    """正文前几段的开头，按顺序给一串。

    只用第一段会有个坑：很多学校模板的前置件里放了一份示例目录，「第一章 绪论」正好也
    在上面，拿它去定位正文起点就会落在封面上。多给几个候选，让验真那边把在前置件里出现
    过的剔掉，剩下的才用来定位。
    """
    heads: list[str] = []
    for block in blocks:
        text = re.sub('\\s+', '', ''.join((_inline_text(inline) for inline in getattr(block, 'inlines', []) or [])))
        if len(text) >= 4 and text[:12] not in heads:
            heads.append(text[:12])
        if len(heads) >= limit:
            break
    return heads or ['']

def _first_text(blocks) -> str:
    """取正文第一段的前几个字：既要能在 PDF 里定位，又不能出现在封面上。"""
    for block in blocks:
        text = re.sub('\\s+', '', ''.join((_inline_text(inline) for inline in getattr(block, 'inlines', []) or [])))
        if text:
            return text[:12]
    return ''

def _normalized_csl(path: Path, temp_dir: Path, res: ExportResult) -> Path:
    """把 CSL 里的 direct formatting 去掉，返回可用的路径（就地清理要写副本）。"""
    from word_markdown.transform.csl_clean import is_csl, strip_direct_formatting
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        return path
    if not is_csl(text):
        return path
    cleaned, removed = strip_direct_formatting(text)
    if not removed:
        return path
    target = temp_dir / 'csl-normalized.csl'
    target.write_text(cleaned, encoding='utf-8')
    res.messages.append(t('pipeline.csl_normalized', res.lang, name=path.name, attrs=', '.join(removed)))
    return target

def _provenance(fmt_file) -> str:
    """产物自证的出处：哪个版本、照哪份格式文件。内容哈希而不是路径——路径换了、
    机器换了，同一份格式文件还是同一个身份，而产物要能逐字节复现。"""
    from word_markdown import __version__
    if fmt_file is None:
        return f'md-to-word {__version__}'
    digest = hashlib.sha256(Path(fmt_file).read_bytes()).hexdigest()[:8]
    return f'md-to-word {__version__} · {Path(fmt_file).name} · sha256:{digest}'

def run_export_pipeline(options: ExportOptions) -> ExportResult:
    """执行完整的纯 Python 学术导出流水线。"""
    res = ExportResult(returncode=0)
    in_p = Path(options.input_path).resolve()
    if not in_p.is_file():
        res.returncode = EXIT_ERROR
        res.errors.append(t('pipeline.input_missing', res.lang, path=in_p))
        return res
    try:
        manuscript_text = in_p.read_text(encoding='utf-8', errors='replace')
    except OSError:
        manuscript_text = ''
    res.lang = resolve_lang(manuscript_text, options.lang)
    if options.output_path:
        out_p = Path(options.output_path).resolve()
    else:
        out_p = in_p.with_suffix('.docx').resolve()
    res.output_docx = out_p
    if not out_p.parent.is_dir():
        res.returncode = EXIT_ERROR
        res.errors.append(t('pipeline.output_dir_missing', res.lang, dir=out_p.parent))
        return res
    if out_p.is_dir():
        res.returncode = EXIT_ERROR
        res.errors.append(t('pipeline.output_is_dir', res.lang, path=out_p))
        return res
    fmt_file: Optional[Path] = None
    fmt_content: Optional[str] = None
    if options.format_path:
        p_fmt = Path(options.format_path).resolve()
        if not p_fmt.is_file():
            res.returncode = EXIT_ERROR
            res.errors.append(t('pipeline.format_missing', res.lang, path=p_fmt))
            return res
        fmt_file = p_fmt
        fmt_content = p_fmt.read_text(encoding='utf-8')
    elif options.preset:
        p_pre = resolve_preset_path(options.preset)
        if not p_pre or not p_pre.is_file():
            res.returncode = EXIT_ERROR
            res.errors.append(t('pipeline.preset_missing', res.lang, name=options.preset))
            return res
        fmt_file = p_pre
        fmt_content = p_pre.read_text(encoding='utf-8')
    else:
        named = in_p.parent / 'format.txt'
        if named.is_file():
            fmt_file = named
            fmt_content = named.read_text(encoding='utf-8')
            res.messages.append(t('pipeline.txt_named', res.lang, path=named.name))
        else:
            others = sorted((p.name for p in in_p.parent.glob('*.txt') if p.is_file()))
            if others:
                shown = '、'.join(others[:3]) + ('…' if len(others) > 3 else '')
                res.messages.append(t('pipeline.txt_ignored', res.lang, names=shown))
            preset_name = DEFAULT_PRESET_ZH if res.lang == 'zh' else DEFAULT_PRESET_EN
            p_default = resolve_preset_path(preset_name)
            if p_default and p_default.is_file():
                fmt_file = p_default
                fmt_content = p_default.read_text(encoding='utf-8')
                res.messages.append(t('pipeline.preset_auto', res.lang, name=preset_name))
    try:
        spec = parse_format(fmt_content) if fmt_content else None
    except ValueError:
        spec = None
    if options.check_md:
        logger.info('🔍 ' + t('pipeline.precheck', res.lang))
        v_report = validate_manuscript(in_p, spec_path=fmt_file, lang=res.lang)
        if not v_report.valid:
            res.returncode = EXIT_ERROR
            res.errors.append(t('pipeline.rejected', res.lang, count=len(v_report.errors)))
            res.errors.extend(v_report.errors)
            return res
        for w in v_report.warnings:
            res.messages.append(w)
            res.warnings.append(w)
    temp_dir = tempfile.mkdtemp(prefix='md2word_')
    try:
        contract_text = strip_html_comments(in_p.read_text(encoding='utf-8'))
        builtin_csl: Optional[Path] = None
        fm_data: dict = {}
        fm_raw, _ = split_frontmatter(contract_text)
        if fm_raw:
            import yaml
            try:
                fm_data = yaml.safe_load(fm_raw)
                if isinstance(fm_data, dict) and fm_data.get('csl'):
                    p, how = resolve_manuscript_path(str(fm_data['csl']), in_p, skill_csl_dir=get_csl_dir())
                    if how == 'skill-builtin':
                        builtin_csl = p
            except Exception:
                pass
        if isinstance(fm_data, dict) and fm_data.get('bibliography') and (not fm_data.get('csl')):
            builtin_csl = default_csl_path(contract_text)
        if not fmt_content:
            res.returncode = EXIT_ERROR
            res.errors.append(t('pipeline.no_spec', res.lang))
            return res
        render_target = out_p if not options.base_template else Path(temp_dir) / 'rendered.docx'
        temp_tpl_path = Path(temp_dir) / 'template.docx'
        try:
            build_template(fmt_content, temp_tpl_path, lang=res.lang)
        except TemplateError as exc:
            res.returncode = EXIT_ERROR
            res.errors.append(t('pipeline.format_template_failed', res.lang, reason=exc))
            return res
        tpl_for_render = temp_tpl_path
        from word_markdown.model.reader import read_markdown
        from word_markdown.utils.skill_utils import parse_bib_keys
        from word_markdown.writer import UnsupportedFeature, write_docx
        citation_keys: set[str] = set()
        bib_text = ''
        if isinstance(fm_data, dict) and fm_data.get('bibliography'):
            bib_path, how = resolve_manuscript_path(str(fm_data['bibliography']), in_p)
            if how:
                try:
                    bib_text = Path(bib_path).read_text(encoding='utf-8')
                    citation_keys = parse_bib_keys(bib_text)
                except OSError:
                    citation_keys = set()
        engine = None
        if citation_keys:
            from word_markdown.transform.citations import CitationEngine, CitationError
            csl_path = builtin_csl
            if csl_path is None and isinstance(fm_data, dict) and fm_data.get('csl'):
                resolved, how = resolve_manuscript_path(str(fm_data['csl']), in_p, skill_csl_dir=get_csl_dir())
                if how:
                    csl_path = resolved
            if csl_path is None:
                csl_path = default_csl_path(contract_text)
            csl_path = _normalized_csl(csl_path, Path(temp_dir), res)
            from word_markdown.transform.bibtex import crossref_keys
            inherited = crossref_keys(bib_text)
            if inherited:
                note = t('bib.crossref_not_inherited', res.lang, keys='、'.join(inherited[:5]))
                res.messages.append(note)
                res.warnings.append(note)
            try:
                engine = CitationEngine(bib_text, csl_path, res.lang)
            except (CitationError, OSError) as exc:
                res.returncode = EXIT_ERROR
                res.errors.append(t('pipeline.csl_error', res.lang, reason=exc))
                return res
        try:
            model = read_markdown(contract_text, citation_keys=citation_keys)
            if engine is not None:
                from word_markdown.transform.citations import cited_keys
                engine.reserve(cited_keys(model))
            write_docx(model, tpl_for_render, render_target, citations=engine, base_dir=in_p.parent, equation_number_format=spec.equation_number_format if spec else '', appendix_equation_number_format=spec.appendix_equation_number_format if spec else '', equation_number_placement=spec.equation_number if spec else 'right', no_row_split=bool(spec and spec.table.get('no_row_split')), repeat_header=bool(spec and spec.table_header and spec.table_header.get('repeat_header')), table_space_after=float(spec.table.get('space_after_pt') or 0) if spec else 0.0, provenance=_provenance(fmt_file), lang=res.lang)
        except UnsupportedFeature as exc:
            res.returncode = EXIT_ERROR
            res.errors.append(t('pipeline.unsupported', res.lang, reason=exc))
            return res
        except OSError as exc:
            res.returncode = EXIT_ERROR
            res.errors.append(t('pipeline.write_failed', res.lang, path=out_p, err=exc))
            return res
        if options.postflight:
            all_kept, missing = verify_numeric_integrity(in_p, render_target)
            if not all_kept:
                res.returncode = EXIT_ERROR
                res.errors.append(t('pipeline.numeric_failed', res.lang, count=len(missing), sample=', '.join(missing[:5])))
                return res
            logger.info('✓ ' + t('pipeline.numeric_ok', res.lang))
            all_kept, missing = verify_chinese_integrity(in_p, render_target)
            if not all_kept:
                res.returncode = EXIT_ERROR
                res.errors.append(t('pipeline.chinese_failed', res.lang, count=len(missing), sample=''.join(missing[:12])))
                return res
        if options.audit and fmt_file:
            matched, problems = audit_docx_against_spec(render_target, fmt_file, res.lang)
            if not matched:
                res.returncode = EXIT_ERROR
                res.errors.append(t('pipeline.audit_failed', res.lang, count=len(problems)))
                for cat, msg in problems:
                    res.errors.append(f'  [{cat}] {msg}')
                return res
            logger.info('✓ ' + t('pipeline.audit_ok', res.lang))
        if options.base_template:
            from word_markdown.template.base_inject import BaseTemplateError, apply_base_template
            base_p = Path(options.base_template).resolve()
            if not base_p.is_file():
                res.returncode = EXIT_ERROR
                res.errors.append(t('pipeline.base_missing', res.lang, path=base_p))
                return res
            from word_markdown.template.base_inject import inspect_base
            report = inspect_base(base_p)
            if not report.ok:
                res.returncode = EXIT_ERROR
                res.errors.append(t('pipeline.base_failed', res.lang, reason='；'.join(report.blocking)))
                res.errors.extend((f'  {n}' for n in report.notes))
                return res
            sentinel = _first_text(model.blocks)
            sections = options.cover_sections
            if not sections and (not options.keep_pages) and (not options.keep_until):
                res.returncode = EXIT_ERROR
                res.errors.append(t('pipeline.base_need_sections', res.lang))
                return res
            try:
                if not sections and options.keep_until:
                    from word_markdown.template.base_inject import section_outline, sections_until_text
                    hits = sections_until_text(base_p, options.keep_until)
                    if not hits:
                        outline = '\n'.join((f'  第 {i} 节：{line}' for i, line in enumerate(section_outline(base_p), 1)))
                        res.returncode = EXIT_ERROR
                        res.errors.append(t('pipeline.base_until_missing', res.lang, text=options.keep_until, outline=outline))
                        return res
                    if len(hits) > 1:
                        res.returncode = EXIT_ERROR
                        res.errors.append(t('pipeline.base_until_ambiguous', res.lang, text=options.keep_until, sections='、'.join((str(h) for h in hits))))
                        return res
                    sections = hits[0]
                    res.messages.append(t('pipeline.base_until_resolved', res.lang, text=options.keep_until, sections=sections))
                elif not sections and options.keep_pages:
                    from word_markdown.template.base_inject import resolve_sections_for_pages, section_count
                    from word_markdown.visual_qa.word_pdf import can_render_word_pdf
                    if not can_render_word_pdf()[0]:
                        res.returncode = EXIT_ERROR
                        res.errors.append(t('pipeline.base_keep_pages_needs_word', res.lang, sections=section_count(base_p)))
                        return res
                    sections = resolve_sections_for_pages(base_p, render_target, options.keep_pages, Path(temp_dir), sentinel)
                    res.messages.append(t('pipeline.base_resolved', res.lang, pages=options.keep_pages, sections=sections))
                outcome = apply_base_template(base_p, render_target, sections, out_p, _first_texts(model.blocks))
            except BaseTemplateError as exc:
                res.returncode = EXIT_ERROR
                res.errors.append(t('pipeline.base_failed', res.lang, reason=exc.reason))
                res.errors.extend((f'  {n}' for n in exc.notes))
                return res
            for note in outcome.notes:
                res.messages.append(note)
                res.warnings.append(note)
            key = 'pipeline.base_ok' if outcome.layout_verified else 'pipeline.base_ok_structure'
            logger.info('✓ ' + t(key, res.lang, pages=outcome.cover_sections))
        if options.make_pdf:
            pdf_path = out_p.with_suffix('.pdf')
            success, reason = render_pdf_via_word(out_p, pdf_path)
            if success:
                res.output_pdf = pdf_path
                logger.info('🖨️ ' + t('pipeline.pdf_ok', res.lang, path=pdf_path))
            else:
                res.messages.append(f'MD2WORD_SKIP kind=pdf-render reason={reason}')
                res.pdf_skip_reason = str(reason or '')
                res.returncode = 3
        logger.info('✅ ' + t('pipeline.success', res.lang, path=out_p))
        res.messages.append(t('pipeline.reminder', res.lang))
        return res
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
