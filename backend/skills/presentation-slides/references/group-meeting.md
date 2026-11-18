# 组会汇报

## 组织方式
先选实际任务：论文精读、复现排错、阶段进展；可混合，章节由用户和模型根据证据决定。

- 论文精读：研究问题与已有缺口 → 核心方法 → 实验证据 → 局限与可借鉴点。交代输入输出、关键假设、公式符号和直觉；区分原论文结果与自己的判断，不逐段搬运论文。
- 复现排错：目标和原报告结果 → 环境/数据/配置 → 当前复现结果 → 差距定位 → 下一步验证。给出版本、随机种子、数据划分、指标口径和运行记录；失败与负结果如实保留。
- 阶段进展：上次目标 → 本次完成与证据 → 新发现与问题 → 讨论决策 → 下次计划及验收。重点解释变化及原因，不堆工作日志。

推荐页面：问题与本次要讨论的决策；方法接口/流程；基线与对比结果；消融或案例；失败分析；需讨论的选择及验证计划。实验页写清样本单位、重复次数、均值/误差定义和公平对比条件。模型图只画已确认关系，公式较长时拆成定义、机制、例子。结尾给出明确可回答的讨论问题与下一步交付物。

## 模板与版式
assets/group-meeting-template.pptx为用户原3页模板：1封面、2正文（画面继承母版，无本页对象）、3结束。正文重复使用第2页并新增元素。封面标题ID5、团队ID6，汇报人位于组合内，先inspect找实际子对象；结束页标题ID5。禁止因正文无对象而重建或删除其母版背景。

新增正文建议[0.92,1.7,11.5,4.95]；双栏[0.92,1.85,5.4,4.5]和[7.02,1.85,5.4,4.5]；图表[0.92,1.85,7.5,4.5]、右结论[8.85,2,3.5,3.8]，来源靠近y=6.5。默认蓝白科研风格仅作用于新增内容。

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

主要action如下；参数文件使用UTF-8 JSON，所有工作区和输出在当前项目内：

普通内容页可在prepare之后先用compose，避免为通用文字/配图逐字生成SVG：
```json
{"action":"compose","workspace":"internal/presentation","source_slide":2,"pages":[{"title":"研究问题","bullets":["研究问题与真实观察"],"image":"figures/result.png","source":"数据来源与定位","notes":"本页讲稿与来源"}]}
```
compose仅选择模板中无页面级对象的内容页，保留原背景、母版与版式；内置组会模板用source_slide=2。每页支持title、bullets、可选PNG/JPEG image/source、必需notes；页数由pages决定。标题32px、正文26px，蓝白科研色；可在style指定font、title_size、body_size、accent、text，不修改背景。长内容超出行数会拒绝，须精简或拆页，不裁切。生成后仍需build。复杂模板已有对象或需原生图表时用下方SVG路径。

正文默认最多缩小2px且不低于24px（18pt）以容纳边缘换行，返回layout记录实际字号/行数；显式body_size默认保持指定值，auto_fit_body=false禁用适配。配图页正文约可容纳9–10行，每页优先2–4条短要点，详细统计与解释放notes，不将整段论文塞进正文。失败按错误中的页号精简，不重复读取实现或反复运行未改参数。

1. 检查模板：`{"action":"inspect","mode":"group","report":"internal/template-index.json"}`。返回源页、文字对象ID和局部英寸坐标；不要将组合子坐标直接当成页面坐标。
2. 准备原生编辑：`{"action":"prepare","mode":"group","workspace":"internal/presentation","report":"internal/import-report.json"}`。用户模板通过template指定项目内路径，优先于内置模板。工作区必须为空；续改时使用已有工作区，不重新导入。查看authoring-svg-flat/authoring_summary.json和conversion_report；不把导入警告直接当作最终输出失败，也不忽略源代理的保留限制。
3. 编辑authoring-svg-flat中的SVG，保留母版/版式来源标记和未改对象的data-pptx-*属性。只改需要改的对象。不可编辑或删除继承自master:/layout:的对象、不可修改source-proxy代理；若背景含固定内容，先确认是否确实属于应替换的内容，再选择安全方案。新内容直接使用SVG原生文字、形状、图片或带原生数据声明的图表/表格。notes/<svg文件名去后缀>.md写逐页来源与讲稿。
4. 校验并导出：`{"action":"build","route":"template","workspace":"internal/presentation","output":"汇报.pptx","expected_pages":实际页数,"report":"internal/build-report.json"}`。仅overwrite=true可覆盖同名输出；禁止覆盖原稿和工作区sources/source.pptx。默认静态输出，确需保留源动画才传preserve_motion=true，并检查对象目标仍有效。

