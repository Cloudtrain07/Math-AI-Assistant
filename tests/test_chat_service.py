# -*- coding: utf-8 -*-
"""继续生成消息的独立测试。"""

from core.chat_service import build_continue_message, prepare_api_messages,stream_reply
import core.chat_service as chat_service
from unittest.mock import Mock
import pytest
from conftest import (
    CountingDailyUsageStore,
    FailingDailyUsageStore,
    FailingSaveStore,
    MemoryEventLogger,
    MemoryHistoryStore,
)
from core.usage_store import DailyUsageReadError
from types import SimpleNamespace

def test_uses_latest_user_message_and_removes_image_note():
    history = [
        {"role": "user", "content": "旧题目"},
        {"role": "assistant", "content": "旧回答"},
        {"role": "user", "content": "这是新题目\n[附图]"},
        {"role": "assistant", "content": "尚未完成的回答"},
    ]

    result = build_continue_message(history, "[附图]")

    assert result == "这是新题目\n（请继续完成上一条回答）"


def test_no_user_message_returns_none():
    assert build_continue_message([], "[附图]") is None
    assert build_continue_message(
        [{"role": "assistant", "content": "只有助手的回复"}],
        "[附图]",
    ) is None


def test_latest_user_message_only_has_image_note():
    history = [
        {"role": "user", "content": "更早的题目"},
        {"role": "user", "content": "[附图]"},
        {"role": "assistant", "content": "尚未完成的回答"},
    ]

    assert build_continue_message(history, "[附图]") is None


def test_prepare_api_messages_without_compression():
    history = [
        {"role": "user", "content": "什么是导数？"},
        {"role": "assistant", "content": "导数描述变化率。"},
    ]
    compressor = Mock()

    messages = prepare_api_messages(
        username="tester",
        history=history,
        level="高中",
        mode="解答模式",
        answer_style=None,
        active_client=object(),
        history_compressor=compressor,
        image_data_url=None,
        stage=None,
        mode_changed=False,
        max_history_messages=2,
    )

    compressor.get_summary.assert_not_called()
    assert messages[-2:] == history
    assert not any(
        "【更早对话摘要】" in str(message["content"])
        for message in messages
    )


def test_prepare_api_messages_with_compression():
    history = [
        {"role": "user", "content": "第一条较早的问题"},
        {"role": "assistant", "content": "第一条较早的回答"},
        {"role": "user", "content": "最近的问题"},
    ]
    compressor = Mock()
    compressor.get_summary.return_value = "测试摘要"
    active_client = object()

    messages = prepare_api_messages(
        username="tester",
        history=history,
        level="高中",
        mode="解答模式",
        answer_style=None,
        active_client=active_client,
        history_compressor=compressor,
        image_data_url=None,
        stage=None,
        mode_changed=False,
        max_history_messages=2,
    )

    compressor.get_summary.assert_called_once_with(
        "tester", history, 2, active_client, None
    )
    assert any(
        message["role"] == "system"
        and "【更早对话摘要】\n测试摘要" in message["content"]
        for message in messages
    )
    assert messages[-2:] == history[-2:]


def test_stream_reply_accumulates_model_chunks():
    usage_marker = object()
    messages = [{"role": "user", "content": "请解题"}]

    class FakeClient:
        def chat_stream_with_usage(
            self,
            received_messages,
            thinking,
            reasoning_effort,
            model,
            max_tokens=None,
            max_wait_seconds=None,
        ):
            self.received = (
                received_messages, thinking, reasoning_effort, model
            )
            yield "先求导", None, None, None
            yield None, "正在分析", None, None
            yield "，得到结果", None, usage_marker, "length"

    client = FakeClient()
    snapshots = list(
        stream_reply(
            client,
            messages,
            thinking=True,
            reasoning_effort="high",
            model="test-model",
        )
    )

    assert client.received == (messages, True, "high", "test-model")
    assert snapshots == [
        {
            "answer": "先求导",
            "reasoning_text": "",
            "usage": None,
            "truncated": False,
        },
        {
            "answer": "先求导",
            "reasoning_text": "正在分析",
            "usage": None,
            "truncated": False,
        },
        {
            "answer": "先求导，得到结果",
            "reasoning_text": "正在分析",
            "usage": usage_marker,
            "truncated": True,
        },
    ]


