"""用户个性化规则：把"输出风格 / 注意事项 / 个人喜好"追加进 system prompt。

**为什么单独存一份**：角色提示词是随版本发布的（改它等于改产品行为）；用户偏好是**本机设置**，
要能在页面上改、重启生效，且不该混进代码库。所以落在 ``<data_dir>/preferences.json``。

**注入位置**：``<通用规则>`` 之后、角色专项规则之前——它属于"规则层"，但不该盖过角色职责；
文本里明确写了"与角色职责冲突时以角色职责为准"，避免用户偏好把安全边界说没了。
"""
from __future__ import annotations

import json
from pathlib import Path

#: 偏好文件（相对 data 根）
PREFERENCES_FILE = "preferences.json"

#: 上限：给用户写偏好，但不该让它把上下文吃光
MAX_APPEND_CHARS = 4000


def preferences_path(data_dir) -> Path:
    return Path(data_dir) / PREFERENCES_FILE


def load_preferences(data_dir) -> dict:
    """读偏好；文件不存在或坏了都返回空（偏好读失败不该拦住启动）。"""
    path = preferences_path(data_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"system_append": ""}
    text = payload.get("system_append") if isinstance(payload, dict) else ""
    return {"system_append": str(text or "")[:MAX_APPEND_CHARS]}


def save_preferences(data_dir, system_append: str) -> dict:
    """保存偏好（超长直接拒绝，不静默截断——用户要知道自己写多了）。"""
    text = str(system_append or "").strip()
    if len(text) > MAX_APPEND_CHARS:
        raise ValueError(f"个性化规则最多 {MAX_APPEND_CHARS} 字，当前 {len(text)} 字")
    path = preferences_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"system_append": text}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"system_append": text}


def preferences_segment(data_dir) -> str:
    """拼进 system 的那一段；没设置就返回空串（不占上下文）。"""
    text = load_preferences(data_dir)["system_append"].strip()
    if not text:
        return ""
    return (
        "<用户偏好>\n"
        "以下是本机用户设置的输出风格与注意事项，在不违反上面的通用规则与你的角色职责的前提下照办；"
        "两者冲突时以角色职责为准。\n"
        f"{text}\n"
        "</用户偏好>"
    )
