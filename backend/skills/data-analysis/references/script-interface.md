# 脚本输入契约

## 多表日志审查
scripts/audit_observations.py，使用--input和--base-dir。输入单目标对象或targets数组：


```json
{"inputs":[{"path":"raw/part_a.csv"},{"path":"raw/part_b.xlsx","sheet":"runs"}],"group_by":["task"],"keys":["seed"],"method":"method","value":"score","status":"status","accepted_status":"ok","excluded_statuses":["failed"],"candidate":"Candidate","controls":["Baseline","Ablation"],"precision":10,"reproduce":"reproduce.py","facts":"internal/paired_facts.json"}
```


列名、方法名和状态值按实际协议配置。inputs表头只能包含声明的列，额外元数据需明确处理；不悄悄忽略。先按文本去空格、数值等价去重，再按业务键(group_by+method+keys)隔离冲突，最后排除失败。缺失有效score和未知状态报错。默认输出cleaned.csv、metrics.csv、paired.csv、issues.json；可用outputs对象的cleaned/metrics/paired/issues指定路径。precision只控制均值和标准差，观测及差值保留浮点往返表示，避免小差值被格式化成零。单观测样本标准差为空。
reproduce参数自动复制自包含Python助手与JSON配方到internal/，生成项目入口，并实际执行一次，核对四文件哈希相同且输入未改，在verification返回结果；无需手写封装或另建QA程序。复制后的助手只需标准库和openpyxl，可在新目录重生成四份数据，不依赖PeiFeng包。无状态列时明确status:null，表示未提供状态信息，只审查已有数值，不声称运行成功；不人为添加status列。facts可选，首次运行时生成并交接事实；复现配方只重建四份数据，若数据变化需重新生成事实后成稿。全失败、未知列或不同冲突处理协议不由本脚本臆测，报告范围并选其他实现。

排序核验scripts/check_order.py：对象数组输入`{"path":"output.json","array":"records","keys":["group","method"]}`；顶层数组省略array，CSV省略array。样本ID等标量数组直接输入path、array，省略keys或设为空数组。数组下标用点分隔数字，如pairing_gaps.0.candidate_only。按原类型升序比较，不转换类型、不改原文件；失败时指出首个顺序差异并返回非零CLI状态。直接导入check_order函数返回JSON字符串，需json.loads，不能直接用['status']索引；默认用CLI即可。audit_observations回执sorting_checked=true已表示实际读取四份输出并完成CSV/JSON及缺配键数组排序检查，不必重复逐项调用。

--input指定UTF-8 JSON文件，内容可为单目标对象、目标数组或含targets的对象。--base-dir指定输入输出的工作目录。



```json
{
  "values": [
    1,
    2,
    3
  ],
  "operation": "describe",
  "output": "statistics.json"
}
```



统计operation为describe/welch/paired/meta；paired/welch另提供other，meta另提供variances。

## 配对事实脚本
scripts/paired_evidence.py使用同一CLI。示例输入：


```json
{"path":"cleaned.csv","output":"internal/paired_facts.json","group_by":["task"],"keys":["seed"],"method":"method","value":"score","candidate":"Candidate","controls":["Baseline","Ablation"]}
```


无task等分组列时group_by为空数组。列名和方法名按实际输入指定；不改输入、不清洗或去重。输出comparisons是结构化统计，facts是确定性中文事实块，sources记录相对事实文件的来源路径和SHA256。将output路径与facts的实际ID交接给Writer；不要重写facts中的正负零数量或缺配侧。没有共同键时均值为空而非0。此脚本不推断单位、独立性或方法优劣。
事实文件包含固定producer和recipe。write(facts=...)或create_word.py（action=markdown,facts=...）在绑定时重新计算配方并比对完整结果，不只比对哈希；手改事实、过期来源、未知ID均失败，已有文档保持不变。
# 分组统计独立复现

summarize_table.py支持可选reproduce字段，例如{"path":"data.csv","group":"class","value":"score","metrics":["count","mean"],"precision":2,"output":"left/summary.csv","reproduce":"left/reproduce.py"}。通过write(json_data=对象)保存参数，再直接用run_code运行技能入口。生成的reproduce.py内嵌实现与相对路径参数，可复制到新项目目录运行，不依赖skill://或培风安装目录。
