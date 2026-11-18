# Word制作、编辑与检查

## 制作
普通文档参数示例：
```json
{"action":"create","file":"通知.docx","style":{"page":{"top":2.54,"bottom":2.54,"left":3,"right":3},"styles":{"title":{"font":"宋体","size":18,"bold":true,"align":"center"},"body":{"font":"宋体","size":12,"line_spacing":1.5},"heading1":{"font":"黑体","size":12},"signature":{"font":"宋体","size":12,"align":"right"}}},"blocks":[{"type":"title","text":"通知标题"},{"type":"paragraph","text":"具体事项"},{"type":"paragraph","style":"signature","text":"落款与日期"}]}
```
blocks类型为title、heading（level=1/2/3）、paragraph、table、image、page_break；不要把heading1当type。样式名放style字段，样式值由用户/学校要求决定，A4和中文字体不能由英文默认值覆盖。表格header与rows分开，保持列数、单位和宽度一致；图片path为项目内文件，width_cm指定宽度。

追加用append=true；重建用overwrite=true；定点改段用patch=[{index,text}]或[{match,text}]，index从1开始、match须唯一，不能与append/overwrite混用。inspect使用{"action":"inspect","file":"通知.docx"}。批处理使用targets，独立项各自成功/失败，同文件操作依次执行；检查每项回执而非只看最后一行。

## 原生XML编辑

以下动作均由create_word.py统一入口调用；技术实现edit_word_xml.py按需执行。
- comment：path原稿、file输出、paragraph段号、text批注、author作者；正文锚点自动添加。回复传parent批注ID，不重复添加正文锚点。
- merge_runs：path原稿、file输出；合并相邻同格式run，便于可靠定位文字，不改变可见内容。
- unpack：path原稿、directory新目录。使用迁移的安全解包，阻止路径逃逸和链接；不要普通extractall处理外部文档。
- pack：path为解包目录、file为输出.docx；重打包后运行XML校验，原稿可留作对照。
- validate：path待检查文件，可选original原稿；author用于检查是否存在未留修订记录的改动。不自动修改文件，不是视觉验收。
- accept_changes：path修订稿、file清洁稿。纯Python接受插入、删除、移动与常见属性变更，删除段落标记时合并相邻段落；跨表格等无法安全合并的情况拒绝发布，不悄悄删内容。

修订编辑时用w:ins/w:del和真实author/date/id记录变化，删除文字用w:delText；只换正文不会自动成为“带修订稿”。可通过unpack定点修改，再pack和validate(original,author)。不要直接给整个段落.text赋值来修改一个词，否则会清掉run格式。为复杂改动保留原稿和清洁稿两份。

## 格式核对
保持真实标题层级、目录依据、页码域、编号、题注和链接；不要手打空格对齐或用字面点号冒充目录引导线。列表用编号定义，表格先定逻辑网格再装饰，宽度与页面可用宽度对应；长表检查续页与段落关联，不能靠缩字号隐藏内容。模板优先，生成后重新读取段落、表格、图片数及来源；无Office渲染能力时不能声称原生分页正确。

来源：skills-main/docx的创建/编辑、修订、批注和XML校验方法，Apache-2.0许可保存在本技能assets，所需实现直接位于scripts。未启用其docx-js、Pandoc和LibreOffice路径。
