# 项目答辩

## 组织方式
章节和顺序由用户与材料决定，默认路线：项目问题与价值 → 方案与关键实现 → 已完成工作与结果 → 问题、改进和后续计划。可增删合并，不固定四章、页数或模板中的章节名。

先整理事实清单：问题、目标、用户/场景、方法、贡献、完成状态、实验条件、原始数值及单位、来源位置、限制与计划。根据时长分配页面，封面/结束页计入总页数；目录仅在内容较长时使用。

推荐页面组织：
- 开场：项目名称、人员、日期；用一个具体场景解释研究问题。
- 价值：现有方式、缺口、目标与验收指标；避免只列口号。
- 方案：输入输出、总体结构、模块职责；关键方法用流程、接口或对比讲清。
- 证据：完成清单配实物/代码/实验来源；结果按条件、基线、指标、误差展示，不把界面截图当效果证明。
- 问题闭环：现象 → 原因证据 → 改进 → 复核；尚未解决要标明。
- 计划：下一步任务、依赖、交付物和验收条件；结束页简明总结贡献。
每页提供问题、结论、证据、来源和讲稿。多方法、多结果或多问题时拆页，不缩小字体硬塞。

## 模板与版式
assets/project-defense-template.pptx为用户原模板。13.333×7.5英寸，共11个源页：1封面、2目录、3/5/7/9章节、4/6/8/10正文、11结束。源页可重复或跳过；四个章节样页不限制实际章节数。先inspect确认对象，封面标题ID12、信息ID6，章节标题首张ID6、其余ID21；不要跨页套用ID。

新增正文建议[1.3,1.8,10.6,4.85]；双栏分别[1.3,1.9,5,4.5]和[6.9,1.9,5,4.5]；图表[1.3,1.9,7,4.4]、右侧结论[8.8,2.1,3.1,3.6]，来源靠近y=6.4。默认蓝白科研风格仅用于新增内容，不重绘背景。

## 页面表达与视觉层级

先按信息关系选版式，再写上屏文案，不将每页都排成项目符号或相同卡片。章节页负责转场，正文负责解释；短汇报可省目录和部分转场。标题区保留章节标识时，正文上沿另放一句本页结论，避免把短章节名当成唯一信息。

| 要表达的关系 | 页面组织 | 约束 |
|---|---|---|
| 问题与依据、需求与目标 | 左右对应或逐行配对 | 对应项同一水平线，证据靠近所支撑的主张，数量由材料决定 |
| 系统、方法或机制 | 一张主图＋少量局部注释 | 画出输入、处理、输出和已确认关系，颜色区分职责，连线不穿文字 |
| 功能或角色分工 | 分层结构或职责矩阵 | 同层同尺度，说明边界和交接，不把所有关系画成顺序流程 |
| 贡献与价值、风险与措施 | 窄标签列＋宽解释列 | 每行一个机制与其作用，保留限制，长解释拆页 |
| 实验和统计结果 | 图表＋关键读数＋条件 | 保留单位、基线、样本和误差定义，解释差异，不只展示数字 |
| 已完成成果 | 少量大数字＋指标名称＋口径 | 大数字须有真实来源和统计范围，失败/跳过与成功不能混算 |
| 实物演示 | 主截图＋输入与输出注释 | 优先截取可读区域，标注演示范围，不能用界面证明效果或正确率 |
| 后续计划 | 并列任务或时间线 | 每项含动作、可交付结果和验收，顺序/依赖确实时才画箭头 |

正文区沿同一网格对齐。并列块等宽等距；行内标签和说明共享基线；先保留空白与边距，再确定字号。使用默认模板时沿用现有标题位置与蓝白风格；用户模板以其实际内容安全区为准。禁止覆盖原背景装饰来扩大正文区。

文字分层建议：页内结论24—28pt，小标题20—24pt，正文18—22pt，来源14—16pt；大数字40—60pt且配较小的指标/口径说明。这是起点而非强制尺度：不照抄示例中所有小字号，按投影距离、画布和材料调整；先删冗词或拆页，再缩字。每个区块保留短标题和必要解释，细节进入讲稿。关键词可用加粗或单一强调色，避免整段高亮。

