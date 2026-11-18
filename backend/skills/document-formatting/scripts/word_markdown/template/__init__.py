"""word_markdown.template — 模板生成与反推子包。"""
from word_markdown.template.builder import build_template
from word_markdown.template.extractor import extract_spec_from_docx
__all__ = ['build_template', 'extract_spec_from_docx']
