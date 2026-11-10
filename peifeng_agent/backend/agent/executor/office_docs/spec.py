"""worker 注册信息：装配时据此生成 planner 看到的「可用worker」清单。"""

SPEC = {
    "name": "office_docs",
    "description": (
        "写文档。产出 Word(.docx)、Excel(.xlsx)、PPT(.pptx) 以及 Markdown(.md)、txt、csv 内容——凡是要「写一份报告/小结/说明/方案、做一份表格、做一版演示稿」，都归它。它负责内容组织与版式（字体字号、章节层级、表格样式），并能渲染页面自查。不画统计图（那是 chart）、不整理目录（那是 file_manager）。"
    ),
}