采用深蓝主色、深灰正文、浅蓝底板，类别较多时才增加少量辅助色，同一含义全篇同色。底板只用于形成明确分组，不给每句话套框；避免为了装饰添加无意义连线、渐变与图标。复杂架构不受process的4节点限制误导：使用已验证的绘图产物作为image，或通过run_code制作原生组合并检查，不把多分支架构挤成4个步骤。

参考成品只迁移表达规律，不复制其章节数量、对象ID、项目名、人员、数字、截图或具体技术结论。优先做到事实可靠、可读、对应关系清楚；模板相同不代表内容应相同。

## 静态交付与检查重点

inspect返回has_animation时，检查参考是否用动画分步显示重叠对象。组合子对象的box是局部坐标，不能直接当页面坐标或据此判定重叠；须查看父组变换或实际渲染。适配脚本默认输出静态页面，不启用源动画，所有必要内容必须在静态页面内完整且互不遮挡；需要分步展示时拆页，不依赖动画隐藏冲突。

逐页核对：结论是否容易找到、标签和证据能否对应、图的主要内容是否可读、真实来源和统计口径是否齐全、文字是否遮住图片或其他文字。整稿核对：目录/章节/正文是否对应、颜色和术语是否一致、密集页与简洁页是否平衡、结束是否回到研究问题。结构检查通过不替代这些判断，也不等于已达到参考成品的视觉质量。

## 制作接口：本技能原生实现

通过run_code执行：
`file_path=skill://presentation-slides/scripts/create_presentation.py`，`args=["--input", "internal/ppt.json", "--base-dir", "."]`

同一入口分四种action；参数文件使用UTF-8 JSON，所有工作区和输出在当前项目内：

1. 检查模板：`{"action":"inspect","mode":"defense","report":"internal/template-index.json"}`。返回源页、文字对象ID和局部英寸坐标；不要将组合子坐标直接当成页面坐标。
2. 准备原生编辑：`{"action":"prepare","mode":"defense","workspace":"internal/presentation","report":"internal/import-report.json"}`。用户模板通过template指定项目内路径，优先于内置模板。工作区必须为空；续改时使用已有工作区，不重新导入。查看authoring-svg-flat/authoring_summary.json和conversion_report；不把导入警告直接当作最终输出失败，也不忽略源代理的保留限制。
3. 编辑authoring-svg-flat中的SVG，保留母版/版式来源标记和未改对象的data-pptx-*属性。只改需要改的对象。不可编辑或删除继承自master:/layout:的对象、不可修改source-proxy代理；若背景含固定内容，先确认是否确实属于应替换的内容，再选择安全方案。新内容直接使用SVG原生文字、形状、图片或带原生数据声明的图表/表格。notes/<svg文件名去后缀>.md写逐页来源与讲稿。
4. 校验并导出：`{"action":"build","route":"template","workspace":"internal/presentation","output":"汇报.pptx","expected_pages":实际页数,"report":"internal/build-report.json"}`。仅overwrite=true可覆盖同名输出；禁止覆盖原稿和工作区sources/source.pptx。默认静态输出，确需保留源动画才传preserve_motion=true，并检查对象目标仍有效。

源页的选择、排序或重复放在工作区page_plan.json：
```json
{"schema":"ppt-master.roundtrip-page-plan.v1","pages":[{"source_slide":1},{"source_slide":4,"svg":"result.svg"},{"source_slide":4,"svg":"plan.svg"}]}
```
result.svg与plan.svg分别复制自对应源页的SVG再编辑，必须各自独立；source_slide从1开始。它是完整输出顺序，不自动补页。仅更改正文而输出与源页顺序相同时，可直接编辑原SVG，不写page_plan.json。工作区analysis、native-payloads和sources是引擎维护的不可修改底稿。

