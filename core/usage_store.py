"""每日用量的本地持久化：按日期记录各账号累计、全局累计与登录/提问次数。

数据仍保存在单个 JSON 文件（默认 data/usage_daily.json），格式与历史版本一致。
集中规则：
- 文件缺失视为当天无记录；
- 文件损坏/编码/权限问题抛 DailyUsageReadError，明确报错且绝不覆盖原文件；
- 累加语义：读-改-写在同一把进程锁内完成，并发完成的多笔都记入；
- 写入用同目录独立临时文件，写完关闭后 os.replace；失败只清理自己的临时文件。
只保护单个应用进程内部，不提供跨进程方案。
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_DAILY_USAGE_FILE = DATA_DIR / "usage_daily.json"

_TOTAL_KEY = "__total__"
_LOGINS_KEY = "__logins__"
_QUESTIONS_KEY = "__questions__"
_RESERVED_KEYS = (_TOTAL_KEY, _LOGINS_KEY, _QUESTIONS_KEY)

# 所有实例（同一进程内）共用的读-改-写锁
_daily_lock = threading.Lock()


class DailyUsageReadError(Exception):
    """每日用量文件存在但无法正确读取（JSON 损坏、编码或权限问题）。"""


class DailyUsageStore:
    """基于 JSON 文件的每日用量存储。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_DAILY_USAGE_FILE

    # ---- 读取 ----

    def get_used(self, date: str, account: str) -> int:
        """指定日期、账号的已用 token 数；无记录返回 0。"""
        with _daily_lock:
            data = self._load()
        day = data.get(date)
        return int(day.get(account, 0) or 0) if isinstance(day, dict) else 0

    def get_total(self, date: str) -> int:
        """指定日期的全局已用 token；旧数据无 __total__ 时回退为各账号求和。"""
        with _daily_lock:
            data = self._load()
        day = data.get(date)
        if not isinstance(day, dict):
            return 0
        if _TOTAL_KEY in day:
            return int(day[_TOTAL_KEY] or 0)
        return sum(
            int(value or 0)
            for key, value in day.items()
            if key not in _RESERVED_KEYS
        )

    def get_account_usage(self, date: str) -> dict[str, int]:
        """指定日期各账号的用量（排除保留键）。"""
        with _daily_lock:
            data = self._load()
        day = data.get(date)
        if not isinstance(day, dict):
            return {}
        return {
            key: int(value or 0)
            for key, value in day.items()
            if key not in _RESERVED_KEYS
        }

    def get_logins(self, date: str) -> int:
        with _daily_lock:
            data = self._load()
        return self._read_counter(data, date, _LOGINS_KEY)

    def get_questions(self, date: str) -> int:
        with _daily_lock:
            data = self._load()
        return self._read_counter(data, date, _QUESTIONS_KEY)

    # ---- 记录（读-改-写） ----

    def add_usage(self, date: str, account: str, tokens: int) -> int:
        """累加指定日期、账号的 token 用量并同步累加全局总量，返回该账号当日累计。"""

        def mutate(data: dict) -> None:
            day = _ensure_day(data, date)
            day[account] = int(day.get(account, 0) or 0) + int(tokens)
            day[_TOTAL_KEY] = int(day.get(_TOTAL_KEY, 0) or 0) + int(tokens)

        data = self._update(mutate)
        return int(data[date][account])

    def add_login(self, date: str) -> int:
        """记录一次成功登录，返回当日登录次数。"""
        data = self._update(lambda d: _bump_day(d, date, _LOGINS_KEY, 1))
        return int(data[date][_LOGINS_KEY])

    def add_question(self, date: str) -> int:
        """记录一次提问，返回当日提问次数。"""
        data = self._update(lambda d: _bump_day(d, date, _QUESTIONS_KEY, 1))
        return int(data[date][_QUESTIONS_KEY])

    # ---- 内部原语（测试内存假实现只需替换以下三个方法） ----

    def _load(self) -> dict:
        """读取整份数据；文件缺失返回空表，损坏/权限问题抛错。"""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise DailyUsageReadError(f"每日用量文件读取失败：{exc}") from exc
        return raw if isinstance(raw, dict) else {}

    def _update(self, mutate) -> dict:
        """在进程锁内完成读-改-写；读取失败时抛错，不写入。"""
        with _daily_lock:
            data = self._load()
            mutate(data)
            self._atomic_write(data)
            return data

    def _atomic_write(self, data: dict) -> None:
        """同目录独立临时文件，写完并关闭后 os.replace 替换正式文件。

        失败时尽力清理自己的临时文件并抛出原始错误；不触碰其他文件。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(tmp, self.path)
        except BaseException:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    @staticmethod
    def _read_counter(data: dict, date: str, key: str) -> int:
        day = data.get(date)
        return int(day.get(key, 0) or 0) if isinstance(day, dict) else 0


def _ensure_day(data: dict, date: str) -> dict:
    day = data.get(date)
    if not isinstance(day, dict):
        day = {}
        data[date] = day
    return day


def _bump_day(data: dict, date: str, key: str, amount: int) -> None:
    day = _ensure_day(data, date)
    day[key] = int(day.get(key, 0) or 0) + int(amount)
