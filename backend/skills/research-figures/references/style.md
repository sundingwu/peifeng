# 风格由技能定义，通过style传给绘图脚本

不传style使用蓝白科研默认值；只传部分字段时，其余保留默认值。脚本不按任务自动选择主题。
下面只是可改造的样式示例，不代表任何期刊的合规要求：



```json
{
  "font": "Microsoft YaHei",
  "font_latin": "Arial",
  "font_size": 10,
  "title_size": 12,
  "tick_size": 9,
  "legend_size": 9,
  "value_size": 9,
  "caption_size": 8,
  "palette": ["#222222", "#777777", "#BBBBBB"],
  "text": "#222222",
  "background": "white",
  "grid_color": "#DDDDDD",
  "grid_linewidth": 0.5,
  "grid_linestyle": ":",
  "spine": "#555555",
  "axes_linewidth": 0.8,
  "error_color": "#222222",
  "error_linewidth": 1.2,
  "error_capsize": 3,
  "show_grid": true,
  "legend": "auto",
  "title_position": "top",
  "dpi": 300
}
```



将选定对象原样放入绘图目标的style参数。values/errors/counts/caption是内容参数，不是风格。
未单独指定value_size时继承tick_size；caption_size默认font_size的0.8倍；误差棒颜色默认继承text。
SVG导出保留文本节点，使用同一style，不另选配色。

diagram也通过style控制：font/font_latin、node_font_size（显式font_size亦可）、node_fill、node_border、node_text、edge_color、background、node_radius、node_border_width、edge_width。
例如黑白示意图可使用`{"node_fill":"white","node_border":"#222222","node_text":"#222222","edge_color":"#222222","node_radius":0,"node_font_size":16}`。
diagram的颜色和字体不硬编码为单一风格；默认仍为蓝白。字号改变时重新换行并调整节点高度，关系与数据不变。
