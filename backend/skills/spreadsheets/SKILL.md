---
name: spreadsheets
title: Excel表格
description: 创建、编辑和检查Excel工作簿，保留公式、宏及格式，识别缓存和特殊对象的保存风险。
---

# Excel表格

先read表结构、单位和公式，修改已有文件时保留原有约定。通过run_code执行scripts/edit_workbook.py，接口与格式见[工作簿规则](references/guide.md)。

优先用公式表达可变计算，输入与公式区分；不要以data_only=True加载后保存。输出脚本会列出公式错误与not_computed；纯Python路径不执行通用Excel公式重算，不能把空缓存说成正确结果。编辑含特殊对象的工作簿须保留XML，脚本拒绝可能丢失的整体保存。

命令：`file_path=skill://spreadsheets/scripts/edit_workbook.py`，`args=["--input", "internal/workbook.json", "--base-dir", "."]`。回执检查每项状态及实际files；失败不覆盖旧文件。使用Python库，不调用LibreOffice或新tool。

格式专属实现和读取解析均位于本技能scripts；模板、校验数据与来源许可位于assets。通用read只作分发，run_code只负责受控执行；不依赖独立文档vendor或CLI转发。
