---
name: research-review
title: 研究审查
description: 审查研究思路、论文、证据与统计；核验修订和交付；按给定标准校准审查。
---

# 研究审查

按验收范围读实际证据，给可定位问题与最小修复，区分通过/警告/未检查。多视角在当前上下文完成，不构建独立评审或虚构共识。

## 按需读取

- references/methods.md：全面/快速/方法/引导/复审/校准及交付核验。
- references/inference-boundaries.md：涉及统计与科学推断时。
- references/script-interface.md：确定性审计的参数和范围。
- references/venue_profile.schema.json：明确提供投稿限制声明时。

角色分工：Mentor用read读取证据并作出审查判断，不执行代码；需要确定性检查时派Engineer运行本技能脚本并交付真实回执，不将脚本通过等同于科学结论成立。

Engineer执行入口：`file_path=skill://research-review/scripts/audit.py`，`args=["--input", "input.json", "--base-dir", "."]`。输入为单对象、数组或targets；参数见上方接口，检查每项真实结果。

交付归档用scripts/verify_package.py生成清单与外部哈希回执，避免手写过期哈希和自引用循环；仅机械可读性检查，接口见references/script-interface.md。
