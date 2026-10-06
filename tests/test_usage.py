# -*- coding: utf-8 -*-
"""会话内 token 累计逻辑测试。

每日用量存储本身的测试见 test_usage_store.py，
应用接入点的失败处理见 test_chat_service.py 与 test_api_*。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.usage import (
    check_daily_quota,
    estimate_tokens,
    format_cumulative,
    get_usage_part,
    get_usage_total,
    is_over_limit,
    new_usage_state,
    record_usage,
)


def test_new_state_zero():
    assert new_usage_state() == {
        "total_tokens": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "turns": 0,
    }


def test_record_accumulates():
    state = new_usage_state()
    state = record_usage(state, 100)
    state = record_usage(state, 250)
    assert state["total_tokens"] == 350
    assert state["turns"] == 2


def test_record_none_only_counts_turn():
    state = new_usage_state()
    state = record_usage(state, None)
    assert state["total_tokens"] == 0
    assert state["turns"] == 1


def test_record_does_not_mutate_input():
    state = new_usage_state()
    returned = record_usage(state, 100)
    assert state["total_tokens"] == 0 and state["turns"] == 0
    assert returned["total_tokens"] == 100 and returned["turns"] == 1


def test_record_prompt_completion_split():
    state = new_usage_state()
    state = record_usage(state, 500, prompt_tokens=400, completion_tokens=100)
    state = record_usage(state, 300, prompt_tokens=200, completion_tokens=100)
    assert state == {
        "total_tokens": 800,
        "prompt_tokens": 600,
        "completion_tokens": 200,
        "turns": 2,
    }
    # 缺细分的轮次只累计总数
    state = record_usage(state, 50)
    assert state["total_tokens"] == 850
    assert state["prompt_tokens"] == 600


def test_format_cumulative():
    state = record_usage(record_usage(new_usage_state(), 123), 456)
    text = format_cumulative(state)
    assert "累计 579 tokens" in text
    assert "2 轮" in text


def test_format_cumulative_with_split():
    state = record_usage(
        new_usage_state(), 500, prompt_tokens=400, completion_tokens=100
    )
    text = format_cumulative(state)
    assert "输入 400 + 输出 100" in text
    assert "1 轮" in text


def test_format_cumulative_fallback_without_split():
    state = {"total_tokens": 579, "turns": 2}
    text = format_cumulative(state)
    assert "累计 579 tokens" in text
    assert "输入" not in text


def test_is_over_limit_boundary():
    assert not is_over_limit(999_999, 1_000_000)
    assert is_over_limit(1_000_000, 1_000_000)
    assert is_over_limit(1_000_001, 1_000_000)


def test_global_limit_boundary():
    assert not is_over_limit(2_999_999, 3_000_000)
    assert is_over_limit(3_000_000, 3_000_000)


def test_estimate_tokens():
    assert estimate_tokens("") == 1
    assert estimate_tokens("a" * 10) == 5
    assert estimate_tokens("数学" * 4) == 4


def test_check_daily_quota_ok():
    assert check_daily_quota(100, 200, 1_000_000, 3_000_000) is None


def test_check_daily_quota_rejected_on_limit():
    msg = check_daily_quota(1_000_000, 100, 1_000_000, 3_000_000)
    assert msg is not None and "额度已用尽" in msg
    msg2 = check_daily_quota(100, 3_000_000, 1_000_000, 3_000_000)
    assert msg2 is not None and "额度已用尽" in msg2


def test_get_usage_total_without_usage_returns_none():
    assert get_usage_total(None) is None


def test_get_usage_total_reads_integer_or_numeric_string():
    class Usage:
        total_tokens = 123

    usage = Usage()
    assert get_usage_total(usage) == 123

    usage.total_tokens = "456"
    assert get_usage_total(usage) == 456


def test_get_usage_total_missing_or_none_returns_zero():
    class Usage:
        pass

    usage = Usage()
    assert get_usage_total(usage) == 0

    usage.total_tokens = None
    assert get_usage_total(usage) == 0


def test_get_usage_total_invalid_value_keeps_raising():
    class Usage:
        total_tokens = "不是数字"

    with pytest.raises(ValueError):
        get_usage_total(Usage())


def test_get_usage_part_reads_requested_field():
    class Usage:
        prompt_tokens = "100"
        completion_tokens = 23

    usage = Usage()
    assert get_usage_part(usage, "prompt_tokens") == 100
    assert get_usage_part(usage, "completion_tokens") == 23


def test_get_usage_part_missing_or_none_returns_none():
    class Usage:
        prompt_tokens = None

    assert get_usage_part(None, "prompt_tokens") is None
    assert get_usage_part(Usage(), "prompt_tokens") is None
    assert get_usage_part(Usage(), "completion_tokens") is None


def test_get_usage_part_invalid_value_returns_none():
    class Usage:
        prompt_tokens = "不是数字"
        completion_tokens = object()

    assert get_usage_part(Usage(), "prompt_tokens") is None
    assert get_usage_part(Usage(), "completion_tokens") is None


def test_get_usage_part_zero_is_preserved():
    class Usage:
        prompt_tokens = 0

    assert get_usage_part(Usage(), "prompt_tokens") == 0