def test_route_image_attaches_fresh_image(monkeypatch):
    monkeypatch.setattr(
        chat_service,
        "image_to_data_url",
        lambda path: "data:image/jpeg;base64,TEST",
    )
    history = [{"role": "user", "content": "求解 x=1"}]
    image_state = {"path": "question.png", "fresh": True}

    result = chat_service.route_image_for_turn(
        message="求解 x=1",
        history=history,
        image_state=image_state,
        previous_stage="summary",
        supports_vision=True,
        force_continue=False,
        attach_image=True,
        image_note="[附图]",
    )

    assert result["image_data_url"] == "data:image/jpeg;base64,TEST"
    assert result["history"] == [
        {"role": "user", "content": "求解 x=1\n[附图]"}
    ]
    assert result["image_state"] == {"path": "question.png", "fresh": False}
    assert result["image_status"] == ""
    assert result["reject_message"] is None
    assert history == [{"role": "user", "content": "求解 x=1"}]  # 不改原历史
    assert image_state == {"path": "question.png", "fresh": True}


def test_route_image_clears_old_image_for_new_problem(monkeypatch):
    def should_not_read_image(path):
        raise AssertionError("旧图不应该被读取")

    monkeypatch.setattr(chat_service, "image_to_data_url", should_not_read_image)
    history = [{"role": "user", "content": "求解 x=1"}]

    result = chat_service.route_image_for_turn(
        message="求解 x=1",
        history=history,
        image_state={"path": "old.png", "fresh": False},
        previous_stage="summary",
        supports_vision=True,
        force_continue=False,
        attach_image=True,
        image_note="[附图]",
    )

    assert result["history"] == history
    assert result["image_data_url"] is None
    assert result["image_state"] == {"path": None, "fresh": False}
    assert result["reject_message"] is None


def test_route_image_rejects_text_client_and_keeps_pending_image():
    history = [{"role": "user", "content": "这道题怎么做"}]
    image_state = {"path": "question.png", "fresh": True}

    result = chat_service.route_image_for_turn(
        message="这道题怎么做",
        history=history,
        image_state=image_state,
        previous_stage="initial_hint",
        supports_vision=False,
        force_continue=False,
        attach_image=True,
        image_note="[附图]",
    )

    assert "不支持图片输入" in result["reject_message"]
    assert result["image_data_url"] is None
    assert result["image_state"] == image_state
    assert result["history"] == history


def test_route_image_read_failure_falls_back_to_text(monkeypatch):
    def fail_to_read(path):
        raise ValueError("文件损坏")

    monkeypatch.setattr(chat_service, "image_to_data_url", fail_to_read)
    history = [{"role": "user", "content": "这道题怎么做"}]

    result = chat_service.route_image_for_turn(
        message="这道题怎么做",
        history=history,
        image_state={"path": "broken.png", "fresh": True},
        previous_stage="initial_hint",
        supports_vision=True,
        force_continue=False,
        attach_image=True,
        image_note="[附图]",
    )

    assert result["image_data_url"] is None
    assert result["history"] == history
    assert result["image_state"] == {"path": None, "fresh": False}
    assert "图片读取失败" in result["image_status"]
    assert "已按纯文本发送" in result["image_status"]
    assert result["reject_message"] is None  # 提示错误，但仍允许发文字


def test_route_image_skips_attachment_for_continue_or_text_only(monkeypatch):
    def should_not_read_image(path):
        raise AssertionError("本轮不应该读取图片")

    monkeypatch.setattr(chat_service, "image_to_data_url", should_not_read_image)
    history = [{"role": "user", "content": "求解 x=1"}]
    image_state = {"path": "question.png", "fresh": True}

    for force_continue, attach_image in [(True, True), (False, False)]:
        result = chat_service.route_image_for_turn(
            message="求解 x=1",
            history=history,
            image_state=image_state,
            previous_stage="summary",
            supports_vision=True,
            force_continue=force_continue,
            attach_image=attach_image,
            image_note="[附图]",
        )

        assert result["history"] == history
        assert result["image_data_url"] is None
        assert result["image_state"] == image_state
        assert result["reject_message"] is None


def test_prepare_user_message_preserves_normal_text():
    result = chat_service.prepare_user_message(
        message="  这道题怎么做？  ",
        has_pending_image=False,
        max_message_len=100,
        image_only_prompt="请解答图中的题目。",
    )

    assert result == {
        "message": "  这道题怎么做？  ",
        "error": None,
    }


