# 筛选计数与试标接口
`file_path=skill://literature-review/scripts/screening.py`，`args=["--input", "input.json", "--base-dir", "."]`

`{"path":"decisions.json","output":"report.json"}`，可加reference字段（人工决策JSON路径）比较给定人工标签。
决策文件是数组：`[{"id":"p1","stage":"title_abstract","decision":"include"}]`。
stage为title_abstract/full_text；decision为include/exclude/uncertain。排除需reason，全文排除另需locator；同id同阶段重复或题录已排除又进入全文报错。

输出含各阶段真实计数与原记录。提供reference时按id+stage配对，include/uncertain均映射“推进”，exclude映射“排除”；返回一致率、kappa、PABAK、2×2计数和未匹配键。单一类别导致kappa分母为零时为null，不冒称完美校准。未提供reference不评估一致性。脚本不判断资格，不生成双盲意见或完整PRISMA。
