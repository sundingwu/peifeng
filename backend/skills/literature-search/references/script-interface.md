# 公开检索与获取接口
`file_path=skill://literature-search/scripts/research_web.py`，`args=["--input", "input.json", "--base-dir", "."]`
输入单对象、对象数组或targets对象，项目路径均相对项目根。

| action | 参数 | 结果范围 |
|---|---|---|
| search | query、count=1–20，可选need_content | 公开搜索命中；附页失败逐条保留 |
| paper | query、count，source=crossref/arxiv/pubmed | 题录/摘要，不等于全文 |
| fetch | url | 网页或PDF文本，附截断与读取级别 |
| download | url、output | 保存公开文件，下载不代表已读 |

除download外output可保存结果JSON，否则直接返回外部证据边界。已有输出默认拒绝覆盖，明确需要传overwrite=true。URL逐跳受服务器域名、端口、DNS白名单约束，不支持账号、代理绕过或内网。检索失败返回失败，不能写成“没有研究”。DOI及精确书目字段核验使用paper-download的doi.py；参考文件转换用citation-management。