没有模板或用户明确要求全新设计时，建立workspace/svg_output/*.svg与notes/*.md，使用route=generate；不把要求保留背景的任务改走自由设计。两种路线都由适配脚本先运行上游质量检查，再导出并检查实际PPTX，检查失败不覆盖已有文件。

## 坐标、文字和原生对象

导入SVG以根viewBox为准；不能把旧工具的英寸box参数直接粘进SVG。内置16:9模板通常为1280×720，96px约1英寸，24pt约32px。页内标题/正文按前文规则换算，字体优先Microsoft YaHei或模板原字体。不要把28px误当28pt。旧JSON slides/elements/slot接口已由SVG工作区替代。

短章节名放模板原有标题框，长结论另放正文上沿；原框宽度不足时不强塞文字。检查器报owning frame溢出时，精简文字、换行、拆页或明确调整所属data-pptx-frame及其几何，不只给text换个更大的坐标。使用明确x/y定位的多行文字，中文标点不孤立在行首，数字与单位不拆开。

新增底板用rect，文字用text/tspan，分组用g，图片用image并嵌入项目资源。不要把普通正文变为路径；图片和复杂特效不能冒称内部可编辑。XML使用原生Unicode，&和<等保留字符必须转义；不要使用HTML实体、foreignObject、外链CSS、任意浏览器特效。背景和证据图不可用装饰底板遮盖。

图表/表格需要可编辑的数据对象时，按原生数据接口写入声明与真实数据，不用SVG形状假装原生图表。表格先确定行列、单位、列宽、对齐和完整数据，再做装饰；只有行列交叉定义事实时才用表格，不把指标卡或两栏说明强行排成表格。图表检查值、轴范围、比例、单位和误差，定性关系图不伪装成定量图。

需要技术细节时，用同一脚本按需导出规则到项目再read：
`{"action":"guide","topic":"svg"}`，topic支持svg（语法）、shapes（原生形状）、charts（图表/表格数据）、tables（表格设计）、text（文字与往返技术）。这些是本技能内的中文语法说明，按主题提取自组会参考的技术章节。实际实现直接位于scripts/presentation，不启动其他引擎或上游工作流。

## 交付验收

先执行适配脚本的质量门，修改源SVG或数据再重试，不能通过跳过检查交付。报告中的导入损失、字体可移植性和可选动画提示逐项判断：原生保留的对象与重建对象不同，不承诺任意对象或跨软件像素一致。

独立检查命令：
`file_path=skill://presentation-slides/scripts/check_presentation.py`，`args=["--input", "internal/check.json", "--base-dir", "."]`
输入：`{"path":"汇报.pptx","expected_pages":实际页数}`。检查包括包关系、原生对象、媒体、字体和动画；不会调用Office/TeX，也不会自动发起网络服务。

有可用渲染器时，查看整稿缩略图和逐页大图，与模板对照背景、版式和文字；修改后重渲染受影响页，确认未引入新的遮挡、溢出、乱码、重复页码或标签错位。继承的页码域由演示软件计算，不额外叠加第二份页码。无渲染器时明确只完成结构验收。测试渲染器不进入产品依赖，评估水印不属于PPT原稿。

## 来源与许可

融合build-project-defense-ppt的证据闭环、build-research-group-ppt的精读/复现/进展组织，以及培风原生制作与校验。借鉴[PPT Master](https://github.com/hugohe3/ppt-master)的原生保留、数据决定图形、页面与讲稿一致及实际复查原则，所需PPT Master v6.9.0算法已融合到本技能scripts/presentation，直接调用函数，许可和来源记录位于assets。


以下保留来源MIT许可原文，技术与法律文本不翻译改写。

MIT License

Copyright (c) 2025-2026 Hugo He

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## 补充来源

吸收skills-main/pptx关于母版承载重复元素、文字安全区、图表轴关系、模板对象完整性和修改后重渲染的规则；读取与检查实现位于本技能scripts，迁移的规则来源及Apache-2.0许可保留于assets。使用本技能内已整合的原生制作实现，不启用pptxgenjs、Node或LibreOffice，也不覆盖用户指定的背景和默认配色。