def test_prepare_user_message_rejects_blank_without_image():
    for message in ["", "   ", "\n\t"]:
        result = chat_service.prepare_user_message(
            message=message,
            has_pending_image=False,
            max_message_len=100,
            image_only_prompt="请解答图中的题目。",
        )

        assert result == {"message": message, "error": "empty"}


def test_prepare_user_message_uses_prompt_for_image_only():
    result = chat_service.prepare_user_message(
        message="   ",
        has_pending_image=True,
        max_message_len=100,
        image_only_prompt="请解答图中的题目。",
    )

    assert result == {
        "message": "请解答图中的题目。",
        "error": None,
    }


def test_prepare_user_message_accepts_exact_length_limit():
    result = chat_service.prepare_user_message(
        message="12345",
        has_pending_image=False,
        max_message_len=5,
        image_only_prompt="请解答图中的题目。",
    )

    assert result == {"message": "12345", "error": None}


def test_prepare_user_message_rejects_overlong_text():
    result = chat_service.prepare_user_message(
        message="123456",
        has_pending_image=False,
        max_message_len=5,
        image_only_prompt="请解答图中的题目。",
    )

    assert result == {"message": "123456", "error": "too_long"}


def test_prepare_user_message_checks_generated_prompt_length():
    result = chat_service.prepare_user_message(
        message="",
        has_pending_image=True,
        max_message_len=5,
        image_only_prompt="请解答图中的题目。",
    )

    assert result == {
        "message": "请解答图中的题目。",
        "error": "too_long",
    }


# 检查用量，分别为真实用量、异常估算、正常结束不估算、空回答不估算、零用量有效、原状态不被修改
def test_settle_reply_usage_uses_model_usage():
    from core.chat_service import settle_reply_usage

    class Usage:
        total_tokens = 123
        prompt_tokens = 100
        completion_tokens = 23

    turn_total, state = settle_reply_usage(
        {
            "total_tokens": 10,
            "prompt_tokens": 6,
            "completion_tokens": 4,
            "turns": 2,
        },
        Usage(),
        "部分回答",
        interrupted=True,
    )

    assert turn_total == 123
    assert state == {
        "total_tokens": 133,
        "prompt_tokens": 106,
        "completion_tokens": 27,
        "turns": 3,
    }


def test_settle_reply_usage_estimates_interrupted_answer():
    from core.chat_service import settle_reply_usage
    from core.usage import estimate_tokens, new_usage_state

    answer = "已经生成的部分回答"
    turn_total, state = settle_reply_usage(
        new_usage_state(),
        None,
        answer,
        interrupted=True,
    )

    assert turn_total == estimate_tokens(answer)
    assert state == {
        "total_tokens": turn_total,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "turns": 1,
    }


def test_settle_reply_usage_does_not_estimate_normal_reply():
    from core.chat_service import settle_reply_usage
    from core.usage import new_usage_state

    turn_total, state = settle_reply_usage(
        new_usage_state(),
        None,
        "正常结束的回答",
    )

    assert turn_total is None
    assert state == {
        "total_tokens": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "turns": 1,
    }


def test_settle_reply_usage_does_not_estimate_empty_answer():
    from core.chat_service import settle_reply_usage
    from core.usage import new_usage_state

    turn_total, state = settle_reply_usage(
        new_usage_state(),
        None,
        "",
        interrupted=True,
    )

    assert turn_total is None
    assert state == {
        "total_tokens": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "turns": 1,
    }


def test_settle_reply_usage_keeps_zero_model_usage(monkeypatch):
    from core import chat_service
    from core.usage import new_usage_state

    class ZeroUsage:
        total_tokens = 0
        prompt_tokens = 0
        completion_tokens = 0

    def unexpected_estimate(text):
        raise AssertionError("已有模型用量，不应该估算")

    monkeypatch.setattr(
        chat_service, "estimate_tokens", unexpected_estimate
    )

    turn_total, state = chat_service.settle_reply_usage(
        new_usage_state(),
        ZeroUsage(),
        "部分回答",
        interrupted=True,
    )

    assert turn_total == 0
    assert state == {
        "total_tokens": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "turns": 1,
    }


