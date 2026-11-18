---
name: research-figures
title: 科研绘图
description: 基于本地 nature-figure 的真实数据绘图、可编辑结构图、多面板对齐和导出质量检查；仅使用 Python。
---

# 科研绘图

1. 先确定科学问题、原始数据、单位、样本数和误差定义。只绘制提供或计算得到的真实值；不根据示例图捏造数据。
2. 简单图**直接给字段**，不用先写 JSON 文件：
   `file_path=skill://research-figures/scripts/draw_figure.py`，`args=["--base-dir", ".", "--kind", "line", "--output", "trend.png", "--title", "月度销售额", "--xlabel", "月份", "--ylabel", "千元", "--categories", "[\"1月\",\"2月\"]", "--series", "[{\"name\":\"华东\",\"values\":[820,900]}]"]`
3. 批量或多目标才写 `--input figure.json`（`{"targets":[{...}]}` 或单个对象）；每项独立报错，失败不能宣称完成。
4. kind=bar/line/pie/scatter，output 为 PNG；categories 与 series=[{name,values}]，柱状图可带 errors、counts、caption、svg_output。配色、字体、尺寸由 style 控制，默认蓝白科研风格，详见 references/style.md。
5. CSV 专业图用 kind=volcano/roc/dotplot/marginal/paired，path 为项目内 CSV，output 为无扩展名输出前缀；列名与参数见 references/nature-workflow.md。
6. 结构图用 kind=diagram，output 为 SVG，nodes=[{id,label}]、edges=[{from,to}]、direction=LR/TB。不能为美观改动科学关系。复杂多面板图可用 write 保存项目 Python 脚本，用 Matplotlib 和本技能 figure_qa.save_bundle 导出。
7. 统计图输出 PNG、可编辑 SVG、PDF、对齐与碰撞审计 JSON；CSV 模板另输出 TIFF。审计失败应修复，不能只删检查。用 read 查看实际图像，人工检查语义、标签、线条与字体；自动审计不等于审稿或视觉验收。
8. 不使用 R、外部生成 API、账号、TeX 或原项目的路径绑定运行器。源码迁移记录在 backend/vendor/nature_figure/UPSTREAM.json，适配边界见 references/nature-workflow.md。

## 按需资源

- references/style.md：基础图与结构图的 style 字段。
- references/nature-workflow.md：CSV 模板、复合图、质量门禁和迁移范围。
- references/methods.md：图件组织与内容方法。
- scripts/aggregate.py：按列分组汇总；不得用汇总值代替原始样本推断。
