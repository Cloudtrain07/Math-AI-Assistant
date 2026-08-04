"""会话历史本地持久化：保存在项目 data/chat_history.json。"""

from __future__ import annotations

import json
from pathlib import Path

from core.math_format import normalize_math_text

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
HISTORY_FILE = DATA_DIR / "chat_history.json"


def _ensure_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_history() -> list[dict]:
    """读取历史会话；文件缺失或损坏时返回空列表。"""
    try:
        raw = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
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


def save_history(history: list[dict]) -> None:
    """保存历史；只保留纯文本消息，绘图/图片消息不持久化。"""
    _ensure_dir()
    clean: list[dict] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        if role == "assistant" and content.startswith("📈"):
            continue  # 绘图占位消息不持久化
        clean.append({"role": role, "content": content})
    HISTORY_FILE.write_text(
        json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8"
    )