源页的选择、排序或重复放在工作区page_plan.json：
```json
{"schema":"ppt-master.roundtrip-page-plan.v1","pages":[{"source_slide":1},{"source_slide":2,"svg":"result.svg"},{"source_slide":2,"svg":"plan.svg"}]}
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

## 补充来源

吸收skills-main/pptx关于母版承载重复元素、文字安全区、图表轴关系、模板对象完整性和修改后重渲染的规则；读取与检查实现位于本技能scripts，迁移的规则来源及Apache-2.0许可保留于assets。使用本技能内已整合的原生制作实现，不启用pptxgenjs、Node或LibreOffice，也不覆盖用户指定的背景和默认配色。


## 论文类型与截图重建（按需）
发现/机制用问题—证据，方法/算法用瓶颈—方案—公平对比，资源用覆盖—流程—验证，临床用设计—推断，材料用工艺/结构—性能/机制，综述用证据版图—分歧；按主要论证选路线，不按学科机械分类。
用户明确要求图片/截图/图像PDF转可编辑页时，先read看源图并标清不可辨信息，再在现有PPT Master项目SVG中重建文字、形状、表格与连接，照片/不可可靠重建部分保留为图片并说明。只重建已看清对象，不臆造小字、数值或遮挡结构。沿用inspect/prepare/build与guide技术接口；无模板保留要求时才用已支持的generate路线。每页对照源图及实际可用渲染结果；仅贴整张截图不称对象级可编辑。当前不含自动OCR/图像模型重建，像素级还原和完整自动化不保证。


## 技术语法：svg
SVG根明确width、height、viewBox、字体和语言；普通页面从svg_output读取。文字、形状、图片独立可编辑，关系用真实方向与层级。使用可解析XML和内联属性，避免外部CSS、脚本、foreignObject或未声明资源。图片路径指向项目资源，保持比例和来源。
每个逻辑正文单元用稳定id的顶层g，data-pptx-bounds="x y width height"声明页面坐标边界；边界不裁切或自动排版。背景/装饰原语可用data-pptx-role="background"或decoration。模板导入的原生代理和source-ref标记保留；编辑authoring-svg-flat，底稿analysis/native-payloads/sources只读。质量检查与导出均由本地函数完成。

## 技术语法：shapes
rect/circle/ellipse/line及path等直接导出为原生几何；同一语义对象可分组，文字、图片与图标保持独立。导入的预设几何、连接线、图表/表格代理保留原标记和原生XML；不能靠重画近似轮廓冒称保留原对象。复杂布尔轮廓可用真实SVG路径，但不保证保留PowerPoint的布尔操作历史。元数据与可见几何同步，未知的原始对象保留并说明，不能删掉只为通过检查。

## 技术语法：charts
原生图表组使用data-pptx-replace-with="chart"，一个metadata type="application/json"子节点和完整可见SVG预览。metadata给x/y/width/height、name、type、categories、series，例如series=[{"name":"实测","values":[1,2]}]；连续点图按实际实现的x/y数据格式，不把类别轴套到连续变量。图表元数据包括所有真实数值、样式与标题/轴/图例，不能只提供部分数据。
导入对象带data-pptx-native-authority="json"时JSON是权威，预览仅派生；手工SVG默认由可见图形决定，JSON和预览必须同轮同步。随后用create_presentation.py的action=sync_native、path=SVG、write=true记录同步指纹，再build。指纹只证明同步操作，不证明数据语义等价。构建默认启用原生图表/表格，元数据不完整或失配会拒绝导出。

## 技术语法：tables
仅真正的行列事实表使用原生table，说明卡片无需伪装表格。标记data-pptx-replace-with="table"，metadata.schema="ppt-master.semantic-table.v2"，x/y/width/height、可选columns表头和rows正文；列宽/行高使用column_widths/row_heights相对权重。
单元格可为字符串或对象，text/paragraphs、fill、color、align=l/ctr/r、valign=top/middle/bottom、bold、font_size、padding；borders按left/right/top/bottom等边名声明style=none/solid和颜色/宽度。合并锚点用row_span/col_span，覆盖格必须{"merge_continuation":true}。JSON必须包含所有要保留的内容，数值列用右对齐；SVG-first同步规则同charts。

## 技术语法：text
每个逻辑段落用一个text；混排用非定位tspan，换行用重复x和正相对dy的直接tspan，首行保留直接文字或dy=0。字号、颜色、粗细按局部层级保留，不把每一行拆成独立段落。避免用CSS line-height替代可导出的行距，不用缩小到不可读字号隐藏溢出。
模板页选择/重复使用page_plan.json，schema继续为ppt-master.roundtrip-page-plan.v1，pages给source_slide和独立svg路径；notes按输出SVG文件名对应。沿用这些数据协议名称不依赖原上游安装。编辑后对照源图/实际可用预览核字、框、对齐和裁切，结构检查不等同于Office原生视觉验收。
