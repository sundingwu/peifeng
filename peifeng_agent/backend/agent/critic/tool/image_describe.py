"""本角色的看图入口，共享实现保持一致。

评审需要真的看一眼图才能核对版面：渲染出来的页面有没有错位、图表标题与标签读不读得出、
文档里的截图内容对不对。`read` 对图片只返回元信息（尺寸/格式），看内容得靠这个工具。
"""
from peifeng_agent.backend.utils.image_tool import image_describe as image_describe
from peifeng_agent.backend.utils.image_tool import information as information
