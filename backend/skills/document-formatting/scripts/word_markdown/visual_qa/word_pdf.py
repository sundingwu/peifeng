"""本地能力边界：不执行 Office，也不伪造分页视觉验证。"""
def can_render_word_pdf():
    return False, '本技能只生成Word，不调用Office渲染'
def explain_unavailable(reason, lang='zh'):
    return reason
def render_pdf_via_word(docx_path, pdf_path=None):
    return False, '本技能只生成Word，不调用Office渲染'
