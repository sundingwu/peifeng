---
name: paper-reading
title: 论文阅读
description: 论文问答、深读X-ray、十六节精读卡、公式微例与逐块双语阅读。
---

# 论文阅读

按用户范围选择问答、深读X-ray、十六节卡片或逐块双语阅读；来源自述、分析与教学示例分开，不伪造页码或全文覆盖。

## 按需读取

- references/methods.md：深读、承重设计、公式微例和证据边界。
- references/paper-card.md：明确要完整十六节精读卡时。
- references/reader-format.md：全文/节选对照、图表公式与source_map。
- references/script-interface.md：PDF提取及精读卡源证据包。

执行入口：`file_path=skill://paper-reading/scripts/extract_pdf.py`，`args=["--input", "input.json", "--base-dir", "."]`。输入为单对象、数组或targets；参数见上方接口，检查每项真实结果。
