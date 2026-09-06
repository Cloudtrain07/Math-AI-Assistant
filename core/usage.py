"""会话内 token 用量累计与每日限额：纯函数，便于界面调用与单元测试。"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DAILY_USAGE_FILE = DATA_DIR / "usage_daily.json"

_daily_lock = threading.Lock()

_TOTAL_KEY = "__total__"
_LOGINS_KEY = "__logins__"
_QUESTIONS_KEY = "__questions__"
_RESERVED_KEYS = (_TOTAL_KEY, _LOGINS_KEY, _QUESTIONS_KEY)


def new_usage_state() -> dict:
    """新建会话累计状态：累计 token 数（含输入/输出细分）与完成轮数。"""
    return {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0, "turns": 0}


def record_usage(
    state: dict,
    turn_total: int | None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> dict:
    """记录一轮回复的用量，返回新状态（不修改入参）。

    turn_total 为 None 时（如请求失败/无用量信息）只累计轮数，不累计 token；
    prompt/completion 缺失时（如按字符估算的兜底路径）只累计总数。
    """
    result = dict(state)
    result["turns"] = result.get("turns", 0) + 1
    if turn_total is not None:
        result["total_tokens"] = result.get("total_tokens", 0) + int(turn_total)
    if prompt_tokens is not None:
        result["prompt_tokens"] = result.get("prompt_tokens", 0) + int(prompt_tokens)
    if completion_tokens is not None:
        result["completion_tokens"] = result.get("completion_tokens", 0) + int(
            completion_tokens
        )
    return result


def format_cumulative(state: dict) -> str:
    """生成状态栏提示：本会话累计 X tokens（共 N 轮），有细分时显示输入/输出拆分。"""
    total = int(state.get("total_tokens", 0) or 0)
    turns = int(state.get("turns", 0) or 0)
    prompt = state.get("prompt_tokens")
    completion = state.get("completion_tokens")
    # 细分从未记录过（旧状态或全部走了估算兜底）时不显示拆分，避免"输入 0 + 输出 0"误导
    if not prompt and not completion:
        return f"📊 本会话累计 {total} tokens（共 {turns} 轮）"
    return (
        f"📊 本会话累计 {total} tokens"
        f"（输入 {int(prompt):,} + 输出 {int(completion):,}，共 {turns} 轮）"
    )


def _read_daily(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_daily(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def get_daily_used(date: str, ip: str, path: Path | None = None) -> int:
    """读取指定日期、账号的已用 token 数（v1.2 起按账号记录）。"""
    path = path or DAILY_USAGE_FILE
    with _daily_lock:
        data = _read_daily(path)
    return int(data.get(date, {}).get(ip, 0) or 0)


def add_daily_usage(date: str, ip: str, tokens: int, path: Path | None = None) -> int:
    """累加指定日期、账号的 token 用量，并同步累加全局总量，返回该账号当日累计值。"""
    path = path or DAILY_USAGE_FILE
    with _daily_lock:
        data = _read_daily(path)
        day = data.get(date)
        if not isinstance(day, dict):
            day = {}
            data[date] = day
        day[ip] = int(day.get(ip, 0) or 0) + int(tokens)
        day[_TOTAL_KEY] = int(day.get(_TOTAL_KEY, 0) or 0) + int(tokens)
        _write_daily(path, data)
        return int(day[ip])


def get_daily_total(date: str, path: Path | None = None) -> int:
    """读取指定日期的全局已用 token；旧数据无 __total__ 时回退为各账号求和。"""
    path = path or DAILY_USAGE_FILE
    with _daily_lock:
        data = _read_daily(path)
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


def get_daily_ip_usage(date: str, path: Path | None = None) -> dict:
    """读取指定日期各账号的用量（排除保留键）。"""
    path = path or DAILY_USAGE_FILE
    with _daily_lock:
        data = _read_daily(path)
        day = data.get(date)
        if not isinstance(day, dict):
            return {}
    return {
        ip: int(tokens or 0)
        for ip, tokens in day.items()
        if ip not in _RESERVED_KEYS
    }


def _bump_daily_counter(
    date: str, key: str, amount: int, path: Path | None = None
) -> int:
    path = path or DAILY_USAGE_FILE
    with _daily_lock:
        data = _read_daily(path)
        day = data.get(date)
        if not isinstance(day, dict):
            day = {}
            data[date] = day
        day[key] = int(day.get(key, 0) or 0) + int(amount)
        _write_daily(path, data)
        return int(day[key])


def add_daily_login(date: str, path: Path | None = None) -> int:
    """记录一次成功登录，返回当日登录次数。"""
    return _bump_daily_counter(date, _LOGINS_KEY, 1, path)


def get_daily_logins(date: str, path: Path | None = None) -> int:
    path = path or DAILY_USAGE_FILE
    with _daily_lock:
        data = _read_daily(path)
        day = data.get(date)
    return int(day.get(_LOGINS_KEY, 0) or 0) if isinstance(day, dict) else 0


def add_daily_question(date: str, path: Path | None = None) -> int:
    """记录一次提问，返回当日提问次数。"""
    return _bump_daily_counter(date, _QUESTIONS_KEY, 1, path)


def get_daily_questions(date: str, path: Path | None = None) -> int:
    path = path or DAILY_USAGE_FILE
    with _daily_lock:
        data = _read_daily(path)
        day = data.get(date)
    return int(day.get(_QUESTIONS_KEY, 0) or 0) if isinstance(day, dict) else 0


def is_over_limit(used: int, limit: int) -> bool:
    """已用 token 是否达到或超过每日上限。"""
    return used >= limit


def estimate_tokens(text: str) -> int:
    """按字符数估算 token（中文约 2 字符/token，偏保守）。"""
    return max(1, len(text or "") // 2)


def check_daily_quota(
    daily_used: int, global_used: int, daily_limit: int, global_limit: int
) -> str | None:
    """检查每日限额；超限返回拒绝提示文案，未超限返回 None。"""
    if is_over_limit(daily_used, daily_limit) or is_over_limit(
        global_used, global_limit
    ):
        return (
            f"⚠️ 今日额度已用尽（每账号 {daily_limit:,} / 全局 "
            f"{global_limit:,} tokens），请明天再试。"
        )
    return None
