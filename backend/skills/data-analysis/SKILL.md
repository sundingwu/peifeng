---
name: data-analysis
title: 数据分析
description: 清洗和汇总数据，配对比较、统计检验及荟萃分析。
---

# 数据分析

按明确协议处理实验单位、缺失、重复、失败、配对与排序；保留原始数据、复现配方和真实验证。内置统计不覆盖所有模型，扩展分析用项目Python并验证。

## 按需读取

- references/methods.md：数据交付、统计报告、方法选择与荟萃分析。
- references/script-interface.md：现有五个脚本、事实绑定及排序参数。

执行入口：`file_path=skill://data-analysis/scripts/analyze_statistics.py`，`args=["--input", "input.json", "--base-dir", "."]`。输入为单对象、数组或targets；参数见上方接口，检查每项真实结果。
