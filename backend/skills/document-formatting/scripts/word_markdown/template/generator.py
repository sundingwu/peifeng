"""
生成学术 Word 模板 (docx)

从纯净现代 Word 365 基础母版出发，根据 format.txt 格式要求修改样式，
输出是只含样式与页面设置的 template.docx，写出器直接用它。

用法:
    python3 generate_template.py format.txt -o template.docx
    python3 generate_template.py --help
"""
from __future__ import annotations
import os
import re
import shutil
import sys
import tempfile
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, RGBColor
from lxml import etree
from word_markdown.config import get_base_template_path
from word_markdown.i18n import t
from word_markdown.spec.constants import STYLE_CN, STYLE_MAP
from word_markdown.spec.models import MinLineSpacing
from word_markdown.utils.skill_utils import TOC_MARKER_RE, clear_color_theme_attrs, clear_font_theme_attrs, get_logger, strip_theme_attrs_from_xml
logger = get_logger(__name__)
NS_W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
NS_M = 'http://schemas.openxmlformats.org/officeDocument/2006/math'

def W(tag):
    return f'{{{NS_W}}}{tag}'

def M(tag):
    return f'{{{NS_M}}}{tag}'
SIZE_MAP = {'初号': Pt(42), '小初': Pt(36), '一号': Pt(26), '小一': Pt(24), '二号': Pt(22), '小二': Pt(18), '三号': Pt(16), '小三': Pt(15), '四号': Pt(14), '小四': Pt(12), '五号': Pt(10.5), '小五': Pt(9), '六号': Pt(7.5), '小六': Pt(6.5), '七号': Pt(5.5), '八号': Pt(5)}
PAGE_NUMBER_FMT = {'阿拉伯': 'decimal', '罗马': 'upperRoman', '小写罗马': 'lowerRoman', '字母': 'lowerLetter', '大写字母': 'upperLetter'}
PAGE_SIZE_MAP = {'A4': 'a4', 'Letter': 'letter'}
PAGE_SIZES_CM = {'a4': (21.0, 29.7), 'letter': (21.59, 27.94)}
_TWIPS_PER_CM = 566.93

def page_size_twips(page_size: str) -> tuple:
    """纸张 → (宽, 高) twips（对账侧与写入侧共用，避免各写一份常量）。"""
    w_cm, h_cm = PAGE_SIZES_CM.get(page_size, PAGE_SIZES_CM['a4'])
    return (int(round(w_cm * _TWIPS_PER_CM)), int(round(h_cm * _TWIPS_PER_CM)))
_RE_DECOR_TOKEN = re.compile('\\{([^{}]*)\\}')
_RE_STYLEREF = re.compile('^样式标题\\s*[:：]\\s*(.+?)\\s*$')
_ALIGN = {'居中': (WD_ALIGN_PARAGRAPH.CENTER, 'center'), '左对齐': (WD_ALIGN_PARAGRAPH.LEFT, 'left'), '右对齐': (WD_ALIGN_PARAGRAPH.RIGHT, 'right'), '两端对齐': (WD_ALIGN_PARAGRAPH.JUSTIFY, 'both')}
ALIGN_MAP = {cn: enum for cn, (enum, _) in _ALIGN.items()}
ALIGN_VAL = {cn: val for cn, (_, val) in _ALIGN.items()}
CJK_FONTS = {'宋体', '黑体', '仿宋', 'FangSong', '楷体', '微软雅黑'}
LATIN_FONTS = {'Times New Roman', 'Arial', 'Calibri', 'Verdana'}

def ensure_rpr(element):
    """确保元素有 rPr 子元素，返回 rPr"""
    rpr = element.find(W('rPr'))
    if rpr is None:
        rpr = etree.SubElement(element, W('rPr'))
    return rpr
_RPR_ORDER = ['rStyle', 'rFonts', 'b', 'bCs', 'i', 'iCs', 'caps', 'smallCaps', 'strike', 'dstrike', 'outline', 'shadow', 'emboss', 'imprint', 'noProof', 'snapToGrid', 'vanish', 'webHidden', 'color', 'spacing', 'w', 'kern', 'position', 'sz', 'szCs', 'highlight', 'u', 'effect', 'bdr', 'shd', 'fitText', 'vertAlign', 'rtl', 'cs', 'em', 'lang', 'eastAsianLayout', 'specVanish', 'oMath']
_PPR_ORDER = ['pStyle', 'keepNext', 'keepLines', 'pageBreakBefore', 'framePr', 'widowControl', 'numPr', 'suppressLineNumbers', 'pBdr', 'shd', 'tabs', 'suppressAutoHyphens', 'kinsoku', 'wordWrap', 'overflowPunct', 'topLinePunct', 'autoSpaceDE', 'autoSpaceDN', 'bidi', 'adjustRightInd', 'snapToGrid', 'spacing', 'ind', 'contextualSpacing', 'mirrorIndents', 'suppressOverlap', 'jc', 'textDirection', 'textAlignment', 'textboxTightWrap', 'outlineLvl', 'divId', 'cnfStyle', 'rPr', 'sectPr', 'pPrChange']
_STYLE_ORDER = ['name', 'aliases', 'basedOn', 'next', 'link', 'autoRedefine', 'hidden', 'uiPriority', 'semiHidden', 'unhideWhenUsed', 'qFormat', 'locked', 'personal', 'personalCompose', 'personalReply', 'rsid', 'pPr', 'rPr', 'tblPr', 'trPr', 'tcPr', 'tblStylePr']
_TBLSTYLEPR_ORDER = ['pPr', 'rPr', 'tblPr', 'trPr', 'tcPr']
_TBLPR_ORDER = ['tblStyle', 'tblpPr', 'tblOverlap', 'bidiVisual', 'tblStyleRowBandSize', 'tblStyleColBandSize', 'tblW', 'jc', 'tblCellSpacing', 'tblInd', 'tblBorders', 'shd', 'tblLayout', 'tblCellMar', 'tblLook', 'tblCaption', 'tblDescription']
_TCPR_ORDER = ['cnfStyle', 'tcW', 'gridSpan', 'hMerge', 'vMerge', 'tcBorders', 'shd', 'noWrap', 'tcMar', 'textDirection', 'tcFitText', 'vAlign', 'hideMark']
_TRPR_ORDER = ['cnfStyle', 'divId', 'gridBefore', 'gridAfter', 'wBefore', 'wAfter', 'cantSplit', 'trHeight', 'tblHeader', 'tblCellSpacing', 'jc', 'hidden']
_SECTPR_ORDER = ['footnotePr', 'endnotePr', 'type', 'pgSz', 'pgMar', 'paperSrc', 'pgBorders', 'lnNumType', 'pgNumType', 'cols', 'formProt', 'vAlign', 'noEndnote', 'titlePg', 'textDirection', 'bidi', 'rtlGutter', 'docGrid', 'printerSettings']
BOTTOM_BORDER_STYLE = {'single': 'single', 'double': 'thinThickMediumGap', 'even': 'double', 'thickThin': 'thickThinMediumGap', 'triple': 'thinThickThinMediumGap'}
BOTTOM_BORDER_DEFAULT_PT = {'single': 0.5, 'double': 1.5, 'even': 0.5, 'thickThin': 1.5, 'triple': 1.0}
_FALLBACK_CONTENT_WIDTH_TWIPS = int(round((PAGE_SIZES_CM['a4'][0] - 2.54 * 2) * _TWIPS_PER_CM))

def _content_width_twips(page_size, margins, gutter_cm=None) -> int:
    """正文可用宽度（twips）。装订线是折进**左**边距写的，所以要一起减掉。"""
    width_cm = PAGE_SIZES_CM.get(page_size, PAGE_SIZES_CM['a4'])[0]
    left = margins['left'] + (gutter_cm or 0)
    return int(round((width_cm - left - margins['right']) * _TWIPS_PER_CM))

