# PDF阅读接口
`file_path=skill://paper-reading/scripts/extract_pdf.py`，`args=["--input", "input.json", "--base-dir", "."]`

`{"path":"paper.pdf","pages":[1],"output":"extracted.json"}`；省略pages取全部页。页面从1起，越界/布尔/重复页码拒绝。返回源哈希、所读页文本与范围；无可提取文本给警告，未读全文不能标完整全文。

需要供精读卡审计的证据包时加`source_bundle:true`，结果增加pages中的pdf_page、物理page_count、已读页与覆盖范围，evidence_inventory初始为未人工核对状态。需核对图表公式后才可填清单，脚本不伪称已识别全部图表或OCR。
