---
name: document-formatting
title: Word排版与批注
description: 创建、编辑和检查Word文档，将Markdown和可核验事实导出Word，处理批注、修订、模板和公文格式；LaTeX只交付源码。
---

# Word排版与批注

1. 先read材料或现有Word的结构，确认保留内容和目标格式。普通通知/报告用scripts/create_word.py的create；多段科研报告先写Markdown，再用markdown导出。接口见[制作与格式](references/guide.md)。
2. 图表沿用已验证数据与单位；收到peifeng.evidence.v1时在markdown动作传facts，脚本复算后绑定一次，不再手写重复的数量/符号。细节见[事实与科研排版](references/operations.md)。
3. 修改现有文件时优先patch；需要批注、修订或保留复杂结构时用comment、merge_runs、unpack/pack、validate或accept_changes，见[制作与格式](references/guide.md)。源文件优先另存，明确overwrite=true才覆盖；不靠覆盖重建来冒充修订。
4. 简单用例**直接给字段**，不用先写 JSON：
   `file_path=skill://document-formatting/scripts/create_word.py`，`args=["--base-dir", ".", "--action", "markdown", "--path", "internal/report.md", "--file", "deliverables/report.docx"]`
   复杂结构或批量才写 `--input internal/word.json`（对象或 targets 数组）。输出 JSON 含实际 files；脚本先生成并检查候选文件，再检测来源/目标冲突并发布，覆盖前备份到 internal。
5. 用read核对段落、表格、图片、页眉和来源，XML修改再validate。当前纯Python路径不做Office原生分页或渲染，不宣称视觉验收完成。不使用Node、LibreOffice或TeX编译，也不再调用document工具。

上游docx的批注、文本run合并和OOXML校验已适配；制作通过python-docx与本技能scripts/word_markdown内置排版实现；格式预设、CSL和基础模板位于assets/markdown。PDF交pdf-documents，Excel交spreadsheets，PPT交presentation-slides，避免重复实现。

格式专属实现和读取解析均位于本技能scripts；模板、校验数据与来源许可位于assets。通用read只作分发，run_code只负责受控执行；不依赖独立文档vendor或CLI转发。