def test_settle_reply_usage_does_not_modify_original_state():
    from core.chat_service import settle_reply_usage

    original = {
        "total_tokens": 10,
        "prompt_tokens": 6,
        "completion_tokens": 4,
        "turns": 2,
    }
    snapshot = original.copy()

    _, updated = settle_reply_usage(
        original,
        None,
        "部分回答",
        interrupted=True,
    )

    assert original == snapshot
    assert updated is not original
    assert updated["turns"] == 3


def test_build_answer_history_adds_normal_reply_without_mutating_input():
    from core.chat_service import build_answer_history

    history = [{"role": "user", "content": "求解"}]
    result = build_answer_history(history, "答案是 2")

    assert result == [
        {"role": "user", "content": "求解"},
        {"role": "assistant", "content": "答案是 2"},
    ]
    assert result is not history
    assert history == [{"role": "user", "content": "求解"}]


def test_build_answer_history_closes_partial_formula():
    from core.chat_service import build_answer_history

    result = build_answer_history([], "结果为 $x^2+1")

    assert result == [{"role": "assistant", "content": "结果为 $x^2+1$"}]


def test_build_answer_history_skips_empty_reply():
    from core.chat_service import build_answer_history

    history = [{"role": "user", "content": "求解"}]

    assert build_answer_history(history, "") is None
    assert history == [{"role": "user", "content": "求解"}]


@pytest.mark.parametrize(
    "scenario, expected_daily, expected_global, expected_event",
    [
        ("recorded", 40, 40, None),
        ("record_failed", None, None, "每日用量记录失败"),
        ("stats_failed", 40, None, "全局统计刷新失败"),
    ],
)
def test_record_daily_usage(
    scenario, expected_daily, expected_global, expected_event
):
    class GlobalStatsFailStore(CountingDailyUsageStore):
        def get_total(self, date):
            raise DailyUsageReadError("模拟全局统计读取失败")

    if scenario == "record_failed":
        store = FailingDailyUsageStore()
    elif scenario == "stats_failed":
        store = GlobalStatsFailStore()
    else:
        store = CountingDailyUsageStore()

    logger = MemoryEventLogger()
    date = "2026-10-01"
    username = "user1"

    result = chat_service.record_daily_usage(
        date=date,
        username=username,
        turn_total=40,
        daily_usage_store=store,
        event_logger=logger,
    )

    assert result == {
        "daily_used": expected_daily,
        "global_used": expected_global,
        "status": scenario,
    }
    assert store.add_usage_calls == [(date, username, 40)]

    stored_tokens = 0 if expected_daily is None else expected_daily
    assert store.get_used(date, username) == stored_tokens

    if expected_event is None:
        assert logger.access_events == []
    else:
        assert len(logger.access_events) == 1
        assert logger.access_events[0][0] == expected_event


@pytest.mark.parametrize(
    "store_factory, expected_save_status",
    [
        (MemoryHistoryStore, "saved"),
        (FailingSaveStore, "failed"),
        (MemoryHistoryStore, "stale"),
    ],
)
@pytest.mark.parametrize("reports_usage", [True, False])
def test_finish_chat_turn_normal_answer(
    store_factory, expected_save_status, reports_usage
):
    history_store = store_factory()
    daily_store = CountingDailyUsageStore()
    logger = MemoryEventLogger()

    username = "user1"
    date = "2026-10-01"
    history = [{"role": "user", "content": "1+1=?"}]
    answer = "答案是 2"
    initial_state = {"total_tokens": 10, "turns": 2}
    usage = SimpleNamespace(total_tokens=40) if reports_usage else None

    turn_token = history_store.start_turn(username)
    if expected_save_status == "stale":
        history_store.clear(username)

    result = chat_service.finish_chat_turn(
        history=history,
        answer=answer,
        usage=usage,
        usage_state=initial_state,
        username=username,
        date=date,
        turn_token=turn_token,
        history_store=history_store,
        daily_usage_store=daily_store,
        event_logger=logger,
    )

    expected_history = history + [
        {"role": "assistant", "content": answer}
    ]
    assert result["history"] == expected_history
    assert result["save_status"] == expected_save_status
    assert history_store.save_attempts == 1

    if expected_save_status == "saved":
        assert history_store.load(username) == expected_history
    else:
        assert history_store.load(username) == []

    assert result["turn_total"] == (40 if reports_usage else None)
    assert result["usage_state"] == {
        "total_tokens": 50 if reports_usage else 10,
        "turns": 3,
    }
    assert initial_state == {"total_tokens": 10, "turns": 2}

    if reports_usage:
        assert result["daily_usage"] == {
            "daily_used": 40,
            "global_used": 40,
            "status": "recorded",
        }
        assert daily_store.add_usage_calls == [(date, username, 40)]
        assert daily_store.get_used(date, username) == 40
    else:
        assert result["daily_usage"] is None
        assert daily_store.add_usage_calls == []

    assert logger.chat_events == [("回复", answer)]


