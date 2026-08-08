"""习题库：按账号保存的题目收藏，支持增删改查、查重与筛选。

数据保存在 data/exercise_bank/<用户名>.json（已 gitignore），
每条记录字段：题目（LaTeX）、我的卡点、正确解、错误类型、标签、创建时间、最后复习时间。
"""

from __future__ import annotations

import datetime
import json
import os
import re
import uuid
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "exercise_bank"

# 错误类型选项（首个为空表示不选）
ERROR_TYPES = ["", "概念不清", "计算失误", "方法不会", "审题不清"]

_SAFE_NAME = re.compile(r"[^\w\u4e00-\u9fff-]")


def _path(username: str) -> Path:
    safe = _SAFE_NAME.sub("_", username or "default") or "default"
    return DATA_DIR / f"{safe}.json"


def _sanitize_entry(item: dict) -> dict:
    return {
        "id": str(item.get("id") or ""),
        "title": str(item.get("title") or "").strip(),
        "question": str(item.get("question") or "").strip(),
        "mistake": str(item.get("mistake") or "").strip(),
        "solution": str(item.get("solution") or "").strip(),
        "error_type": str(item.get("error_type") or "").strip(),
        "tags": [
            str(tag).strip()
            for tag in (item.get("tags") or [])
            if str(tag).strip()
        ],
        "created_at": str(item.get("created_at") or ""),
        "last_reviewed": str(item.get("last_reviewed") or ""),
    }


def _read(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    result: list[dict] = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, dict):
            result.append(_sanitize_entry(item))
    return result


def _write(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, path)


def load_entries(username: str = "default") -> list[dict]:
    """读取指定账号的习题库；文件缺失或损坏时返回空列表。"""
    return _read(_path(username))


def save_entries(entries: list[dict], username: str = "default") -> None:
    """整表覆盖保存（供测试与批量操作）。"""
    _write(_path(username), [_sanitize_entry(e) for e in entries])


def get_entry(entry_id: str, username: str = "default") -> dict | None:
    for entry in load_entries(username):
        if entry["id"] == entry_id:
            return entry
    return None


def add_entry(fields: dict, username: str = "default") -> dict:
    """新增一条；自动补 id 与创建时间，返回完整记录。"""
    entries = load_entries(username)
    entry = _sanitize_entry(fields)
    entry["id"] = entry["id"] or uuid.uuid4().hex
    entry["created_at"] = entry["created_at"] or datetime.datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    entries.append(entry)
    _write(_path(username), entries)
    return entry


def update_entry(
    entry_id: str, fields: dict, username: str = "default"
) -> dict | None:
    """按 id 更新字段；成功返回更新后的记录，找不到返回 None。"""
    entries = load_entries(username)
    for index, entry in enumerate(entries):
        if entry["id"] == entry_id:
            updated = _sanitize_entry({**entry, **fields})
            entries[index] = updated
            _write(_path(username), entries)
            return updated
    return None


def delete_entry(entry_id: str, username: str = "default") -> bool:
    """按 id 删除；删除成功返回 True。"""
    entries = load_entries(username)
    before = len(entries)
    entries = [e for e in entries if e["id"] != entry_id]
    if len(entries) != before:
        _write(_path(username), entries)
        return True
    return False


def normalize_question(text: str) -> str:
    """规范化题目文本用于查重：去空白、统一空格。"""
    return re.sub(r"\s+", " ", (text or "").strip())


def find_duplicate(
    question: str,
    entries: list[dict],
    exclude_id: str | None = None,
) -> dict | None:
    """查找规范化后完全相同的题目；返回已有条目或 None。"""
    target = normalize_question(question)
    if not target:
        return None
    for entry in entries:
        if entry["id"] == exclude_id:
            continue
        if normalize_question(entry["question"]) == target:
            return entry
    return None


def filter_entries(
    entries: list[dict], tag: str | None = None, keyword: str | None = None
) -> list[dict]:
    """按标签与关键词（匹配简短描述/题目/标签）过滤；无过滤条件时原样返回。"""
    result = list(entries)
    if tag:
        result = [e for e in result if tag in (e.get("tags") or [])]
    keyword = (keyword or "").strip()
    if keyword:
        result = [
            e
            for e in result
            if keyword in e.get("title", "")
            or keyword in e.get("question", "")
            or any(keyword in t for t in (e.get("tags") or []))
        ]
    return result


def entry_label(entry: dict) -> str:
    """生成下拉条目标签：索引描述 + 收录日期（月-日）。"""
    title = (entry.get("title") or "").strip()
    label = title[:20] if title else "（无索引）"
    return f"{label}（{entry.get('created_at', '')[5:10]}）"
