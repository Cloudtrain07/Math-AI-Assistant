"""会话历史本地持久化：按账号保存在 data/history/<用户名>.json。"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from core.math_format import normalize_math_text

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

# 用户名只保留安全字符，避免路径穿越
_SAFE_NAME = re.compile(r"[^\w\u4e00-\u9fff-]")


def _ensure_dir() -> None:
    (DATA_DIR / "history").mkdir(parents=True, exist_ok=True)


def _history_path(username: str) -> Path:
    safe = _SAFE_NAME.sub("_", username or "default") or "default"
    return DATA_DIR / "history" / f"{safe}.json"


def load_history(username: str = "default") -> list[dict]:
    """读取指定账号的历史会话；文件缺失或损坏时返回空列表。"""
    try:
        raw = json.loads(_history_path(username).read_text(encoding="utf-8"))
    except Exception:
        return []
    result: list[dict] = []
    for item in raw if isinstance(raw, list) else []:
        if (
            isinstance(item, dict)
            and item.get("role") in ("user", "assistant")
            and isinstance(item.get("content"), str)
        ):
            result.append(
                {
                    "role": item["role"],
                    "content": normalize_math_text(item["content"], auto_close=True),
                }
            )
    return result


def save_history(history: list[dict], username: str = "default") -> None:
    """保存指定账号的历史；只保留纯文本消息。"""
    _ensure_dir()
    path = _history_path(username)
    clean: list[dict] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        clean.append({"role": role, "content": content})
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, path)
