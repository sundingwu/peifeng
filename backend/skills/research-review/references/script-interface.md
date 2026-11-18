# 审计接口

归档入口：`file_path=skill://research-review/scripts/verify_package.py`，`args=["--input", "package.json", "--base-dir", "."]`。输入为`{"files":["deliverables/report.md","analysis/results.json"],"manifest":"deliverables/清单.md","report":"analysis/package-verification.json"}`。缺失或不可解析输入会拒绝，不覆盖旧清单；清单不嵌入自身哈希，外部JSON记录清单当前哈希，JSON自身不自引用。历史数值检查回执可作为静态输入保留，不把其内部旧版本哈希冒称当前哈希。此入口不验证科学结论，仍由Mentor审核。

`file_path=skill://research-review/scripts/audit.py`，`args=["--input", "input.json", "--base-dir", "."]`
输入单对象、对象数组或targets；path为被核材料，operation如下：

| operation | 其他参数 | 边界 |
|---|---|---|
| fingerprint | 无 | 原始字节SHA256与大小 |
| quote | quote | 字面出现位置，不证明语义支持 |
| office | 无 | Office结构，不是分页渲染 |
| reader_math | 可选source_map | 公式表示/映射，不证明推导 |
| paper_card | bundle、locator_mode | 01–16节及来源定位；page-grounded需bundle |
| response_package | response，可选同时给clean/marked | 文本/LaTeX修订包一致性，不编译 |
| submission | 可选passport、join_map、venue_profile | 已声明投稿包与证据检查 |

locator_mode只选page-grounded/structure-grounded/source-limited；参考资料不全时明确降级，不捏页码。submission的venue_profile按venue_profile.schema.json声明，未知限制留空，不能从期刊名猜测。报告中not_checked/warn不是pass，CLI执行成功也不等于被审材料通过。audit_snapshot.py仅为现有审计依赖保留，不启用其上游提示词生成/额外模型流程。
