# -*- coding: utf-8 -*-
"""共享测试设施：与 HistoryStore 接口一致的内存假存储及失败注入变体。

消息过滤与公式归一化复用 core.storage 的公共规则，保证与文件存储读出结果一致。
"""

import copy
import threading

from core.storage import (
    filter_valid_messages,
    normalize_loaded_messages,
    safe_account_name,
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