@pytest.mark.parametrize(
    "scenario", ["recorded", "record_failed", "unknown"]
)
@pytest.mark.parametrize("truncated", [False, True])
def test_finish_chat_turn_empty_answer(scenario, truncated):
    history_store = MemoryHistoryStore()
    daily_store = (
        FailingDailyUsageStore()
        if scenario == "record_failed"
        else CountingDailyUsageStore()
    )
    logger = MemoryEventLogger()

    username = "user1"
    date = "2026-10-01"
    history = [{"role": "user", "content": "1+1=?"}]
    initial_state = {"total_tokens": 10, "turns": 2}
    reports_usage = scenario != "unknown"
    usage = SimpleNamespace(total_tokens=40) if reports_usage else None

    result = chat_service.finish_chat_turn(
        history=history,
        answer="",
        usage=usage,
        usage_state=initial_state,
        username=username,
        date=date,
        turn_token=history_store.start_turn(username),
        history_store=history_store,
        daily_usage_store=daily_store,
        event_logger=logger,
        truncated=truncated,
    )

    assert result["history"] == history
    assert history == [{"role": "user", "content": "1+1=?"}]
    assert result["save_status"] is None
    assert history_store.save_attempts == 0
    assert history_store.load(username) == []

    expected_total = 40 if reports_usage else None
    assert result["turn_total"] == expected_total
    assert result["usage_state"] == {
        "total_tokens": 50 if reports_usage else 10,
        "turns": 3,
    }
    assert initial_state == {"total_tokens": 10, "turns": 2}

    if reports_usage:
        expected_daily = 40 if scenario == "recorded" else None
        assert result["daily_usage"] == {
            "daily_used": expected_daily,
            "global_used": expected_daily,
            "status": scenario,
        }
        assert daily_store.add_usage_calls == [(date, username, 40)]
        assert daily_store.get_used(date, username) == (
            40 if scenario == "recorded" else 0
        )
    else:
        assert result["daily_usage"] is None
        assert daily_store.add_usage_calls == []

    assert logger.empty_replies == [{
        "account": username,
        "turn_total": expected_total,
        "truncated": truncated,
    }]
    assert logger.chat_events == []
    assert not any(
        event[0] == "回复" for event in logger.access_events
    )


@pytest.mark.parametrize(
    "answer, reports_usage, expected_total",
    [
        ("部分回答", False, 2),
        ("部分回答", True, 40),
        ("", False, None),
        ("", True, 40),
    ],
)
def test_finish_chat_turn_interrupted(
    answer, reports_usage, expected_total
):
    history_store = MemoryHistoryStore()
    daily_store = CountingDailyUsageStore()
    logger = MemoryEventLogger()

    username = "user1"
    date = "2026-10-01"
    history = [{"role": "user", "content": "1+1=?"}]
    initial_state = {"total_tokens": 10, "turns": 2}
    usage = SimpleNamespace(total_tokens=40) if reports_usage else None
    error = RuntimeError("模拟流式中断")

    result = chat_service.finish_chat_turn(
        history=history,
        answer=answer,
        usage=usage,
        usage_state=initial_state,
        username=username,
        date=date,
        turn_token=history_store.start_turn(username),
        history_store=history_store,
        daily_usage_store=daily_store,
        event_logger=logger,
        error=error,
    )

    expected_history = history + (
        [{"role": "assistant", "content": answer}] if answer else []
    )
    assert result["history"] == expected_history
    assert result["error"] is error
    assert result["save_status"] == ("saved" if answer else None)
    assert history_store.save_attempts == (1 if answer else 0)
    assert history_store.load(username) == (
        expected_history if answer else []
    )

    assert result["turn_total"] == expected_total
    assert result["usage_state"] == {
        "total_tokens": 10 + (
            expected_total if expected_total is not None else 0
        ),
        "turns": 3,
    }
    assert initial_state == {"total_tokens": 10, "turns": 2}

    expected_calls = (
        [(date, username, expected_total)]
        if expected_total is not None
        else []
    )
    assert daily_store.add_usage_calls == expected_calls

    assert logger.errors == [
        (error, {
            "account": username,
            "turn_total": expected_total,
            "partial": bool(answer),
        })
    ]
    assert logger.chat_events == []
    assert logger.empty_replies == []

    expected_event = (
        "回复失败", f"账号={username}", "错误=RuntimeError"
    )
    if expected_total is not None:
        expected_event += (
            f"估算tokens={expected_total}",
            f"本账号={expected_total}",
        )
    assert logger.access_events == [expected_event]


