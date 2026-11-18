---
name: literature-review
title: 文献综述
description: 研究简报、叙述综述、系统综述、筛选协议与偏倚评估。
---

# 文献综述

叙述综合、WHY/HOW/WHAT扫描、系统/范围/快速综述按问题选择，不默认完整PRISMA。筛选只据实际文本，uncertain不直接排除；当前为单上下文辅助。

## 按需读取

- references/methods.md：综述、八种筛选任务、试标、RoB/GRADE与报告。
- references/script-interface.md：筛选计数和给定人工标签对比。
- references/inference-boundaries.md：统计与因果解释边界。

执行入口：`file_path=skill://literature-review/scripts/screening.py`，`args=["--input", "input.json", "--base-dir", "."]`。输入为单对象、数组或targets；参数见上方接口，检查每项真实结果。