def body_line_spacing_multiple(styles) -> float:
    """正文的行距**倍数**——文档网格补偿按它算。

    `styles` 是任一含 `style_name` / `line_spacing` 的序列（写入侧是 `spec.styles`，
    读取侧是解析出来的样式行）。优先「正文」，退回「普通」；固定值行距（`行距20磅`）
    不是倍数、不参与相乘（Word 只把行高向上取整到网格），故返回 1.0。
    """
    for st in styles:
        if st.get('style_name') in ('Body Text', 'Normal'):
            ls = st.get('line_spacing')
            if isinstance(ls, float) and ls > 0:
                return ls
    return 1.0

def _body_height_twips(page_size, margins) -> int:
    _, h_cm = PAGE_SIZES_CM.get(page_size, PAGE_SIZES_CM['a4'])
    return int(round((h_cm - margins['top'] - margins['bottom']) * _TWIPS_PER_CM))

def doc_grid_pitch(spec, margins) -> int:
    """`文档网格：每页N行` → `w:docGrid/@w:linePitch`（每行多少 twips）。

    写入侧、导出后对账侧共用这一份，免得两边各算一遍、算法漂移还看不出来。
    """
    return int(_body_height_twips(spec.page_size, margins) / spec.doc_grid_lines / body_line_spacing_multiple(spec.styles))

def doc_grid_lines_from_pitch(pitch: int, line_multiple: float, page_size, margins) -> int:
    """`w:docGrid/@linePitch` → 声明的「每页 N 行」。是 `doc_grid_pitch` 的逆，
    读参考 .docx 时用（把别人模板里的网格反推成本工具的写法）。"""
    return max(1, int(round(_body_height_twips(page_size, margins) / pitch / line_multiple)))
_CJK_LINE_HEIGHT_RATIO = 1.3

def body_line_height_pt(spec) -> float:
    """正文一行实际占多高（磅）：固定值就用那个值，倍数按 1.3×字号 折算。"""
    for name in ('Body Text', 'Normal'):
        style = spec.get_style(name)
        if not style:
            continue
        size = style.get('font_size')
        size_pt = size.pt if hasattr(size, 'pt') else 12.0
        spacing = style.get('line_spacing')
        if hasattr(spacing, 'pt'):
            return float(spacing.pt)
        multiple = spacing if isinstance(spacing, float) and spacing > 0 else 1.0
        return _CJK_LINE_HEIGHT_RATIO * size_pt * multiple
    return _CJK_LINE_HEIGHT_RATIO * 12.0