@pytest.mark.parametrize(
    "store_factory, expected_save_status",
    [
        (MemoryHistoryStore, "saved"),
        (FailingSaveStore, "failed"),
    ],
)
@pytest.mark.parametrize(
    "scenario", ["normal", "truncated", "interrupted"]
)
def test_generate_reply_composes_stream_and_finalization(
    store_factory, expected_save_status, scenario
):
    interrupted = scenario == "interrupted"
    usage = SimpleNamespace(total_tokens=40)
    error = RuntimeError("模拟流式中断")

    class FakeClient:
        def __init__(self):
            self.calls = 0

        def chat_stream_with_usage(
            self,
            messages,
            thinking,
            reasoning_effort,
            model,
            max_tokens=None,
            max_wait_seconds=None,
        ):
            self.calls += 1
            self.received = (
                messages, thinking, reasoning_effort, model
            )
            yield "先求导", "分析题目", None, None
            if interrupted:
                raise error
            finish_reason = (
                "length" if scenario == "truncated" else "stop"
            )
            yield "再代入", None, usage, finish_reason

    client = FakeClient()
    compressor = Mock()
    history_store = store_factory()
    daily_store = CountingDailyUsageStore()
    logger = MemoryEventLogger()

    username = "user1"
    date = "2026-10-01"
    history = [{"role": "user", "content": "求函数的导数"}]
    initial_state = {"total_tokens": 10, "turns": 2}

    events = chat_service.generate_reply(
        history=history,
        usage_state=initial_state,
        username=username,
        date=date,
        turn_token=history_store.start_turn(username),
        level="高中",
        mode="解答模式",
        answer_style=None,
        stage=None,
        mode_changed=False,
        thinking=True,
        reasoning_effort="high",
        model="test-model",
        image_data_url=None,
        max_history_messages=None,
        active_client=client,
        history_compressor=compressor,
        history_store=history_store,
        daily_usage_store=daily_store,
        event_logger=logger,
    )

    # 创建生成器时还没有调用模型；读取时才开始执行。
    assert client.calls == 0
    first = next(events)
    assert first == {
        "type": "stream",
        "reply": {
            "answer": "先求导",
            "reasoning_text": "分析题目",
            "usage": None,
            "truncated": False,
        },
    }
    assert history_store.save_attempts == 0
    assert daily_store.add_usage_calls == []

    remaining = list(events)
    assert [event["type"] for event in remaining] == (
        ["final"] if interrupted else ["stream", "final"]
    )
    final = remaining[-1]
    result = final["result"]

    expected_answer = "先求导" if interrupted else "先求导再代入"
    expected_total = 1 if interrupted else 40
    expected_history = history + [
        {"role": "assistant", "content": expected_answer}
    ]

    assert final["reply"] == {
        "answer": expected_answer,
        "reasoning_text": "分析题目",
        "usage": None if interrupted else usage,
        "truncated": scenario == "truncated",
    }
    assert result["history"] == expected_history
    assert result["error"] is (error if interrupted else None)
    assert result["save_status"] == expected_save_status
    assert result["turn_total"] == expected_total
    assert result["usage_state"] == {
        "total_tokens": 10 + expected_total,
        "turns": 3,
    }
    assert initial_state == {"total_tokens": 10, "turns": 2}

    assert client.calls == 1
    assert client.received[0][-1] == history[-1]
    assert client.received[1:] == (True, "high", "test-model")
    compressor.get_summary.assert_not_called()

    assert history_store.save_attempts == 1
    assert history_store.load(username) == (
        expected_history if expected_save_status == "saved" else []
    )
    assert daily_store.add_usage_calls == [
        (date, username, expected_total)
    ]


