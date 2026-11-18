# DOI与下载接口
两脚本均用`--input input.json --base-dir .`，支持单对象、数组或targets。

- `file_path=skill://paper-download/scripts/doi.py`，`args=["..."]`：`{"doi":"10.xxxx/xxxx","output":"doi-report.md"}`；或query与limit检索。报告output为.md；核对metadata_complete、missing_fields和实际字段。
- `file_path=skill://paper-download/scripts/download.py`，`args=["..."]`：`{"url":"公开文件URL","output":"paper.pdf"}`，可选doi先查注册记录。PDF必须有PDF签名并可解析页面；结果含source_url、bytes、sha256、doi_metadata，document_identity仍为not_checked，需读取内容确认。

字段校验、真实覆盖行为以脚本为准；不要将本地保存或DOI存在写成全文支持结论。
