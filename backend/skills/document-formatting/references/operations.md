# 文档操作细节

## 默认执行路径
普通Word通知或报告用create_word.py（action=create）结构化制作；多段科研报告优先写Markdown，表格用Markdown表格，再用create_word.py（action=markdown）导出Word；不要为普通报告编写多文件Word生成程序。原引擎只导出Word，不启用Office PDF。PDF另用pdf-documents的process_pdf.py（action=create）。
Markdown标题使用#和##，不要发明`%% 标题`样式。图片使用`![](实际相对路径.png)`，相对路径从Markdown所在目录解析：internal/report.md引用根目录图件需写../result.png。图片题注单独一段写`%% 图片题注 图 1 ...`；方括号仅用于无障碍替代文字，不能承担题注。导出前检查所有图片路径确实存在；原引擎严格检查样式与题注。
校园通知按需读取references/guide.md，保持原材料时间、地点、联系人，不凭空添加文号和印章。仅结构检查，不调用Office原生渲染或TeX。

## 绑定精确事实
工程交接包含peifeng.evidence.v1事实JSON时，在create_word.py（action=markdown）设置facts为该JSON路径。脚本调用本技能scripts/bind_facts.py，校验来源并用固定配方复算，自动附加“配对结果”段；不修改原始Markdown。无需手写占位符；确需自选位置时`{{fact:comparison_1}}`等ID必须独立成段，不得嵌入表格或其他句子。不要在摘要或后文重写同一数量、差值符号或缺配侧。直接Markdown交付可用write(facts=...)做同样绑定，Word只在markdown导出时绑定一次。解释部分仍需按证据审查；绑定不证明科学结论。

## 结构核验与版本

先read(view=content)核对正文表格，再按需要用view=structure检查页面设置、正文宽度、图片与页眉；只需尺寸时用view=metadata。已有回执充分时不另写结构检查程序或重复导出全文。Markdown或生成源码改动后必须重新导出，再读取当前产物。

python-docx尺寸运算的结果为整数EMU，不带.cm；直接用整数比较，确需厘米时用docx.shared.Emu(差值).cm。不能让检查代码错误冒充通过。

source_sha256指向输入Markdown版本，evidence_sha256及facts_used指向复算绑定的事实。自动附加事实段无需手抄回Markdown，不把源文件未手写该段当成版本错误；也不在write与Word导出各展开一次。沿用已验收图件，保留可重建文档的源码与依赖。

结构检查不证明Word原生分页或视觉效果；不能用Python文本重绘PDF冒充Office渲染，不为模拟已排除的Office能力另建排版引擎。LaTeX仅交源码。

## 格式要求与参考稿迁移
先读用户学校/期刊/参考稿，把要求、来源位置、适用范围、显式/推断/默认与当前引擎支持分别记录；视觉示例不能自动推成强制规格。参考稿用于版式规律，不抄正文或引用。精确项有冲突时列出处，默认不冒充官方规则。
Markdown导出用preset或format_path指定已有Python引擎支持的规则，接口以create_word.py为准。字体/段落/标题层级/表格/题注/页眉页码分别核对；引擎不能支持的项目单列未完成，不声称已应用。复杂字段与公式只能在当前支持范围使用原生OMML，不将图片冒称可编辑公式。
引用风格按用户目标统一APA7、Chicago、MLA、IEEE或Vancouver；中英文姓名、多人作者、省略规则、同年a/b、直接引语页码、DOI/URL及数字引用顺序依实际规则和原始记录，不凭文献名补卷页。参考文献表与正文引用一一对应，格式变化不改变作品身份。
LaTeX只交源码；原有LNCS模板保留在assets/latex，未增加编译能力或安装依赖。
