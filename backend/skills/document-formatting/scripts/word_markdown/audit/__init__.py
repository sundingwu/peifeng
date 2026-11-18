"""word_markdown.audit — 质量防检与审计门禁子包。"""
from word_markdown.audit.inspector import audit_docx_against_spec
from word_markdown.audit.postflight import verify_numeric_integrity
__all__ = ['audit_docx_against_spec', 'verify_numeric_integrity']
