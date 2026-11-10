"""image_describe 工具：让模型真正看一眼图片并回答针对它的问题。

图片内容（标题有没有乱码、文字有没有溢出、图例对不对得上、截图里是什么界面）
读文本读不出来——本工具把图交给模型的多模态能力，用一句话问出要核对的事。

工具约定：文件头 information 元数据 + 与 name 同名的业务函数；成功 return 文本，失败抛 RuntimeError。
"""

from __future__ import annotations

from pathlib import Path

from peifeng_agent.backend.utils.concurrency import run_parallel_safe
from peifeng_agent.backend.utils.text import IMAGE_SUFFIXES, as_text_list, image_info

#: 看图并发度：与 VISION_CALL_LIMIT 对齐，再多也没有收益（每张都要过一次模型）
IMAGE_WORKERS = 5

information = {
    "name": "image_describe",
    "description": (
        "看一眼图片并回答针对它的问题：返回模型对图像内容的文字描述"
        "（标题是否完整、文字有没有溢出或重叠、图例与数据对不对得上、截图界面里有什么）。"
        "用于核对生成出来的图或渲染出来的页面是否符合预期——图表出完看标题与标签、"
        "文档与演示渲染成图后看版面是否错位、读别人给的截图取信息。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "图片路径（相对 <项目目录> 或绝对路径）"},
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "一次看多张图（与 path 二选一，张数不限）：逐个返回，不要一张图一次调用"
                ),
            },
            "question": {
                "type": "string",
                "description": "针对这张图要回答的问题，如「标题文字是否完整、有无乱码」「正文有没有超出边框」",
            },
            "max_side": {"type": "integer", "description": "送入模型前把长边缩到多少像素（默认 1400）"},
        },
        "required": [],
    },
}


def image_describe(
    path: str = "",
    question: str = "描述这张图的内容，指出任何显示异常的地方",
    max_side: int = 1400,
    paths: list | None = None,
    vision=None,
    base_dir=None,
) -> str:
    """看图并回答；相对路径以 base_dir（项目工作目录）为基准。

    ``paths`` 一次看多张（逐张提问、分段返回）：核对多页版面时一次问完，
    比"一张图一次调用"少很多次往返。
    """
    batch = as_text_list(paths, "paths", 0)
    if batch:
        return describe_many(batch, question, max_side, vision, base_dir)
    if not str(path or "").strip():
        raise RuntimeError("image_describe 需要 path（单张）或 paths（多张）之一")
    return describe_one(path, question, max_side, vision, base_dir)


def describe_many(paths: list[str], question: str, max_side: int, vision, base_dir) -> str:
    """并发描述多张；单张失败只记在它自己那一段，结果按输入顺序拼。

    每张图都要过一次模型（几秒），串行看 5 张就是 5 倍等待——这里是并发的收益最大处。
    """
    results = run_parallel_safe(
        lambda item: describe_one(item, question, max_side, vision, base_dir),
        paths,
        workers=IMAGE_WORKERS,
        thread_prefix="vision",
    )
    blocks: list[str] = []
    for item, (body, error) in zip(paths, results):
        blocks.append(f"===== {item} =====\n{body if error is None else f'识别失败：{error}'}")
    return "\n\n".join(blocks)


def describe_one(path: str, question: str, max_side: int, vision, base_dir) -> str:
    """看一张图并回答。"""
    if vision is None:
        raise RuntimeError("当前角色没有接入看图能力")
    target = resolve(path, base_dir)
    if not target.is_file():
        raise RuntimeError(f"图片不存在：{path}")
    if target.suffix.lower() not in IMAGE_SUFFIXES:
        raise RuntimeError(f"只支持图片：{'、'.join(IMAGE_SUFFIXES)}（当前：{target.suffix or '无后缀'}）")
    side = max(400, min(int(max_side or 1400), 2400))
    answer = vision(target, str(question or "").strip() or "描述这张图的内容", side)
    return "\n".join([f"图片：{image_info(target)}", f"问题：{question}", f"回答：{answer.strip()}"])


def resolve(path: str, base_dir) -> Path:
    """相对路径以 base_dir（项目工作文件夹）为基准；绝对路径原样使用。"""
    target = Path(path)
    if target.is_absolute() or base_dir is None:
        return target
    return Path(base_dir) / target
