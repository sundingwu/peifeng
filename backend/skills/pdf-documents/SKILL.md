---
name: pdf-documents
title: PDF处理
description: 创建、渲染、合并、拆分、旋转PDF，处理图片、水印及表单并保留来源验证。
---

# PDF处理

普通PDF先用read读取文字或结构，制作和页面操作通过scripts/process_pdf.py完成。接口见[PDF操作](references/guide.md)。表单先form_info判定真实字段，再按字段ID与页码fill_form；非交互表单用明确坐标的annotate，不能猜位置。

命令：`file_path=skill://pdf-documents/scripts/process_pdf.py`，`args=["--input", "internal/pdf.json", "--base-dir", "."]`。创建/编辑后read或render抽查文字、页数和版面；修改后重新渲染，不用旧图证明新版本。输出回执含实际files，来源变化或生成失败不覆盖旧稿。

仅使用Python库。PDF渲染用pypdfium2，不引入Poppler；不安装OCR/Tesseract或调用Office/TeX。扫描件没有可提取文字时明确说明。AES加密依赖可选密码库，缺失时拒绝，不自动降级。

格式专属实现和读取解析均位于本技能scripts；模板、校验数据与来源许可位于assets。通用read只作分发，run_code只负责受控执行；不依赖独立文档vendor或CLI转发。
