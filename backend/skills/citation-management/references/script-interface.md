# 引用脚本接口
`file_path=skill://citation-management/scripts/references.py`，`args=["--input", "input.json", "--base-dir", "."]`
输入单对象、对象数组或`{"targets":[...]}`；逐项返回实际状态。

- 查询：`{"operation":"lookup","source":"crossref","query":"主题","limit":5}`；Crossref也可传doi，arxiv/pubmed只接收query和1–20的limit。结果为元数据/摘要，未读全文。
- 转换：`{"operation":"convert","path":"refs.nbib","output":"refs.ris"}`。
- 去重：`{"operation":"deduplicate","path":"refs.json","output":"unique.bib"}`。

读取扩展名.bib/.ris/.nbib/.json；导出仅.bib/.ris/.json。JSON为对象数组，常用id、type、title、author（以` and `连接）、year、doi、journal、url。转换不会自动查缺补漏。DOI字段核验另用本角色paper-download；主张对应和严格他引判断按methods.md执行，不是此脚本自动完成。
