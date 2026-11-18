---
name: paper-download
title: 论文下载
description: 核验DOI题录，获取无需账号的公开论文全文与附件。
---

# 论文下载

所需题录字段齐全时不额外下载；需要全文才获取公开文件。DOI存在、文件下载与论文身份/正文支持分别核验。doi.py先查Crossref，明确404时再查DataCite，支持数据集DOI；注册源均只提供元数据。

同一注册源超时最多重试一次；之后记录无法核验，不靠新建编号参数文件或增加timeout继续重复请求。可用已核验的输入记录，保留其原始来源与检索时间，不假装本次重新检索成功。

## 按需读取

- references/methods.md：公开来源选择、批量失败与交付核验。
- references/script-interface.md：doi.py和download.py参数。
- references/doi-report.md：脚本报告占位模板，非额外流程。

执行入口：`file_path=skill://paper-download/scripts/doi.py`，`args=["--input", "input.json", "--base-dir", "."]`。输入为单对象、数组或targets；参数见上方接口，检查每项真实结果。
