"""会话内 token 用量累计、估算与每日额度判断：纯函数，便于界面调用与单元测试。

每日用量的文件读写集中在 core/usage_store.DailyUsageStore。
"""

from __future__ import annotations


def new_usage_state() -> dict:
    """新建会话累计状态：累计 token 数（含输入/输出细分）与完成轮数。"""
    return {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0, "turns": 0}


def get_usage_total(usage) -> int | None:
    """读取总用量；没有 usage 时返回 None，缺失总量时按 0 处理。"""
    if usage is None:
        return None
    return int(getattr(usage, "total_tokens", 0) or 0)


def get_usage_part(usage, attr: str) -> int | None:
    """读取用量细分字段；字段缺失或无法转换时返回 None。"""
    if usage is None:
        return None

    value = getattr(usage, attr, None)
    if value is None:
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