@pytest.mark.parametrize(
    "scenario, expected_status, expected_questions, added_user",
    [
        ("normal", "ready", 1, True),
        ("empty", "empty", 0, False),
        ("too_long", "too_long", 0, False),
        ("quota", "quota", 0, False),
        ("quota_read_failed", "quota_read_failed", 0, False),
        ("no_client", "no_client", 1, True),
        ("image", "image_rejected", 1, True),
    ],
)
def test_prepare_chat_turn_preserves_rejection_order(
    scenario, expected_status, expected_questions, added_user
):

    class QuotaReadFailStore(CountingDailyUsageStore):
        def get_used(self, date, account):
            raise DailyUsageReadError("模拟额度读取失败")

    daily_store = (
        QuotaReadFailStore()
        if scenario == "quota_read_failed"
        else CountingDailyUsageStore()
    )
    history_store = MemoryHistoryStore()
    history_store.start_turn = Mock(
        wraps=history_store.start_turn
    )
    logger = MemoryEventLogger()
    client = Mock(supports_vision=False)

    username = "user1"
    date = "2026-10-01"
    if scenario == "quota":
        daily_store.add_usage(date, username, 100)
    initial_usage_calls = list(daily_store.add_usage_calls)

    message = {
        "empty": "   ",
        "too_long": "123456",
    }.get(scenario, "1+1=?")
    original_stage = {
        "stage": "full_solution",
        "hints": 2,
        "last_mode": "互动模式",
    }
    original_image = (
        {"path": "unused.png", "fresh": True}
        if scenario == "image"
        else None
    )
    original_history = []

    result = chat_service.prepare_chat_turn(
        message=message,
        history=original_history,
        mode="互动模式",
        stage_state=original_stage,
        image_state=original_image,
        username=username,
        ip="127.0.0.1",
        date=date,
        active_client=None if scenario == "no_client" else client,
        history_store=history_store,
        daily_usage_store=daily_store,
        event_logger=logger,
        max_message_len=5,
        daily_limit=100,
        global_daily_limit=1000,
        image_only_prompt="请解答图中的题目。",
        image_note="[附图]",
    )

    assert result["status"] == expected_status
    assert result["history"] == (
        [{"role": "user", "content": message}] if added_user else []
    )
    assert result["stage_state"]["stage"] == (
        "initial_hint" if added_user else "full_solution"
    )
    assert daily_store.get_questions(date) == expected_questions
    assert daily_store.add_usage_calls == initial_usage_calls
    assert history_store.save_attempts == 0
    client.chat_stream_with_usage.assert_not_called()

    if expected_status == "ready":
        assert result["turn_token"] == 0
        history_store.start_turn.assert_called_once_with(username)
    else:
        assert result["turn_token"] is None
        history_store.start_turn.assert_not_called()

    assert original_history == []
    assert original_stage == {
        "stage": "full_solution",
        "hints": 2,
        "last_mode": "互动模式",
    }
    if scenario == "image":
        assert result["image_state"] == original_image
        assert original_image == {
            "path": "unused.png",
            "fresh": True,
        }


