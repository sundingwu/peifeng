# PDF操作接口

参数为单对象或targets数组，默认action=create。输出file在当前项目内，已有输出需overwrite=true。所有页码从1开始，操作后检查实际页数；默认另存，来源不得在处理中变化。

| action | 参数 |
|---|---|
| create | file、text、可选title；保留中文，正文转义，不编译TeX |
| render | path、可选pages数组、width；PNG与源哈希写入internal/render |
| merge | paths数组、file；按数组顺序合并 |
| select / split | path、pages数组、file；选择指定页面生成一个新文件，分多个文件用targets |
| rotate | path、degrees（90整数倍）、可选pages、file |
| watermark | path、watermark（单页PDF）、可选pages、file |
| extract_images | path、directory（新目录）；提取嵌入图，不等于完整页面渲染 |
| form_info | path；返回字段ID、页码、类型、选项和坐标 |
| fill_form | path、fields（JSON文件）、file；字段元素含field_id、page、value |
| annotate | path、fields、file；非交互表单，使用已核对的坐标，格式见下文 |
| encrypt / decrypt | path、password、file；AES需要可选cryptography/PyCryptodome，缺失时不发布 |

表单填值前先查字段，复选框/单选/下拉必须用真实导出值，不能凭标签猜。非交互表单标注fields JSON格式遵循迁移脚本：包含pages与form_fields及明确的页面尺寸/文本框坐标；先渲染后核对坐标方向与缩放，填写后再次渲染。复杂表格提取按需使用现有read/研究脚本，保留页码、列单位和原文，不把扫描图片当文本。

非交互标注示例（图像坐标以左上为原点，单位为渲染图像素）：
```json
{"pages":[{"page_number":1,"image_width":600,"image_height":800}],"form_fields":[{"page_number":1,"entry_bounding_box":[100,120,300,155],"entry_text":{"text":"Example","font":"Arial","font_size":14,"font_color":"000000"}}]}
```
这里的图像尺寸与坐标只是格式示例，必须替换为实际页面渲染尺寸；中文批注的字体显示须渲染核对，不能仅根据字段字符串判断。

渲染回执记录源文件SHA-256，源变化后旧预览失效。中文字形、上下标和特殊符号必须从实际PNG检查，不能仅凭提取文本判断；ReportLab段落可用sub/super标签，不依赖默认字体不存在的Unicode上下标。

迁移来源为skills-main/pdf的页面操作、表单字段提取/校验/填充与坐标标注，内部保留Apache-2.0许可。渲染改用已有pypdfium2，未启用Poppler、qpdf、pdf-lib或OCR外部程序。不要把Word转PDF、任意原版式复刻或OCR当作此路径的默认能力。


渲染必须初始化表单环境并绘制批注层，否则字段/批注对象存在也可能预览为空。annotate会为新增FreeText补充可显示的外观流，保留原生批注；外观采用可用PDF字体，拉丁文字用Helvetica，非ASCII文字用STSong-Light。不会改写原有批注的外观；文字超出框时拒绝发布，不能把溢出隐藏为成功。
