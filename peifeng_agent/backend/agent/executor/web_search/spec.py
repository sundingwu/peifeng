"""worker 注册信息：装配时据此生成 planner 看到的「可用worker」清单。"""

SPEC = {
    "name": "web_search",
    "description": (
        "查资料。产出可追溯的检索结果：搜索网页、抓取正文、核对来源，把结论与来源 URL 整理成清单或资料文档。不把资料做成正式报告或演示稿（那是 office_docs），也不做数据分析。"
    ),
}
