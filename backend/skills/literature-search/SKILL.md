---
name: literature-search
title: 公开检索与资料获取
description: Researcher 统一检索公开网页、论文、数据集并获取网页或文件，记录证据层级与来源；无需账号。
---

# 公开检索与资料获取

检索式、来源、版本和读取级别可追溯；按用户所需证据停止。公开网页/论文查询、读取与下载统一用research_web.py，不连接账号，不把失败当无结果。

同一来源超时最多重试一次，403/限流不改URL反复访问；失败后记录原因，尝试一个不同公开来源或交付实际证据限制。题录字段已足够就停止，不为未要求的全文追加抓取。

## 按需读取

- references/methods.md：策略、MeSH、六维短名单和增量追踪。
- references/script-interface.md：search/paper/fetch/download参数。

执行入口：`file_path=skill://literature-search/scripts/research_web.py`，`args=["--input", "input.json", "--base-dir", "."]`。输入为单对象、数组或targets；参数见上方接口，检查每项真实结果。
