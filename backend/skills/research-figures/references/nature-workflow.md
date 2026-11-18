# 本地 Nature 绘图流程

## 输入与模板

所有路径位于当前项目，输出前缀可含点号。专业模板使用 CSV，表头如下：

| kind | 必需列 | 内容 |
|---|---|---|
| volcano | gene,log2fc,padj | 差异量与校正后显著性 |
| roc | fpr 与一个或多个 tpr 列 | 按真实曲线比较模型 |
| dotplot | cell_type,gene,pct_exp,avg_exp_scaled | 比例编码与强度编码 |
| marginal | x,y,group | 散点与边际分布 |
| paired | subject,condition,value | 同一主体跨条件比较 |

具体模板参数遵循 scripts/nature_templates.py 的 parser；先读参数定义，不猜测字段。最小输入：
`{"kind":"volcano","path":"data.csv","output":"figures/result","style":{"palette":["#1764A0","#62B5E5","#BDDCF0"]}}`。

## 多面板与导出

先列证据链、面板主张、比较关系，再安排布局。保持同一行/列对齐、图例与标签间距一致，不为齐整裁掉数据或改变量纲。项目自定义 Python 可导入 backend.utils.skills.load_skill_script 加载 research-figures 的 figure_qa，再调用 save_bundle(fig,output_path,spec)。spec 通过 backend.utils.charts.resolve_style 得到。沿用真实数据、脚本与参数作为复现依据。

查看辅助代码使用read(targets=[{"skill":"research-figures","path":"scripts/figure_qa.py"}])，不要把返回中的系统绝对路径作为普通项目文件读取。

save_bundle 在临时目录导出，检查通过后发布；同名产物会更新，重要旧版本应先保留。PDF 内嵌字体，SVG 保留文本。对齐门禁基于上游 Matplotlib 几何审计；PDF 字号审计复用上游解析器，碰撞审计复用其判定逻辑，但以已安装 PDFium 提取几何，曲线采用近似。结构图使用上游 SVG 静态审计，不声称检测全部交叉碰撞。

审计 FAIL 必须处理；WARN 逐项解释。检查不能证明数据真实、推断有效或完全无视觉问题，最后必须 read 实际图片。kind=audit、path=PDF/SVG/Python源码 可单独复查；源码静态预检不能完整识别间接函数调用，须结合真实导出报告。字号阈值默认5pt不是任意期刊合规保证，应按目标规格调整。

## 迁移范围

融合上游五类真实 CSV 模板、证据面板组织、图注/矢量/资产改编规则、对齐、PDF 字号、碰撞和 SVG 审计方法。只读上游源码位于 vendor，模型使用本地 scripts 接口。移除 R、在线图片生成和外部环境安装路线，不增加 PyMuPDF；保留原有基础统计图、结构图及 style 兼容。正式期刊要求由任务提供，不自动套用固定章节或演示数据。