@pytest.mark.parametrize(
    "scenario", ["send", "continue", "empty", "cleared"]
)
def test_run_chat_turn_without_gradio(scenario):
    usage = SimpleNamespace(total_tokens=40)

    class FakeClient:
        supports_vision = False

        def __init__(self):
            self.calls = 0

        def chat_stream_with_usage(
            self,
            messages,
            thinking,
            reasoning_effort,
            model,
            max_tokens=None,
            max_wait_seconds=None,
        ):
            self.calls += 1
            self.received = (
                messages, thinking, reasoning_effort, model
            )
            yield "测试", "分析", None, None
            yield "回答", None, usage, "stop"

    client = FakeClient()
    compressor = Mock()
    history_store = MemoryHistoryStore()
    daily_store = CountingDailyUsageStore()
    logger = MemoryEventLogger()

    username = "user1"
    date = "2026-10-01"
    history = [
        {"role": "user", "content": "1+1=?"},
        {"role": "assistant", "content": "先计算"},
    ]
    initial_state = {"total_tokens": 10, "turns": 2}
    image_state = (
        None if scenario == "empty"
        else {"path": "unused.png", "fresh": False}
    )
    force_continue = scenario == "continue"
    message = (
        chat_service.build_continue_message(history, "[附图]")
        if force_continue
        else "   " if scenario == "empty"
        else "2+2=?"
    )

    events = chat_service.run_chat_turn(
        message=message,
        history=history,
        usage_state=initial_state,
        username=username,
        ip="127.0.0.1",
        date=date,
        level="高中",
        mode="互动模式",
        stage_state={
            "stage": "full_solution",
            "hints": 2,
            "last_mode": "互动模式",
        },
        image_state=image_state,
        thinking=True,
        model="test-model",
        reasoning_effort="high",
        answer_style=None,
        active_client=client,
        history_compressor=compressor,
        history_store=history_store,
        daily_usage_store=daily_store,
        event_logger=logger,
        max_message_len=2000,
        max_history_messages=None,
        daily_limit=1000,
        global_daily_limit=3000,
        image_only_prompt="请解答图中的题目。",
        image_note="[附图]",
        force_continue=force_continue,
    )

    first = next(events)
    assert client.calls == 0
    assert history_store.save_attempts == 0

    if scenario == "empty":
        assert first["type"] == "rejected"
        assert first["turn"]["status"] == "empty"
        assert first["turn"]["history"] == history
        assert list(events) == []
        assert daily_store.get_questions(date) == 0
        assert daily_store.add_usage_calls == []
        assert initial_state == {"total_tokens": 10, "turns": 2}
        return

    assert first["type"] == "ready"
    prepared = first["turn"]
    assert prepared["stage_state"]["stage"] == (
        "full_solution" if force_continue else "initial_hint"
    )
    assert prepared["stage_state"]["hints"] == (
        2 if force_continue else 0
    )
    assert prepared["image_state"] == (
        image_state if force_continue
        else {"path": None, "fresh": False}
    )

    if scenario == "cleared":
        history_store.clear(username)

    remaining = list(events)
    assert [event["type"] for event in remaining] == [
        "stream", "stream", "final"
    ]
    final = remaining[-1]
    result = final["result"]
    expected_history = history + [
        {"role": "user", "content": message},
        {"role": "assistant", "content": "测试回答"},
    ]

    assert final["reply"]["answer"] == "测试回答"
    assert result["history"] == expected_history
    assert result["error"] is None
    assert result["save_status"] == (
        "stale" if scenario == "cleared" else "saved"
    )
    assert result["usage_state"] == {
        "total_tokens": 50,
        "turns": 3,
    }
    assert initial_state == {"total_tokens": 10, "turns": 2}

    assert client.calls == 1
    assert client.received[0][-1] == prepared["history"][-1]
    assert client.received[1:] == (True, "high", "test-model")
    compressor.get_summary.assert_not_called()

    assert history_store.save_attempts == 1
    assert history_store.load(username) == (
        [] if scenario == "cleared" else expected_history
    )
    assert daily_store.get_questions(date) == 1
    assert daily_store.add_usage_calls == [(date, username, 40)]


def test_prepare_chat_turn_survives_question_count_failure():
    """提问次数写盘失败不阻断聊天：回合照常就绪，并留下明确失败信息。"""

    class FailingQuestionStore(CountingDailyUsageStore):
        def add_question(self, date):
            raise OSError("模拟提问次数写盘失败")

    daily_store = FailingQuestionStore()
    history_store = MemoryHistoryStore()
    logger = MemoryEventLogger()
    client = Mock(supports_vision=False)

    result = chat_service.prepare_chat_turn(
        message="1+1=?",
        history=[],
        mode="互动模式",
        stage_state={
            "stage": "full_solution",
            "hints": 2,
            "last_mode": "互动模式",
        },
        image_state=None,
        username="user1",
        ip="127.0.0.1",
        date="2026-10-01",
        active_client=client,
        history_store=history_store,
        daily_usage_store=daily_store,
        event_logger=logger,
        max_message_len=2000,
        daily_limit=1000,
        global_daily_limit=3000,
        image_only_prompt="请解答图中的题目。",
        image_note="[附图]",
    )

    assert result["status"] == "ready"
    assert result["turn_token"] == 0
    assert daily_store.get_questions("2026-10-01") == 0
    assert any(
        "提问次数记录失败" in str(parts) for parts in logger.access_events
    )
