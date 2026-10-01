"""会话历史本地持久化：按账号保存在 <数据目录>/history/<用户名>.json。

统一入口 HistoryStore 提供：
- load：读取历史；文件缺失返回空列表，损坏/编码/权限问题抛 HistoryReadError 并保留原文件；
- save：在同一账号锁内核对本轮保存标记、检查现有文件可读后原子写入；
- clear：显式清空，直接写入空历史并推进保存计数；
- start_turn：本轮开始（模型调用前）取得保存标记。

消息过滤与公式归一化（含尾部闭合）是本模块的公共规则（filter_valid_messages /
normalize_loaded_messages），文件存储与测试内存假实现共用，保证读出结果一致。
同一数据目录通过 get_history_store 复用同一实例，共用锁与计数。
本方案只保护单个应用进程内部：不防外部程序修改，也不跨进程加锁。
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from pathlib import Path

from core.math_format import normalize_math_text

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

# 用户名只保留安全字符，避免路径穿越
_SAFE_NAME = re.compile(r"[^\w\u4e00-\u9fff-]")


class HistoryReadError(Exception):
    """历史文件存在但无法正确读取（JSON 损坏、编码或权限问题）。"""


def safe_account_name(username: str | None) -> str:
    """把用户名转成安全的文件名/账号键（文件存储与测试假实现共用）。"""
    return _SAFE_NAME.sub("_", username or "default") or "default"


def filter_valid_messages(history: list[dict] | None) -> list[dict]:
    """只保留纯文本的 user/assistant 消息，返回新列表。"""
    clean: list[dict] = []
    for item in history or []:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        clean.append({"role": role, "content": content})
    return clean


def normalize_loaded_messages(raw) -> list[dict]:
    """读取侧公共规则：过滤合法消息并做公式归一化（自动补尾部闭合）。"""
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
                    "content": normalize_math_text(
                        item["content"], auto_close=True
                    ),
                }
            )
    return result


class _AccountGuard:
    """同一账号历史文件共用的保存锁与保存计数。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.save_seq = 0


def _guard_for(
    guards: dict[str, _AccountGuard], lock: threading.Lock, username: str | None
) -> _AccountGuard:
    """取（或建）指定账号的守卫；同一账号共用锁与计数。"""
    key = safe_account_name(username)
    with lock:
        guard = guards.get(key)
        if guard is None:
            guard = _AccountGuard()
            guards[key] = guard
        return guard


class HistoryStore:
    """基于 JSON 文件的历史存储。"""

    def __init__(self, data_dir: str | Path | None = None) -> None:
        self.data_dir = Path(data_dir) if data_dir is not None else DATA_DIR
        self._guards: dict[str, _AccountGuard] = {}
        self._guards_lock = threading.Lock()

    def _guard(self, username: str | None) -> _AccountGuard:
        return _guard_for(self._guards, self._guards_lock, username)

    def _history_path(self, username: str | None) -> Path:
        return self.data_dir / "history" / f"{safe_account_name(username)}.json"

    def start_turn(self, username: str | None) -> int:
        """本轮开始（模型调用前）取得当前保存标记，供本轮 save 检查。"""
        guard = self._guard(username)
        with guard.lock:
            return guard.save_seq

    def load(self, username: str | None) -> list[dict]:
        """读取历史：文件缺失返回空列表；存在但读不了则报错并保留原文件。"""
        path = self._history_path(username)
        try:
            raw_text = path.read_text(encoding="utf-8")
            raw = json.loads(raw_text)
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            raise HistoryReadError(f"历史文件读取失败：{exc}") from exc
        return normalize_loaded_messages(raw)

    def save(
        self, history: list[dict], username: str | None, turn_token: int
    ) -> bool:
        """保存本轮历史：在账号锁内核对标记、检查现有文件后原子写入。

        标记已变化（期间发生过其他保存或清空）时跳过并返回 False；
        现有文件损坏/编码/权限错误时抛 HistoryReadError，不覆盖原文件；
        写入失败抛出原始异常，且不推进保存计数。
        """
        guard = self._guard(username)
        with guard.lock:
            if turn_token != guard.save_seq:
                return False
            path = self._history_path(username)
            self._ensure_existing_readable(path)
            self._atomic_write(path, filter_valid_messages(history))
            guard.save_seq += 1
            return True

    def clear(self, username: str | None) -> None:
        """清空指定账号历史：显式操作，直接写空历史并推进保存计数（阻止清空前的旧轮写回）。"""
        guard = self._guard(username)
        with guard.lock:
            self._atomic_write(self._history_path(username), [])
            guard.save_seq += 1

    def _ensure_existing_readable(self, path: Path) -> None:
        """保存前检查现有历史文件可正确读取；损坏或权限问题则拒绝覆盖。

        不依赖调用方是否先调用过 load()；文件不存在视为可保存。
        """
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            raise HistoryReadError(
                f"现有历史文件无法读取，已阻止保存以避免覆盖：{exc}"
            ) from exc

    def _atomic_write(self, path: Path, messages: list[dict]) -> None:
        """同目录独立临时文件，写完并关闭后 os.replace 替换正式文件。

        失败时尽力清理自己的临时文件并抛出原始错误；不触碰其他文件。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(
                json.dumps(messages, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except BaseException:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise


_store_registry: dict[str, HistoryStore] = {}
_registry_lock = threading.Lock()


def get_history_store(data_dir: str | Path | None = None) -> HistoryStore:
    """按数据目录取共享存储实例：同一历史文件共用锁与保存计数。"""
    directory = Path(data_dir) if data_dir is not None else DATA_DIR
    key = str(directory.resolve())
    with _registry_lock:
        store = _store_registry.get(key)
        if store is None:
            store = HistoryStore(directory)
            _store_registry[key] = store
        return store
