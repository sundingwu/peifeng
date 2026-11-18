# 工作簿规则与接口

参数文件为单对象或targets数组。示例：
```json
{"action":"create","file":"结果.xlsx","sheets":[{"name":"实验","header":["条件","数值"],"rows":[["A",2],["B",3],["总和","=SUM(B2:B3)"]],"number_formats":{"B":"0.00"},"widths":{"A":20,"B":16},"freeze":"A2"}],"style":{"font":"微软雅黑","size":11,"header_bold":true,"header_fill":"DCE6F1"}}
```
已有输出需overwrite=true。sheet名和表头按用户指定，不静默截断、改名或更换口径。合并区域使用merge数组，数值格式随含义设置，百分比以小数存储，日期用日期格式，单位写在表头。

edit使用path原稿、file输出、patch=[{sheet,cell,value,number_format?}]。同名覆盖需overwrite=true；不要改动指定输入区以外的公式。xlsm保持后缀并keep_vba，富文本保留；发现文本框、原生形状、切片器、时间线或控件时拒绝整体保存，需单独编写项目内XML定点编辑脚本并对照源包核验。合并单元格只改左上锚点。

inspect/check使用path；check遇到实际错误单元格会失败，返回所有未计算缓存地址。openpyxl只写公式，不计算；data_only=True不能证明公式正确，空值也可能是空字符串结果。不要伪造缓存来让检查通过。外链和新Excel函数不能宣称已在当前环境重算；检查引用范围和对照手算/已给结果，必要时明确需Excel打开重算。

上游xlsx的方法已适配：公式与值双读、数字格式、输入区约定、宏/富文本保护、特殊图形风险识别。图表优先原生，设置轴、图例和标题，避免遮挡；有日期轴时检查交叉轴关系。金融配色只用于金融模型，不强加给科研数据。

不启用上游LibreOffice recalc.py、markitdown或额外服务；通用公式重算不是当前能力。Apache-2.0许可保存在本技能assets，工作簿实现直接位于scripts。
