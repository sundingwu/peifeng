---
name: citation-management
title: 引用管理
description: 管理题录、转换格式与去重；定位主张证据，核查引用与严格他引。
---

# 引用管理

先固定用户要核验的字段或主张；区分作品身份、正文支持和作者独立性。题录/格式转换用references.py，科学支持和严格他引按方法执行，不把索引总数当已核他引。

## 按需读取

- references/methods.md：字段核验、主张映射、版本、格式和严格他引。
- references/script-interface.md：运行题录查询、转换或去重。

执行入口：`file_path=skill://citation-management/scripts/references.py`，`args=["--input", "input.json", "--base-dir", "."]`。输入为单对象、数组或targets；参数见上方接口，检查每项真实结果。
