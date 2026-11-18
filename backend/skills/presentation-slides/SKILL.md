---
name: presentation-slides
title: 答辩与组会PPT
description: 用内置或用户模板制作、编辑可继续修改的项目答辩、论文精读、复现与研究进展PPT，支持明确要求的截图对象重建，原生导入、SVG编辑与PPTX导出直接内置在本技能。
---

# 答辩与组会PPT

1. 明确材料、受众、时长与模板。答辩读[项目答辩](references/project-defense.md)，组会读[组会汇报](references/group-meeting.md)。章节和页数由材料与用户决定，不照搬参考成品的内容。
2. 整理逐页问题、结论、来源、图表与讲稿，区分实测、论文报告、推断和计划。按信息关系选版式，保留对齐、留白与视觉层级。
3. 通过run_code调用scripts/create_presentation.py。先inspect；再prepare导入原生编辑工作区，内置模板分别为assets/project-defense-template.pptx和assets/group-meeting-template.pptx，用户模板优先。读取摘要后只编辑相关SVG；保护背景、母版、未改对象、代理标记和原稿。长标题不硬塞窄章节框，不将组合局部坐标当全页坐标。
4. 内置组会模板的第2页是空白内容页，普通汇报优先compose：传pages中的title、bullets、可选image/source和必需notes，自动生成独立页面与page_plan，再build；具体接口见组会参考。复杂布局才用read/write编辑SVG，保留来源标记；不要整份反复读取包含图片编码的SVG。build直接完成质量检查、原生导出和交付检查。自由设计仅在没有保留模板要求时选route=generate。
5. 需要独立复查时使用scripts/check_presentation.py。有渲染能力时逐页对比，修复后重查；结构通过不等于视觉通过。交付PPTX并说明实际保留与重建范围；页脚编号按最终页序1到N，不沿用选中的模板页编号。

只使用现有read/write/move/delete/run_code工具。所有文档改由对应skill脚本执行；不新增PPT专用tool。制作入口是 scripts/create_presentation.py，检查入口是 scripts/check_presentation.py；OOXML、SVG、图表、字体、媒体与导出的实现模块在它们旁边的 presentation 包里。不启用外部账号服务、生图/语音、浏览器或额外agent，不调用Office或TeX。改编自PPT Master的MIT代码，版权、许可和改动记录保留在assets；不依赖backend/vendor/ppt_master。

需要原生图表/表格时，先按guide的charts/tables主题同步完整SVG与JSON；action=sync_native、path=SVG、write=true生成同步指纹，再build。guide只提取所需技术章节，不加载整套上游资料。

格式专属实现和读取解析均位于本技能scripts；模板、校验数据与来源许可位于assets。通用read只作分发，run_code只负责受控执行；不依赖独立文档vendor或CLI转发。