def max_doc_grid_lines(spec) -> int:
    """声明「每页 N 行」能兑现到几行：版心高度 ÷ 正文行高。

    超过这个数的 N 兑现不了——Word 会把行高向上取整到网格的整数倍，实际行数反而更少，
    产物比不声明网格还差。所以这个数要报给作者，而不是默默按他的 N 排。
    """
    margins = spec.margins or {}
    if not all((k in margins for k in ('top', 'bottom'))):
        return 0
    height_pt = _body_height_twips(spec.page_size, margins) / 20.0
    return int(height_pt // body_line_height_pt(spec))
_ALIGN_VAL = {enum: val for enum, val in _ALIGN.values()}

def ensure_ordered_child(parent, tag, order):
    """按 schema 顺序取/建子元素。顺序表外的标签追加在末尾。"""
    el = parent.find(W(tag))
    if el is not None:
        return el
    el = etree.Element(W(tag))
    idx = order.index(tag) if tag in order else len(order)
    for i, ch in enumerate(parent):
        ln = etree.QName(ch).localname
        if ln in order and order.index(ln) > idx:
            parent.insert(i, el)
            return el
    parent.append(el)
    return el

def split_fonts(font_names, default_latin=None):
    """把字体名列表拆成 (中日韩字体, 西文字体)"""
    cjk = None
    latin = None
    for f in font_names or []:
        if f in CJK_FONTS or re.search('[一-鿿]', f):
            cjk = f
        else:
            latin = f
    if not latin and default_latin:
        latin = default_latin
    return (cjk, latin)

def _neutralize_inherited_first_line(ind):
    """设置悬挂缩进时，显式将继承的首行缩进置零，避免二者冲突。"""
    ind.set(W('firstLineChars'), '0')
    ind.attrib.pop(W('firstLine'), None)

def write_run_props(rpr, props, cjk=None, latin=None):
    """写入字符级属性（字体、字号、加粗、倾斜、颜色）。"""
    if cjk or latin:
        rfonts = ensure_ordered_child(rpr, 'rFonts', _RPR_ORDER)
        if cjk:
            rfonts.set(W('eastAsia'), cjk)
        if latin:
            rfonts.set(W('ascii'), latin)
            rfonts.set(W('hAnsi'), latin)
        clear_font_theme_attrs(rfonts)
    if props.get('font_size'):
        half_pt = str(int(round(props['font_size'].pt * 2)))
        ensure_ordered_child(rpr, 'sz', _RPR_ORDER).set(W('val'), half_pt)
        sz_cs = rpr.find(W('szCs'))
        if sz_cs is not None:
            sz_cs.set(W('val'), half_pt)
    if props.get('bold') is not None:
        b = ensure_ordered_child(rpr, 'b', _RPR_ORDER)
        if not props['bold']:
            b.set(W('val'), '0')
        else:
            b.attrib.pop(W('val'), None)
        b_cs = rpr.find(W('bCs'))
        if b_cs is not None:
            if not props['bold']:
                b_cs.set(W('val'), '0')
            else:
                b_cs.attrib.pop(W('val'), None)
    if props.get('italic') is not None:
        it = ensure_ordered_child(rpr, 'i', _RPR_ORDER)
        if not props['italic']:
            it.set(W('val'), '0')
        else:
            it.attrib.pop(W('val'), None)
        it_cs = rpr.find(W('iCs'))
        if it_cs is not None:
            if not props['italic']:
                it_cs.set(W('val'), '0')
            else:
                it_cs.attrib.pop(W('val'), None)
    color = ensure_ordered_child(rpr, 'color', _RPR_ORDER)
    color.set(W('val'), props.get('color') or '000000')
    clear_color_theme_attrs(color)
    for tag in ('spacing', 'kern'):
        el = rpr.find(W(tag))
        if el is not None:
            rpr.remove(el)
    if props.get('char_spacing_pt'):
        sp = ensure_ordered_child(rpr, 'spacing', _RPR_ORDER)
        sp.set(W('val'), str(int(round(props['char_spacing_pt'] * 20))))

def write_para_props(ppr, props):
    """把**段落级**属性写进任意 pPr 容器（样式 pPr，或 w:tblStylePr/pPr）。"""
    if props.get('alignment') is not None:
        jc = ensure_ordered_child(ppr, 'jc', _PPR_ORDER)
        jc.set(W('val'), _ALIGN_VAL.get(props['alignment'], 'left'))
    spacing = None
    if props.get('line_spacing') is not None:
        spacing = ensure_ordered_child(ppr, 'spacing', _PPR_ORDER)
        ls = props['line_spacing']
        if isinstance(ls, MinLineSpacing):
            spacing.set(W('line'), str(int(round(ls.pt * 20))))
            spacing.set(W('lineRule'), 'atLeast')
        elif isinstance(ls, Pt):
            spacing.set(W('line'), str(int(round(ls.pt * 20))))
            spacing.set(W('lineRule'), 'exact')
        else:
            spacing.set(W('line'), str(int(round(float(ls) * 240))))
            spacing.set(W('lineRule'), 'auto')
    if 'space_before' in props:
        spacing = spacing if spacing is not None else ensure_ordered_child(ppr, 'spacing', _PPR_ORDER)
        spacing.set(W('before'), str(int(round(props['space_before'].pt * 20))))
    if 'space_after' in props:
        spacing = spacing if spacing is not None else ensure_ordered_child(ppr, 'spacing', _PPR_ORDER)
        spacing.set(W('after'), str(int(round(props['space_after'].pt * 20))))
    if props.get('left_indent_chars') is not None:
        ind = ensure_ordered_child(ppr, 'ind', _PPR_ORDER)
        ind.set(W('leftChars'), str(int(props['left_indent_chars'] * 100)))
        ind.attrib.pop(W('left'), None)
    if props.get('dot_leader_tab'):
        tabs = ensure_ordered_child(ppr, 'tabs', _PPR_ORDER)
        for old in tabs.findall(W('tab')):
            tabs.remove(old)
        tab = etree.SubElement(tabs, W('tab'))
        tab.set(W('val'), 'right')
        tab.set(W('leader'), 'dot')
        tab.set(W('pos'), str(props.get('dot_leader_pos', _FALLBACK_CONTENT_WIDTH_TWIPS)))
    if props.get('join_toc'):
        outline = ensure_ordered_child(ppr, 'outlineLvl', _PPR_ORDER)
        outline.set(W('val'), '0')
    if props.get('keep_next'):
        ensure_ordered_child(ppr, 'keepNext', _PPR_ORDER)
    if props.get('keep_lines'):
        ensure_ordered_child(ppr, 'keepLines', _PPR_ORDER)
    if props.get('page_break_before'):
        ensure_ordered_child(ppr, 'pageBreakBefore', _PPR_ORDER)
    if props.get('widow_control') is not None:
        wc = ensure_ordered_child(ppr, 'widowControl', _PPR_ORDER)
        wc.set(W('val'), '1' if props['widow_control'] else '0')
    if props.get('no_border') or props.get('bottom_border') == 'none':
        p_bdr = ppr.find(W('pBdr'))
        if p_bdr is not None:
            ppr.remove(p_bdr)
    if props.get('bottom_border') in BOTTOM_BORDER_STYLE:
        p_bdr = ppr.find(W('pBdr'))
        if p_bdr is None:
            p_bdr = ensure_ordered_child(ppr, 'pBdr', _PPR_ORDER)
        old = p_bdr.find(W('bottom'))
        if old is not None:
            p_bdr.remove(old)
        bot = etree.SubElement(p_bdr, W('bottom'))
        bot.set(W('val'), BOTTOM_BORDER_STYLE[props['bottom_border']])
        bot.set(W('sz'), _pt_to_border_sz(props.get('border_width_pt', BOTTOM_BORDER_DEFAULT_PT[props['bottom_border']])))
        bot.set(W('space'), '1')
        bot.set(W('color'), 'auto')
    if props.get('indent') is not None:
        ind = ensure_ordered_child(ppr, 'ind', _PPR_ORDER)
        indent_type = props['indent_type']
        indent_val = props['indent']
        has_left = props.get('left_indent_chars') is not None
        if props.get('indent_unit', 'pt') == 'chars':
            if indent_type == 'first_line':
                ind.set(W('firstLineChars'), str(int(indent_val * 100)))
                ind.attrib.pop(W('firstLine'), None)
            elif indent_type == 'hanging':
                ind.set(W('hangingChars'), str(int(indent_val * 100)))
                if not has_left:
                    ind.set(W('leftChars'), str(int(indent_val * 100)))
                    ind.attrib.pop(W('left'), None)
                _neutralize_inherited_first_line(ind)
                ind.attrib.pop(W('hanging'), None)
        else:
            twips = str(int(round(indent_val * 20)))
            if indent_type == 'first_line':
                ind.set(W('firstLine'), twips)
                ind.attrib.pop(W('firstLineChars'), None)
            elif indent_type == 'hanging':
                ind.set(W('hanging'), twips)
                if not has_left:
                    ind.set(W('left'), twips)
                    ind.attrib.pop(W('leftChars'), None)
                _neutralize_inherited_first_line(ind)
                ind.attrib.pop(W('hangingChars'), None)

def parse_font_name(text):
    """提取字体名，返回 [cjk_font, latin_font] 或 None"""
    parts = re.split('[/／]', text)
    cjk = None
    latin = None
    for part in parts:
        p = part.strip()
        if not p:
            continue
        known_match = None
        for known in sorted(CJK_FONTS | LATIN_FONTS, key=len, reverse=True):
            if re.search('\\b' + re.escape(known) + '\\b', p):
                known_match = known
                break
        if known_match:
            if known_match in CJK_FONTS:
                cjk = known_match
            else:
                latin = known_match
        else:
            candidate = re.split('[,，]', p)[0].strip()
            if candidate and (not re.match('\\d', candidate)) and re.search('[^\\W\\d_]|[\\u4e00-\\u9fff]', candidate):
                if re.search('[\\u4e00-\\u9fff]', candidate):
                    cjk = candidate
                else:
                    latin = candidate
    result = []
    if cjk:
        result.append(cjk)
    if latin:
        result.append(latin)
    return result if result else None

class TemplateError(ValueError):
    """格式要求或模板无法生成。

    作为库调用时（`run_export_pipeline`、`build_template`）必须抛这个异常，不能
    `sys.exit`：调用方的进程不该被一份写错的格式文件直接终结。
    """
_BUILTIN_TOC = {f'TOC {n}': (f'TOC{n}', f'toc {n}') for n in range(1, 6)}

def _ensure_builtin_toc_style(style, style_name: str) -> None:
    """把目录条目样式写成 Word 的**内置**样式，而不是自建样式。"""
    builtin = _BUILTIN_TOC.get(style_name)
    if builtin is None:
        return
    style_id, name = builtin
    element = style.element
    element.attrib.pop(W('customStyle'), None)
    element.set(W('styleId'), style_id)
    name_el = element.find(W('name'))
    if name_el is None:
        name_el = etree.SubElement(element, W('name'))
    name_el.set(W('val'), name)

def _find_style(doc, name: str):
    """按名字或 styleId 找样式。

    目录条目会被改写成 Word 的内置样式（`TOC 1` → 名 `toc 1`、id `TOC1`），此后按
    名字就查不到了。python-docx 的 `doc.styles[name]` 会退回按 id 查、并对这种用法发
    弃用警告——每次导出刷一屏无关警告。这里自己按「名字或 id」找，不触发那条路径。
    """
    for style in doc.styles:
        if style.name == name or style.style_id == name:
            return style
    return None

def modify_style(doc, style_name, props, default_latin=None):
    """修改或创建样式的属性"""
    style = _find_style(doc, style_name)
    if style is None:
        style = doc.styles.add_style(style_name, WD_STYLE_TYPE.PARAGRAPH)
        try:
            normal_style = doc.styles['Normal']
            style.base_style = normal_style
        except KeyError:
            pass
        logger.debug(f' ✓ {style_name}: 已创建新样式（basedOn Normal）')
    _ensure_builtin_toc_style(style, style_name)
    cjk, latin = split_fonts(props.get('font_name'), default_latin)
    if not cjk and (not latin) and default_latin:
        latin = default_latin
    rpr = ensure_rpr(style.element)
    ppr = ensure_ordered_child(style.element, 'pPr', _STYLE_ORDER)
    if props.get('no_border') and ppr.find(W('pBdr')) is not None:
        logger.debug(f' ✓ {style_name}: 已去除段落边框')
    write_run_props(rpr, props, cjk=cjk, latin=latin)
    write_para_props(ppr, props)
    link_el = style.element.find(W('link'))
    if link_el is not None:
        link_id = link_el.attrib.get(W('val'))
        parent = style.element.getparent()
        if link_id and parent is not None:
            for s in parent.findall(W('style')):
                if s.attrib.get(W('styleId')) == link_id:
                    link_rpr = ensure_rpr(s)
                    write_run_props(link_rpr, props, cjk=cjk, latin=latin)
                    break
    logger.debug(f' ✓ {style_name}: 已应用修改')
    return True
from word_markdown.spec.constants import ROW_NAME_EN
from word_markdown.spec.parser import parse_format, parse_style_props

def apply_proofing_language(doc, lang):
    """把校对语言写进 docDefaults。

    只设 `eastAsia` 槽（中文、日文这些字符用的槽）——西文槽保持底模的 en-US，
    否则中文文档里的英文词会被拿去按中文词典查。底模两个槽都是 en-US，不写中文
    语言，Word 就用英文词典查中文，整篇标成拼写错误。
    """
    root = doc.styles.element
    defaults = root.find(W('docDefaults'))
    if defaults is None:
        defaults = etree.Element(W('docDefaults'))
        root.insert(0, defaults)
    rpr_default = ensure_ordered_child(defaults, 'rPrDefault', ['rPrDefault', 'pPrDefault'])
    rpr = ensure_ordered_child(rpr_default, 'rPr', ['rPr'])
    lang_el = ensure_ordered_child(rpr, 'lang', _RPR_ORDER)
    lang_el.set(W('eastAsia'), lang)
    _match_punctuation_compression(doc, lang)
_PUNCT_BY_LANG = {'zh': 'compressPunctuation', 'ja': 'compressPunctuationAndJapaneseKana'}

def _match_punctuation_compression(doc, lang):
    """标点压缩跟着校对语言走。

    中文文档让行首行尾的标点能压成半角，和中文 Word、和学校模板一致；其余语言保持底模的
    `doNotCompress`。这一项是**文档级**的，没法按段分叉，所以只能按整份稿子的语种定一个值。
    """
    want = _PUNCT_BY_LANG.get((lang or '').lower()[:2], 'doNotCompress')
    node = doc.settings.element.find(W('characterSpacingControl'))
    if node is not None:
        node.set(W('val'), want)

def get_default_template():
    """取纯净现代 Word 365 基底模板（resources/pure_modern_base.docx）。"""
    local_base = get_base_template_path()
    if not local_base.exists():
        raise TemplateError(t('template.base_template_missing', 'zh', path=local_base))
    logger.debug(f'使用内置纯净现代 Word 365 基底模板: {local_base}')
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.docx')
    shutil.copyfile(str(local_base), tmp.name)
    tmp.close()
    return tmp.name

def cleanup_temp(path: str):
    try:
        os.unlink(path)
    except OSError:
        pass
REQUIRED_STYLES = {'Title': '文档标题', 'Heading 1': '一级标题', 'Body Text': '正文', 'First Paragraph': '首段', 'Normal': '普通'}
REQUIRED_ROWS = {}
CONDITIONAL_STYLE_ROWS = {'Header': ('页眉', 'template.example.header'), 'Footer': ('页脚', 'template.example.footer')}
TOC_STYLE_GROUP = {'TOC Heading': '目录标题', 'TOC 1': '目录一级', 'TOC 2': '目录二级', 'TOC 3': '目录三级'}
TOC_EXTRA_LEVELS = {4: ('TOC 4', '目录四级'), 5: ('TOC 5', '目录五级')}
STRUCT_TRIGGERS = [(re.compile('^##\\s+\\S'), 'gate.feat.heading2', [('二级标题', 'Heading 2')]), (re.compile('^###\\s+\\S'), 'gate.feat.heading3', [('三级标题', 'Heading 3')]), (re.compile('^####\\s+\\S'), 'gate.feat.heading4', [('四级标题', 'Heading 4')]), (re.compile('^#####\\s+\\S'), 'gate.feat.heading5', [('五级标题', 'Heading 5')]), (re.compile('^######\\s+\\S'), 'gate.feat.heading6', [('六级标题', 'Heading 6')]), (TOC_MARKER_RE, 'gate.feat.toc', [(cn, en) for en, cn in TOC_STYLE_GROUP.items()]), (re.compile('^>\\s+\\S'), 'gate.feat.block_text', [('引用块', 'Block Text')]), (re.compile('custom-style=(["\\\'])Abstract Title\\1'), 'gate.feat.abstract_title', [('摘要标题', 'Abstract Title')]), (re.compile('custom-style=(["\\\'])Abstract\\1'), 'gate.feat.abstract', [('摘要', 'Abstract')]), (re.compile('custom-style=(["\\\'])Keywords\\1'), 'gate.feat.keywords', [('关键词', 'Keywords')]), (re.compile('custom-style=(["\\\'])References Title\\1'), 'gate.feat.references_title', [('参考文献标题', 'References Title')]), (re.compile('(?:::\\s*\\{#refs\\}|custom-style=(["\\\'])Bibliography\\1)'), 'gate.feat.bibliography', [('参考文献', 'Bibliography')]), (re.compile('!\\[[^\\]]*\\]\\([^)]+\\)'), 'gate.feat.image', [('图', 'Captioned Figure'), ('图片题注', 'Image Caption')]), (re.compile('custom-style=(["\\\'])Table Caption\\1'), 'gate.feat.table_caption', [('表格题注', 'Table Caption')]), (re.compile('custom-style=(["\\\'])Table Note\\1'), 'gate.feat.table_note', [('表注', 'Table Note')]), (re.compile('^\\s*\\|.*\\|'), 'gate.feat.markdown_table', [('紧凑', 'Compact'), ('表格', '__ROW_table__'), ('表头', '__ROW_table_header__')])]

def missing_styles_for_body(masked_body: str, declared: set, start_line: int=1, lang: str='zh') -> list:
    """稿件正文用到的结构 → 要求文件漏声明的样式或配置行。

    `masked_body` 是**已掩码代码块**的正文（`skill_utils.mask_code_fences`）——方法学论文
    常在代码块里演示语法，那里的 `>`、`###` 不是稿件结构。
    返回 [(行号, 结构描述, 中文写法, Word 样式名)]，空列表 = 都声明了。
    结构描述按 `lang` 取词（`gate.feat.*`）：报错里说「二级标题 (##)」还是
    「level-2 heading (##)」，跟读者的稿子走。
    """
    issues, reported = ([], set())
    for i, line in enumerate(masked_body.split('\n'), start_line):
        for pat, feat_key, styles in STRUCT_TRIGGERS:
            if not pat.search(line):
                continue
            for cn, en in styles:
                if en not in declared and (feat_key, en) not in reported:
                    reported.add((feat_key, en))
                    issues.append((i, t(feat_key, lang), cn, en))
    return issues
_PARA = ['font_name', 'font_size', 'alignment', 'line_spacing', 'space_before', 'space_after']
_TITLE = _PARA + ['bold', 'italic']
REQUIRED_FIELDS_BY_STYLE = {k: v + ['indent'] for k, v in {'Title': _TITLE, 'Subtitle': _TITLE, 'Heading 1': _TITLE, 'Heading 2': _TITLE, 'Heading 3': _TITLE, 'Heading 4': _TITLE, 'Heading 5': _TITLE, 'Heading 6': _TITLE, 'Abstract Title': _TITLE, 'References Title': _TITLE, 'TOC Heading': _TITLE, 'TOC 1': _PARA, 'TOC 2': _PARA, 'TOC 3': _PARA, 'TOC 4': _PARA, 'TOC 5': _PARA, 'Abstract': _PARA, 'Keywords': _PARA, 'Table Caption': _PARA, 'Table Note': _PARA, 'Image Caption': _PARA, 'Body Text': _PARA, 'First Paragraph': _PARA, 'Compact': _PARA, 'Normal': _PARA, 'Bibliography': _PARA, 'Block Text': _PARA, 'Footnote Text': _PARA, 'Author': _PARA, 'Date': _PARA, 'Header': _PARA, 'Footer': _PARA, 'Captioned Figure': ['alignment', 'line_spacing', 'space_before', 'space_after']}.items()}
FIELD_KEYS = ('font_name', 'font_size', 'alignment', 'line_spacing', 'space_before', 'space_after', 'indent', 'bold', 'italic')

def _style_label(style_name: str, lang: str) -> str:
    """样式在诊断里的称呼：中文用词表里的中文名（沿用既有写法），英文用正典名。"""
    return style_name if lang == 'en' else STYLE_CN.get(style_name, style_name)

def _row_label(row: str, lang: str) -> str:
    """要求文件「行名」的英文回显：先查正典名（页眉 → Header），再查表格配置行（表格 → Table）。"""
    return row if lang != 'en' else STYLE_MAP.get(row) or ROW_NAME_EN.get(row) or row

def _check_required_fields(spec, lang: str='zh') -> bool:
    """校验必填项是否齐备：必填样式 / 样式的必填字段 / 条件成套必填行。"""
    req_by_style = {r['style_name']: r for r in spec.styles}
    missing_required = []
    missing_styles = []
    missing_rows = []
    for style_name, cn_alias in REQUIRED_STYLES.items():
        alias = style_name if lang == 'en' else cn_alias
        if style_name not in req_by_style:
            missing_styles.append((style_name, alias))
            continue
        req = req_by_style[style_name]
        required_fields = REQUIRED_FIELDS_BY_STYLE.get(style_name, [])
        for fname in required_fields:
            if fname == 'indent':
                if 'indent' not in req or 'indent_type' not in req:
                    missing_required.append((alias, fname))
            elif fname not in req or req[fname] is None or req[fname] == '':
                missing_required.append((alias, fname))
    for req in spec.styles:
        style_name = req['style_name']
        alias = req.get('alias') or _style_label(style_name, lang)
        if style_name in REQUIRED_FIELDS_BY_STYLE and style_name not in REQUIRED_STYLES:
            required_fields = REQUIRED_FIELDS_BY_STYLE[style_name]
            for fname in required_fields:
                if fname == 'indent':
                    if 'indent' not in req or 'indent_type' not in req:
                        missing_required.append((alias, fname))
                elif fname not in req or req[fname] is None or req[fname] == '':
                    missing_required.append((alias, fname))
    if any((s in req_by_style for s in TOC_STYLE_GROUP)):
        for s_name, s_alias in TOC_STYLE_GROUP.items():
            if s_name not in req_by_style:
                missing_styles.append((s_name, t('template.style.toc_group', lang, name=_style_label(s_name, lang))))
    for level, (s_name, s_alias) in TOC_EXTRA_LEVELS.items():
        if s_name in req_by_style:
            prev_name, prev_alias = (f'TOC {level - 1}', f"目录{['', '一', '二', '三', '四'][level - 1]}级")
            if prev_name not in req_by_style:
                missing_styles.append((prev_name, t('template.style.toc_prev_level', lang, name=prev_name if lang == 'en' else prev_alias, this=_style_label(s_name, lang))))
    if spec.table or spec.table_header:
        if not spec.table:
            missing_rows.append(('表格', 'template.example.table'))
        if not spec.table_header:
            missing_rows.append(('表头', 'template.example.table_header'))
        if 'Compact' not in req_by_style:
            missing_styles.append(('Compact', t('template.style.compact_group', lang, name=_style_label('Compact', lang))))
    if ('Captioned Figure' in req_by_style) != ('Image Caption' in req_by_style):
        if 'Captioned Figure' not in req_by_style:
            missing_styles.append(('Captioned Figure', t('template.style.figure_group', lang, name=_style_label('Captioned Figure', lang))))
        else:
            missing_styles.append(('Image Caption', t('template.style.image_caption_group', lang, name=_style_label('Image Caption', lang))))
    if 'Abstract Title' in req_by_style and 'Abstract' not in req_by_style:
        missing_styles.append(('Abstract', t('template.style.abstract_group', lang, name=_style_label('Abstract', lang))))
    if 'References Title' in req_by_style and 'Bibliography' not in req_by_style:
        missing_styles.append(('Bibliography', t('template.style.bibliography_group', lang, name=_style_label('Bibliography', lang))))
    for key, (name, example) in REQUIRED_ROWS.items():
        if not getattr(spec, key, None):
            missing_rows.append((name, example))
    if spec.table_header:
        for fname in _PARA + ['indent']:
            if fname == 'indent':
                if 'indent' not in spec.table_header or 'indent_type' not in spec.table_header:
                    missing_required.append((_row_label('表头', lang), fname))
            elif fname not in spec.table_header or spec.table_header[fname] is None or spec.table_header[fname] == '':
                missing_required.append((_row_label('表头', lang), fname))
    for style_name, (name, example) in CONDITIONAL_STYLE_ROWS.items():
        written = any((kind == ('header' if style_name == 'Header' else 'footer') for kind, _ in spec.decor))
        if written and style_name not in req_by_style:
            missing_rows.append((name, example))
    has_errors = bool(missing_required or missing_styles or missing_rows)
    if missing_styles:
        logger.warning(t('template.missing_styles_header', lang))
        for style_name, alias in missing_styles:
            logger.info(t('template.style_item', lang, alias=alias, style=style_name))
        logger.info(t('template.missing_styles_hint', lang))
    if missing_rows:
        logger.warning(t('template.missing_rows_header', lang))
        for name, example in missing_rows:
            logger.info(t('template.row_item', lang, name=_row_label(name, lang), example=t(example, lang)))
    if missing_required:
        logger.warning(t('template.missing_fields_header', lang))
        for alias, field in missing_required:
            hint = t(f'template.hint.{field}', lang) if field in FIELD_KEYS else field
            logger.info(t('template.field_item', lang, alias=alias, field=t(f'template.field.{field}', lang) if field in FIELD_KEYS else field))
            logger.info(t('template.field_fix', lang, hint=hint))
    if has_errors:
        logger.error(t('template.abort', lang))
        logger.error(t('template.abort_hint', lang))
    else:
        logger.debug(t('template.check_ok', lang))
    return has_errors
_XML_SPACE = '{http://www.w3.org/XML/1998/namespace}space'

def _clear_runs(paragraph):
    for r in list(paragraph.runs):
        r._r.getparent().remove(r._r)

def append_field(paragraph, instr):
    """在段落里插入一个 Word 域（begin → instrText → separate → end）。

    不写缓存结果：值由 Word 在更新域/打印时算出，故导出链在出 PDF 前会
    调用 Word 的 update field（页眉里的 STYLEREF 尤其需要）。
    """
    r = paragraph.add_run()._r
    etree.SubElement(r, W('fldChar')).set(W('fldCharType'), 'begin')
    it = etree.SubElement(r, W('instrText'))
    it.set(_XML_SPACE, 'preserve')
    it.text = instr
    etree.SubElement(r, W('fldChar')).set(W('fldCharType'), 'separate')
    etree.SubElement(r, W('fldChar')).set(W('fldCharType'), 'end')

def write_decor_container(container, parts, style_name):
    """把要求文件里解析出来的页眉/页脚内容写进容器"""
    container.is_linked_to_previous = False
    p = container.paragraphs[0] if container.paragraphs else container.add_paragraph()
    _clear_runs(p)
    p.style = style_name
    for kind, val in parts:
        if kind == 'text':
            p.add_run(val)
        else:
            append_field(p, val)

def collect_styleref_names(spec):
    """取出所有 {样式标题:X} 里的 X（去重、保序）"""
    names = []
    for parts in spec.decor.values():
        for kind, val in parts:
            if kind != 'field':
                continue
            m = re.match('STYLEREF\\s+"(.*?)"', val)
            if m and m.group(1) not in names:
                names.append(m.group(1))
    return names

def ensure_style_alias(doc, style_name, alias):
    """把 alias 注册为样式的 w:aliases，返回样式是否存在于模板。

    **为什么需要**：Word 的 STYLEREF 域按样式的**本地化显示名或别名**匹配，唯独不认
    w:name 的字面值。实测（中文版 Word）：样式 w:name="heading 1" 时，
    STYLEREF "heading 1" 与 STYLEREF "Heading 1" 都报"错误!未找到引用源"，
    只有 STYLEREF "标题 1" 能解析；给该样式加上别名 "Heading 1" 后两者才都能用。
    因此这里把要求在文件里写的那个名字原样注册成别名——用户写什么，域就用什么，
    不依赖使用者 Word 的界面语言。
    """
    style = _find_style(doc, style_name)
    if style is None:
        return False
    el = style.element
    al = el.find(W('aliases'))
    existing = [a.strip() for a in (al.get(W('val')) or '').split(',') if a.strip()] if al is not None else []
    if alias in existing:
        return True
    existing.append(alias)
    if al is None:
        al = etree.Element(W('aliases'))
        name_el = el.find(W('name'))
        if name_el is not None:
            name_el.addnext(al)
        else:
            el.insert(0, al)
    al.set(W('val'), ','.join(existing))
    return True

def apply_page_decorations(doc, spec, lang: str='zh'):
    """写入页眉/页脚内容、奇偶与首页开关、页码格式。

    只在格式文件里**真的出现了**的容器上动手：没写的容器保持"链接上一节"，
    不会凭空建出空页眉把偶数页刷白。
    """
    if not spec.decor and (not spec.page_number):
        return
    if len(doc.sections) != 1:
        raise TemplateError(t('template.multiple_sections', lang, count=len(doc.sections)))
    section = doc.sections[0]
    uses_even = any((which == 'even' for _, which in spec.decor))
    uses_first = any((which == 'first' for _, which in spec.decor))
    for kind, cn in (('header', '页眉'), ('footer', '页脚')):
        base = spec.decor.get((kind, 'default'))
        if base is None:
            continue
        for which, label in (('even', '偶数页'), ('first', '首页')):
            if (uses_even if which == 'even' else uses_first) and (kind, which) not in spec.decor:
                spec.decor[kind, which] = base
                logger.debug(f' ✓ {label}{cn}: 未单独指定，已沿用默认{cn}内容')
    if uses_even:
        doc.settings.odd_and_even_pages_header_footer = True
        logger.debug(' ✓ 页眉页脚: 已开启奇偶页不同')
    if uses_first:
        section.different_first_page_header_footer = True
        logger.debug(' ✓ 页眉页脚: 已开启首页不同')
    which_prefix = {'default': '', 'even': 'even_page_', 'first': 'first_page_'}
    kind_name = {'header': ('header', 'Header', '页眉'), 'footer': ('footer', 'Footer', '页脚')}
    for (kind, which), parts in spec.decor.items():
        attr, style_name, cn = kind_name[kind]
        write_decor_container(getattr(section, which_prefix[which] + attr), parts, style_name)
        label = {'default': '', 'even': '偶数页', 'first': '首页'}[which]
        logger.debug(f' ✓ {label}{cn}: 已设置内容与域')
    for nm in collect_styleref_names(spec):
        if ensure_style_alias(doc, nm, nm):
            logger.debug(f' ✓ 页眉/页脚域: 已把「{nm}」注册为样式别名（STYLEREF 才能解析）')
            continue
        cn = STYLE_MAP.get(nm)
        hint = t('template.styleref_alias_hint', lang, name=nm, cn=cn) if cn else ''
        raise TemplateError(t('template.styleref_unknown', lang, name=nm, hint=hint))
    if spec.page_number and (not any((kind == 'field' and val.split()[0] == 'PAGE' for parts in spec.decor.values() for kind, val in parts))):
        raise TemplateError(t('template.page_number_without_field', lang))
    if spec.page_number:
        fmt, start = spec.page_number
        pg = ensure_ordered_child(section._sectPr, 'pgNumType', _SECTPR_ORDER)
        pg.set(W('fmt'), fmt)
        if start is not None:
            pg.set(W('start'), str(start))
        else:
            pg.attrib.pop(W('start'), None)
        logger.debug(f' ✓ 页码: 格式={fmt}' + (f' 起始={start}' if start is not None else ''))
TABLE_DEFAULT_WIDTHS = {'three-line': {'top': 1.5, 'bottom': 1.5, 'header_bottom': 0.75}, 'grid': {'inside': 0.5}, 'top-bottom': {'top': 1.5, 'bottom': 1.5}}
TABLE_BORDER_SIDES = ('top', 'left', 'bottom', 'right', 'insideH', 'insideV')
TABLE_CELL_MARGIN_DEFAULT_CM = 0.0

def table_border_widths(style: str, declared: dict) -> dict:
    """表格样式 + 要求文件声明的线宽 → {边: 磅值，None = 不画线}。

    **生成（写 XML）与导出后对账（读 XML）都从这里取期望值**：两边各推一次时，
    「全网格线的上下外框能不能单独加粗」这种细节会两处不一致——对账按 `线宽内` 取期望，
    于是把"写了 `线宽上` 却不生效"的产物判成通过。
    """
    w = dict(TABLE_DEFAULT_WIDTHS.get(style, {}))
    w.update(declared or {})
    if style == 'three-line':
        return {'top': w.get('top', 1.5), 'bottom': w.get('bottom', 1.5), 'left': None, 'right': None, 'insideH': None, 'insideV': None}
    if style == 'top-bottom':
        return {side: w.get(side, 1.5) if side in ('top', 'bottom') else None for side in TABLE_BORDER_SIDES}
    if style == 'grid':
        line = w.get('inside', 0.5)
        return {'top': w.get('top', line), 'bottom': w.get('bottom', line), 'left': line, 'right': line, 'insideH': line, 'insideV': line}
    return {}

def _pt_to_border_sz(pt):
    """磅 → OOXML 边框 sz（1/8 磅），下限 2（1/4 磅，Word 最细单实线）"""
    return str(max(2, int(round(pt * 8))))

def _get_tbl_style_pr(style_element, cond_type):
    """取/建 w:tblStylePr[type=…]。同类型只能有一个，边框与表头格式共用它。"""
    for el in style_element.findall(W('tblStylePr')):
        if el.get(W('type')) == cond_type:
            return el
    el = etree.SubElement(style_element, W('tblStylePr'))
    el.set(W('type'), cond_type)
    return el

def _add_border(parent, side, sz='none'):
    el = etree.SubElement(parent, W(side))
    if sz == 'none':
        el.set(W('val'), 'none')
    else:
        el.set(W('val'), 'single')
        el.set(W('sz'), sz)
        el.set(W('space'), '0')
        el.set(W('color'), '000000')

def _write_borders(tbl_pr, sides):
    """把 w:tblBorders 重建到 tbl_pr（幂等）。

    `sides` 是 {边: 线宽磅|'none'}，**只写列出的边**。
    条件格式（tblStylePr）里的 tblBorders 是**按边覆盖**的：没列出的边会继承
    全表边框；若在此处补一圈 'none'，反而会把三线表的顶线/底线抹掉。
    """
    old = tbl_pr.find(W('tblBorders'))
    if old is not None:
        tbl_pr.remove(old)
    borders = ensure_ordered_child(tbl_pr, 'tblBorders', _TBLPR_ORDER)
    for side, width in sides.items():
        _add_border(borders, side, 'none' if width is None else _pt_to_border_sz(width))
    return borders

def _write_cell_borders(tbl_style_pr, sides):
    """把边框写进条件格式的 `tcPr/tcBorders`（**单元格级**）。

    ⚠ 条件格式（tblStylePr）里的边框**只能**走这里，不能走 `tblPr/tblBorders`：
    写在表级会被全表 `tblBorders` 的 `insideH="none"` 压掉，Word 渲染时那条线
    根本不出现——而 XML 看上去完全正常。实测三线表的表头下线：写表级 → 只有
    上下两条线；写单元格级 → 三条线齐。
    （同一个条件格式里的 `pPr`/`rPr`——对齐、字体——一直是正常生效的，
      所以只改边框这一处、不逐条渲染核对，极易漏判成"三线表没问题"。）
    """
    tc_pr = ensure_ordered_child(tbl_style_pr, 'tcPr', _TBLSTYLEPR_ORDER)
    old = tc_pr.find(W('tcBorders'))
    if old is not None:
        tc_pr.remove(old)
    borders = ensure_ordered_child(tc_pr, 'tcBorders', _TCPR_ORDER)
    for side, width in sides.items():
        _add_border(borders, side, 'none' if width is None else _pt_to_border_sz(width))
    return borders

def apply_table_format(doc, spec, default_latin=None, lang: str='zh'):
    """写 Table 样式：对齐、边框（含三线表线宽）、表头行条件格式、单元格垂直对齐"""
    table = spec.table or {}
    table_align = table.get('align', 'center')
    table_style = table.get('style') or 'three-line'
    if table_style not in ('three-line', 'grid', 'top-bottom', 'none'):
        raise TemplateError(t('template.table_style_unsupported', lang, style=table_style))
    declared_widths = table.get('widths') or {}
    if declared_widths and table_style == 'none':
        raise TemplateError(t('template.table_widths_without_lines', lang, widths=declared_widths))
    if 'inside' in declared_widths and table_style in ('three-line', 'top-bottom'):
        raise TemplateError(t('template.table_no_inside_line', lang, style=table_style))
    try:
        style_el = doc.styles['Table'].element
    except KeyError:
        logger.warning(t('template.table_style_missing', lang))
        return
    tbl_pr = ensure_ordered_child(style_el, 'tblPr', _STYLE_ORDER)
    old_jc = tbl_pr.find(W('jc'))
    if old_jc is not None:
        tbl_pr.remove(old_jc)
    jc_el = ensure_ordered_child(tbl_pr, 'jc', _TBLPR_ORDER)
    jc_el.set(W('val'), table_align)
    logger.debug(f' ✓ Table: 表格对齐={table_align}（通过 Table 样式 jc 控制）')
    if table_style != 'none':
        declared = table.get('widths', {})
        widths = dict(TABLE_DEFAULT_WIDTHS.get(table_style, {}))
        widths.update(declared)
        style_names = {'three-line': '三线表', 'grid': '全网格线', 'top-bottom': '顶底线'}
        logger.debug(f' ✓ Table: 已应用{style_names.get(table_style, table_style)}边框' + (f'（线宽 {widths} 磅）' if declared else ''))
        _write_borders(tbl_pr, table_border_widths(table_style, declared))
        if table_style == 'three-line' or 'header_bottom' in declared:
            _write_cell_borders(_get_tbl_style_pr(style_el, 'firstRow'), {'bottom': widths.get('header_bottom', 0.75)})
    if spec.table_header:
        props = spec.table_header
        cjk, latin = split_fonts(props.get('font_name'), default_latin)
        tsp = _get_tbl_style_pr(style_el, 'firstRow')
        write_run_props(ensure_ordered_child(tsp, 'rPr', _TBLSTYLEPR_ORDER), props, cjk=cjk, latin=latin)
        write_para_props(ensure_ordered_child(tsp, 'pPr', _TBLSTYLEPR_ORDER), props)
        logger.debug(' ✓ Table: 已设置表头行格式')
    margin_twips = int(round(table.get('cell_margin_cm', TABLE_CELL_MARGIN_DEFAULT_CM) * _TWIPS_PER_CM))
    cell_mar = ensure_ordered_child(tbl_pr, 'tblCellMar', _TBLPR_ORDER)
    for side in ('top', 'left', 'bottom', 'right'):
        el = ensure_ordered_child(cell_mar, side, ('top', 'left', 'bottom', 'right'))
        el.set(W('w'), str(margin_twips))
        el.set(W('type'), 'dxa')
    if 'cell_margin_cm' in table:
        logger.debug(f" ✓ Table: 单元格内边距={table['cell_margin_cm']}cm")
    if table.get('valign'):
        tsp = _get_tbl_style_pr(style_el, 'wholeTable')
        tc_pr = ensure_ordered_child(tsp, 'tcPr', _TBLSTYLEPR_ORDER)
        ensure_ordered_child(tc_pr, 'vAlign', _TCPR_ORDER).set(W('val'), table['valign'])
        logger.debug(f" ✓ Table: 单元格垂直对齐={table['valign']}")

def _props_from_req(req, page_size, margins):
    """样式行解析结果 → modify_style 吃的那份属性 dict。

    唯一一份：主循环与脚注默认都走这里，免得两条路上的可选属性各自漏一项。
    """
    props = {'font_name': req.get('font_name'), 'font_size': req.get('font_size'), 'bold': req.get('bold', False), 'italic': req.get('italic', False), 'alignment': req.get('alignment'), 'line_spacing': req.get('line_spacing'), 'indent': req.get('indent'), 'indent_type': req.get('indent_type'), 'indent_unit': req.get('indent_unit', 'pt')}
    for key in ('space_before', 'space_after', 'no_border', 'bottom_border', 'border_width_pt', 'left_indent_chars', 'char_spacing_pt', 'color', 'keep_lines', 'page_break_before', 'widow_control'):
        if key in req:
            props[key] = req[key]
    if req.get('dot_leader_tab'):
        props['dot_leader_tab'] = True
        props['dot_leader_pos'] = _content_width_twips(page_size, margins)
    if req.get('join_toc'):
        props['join_toc'] = True
    if req.get('keep_next'):
        props['keep_next'] = True
    return props
_FOOTNOTE_STYLE = 'Footnote Text'
_FOOTNOTE_REF_STYLE = 'Footnote Reference'
_ALIGN_CN = {val: cn for cn, val in ALIGN_VAL.items()}

def _footnote_default_line(spec) -> str:
    """没写「脚注：」时的默认样式行：字体与对齐跟着正文走，字号按 SIZE_MAP 降一档。

    降一档的依据是本工具自己的排版原则（注释比正文小 1.5~2 磅）：小四正文 → 五号脚注。
    行距单倍、段前后 0、首行缩进 0——脚注是几行的注释，跟正文同为两端对齐时再带
    2 字符首行缩进会顶成锯齿。
    """
    body = next((r for r in spec.styles if r.get('style_name') == 'Body Text'), None) or {}
    fonts = '/'.join(body.get('font_name') or ['宋体', 'Times New Roman'])
    align = _ALIGN_CN.get(body.get('alignment') or '', '两端对齐')
    size_pt = (body.get('font_size') or SIZE_MAP['小四']).pt
    names = list(SIZE_MAP)
    i = next((n for n, name in enumerate(names) if SIZE_MAP[name].pt <= size_pt), len(names) - 1)
    size_name = names[min(i + 1, len(names) - 1)]
    return f'{fonts} {size_name} 不加粗 不倾斜 {align} 行距1.0倍 段前0磅 段后0磅 首行缩进0字符'

def apply_footnote_format(doc, spec, page_size, margins, default_latin=None):
    """给两个脚注样式落定义：写了「脚注：」用写的，没写从正文推一套默认。

    另：脚注序号唯一要紧的属性是**上标**——不设它，正文里的序号就是个大号数字，
    读起来和普通数字分不开。
    """
    declared = any((r.get('style_name') == _FOOTNOTE_STYLE for r in spec.styles))
    if not declared:
        req = parse_style_props(_FOOTNOTE_STYLE, _footnote_default_line(spec))
        modify_style(doc, _FOOTNOTE_STYLE, _props_from_req(req, page_size, margins), default_latin=default_latin)
        logger.debug(f' ✓ {_FOOTNOTE_STYLE}: 要求文件未写「脚注」，已按正文推定默认')
    try:
        ref = doc.styles[_FOOTNOTE_REF_STYLE]
    except KeyError:
        ref = doc.styles.add_style(_FOOTNOTE_REF_STYLE, WD_STYLE_TYPE.CHARACTER)
    va = ensure_ordered_child(ensure_rpr(ref.element), 'vertAlign', _RPR_ORDER)
    va.set(W('val'), 'superscript')
    logger.debug(f' ✓ {_FOOTNOTE_REF_STYLE}: 已设为上标')

def generate_template(format_text, output_path, margins=None, lang='zh'):
    """主流程：生成模板"""
    output_path = str(output_path)
    logger.debug('生成学术 Word 模板')
    spec = parse_format(format_text)
    page_size = spec.page_size
    reqs, default_latin = (spec.styles, spec.default_latin)
    if not reqs:
        raise TemplateError(t('template.no_styles_parsed', lang))
    if spec.margins:
        margins = spec.margins
    if not margins:
        margins = {'top': 2.54, 'bottom': 2.54, 'left': 2.54, 'right': 2.54}
    logger.debug(f'解析到 {len(reqs)} 个样式定义:')
    for r in reqs:
        desc = f"{r['style_name']}"
        if 'font_name' in r:
            desc += f" {', '.join(r['font_name'])}"
        if 'font_size' in r:
            desc += f" {r['font_size'].pt}pt"
        logger.debug(f' - {desc}')
    if default_latin:
        logger.debug(f' 全局西文字体: {default_latin}')
    if _check_required_fields(spec, lang):
        raise TemplateError(t('template.required_missing', lang))
    logger.debug('加载纯净现代 Word 365 底模...')
    tmp_path = get_default_template()
    doc = Document(tmp_path)
    cleanup_temp(tmp_path)
    if spec.proofing_lang:
        apply_proofing_language(doc, spec.proofing_lang)
        logger.debug(f' ✓ 校对语言: {spec.proofing_lang}')
    for req in reqs:
        style_name = req.get('style_name')
        if not style_name:
            continue
        modify_style(doc, style_name, _props_from_req(req, page_size, margins), default_latin=default_latin)
    apply_table_format(doc, spec, default_latin=default_latin, lang=lang)
    apply_page_decorations(doc, spec, lang=lang)
    apply_footnote_format(doc, spec, page_size, margins, default_latin=default_latin)
    try:
        hyperlink_style = doc.styles['Hyperlink']
        hyperlink_style.font.color.rgb = RGBColor(0, 0, 0)
        hyperlink_style.font.underline = False
        rpr = hyperlink_style.element.find(W('rPr'))
        if rpr is not None:
            color_el = rpr.find(W('color'))
            if color_el is not None:
                clear_color_theme_attrs(color_el)
        logger.debug(' ✓ Hyperlink: 已设为黑色（取消下划线）')
    except KeyError:
        pass
    try:
        caption_style = doc.styles['Caption']
        caption_style.font.italic = False
        logger.debug(' ✓ Caption: 已清除斜体（影响 Image Caption / Table Caption）')
    except KeyError:
        pass
    try:
        abstract_style = doc.styles['Abstract']
        ppr = abstract_style.element.find(W('pPr'))
        if ppr is not None:
            removed = []
            for tag in ('keepNext', 'keepLines'):
                el = ppr.find(W(tag))
                if el is not None:
                    ppr.remove(el)
                    removed.append(tag)
            if removed:
                logger.debug(f" ✓ Abstract: 已清除 {' / '.join(removed)}（结构化摘要分段不再触发首页分页）")
    except KeyError:
        pass
    for style_el in doc.styles._element:
        style_id = style_el.get(W('styleId'))
        if not style_id:
            continue
        is_char_style = 'Char' in style_id and any((style_id.startswith(p) for p in ('Title', 'Subtitle', 'Heading')))
        is_unused_heading = style_id in ('Subtitle', 'TOCHeading') or (style_id and style_id.startswith('Heading') and (style_id[-1] in '456789'))
        if is_char_style or is_unused_heading:
            rpr = style_el.find(W('rPr'))
            if rpr is not None:
                rfonts = rpr.find(W('rFonts'))
                color_el = rpr.find(W('color'))
                had_font = rfonts is not None and clear_font_theme_attrs(rfonts)
                had_color = color_el is not None and clear_color_theme_attrs(color_el)
                if color_el is not None and color_el.get(W('val')) not in ('000000', 'auto'):
                    color_el.set(W('val'), '000000')
                    had_color = True
                if had_font and is_char_style:
                    logger.debug(f' ✓ {style_id}: 已清除主题字体')
                if had_color and is_char_style:
                    logger.debug(f' ✓ {style_id}: 已清除主题色')
    from docx.shared import Cm
    w_cm, h_cm = PAGE_SIZES_CM.get(page_size, PAGE_SIZES_CM['a4'])
    pg_w, pg_h = (Cm(w_cm), Cm(h_cm))
    eff_left = margins['left'] + (spec.gutter_cm or 0)
    for section in doc.sections:
        section.page_width = pg_w
        section.page_height = pg_h
        section.top_margin = Cm(margins['top'])
        section.bottom_margin = Cm(margins['bottom'])
        section.left_margin = Cm(eff_left)
        section.right_margin = Cm(margins['right'])
        if spec.header_distance is not None:
            section.header_distance = Cm(spec.header_distance)
        if spec.footer_distance is not None:
            section.footer_distance = Cm(spec.footer_distance)
        if spec.doc_grid_lines:
            grid = ensure_ordered_child(section._sectPr, 'docGrid', _SECTPR_ORDER)
            grid.set(W('type'), 'lines')
            grid.set(W('linePitch'), str(doc_grid_pitch(spec, margins)))
        w_cm = round(pg_w.inches * 2.54, 1)
        h_cm = round(pg_h.inches * 2.54, 1)
        content_w = round(w_cm - eff_left - margins['right'], 1)
        decor_cm = ''
        if spec.header_distance is not None or spec.footer_distance is not None:
            decor_cm = f'，页眉距边界 {spec.header_distance}cm 页脚距边界 {spec.footer_distance}cm'
        extra = ''
        left_note = ''
        if spec.gutter_cm:
            extra += f'，装订线 {spec.gutter_cm}cm'
            left_note = f"（{margins['left']}+装订线{spec.gutter_cm}）"
        if spec.doc_grid_lines:
            extra += f'，文档网格 每页 {spec.doc_grid_lines} 行'
        logger.debug(f" ✓ 页面: {page_size} ({w_cm}×{h_cm}cm)，边距 上{margins['top']}cm 下{margins['bottom']}cm 左{eff_left}cm{left_note} 右{margins['right']}cm，内容宽 {content_w}cm{decor_cm}{extra}")
    try:
        doc.styles['Block Text']
    except KeyError:
        doc.styles.add_style('Block Text', WD_STYLE_TYPE.PARAGRAPH)
        logger.debug(' ✓ Block Text: 已创建')
    try:
        code_style = doc.styles['Source Code']
    except KeyError:
        code_style = doc.styles.add_style('Source Code', WD_STYLE_TYPE.PARAGRAPH)
        logger.debug(' ✓ Source Code: 已创建')
    code_style.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT
    doc.save(output_path)
    try:
        import zipfile
        with zipfile.ZipFile(output_path, 'r') as zin:
            styles_xml = zin.read('word/styles.xml')
            has_styles_with_effects = 'word/stylesWithEffects.xml' in zin.namelist()
            other_items = {item.filename: zin.read(item.filename) for item in zin.infolist() if item.filename not in ('word/styles.xml', 'word/stylesWithEffects.xml')}
        cleaned_styles_xml = strip_theme_attrs_from_xml(styles_xml)
        tmp_out = output_path + '.tmp'
        with zipfile.ZipFile(tmp_out, 'w', compression=zipfile.ZIP_DEFLATED) as zout:
            zout.writestr('word/styles.xml', cleaned_styles_xml)
            if has_styles_with_effects:
                zout.writestr('word/stylesWithEffects.xml', cleaned_styles_xml)
            for fname, fcontent in other_items.items():
                zout.writestr(fname, fcontent)
        os.replace(tmp_out, output_path)
    except Exception as e:
        raise RuntimeError(t('template.theme_cleanup_failed', lang, err=e)) from e
    get_logger().info(t('template.built', lang, count=len(reqs)))

def main():
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
        print(__doc__)
        sys.exit(0)
    format_file = None
    output_path = 'template.docx'
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ('-o', '--output'):
            i += 1
            if i < len(args):
                output_path = args[i]
        elif not arg.startswith('-') and format_file is None:
            format_file = arg
        else:
            logger.error(f'错误: 不认识的参数 "{arg}"')
            logger.error('用法: python3 generate_template.py format.txt -o template.docx')
            sys.exit(1)
        i += 1
    if format_file is None:
        logger.error('错误: 未提供格式要求文件')
        logger.error('用法: python3 generate_template.py format.txt -o template.docx')
        sys.exit(1)
    if not os.path.exists(format_file):
        logger.error(f'错误: 文件不存在 "{format_file}"')
        sys.exit(1)
    with open(format_file, 'r', encoding='utf-8') as f:
        format_text = f.read()
    try:
        generate_template(format_text, output_path)
    except TemplateError as exc:
        logger.error(f'错误: {exc}')
        sys.exit(1)
    logger.info(t('template.styles_only', 'zh'))
