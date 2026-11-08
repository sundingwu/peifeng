---
name: summary_docx
description: 生成工作总结类 Word 文档时用：学期/学年工作总结、项目结题总结、阶段总结的章节骨架、完整 theme 参数（页边距字体行距）、数据表写法与自检清单
---
# 工作总结（.docx）

**工具本身不带任何样式**：`docx` 的 `theme` 是版式的唯一来源，本技能的 theme 段就是权威参数——照抄进 `docx(theme=...)`，不要自己发挥字号字体。

## 一、结构骨架（按这个顺序，缺项要补）

| 章节 | 内容要求 |
|---|---|
| 标题 | 「XX 工作总结」；副标题写时间范围（如「2025—2026 学年第一学期」） |
| 引言段 | 3~4 句：为什么做、按什么依据做、总体结论 |
| 一、总体情况 | 任务量与覆盖范围（数字优先：批次数、人数、项数） |
| 二、重点工作 | 分条（一）（二）（三），每条写「做了什么 + 结果如何」，能带数字就带 |
| 三、数据支撑 | 至少一张表（见下）；表后一段解读，不要只放表不说话 |
| 四、存在问题 | 2~4 条，写具体问题 + 原因，不写「有待加强」这类空话 |
| 五、下一步计划 | 与「存在问题」一一对应，写清做什么、什么时候做 |
| 落款 | 单位 + 日期，右对齐 |

## 二、theme 参数（原样传给 `docx(theme=...)`）

```json
{
  "page": {"top": 2.54, "bottom": 2.54, "left": 2.8, "right": 2.6},
  "styles": {
    "title":     {"font": "SimHei",   "size": 22, "bold": true, "align": "center", "space_after": 18},
    "heading1":  {"font": "SimHei",   "size": 16, "bold": true, "space_before": 12, "space_after": 6},
    "heading2":  {"font": "SimHei",   "size": 14, "bold": true, "space_before": 8},
    "heading3":  {"font": "SimSun",   "size": 12, "bold": true},
    "body":      {"font": "SimSun",   "size": 12, "line_spacing": 1.5, "indent": 24},
    "caption":   {"font": "SimSun",   "size": 10.5, "align": "center"},
    "signature": {"font": "SimSun",   "size": 12, "align": "right"}
  },
  "table": {"font": "SimSun", "size": 10.5, "header_bold": true, "border": true, "align": "center"}
}
```

要点：`indent` 与 `line_spacing` 单位是**磅**（首行缩进 2 字 ≈ 24 磅；行距 1.5 倍直接写 1.5，固定值写 >10 的数字如 28）。想换风格就改这里，不要在 `blocks` 里逐段调 `font`/`size`。

## 三、数据表怎么写

```json
{"type": "table",
 "header": ["序号", "工作项", "数量", "完成时间"],
 "rows": [["1", "实验室安全检查", "20 批", "11 月"]],
 "widths": [2, 8, 3, 3],
 "header_fill": "DCE6F1",
 "stripe": true}
```

- 表格第一列建议序号或分类键；**必须有表头**，表头默认加粗
- 有合计时写在最后一行，并在表后正文里复述关键数字（读者不看表也能懂）
- `header_fill` 给 6 位十六进制；`stripe=true` 隔行浅色，行多时更好读

## 四、自检清单（交之前逐条过）

1. 五个一级标题齐全，顺序没乱；标题不带标点
2. 每一节都有实质内容，不出现「待补充」「有待加强」
3. 数据表有表头、有合计（如果有合计的话）、数字与正文一致
4. `theme` 已按上面原样传入（回执里的「字体：…」不该是「未指定」）
5. 落款单位与日期都有，且右对齐
6. 全文字数：1500~3000 字（低于 1000 字通常是没写透）
