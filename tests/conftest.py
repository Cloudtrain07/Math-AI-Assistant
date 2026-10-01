# -*- coding: utf-8 -*-
"""共享测试设施：与正式存储/日志接口一致的内存假实现及失败注入变体。

- MemoryHistoryStore：消息过滤与公式归一化复用 core.storage 的公共规则；
- MemoryDailyUsageStore：每日用量的统计规则与文件实现保持一致；
- MemoryEventLogger：日志对象只记录调用信息，不写任何文件。
"""

import copy
import threading

from core.storage import (
    filter_valid_messages,
    normalize_loaded_messages,
    safe_account_name,
)
from core.usage_store import (
    _LOGINS_KEY,
    _QUESTIONS_KEY,
    _RESERVED_KEYS,
    _TOTAL_KEY,
)


class MemoryHistoryStore:
    """内存版历史存储：接口与 HistoryStore 一致，供测试替换文件实现。

    记录 save_attempts / clear_calls，便于断言每轮只尝试保存一次。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seqs: dict[str, int] = {}
        self._messages: dict[str, list[dict]] = {}
        self.save_attempts = 0
        self.clear_calls = 0

    def start_turn(self, username: str | None) -> int:
        with self._lock:
            return self._seqs.get(safe_account_name(username), 0)

    def load(self, username: str | None) -> list[dict]:
        with self._lock:
            stored = copy.deepcopy(self._messages.get(safe_account_name(username), []))
        return normalize_loaded_messages(stored)

    def save(
        self, history: list[dict], username: str | None, turn_token: int
    ) -> bool:
        self.save_attempts += 1
        key = safe_account_name(username)
        with self._lock:
            if turn_token != self._seqs.get(key, 0):
                return False
            self._messages[key] = copy.deepcopy(filter_valid_messages(history))
            self._seqs[key] = self._seqs.get(key, 0) + 1
            return True

    def clear(self, username: str | None) -> None:
        self.clear_calls += 1
        key = safe_account_name(username)
        with self._lock:
            self._messages[key] = []
            self._seqs[key] = self._seqs.get(key, 0) + 1


class FailingSaveStore(MemoryHistoryStore):
    """save 可控失败的假存储：fail 为 True 时抛写盘异常。"""

    def __init__(self):
        super().__init__()
        self.fail = True

    def save(self, history, username, turn_token):
        if self.fail:
            self.save_attempts += 1
            raise OSError("模拟磁盘保存失败")
        return super().save(history, username, turn_token)


class StaleSaveStore(MemoryHistoryStore):
    """save 始终因保存标记过期被跳过的假存储。"""

    def save(self, history, username, turn_token):
        self.save_attempts += 1
        return False


class FailingClearStore(MemoryHistoryStore):
    """clear 始终抛错的假存储。"""

    def clear(self, username):
        self.clear_calls += 1
        raise OSError("模拟清空写盘失败")


class MemoryDailyUsageStore:
    """内存版每日用量存储：接口与 DailyUsageStore 一致，供测试替换文件实现。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}

    def _day(self, date: str) -> dict:
        day = self._data.get(date)
        return day if isinstance(day, dict) else {}

    # ---- 读取 ----

    def get_used(self, date: str, account: str) -> int:
        with self._lock:
            return int(self._day(date).get(account, 0) or 0)

    def get_total(self, date: str) -> int:
        with self._lock:
            day = self._day(date)
        if _TOTAL_KEY in day:
            return int(day[_TOTAL_KEY] or 0)
        return sum(
            int(value or 0)
            for key, value in day.items()
            if key not in _RESERVED_KEYS
        )

    def get_account_usage(self, date: str) -> dict[str, int]:
        with self._lock:
            day = self._day(date)
        return {
            key: int(value or 0)
            for key, value in day.items()
            if key not in _RESERVED_KEYS
        }

    def get_logins(self, date: str) -> int:
        with self._lock:
            return int(self._day(date).get(_LOGINS_KEY, 0) or 0)

    def get_questions(self, date: str) -> int:
        with self._lock:
            return int(self._day(date).get(_QUESTIONS_KEY, 0) or 0)

    # ---- 记录（累加语义，与文件实现一致） ----

    def add_usage(self, date: str, account: str, tokens: int) -> int:
        with self._lock:
            day = self._data.setdefault(date, {})
            day[account] = int(day.get(account, 0) or 0) + int(tokens)
            day[_TOTAL_KEY] = int(day.get(_TOTAL_KEY, 0) or 0) + int(tokens)
            return int(day[account])

    def add_login(self, date: str) -> int:
        with self._lock:
            day = self._data.setdefault(date, {})
            day[_LOGINS_KEY] = int(day.get(_LOGINS_KEY, 0) or 0) + 1
            return int(day[_LOGINS_KEY])

    def add_question(self, date: str) -> int:
        with self._lock:
            day = self._data.setdefault(date, {})
            day[_QUESTIONS_KEY] = int(day.get(_QUESTIONS_KEY, 0) or 0) + 1
            return int(day[_QUESTIONS_KEY])


class CountingDailyUsageStore(MemoryDailyUsageStore):
    """记录 add_usage 调用的假存储，用于断言每日只尝试记录一次。"""

    def __init__(self):
        super().__init__()
        self.add_usage_calls: list[tuple[str, str, int]] = []

    def add_usage(self, date, account, tokens):
        self.add_usage_calls.append((date, account, tokens))
        return super().add_usage(date, account, tokens)


class FailingDailyUsageStore(CountingDailyUsageStore):
    """add_usage 可控失败的假存储：fail 为 True 时抛写盘异常。"""

    def __init__(self):
        super().__init__()
        self.fail = True

    def add_usage(self, date, account, tokens):
        self.add_usage_calls.append((date, account, tokens))
        if self.fail:
            raise OSError("模拟每日用量写盘失败")
        return MemoryDailyUsageStore.add_usage(self, date, account, tokens)


class MemoryEventLogger:
    """内存版事件日志：接口与 FileEventLogger 一致，供测试替换文件实现。

    记录聊天事件、访问事件、异常与空回答的调用信息供断言，不写文件。
    """

    def __init__(self) -> None:
        self.chat_events: list[tuple[str, str]] = []
        self.access_events: list[tuple[str, ...]] = []
        self.errors: list[tuple[Exception, dict]] = []
        self.empty_replies: list[dict] = []

    def log_chat_event(self, kind: str, text: str) -> None:
        self.chat_events.append((kind, text))

    def log_access_event(self, *parts: str) -> None:
        self.access_events.append(parts)

    def log_error(
        self, exc: Exception, *, account: str, turn_total: int | None, partial: bool
    ) -> None:
        self.errors.append(
            (
                exc,
                {"account": account, "turn_total": turn_total, "partial": partial},
            )
        )

    def log_empty_reply(
        self, *, account: str, turn_total: int | None, truncated: bool
    ) -> None:
        self.empty_replies.append(
            {"account": account, "turn_total": turn_total, "truncated": truncated}
        )
