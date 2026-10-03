# -*- coding: utf-8 -*-
"""respond 主流程测试（mock 客户端与用量文件）。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module
from conftest import (
    CountingDailyUsageStore,
    FailingDailyUsageStore,
    FailingSaveStore,
    MemoryDailyUsageStore,
    MemoryEventLogger,
    MemoryHistoryStore,
    StaleSaveStore,
)
from core import usage as usage_module
from core.logger import FileEventLogger


class _FakeUsage:
    total_tokens = 123


class _FakeClient:
    def __init__(self):
        self.calls = 0

    def chat_stream_with_usage(
        self, messages, thinking=None, reasoning_effort=None, model=None
    ):
        self.calls += 1
        yield "你好", None, None, None
        yield "世界", None, _FakeUsage(), "stop"


class _ReasoningClient:
    """带思考链片段的假客户端：思考 → 内容 → 用量。"""

    def __init__(self):
        self.calls = 0

    def chat_stream_with_usage(
        self, messages, thinking=None, reasoning_effort=None, model=None
    ):
        self.calls += 1
        yield None, "先看定义域", None, None
        yield None, "再讨论单调性，得 $\\frac{a}{2}", None, None
        yield "结论", None, None, None
        yield None, None, _FakeUsage(), "stop"


def _fake_request():
    client = type("Client", (), {"host": "127.0.0.1"})()
    return type("Request", (), {"client": client, "username": "user1"})()


def _use_memory_store(monkeypatch, app_runtime) -> MemoryHistoryStore:
    """用内存假存储替换文件存储，返回实例供断言。"""
    store = MemoryHistoryStore()
    monkeypatch.setattr(app_runtime, "history_store", store)
    return store


def _use_memory_logger(monkeypatch, app_runtime) -> MemoryEventLogger:
    """用内存假日志对象替换文件日志，返回实例供断言。"""
    logger = MemoryEventLogger()
    monkeypatch.setattr(app_runtime, "event_logger", logger)
    return logger


def _stage0():
    return {"stage": "initial_hint", "hints": 0, "last_mode": "互动模式"}


def _wire_usage_store(monkeypatch, app_runtime) -> CountingDailyUsageStore:
    """用内存每日用量存储替换文件实现，返回实例供断言（含 add_usage 调用记录）。"""
    store = CountingDailyUsageStore()
    monkeypatch.setattr(app_runtime, "daily_usage_store", store)
    return store


def test_respond_success_path(monkeypatch, tmp_path, app_runtime):
    fake_client = _FakeClient()
    store = _use_memory_store(monkeypatch, app_runtime)
    monkeypatch.setattr(app_runtime, "client", fake_client)
    _use_memory_logger(monkeypatch, app_runtime)
    monkeypatch.setattr(app_runtime, "daily_limit", 1_000_000)
    monkeypatch.setattr(app_runtime, "global_daily_limit", 3_000_000)
    usage_store = _wire_usage_store(monkeypatch, app_runtime)

    results = list(
        app_module.respond(
            app_runtime,
            "1+1=?",
            [],
            "高中",
            "互动模式",
            False,
            "deepseek-v4-flash",
            "high",
            None,
            {"total_tokens": 0, "turns": 0},
            _stage0(),
            None,
            None,
            _fake_request(),
        )
    )
    final_history, _, status_text, usage_text, usage_state, stage_state, _, _ = results[-1]

    assert final_history[-1]["content"] == "你好世界"
    assert store._messages["user1"] == final_history
    # 本轮只尝试保存一次
    assert store.save_attempts == 1
    assert usage_state == {"total_tokens": 123, "turns": 1}
    assert "123" in status_text
    assert "累计" in usage_text and "123" in usage_text
    assert "本账号" in usage_text
    assert stage_state["stage"] == "initial_hint"
    assert fake_client.calls == 1
    today = __import__("datetime").date.today().isoformat()
    assert usage_store.get_used(today, "user1") == 123
    assert usage_store.get_total(today) == 123


def test_respond_quota_rejected(monkeypatch, tmp_path, app_runtime):
    fake_client = _FakeClient()
    monkeypatch.setattr(app_runtime, "client", fake_client)
    _use_memory_store(monkeypatch, app_runtime)
    _use_memory_logger(monkeypatch, app_runtime)
    monkeypatch.setattr(app_runtime, "daily_limit", 1_000_000)
    monkeypatch.setattr(app_runtime, "global_daily_limit", 3_000_000)
    # 本账号今日额度已用尽
    quota_store = _wire_usage_store(monkeypatch, app_runtime)
    quota_store.add_usage(
        __import__("datetime").date.today().isoformat(), "user1", 1_000_000
    )

    state0 = {"total_tokens": 0, "turns": 0}
    results = list(
        app_module.respond(
            app_runtime,
            "1+1=?",
            [],
            "高中",
            "互动模式",
            False,
            "deepseek-v4-flash",
            "high",
            None,
            state0,
            _stage0(),
            None,
            None,
            _fake_request(),
        )
    )
    final_history, _, status_text, _, usage_state, stage_state, _, _ = results[-1]

    assert "额度已用尽" in status_text
    assert final_history == []
    assert usage_state == state0
    assert stage_state["stage"] == "initial_hint"
    assert fake_client.calls == 0


def test_respond_overlong_message_rejected(monkeypatch, app_runtime):
    fake_client = _FakeClient()
    monkeypatch.setattr(app_runtime, "client", fake_client)
    _use_memory_store(monkeypatch, app_runtime)
    _use_memory_logger(monkeypatch, app_runtime)
    monkeypatch.setattr(app_runtime, "max_message_len", 5)

    state0 = {"total_tokens": 0, "turns": 0}
    results = list(
        app_module.respond(
            app_runtime,
            "123456",
            [],
            "高中",
            "互动模式",
            False,
            "deepseek-v4-flash",
            "high",
            None,
            state0,
            _stage0(),
            None,
            None,
            _fake_request(),
        )
    )
    final_history, _, status_text, _, usage_state, stage_state, _, _ = results[-1]

    assert "过长" in status_text
    assert final_history == []
    assert usage_state == state0
    assert stage_state["stage"] == "initial_hint"
    assert fake_client.calls == 0


def test_respond_escape_keyword_full_solution(monkeypatch, tmp_path, app_runtime):
    fake_client = _FakeClient()
    monkeypatch.setattr(app_runtime, "client", fake_client)
    _use_memory_store(monkeypatch, app_runtime)
    _use_memory_logger(monkeypatch, app_runtime)
    monkeypatch.setattr(app_runtime, "daily_limit", 1_000_000)
    monkeypatch.setattr(app_runtime, "global_daily_limit", 3_000_000)
    _wire_usage_store(monkeypatch, app_runtime)

    results = list(
        app_module.respond(
            app_runtime,
            "没时间了，直接讲答案",
            [],
            "高中",
            "互动模式",
            False,
            "deepseek-v4-flash",
            "high",
            None,
            {"total_tokens": 0, "turns": 0},
            _stage0(),
            None,
            None,
            _fake_request(),
        )
    )
    final_history, _, _, _, _, stage_state, _, _ = results[-1]
    assert stage_state["stage"] == "full_solution"
    assert final_history[-1]["content"] == "你好世界"


def test_respond_practice_keyword_practice_wait(monkeypatch, tmp_path, app_runtime):
    fake_client = _FakeClient()
    monkeypatch.setattr(app_runtime, "client", fake_client)
    _use_memory_store(monkeypatch, app_runtime)
    _use_memory_logger(monkeypatch, app_runtime)
    monkeypatch.setattr(app_runtime, "daily_limit", 1_000_000)
    monkeypatch.setattr(app_runtime, "global_daily_limit", 3_000_000)
    _wire_usage_store(monkeypatch, app_runtime)

    results = list(
        app_module.respond(
            app_runtime,
            "出道题练练",
            [],
            "高中",
            "互动模式",
            False,
            "deepseek-v4-flash",
            "high",
            None,
            {"total_tokens": 0, "turns": 0},
            _stage0(),
            None,
            None,
            _fake_request(),
        )
    )
    _, _, _, _, _, stage_state, _, _ = results[-1]
    assert stage_state["stage"] == "practice_wait"


def test_respond_answer_mode_stage_none(monkeypatch, tmp_path, app_runtime):
    fake_client = _FakeClient()
    monkeypatch.setattr(app_runtime, "client", fake_client)
    _use_memory_store(monkeypatch, app_runtime)
    _use_memory_logger(monkeypatch, app_runtime)
    monkeypatch.setattr(app_runtime, "daily_limit", 1_000_000)
    monkeypatch.setattr(app_runtime, "global_daily_limit", 3_000_000)
    _wire_usage_store(monkeypatch, app_runtime)

    results = list(
        app_module.respond(
            app_runtime,
            "1+1=?",
            [],
            "高中",
            "解答模式",
            False,
            "deepseek-v4-flash",
            "high",
            "完整步骤+答案",
            {"total_tokens": 0, "turns": 0},
            _stage0(),
            None,
            None,
            _fake_request(),
        )
    )
    _, _, _, _, _, stage_state, _, _ = results[-1]
    assert stage_state["stage"] is None
    assert stage_state["last_mode"] == "解答模式"


def test_respond_new_problem_resets_stage(monkeypatch, tmp_path, app_runtime):
    fake_client = _FakeClient()
    monkeypatch.setattr(app_runtime, "client", fake_client)
    _use_memory_store(monkeypatch, app_runtime)
    _use_memory_logger(monkeypatch, app_runtime)
    monkeypatch.setattr(app_runtime, "daily_limit", 1_000_000)
    monkeypatch.setattr(app_runtime, "global_daily_limit", 3_000_000)
    _wire_usage_store(monkeypatch, app_runtime)

    history = [
        {"role": "user", "content": "上一题"},
        {"role": "assistant", "content": "上一题的完整解答"},
    ]
    stage_state = {"stage": "full_solution", "hints": 0, "last_mode": "互动模式"}
    results = list(
        app_module.respond(
            app_runtime,
            "求解方程 x²-5x+6=0",
            history,
            "高中",
            "互动模式",
            False,
            "deepseek-v4-flash",
            "high",
            None,
            {"total_tokens": 0, "turns": 0},
            stage_state,
            None,
            None,
            _fake_request(),
        )
    )
    _, _, _, _, _, new_stage, _, _ = results[-1]
    assert new_stage["stage"] == "initial_hint"
    assert new_stage["hints"] == 0


def test_respond_reasoning_panel_and_status(monkeypatch, tmp_path, app_runtime):
    fake_client = _ReasoningClient()
    monkeypatch.setattr(app_runtime, "client", fake_client)
    _use_memory_store(monkeypatch, app_runtime)
    _use_memory_logger(monkeypatch, app_runtime)
    monkeypatch.setattr(app_runtime, "daily_limit", 1_000_000)
    monkeypatch.setattr(app_runtime, "global_daily_limit", 3_000_000)
    _wire_usage_store(monkeypatch, app_runtime)

    results = list(
        app_module.respond(
            app_runtime,
            "1+1=?",
            [],
            "高中",
            "解答模式",
            True,
            "deepseek-v4-flash",
            "max",
            None,
            {"total_tokens": 0, "turns": 0},
            _stage0(),
            None,
            None,
            _fake_request(),
        )
    )
    # 思考阶段：状态栏含「正在思考」与 token 估算
    thinking_yields = [r for r in results if r[2] and "正在思考" in r[2]]
    assert thinking_yields
    assert "tokens" in thinking_yields[0][2]
    # 思考片段累积显示在 reasoning_md 输出中
    reasoning_texts = [r[6] for r in results if r[6]]
    assert any(
        "先看定义域" in t and "再讨论单调性" in t for t in reasoning_texts
    )
    # 思考面板走公式归一化：尾部未闭合的行内公式被自动补上闭合符
    assert reasoning_texts[-1].endswith("$")
    # 正式回答正常送达
    assert results[-1][0][-1]["content"] == "结论"


def test_continue_last_answer(monkeypatch, tmp_path, app_runtime):
    fake_client = _FakeClient()
    monkeypatch.setattr(app_runtime, "client", fake_client)
    _use_memory_store(monkeypatch, app_runtime)
    _use_memory_logger(monkeypatch, app_runtime)
    monkeypatch.setattr(app_runtime, "daily_limit", 1_000_000)
    monkeypatch.setattr(app_runtime, "global_daily_limit", 3_000_000)
    _wire_usage_store(monkeypatch, app_runtime)

    history = [
        {"role": "user", "content": "求导"},
        {"role": "assistant", "content": "先写一半"},
    ]
    results = list(
        app_module.continue_last_answer(
            app_runtime,
            history,
            "高中",
            "互动模式",
            False,
            "deepseek-v4-flash",
            "high",
            None,
            {"total_tokens": 0, "turns": 0},
            _stage0(),
            None,
            None,
            _fake_request(),
        )
    )
    final_history, _, _, _, _, _, _, _ = results[-1]
    last_user = next(
        m["content"] for m in reversed(final_history) if m["role"] == "user"
    )
    assert "求导" in last_user and "继续" in last_user
    assert final_history[-1]["role"] == "assistant"
    assert final_history[-1]["content"] == "你好世界"



def test_continue_last_answer_keeps_stage(monkeypatch, tmp_path, app_runtime):
    fake_client = _FakeClient()
    monkeypatch.setattr(app_runtime, "client", fake_client)
    _use_memory_store(monkeypatch, app_runtime)
    _use_memory_logger(monkeypatch, app_runtime)
    monkeypatch.setattr(app_runtime, "daily_limit", 1_000_000)
    monkeypatch.setattr(app_runtime, "global_daily_limit", 3_000_000)
    _wire_usage_store(monkeypatch, app_runtime)

    history = [
        {"role": "user", "content": "出道题"},
        {"role": "assistant", "content": "题目写到一半"},
    ]
    results = list(
        app_module.continue_last_answer(
            app_runtime,
            history,
            "高中",
            "互动模式",
            False,
            "deepseek-v4-flash",
            "high",
            None,
            {"total_tokens": 0, "turns": 0},
            {"stage": "practice_wait", "hints": 0, "last_mode": "互动模式"},
            None,
            None,
            _fake_request(),
        )
    )
    assert results[-1][5]["stage"] == "practice_wait"


def test_continue_last_answer_no_history(app_runtime):
    results = list(
        app_module.continue_last_answer(
            app_runtime,
            [],
            "高中",
            "互动模式",
            False,
            "deepseek-v4-flash",
            "high",
            None,
            {"total_tokens": 0, "turns": 0},
            _stage0(),
            None,
            None,
            _fake_request(),
        )
    )
    assert "还没有可继续" in results[-1][2]


def test_respond_empty_answer_still_records_usage(monkeypatch, tmp_path, app_runtime):
    class EmptyAnswerUsage:
        total_tokens = 123
        prompt_tokens = 100
        completion_tokens = 23

    class EmptyAnswerClient:
        def chat_stream_with_usage(
            self, messages, thinking=None, reasoning_effort=None, model=None
        ):
            yield None, "正在分析题目", None, None
            yield None, None, EmptyAnswerUsage(), "length"

    store = _use_memory_store(monkeypatch, app_runtime)
    monkeypatch.setattr(app_runtime, "client", EmptyAnswerClient())
    monkeypatch.setattr(app_runtime, "clients", {})
    _use_memory_logger(monkeypatch, app_runtime)
    monkeypatch.setattr(app_runtime, "daily_limit", 1_000_000)
    monkeypatch.setattr(app_runtime, "global_daily_limit", 3_000_000)
    usage_store = _wire_usage_store(monkeypatch, app_runtime)

    results = list(
        app_module.respond(
            app_runtime,
            "1+1=?",
            [],
            "高中",
            "互动模式",
            True,
            "测试模型",
            "high",
            None,
            usage_module.new_usage_state(),
            _stage0(),
            None,
            None,
            _fake_request(),
        )
    )
    final_history, _, status, _, state, _, _, _ = results[-1]

    assert final_history[-1]["content"] == "（未收到正式回答）"
    # 空回复不保存为助手回答，也没有任何保存尝试
    assert store._messages.get("user1") is None
    assert store.save_attempts == 0
    assert "没能生成正式回答" in status
    assert state["turns"] == 1
    assert state["prompt_tokens"] == 100
    assert state["completion_tokens"] == 23
    assert state["total_tokens"] == 123

    today = __import__("datetime").date.today().isoformat()
    assert usage_store.get_used(today, "user1") == 123
    assert usage_store.get_total(today) == 123


# 没有返回usage，但是输出部分回复
def test_respond_stream_error_keeps_partial_and_estimates_usage(
    monkeypatch, tmp_path, app_runtime
):
    partial_answer = "已经生成的部分回答"
    logger = _use_memory_logger(monkeypatch, app_runtime)

    class InterruptedClient:
        def __init__(self):
            self.calls = 0

        def chat_stream_with_usage(
            self, messages, thinking=None, reasoning_effort=None, model=None
        ):
            self.calls += 1
            yield partial_answer, None, None, None
            raise RuntimeError("模拟流式中断")

    fake_client = InterruptedClient()
    store = _use_memory_store(monkeypatch, app_runtime)

    monkeypatch.setattr(app_runtime, "client", fake_client)
    monkeypatch.setattr(app_runtime, "clients", {})
    monkeypatch.setattr(app_runtime, "daily_limit", 1_000_000)
    monkeypatch.setattr(app_runtime, "global_daily_limit", 3_000_000)
    monkeypatch.setattr(app_runtime, "max_message_len", 2000)
    usage_store = _wire_usage_store(monkeypatch, app_runtime)

    results = list(
        app_module.respond(
            app_runtime,
            "1+1=?",
            [],
            "高中",
            "互动模式",
            False,
            "测试模型",
            "high",
            None,
            usage_module.new_usage_state(),
            _stage0(),
            None,
            None,
            _fake_request(),
        )
    )

    final_history, _, status, _, state, _, _, _ = results[-1]
    expected_tokens = usage_module.estimate_tokens(partial_answer)

    # 中断前的回答仍然出现在最终历史中，并保存一次。
    assert final_history == [
        {"role": "user", "content": "1+1=?"},
        {"role": "assistant", "content": partial_answer},
    ]
    assert store._messages["user1"] == final_history
    assert store.save_attempts == 1
    assert "已保留生成的部分内容" in status

    # 没收到模型用量时，只估算部分回答的总用量。
    assert state["total_tokens"] == expected_tokens
    assert state["prompt_tokens"] == 0
    assert state["completion_tokens"] == 0
    assert state["turns"] == 1

    # 账号和全局每日用量都只增加一次。
    today = __import__("datetime").date.today().isoformat()
    assert (
        usage_store.get_used(today, "user1")
        == expected_tokens
    )
    assert (
        usage_store.get_total(today)
        == expected_tokens
    )

    # 确认走了异常分支，没有重新调用模型。
    assert fake_client.calls == 1
    assert len(logger.errors) == 1
    exc, details = logger.errors[0]
    assert isinstance(exc, RuntimeError)
    assert details["partial"] is True
    assert details["turn_total"] == expected_tokens


# 成功返回usage,随后流式中断
def test_respond_stream_error_uses_received_usage(monkeypatch, tmp_path, app_runtime):
    partial_answer = "已经生成的部分回答"
    store = _use_memory_store(monkeypatch, app_runtime)

    class ReceivedUsage:
        total_tokens = 123
        prompt_tokens = 100
        completion_tokens = 23

    class InterruptedClient:
        def chat_stream_with_usage(
            self, messages, thinking=None, reasoning_effort=None, model=None
        ):
            yield partial_answer, None, None, None
            yield None, None, ReceivedUsage(), None
            raise RuntimeError("收到用量后模拟中断")

    def unexpected_estimate(text):
        raise AssertionError("已有模型用量，不应该再估算")

    monkeypatch.setattr(app_runtime, "client", InterruptedClient())
    monkeypatch.setattr(app_runtime, "clients", {})
    monkeypatch.setattr(
        app_module.chat_service,
        "estimate_tokens",
        unexpected_estimate,
    )
    _use_memory_logger(monkeypatch, app_runtime)
    monkeypatch.setattr(app_runtime, "daily_limit", 1_000_000)
    monkeypatch.setattr(app_runtime, "global_daily_limit", 3_000_000)
    monkeypatch.setattr(app_runtime, "max_message_len", 2000)
    usage_store = _wire_usage_store(monkeypatch, app_runtime)

    results = list(
        app_module.respond(
            app_runtime,
            "1+1=?",
            [],
            "高中",
            "互动模式",
            False,
            "测试模型",
            "high",
            None,
            usage_module.new_usage_state(),
            _stage0(),
            None,
            None,
            _fake_request(),
        )
    )

    final_history, _, status, _, state, _, _, _ = results[-1]

    assert final_history[-1] == {
        "role": "assistant",
        "content": partial_answer,
    }
    assert store._messages["user1"] == final_history
    assert store.save_attempts == 1
    assert "已保留生成的部分内容" in status

    assert state["turns"] == 1
    assert state["total_tokens"] == 123
    assert state["prompt_tokens"] == 100
    assert state["completion_tokens"] == 23

    today = __import__("datetime").date.today().isoformat()
    assert usage_store.get_used(today, "user1") == 123
    assert usage_store.get_total(today) == 123


# 没有输出正式回复，也没有返回usage就报错了
def test_respond_stream_error_without_answer_or_usage(monkeypatch, tmp_path, app_runtime):
    logger = _use_memory_logger(monkeypatch, app_runtime)
    store = _use_memory_store(monkeypatch, app_runtime)

    class FailedClient:
        def chat_stream_with_usage(
            self, messages, thinking=None, reasoning_effort=None, model=None
        ):
            yield None, "正在分析题目", None, None
            raise RuntimeError("正式回答前模拟中断")

    monkeypatch.setattr(app_runtime, "client", FailedClient())
    monkeypatch.setattr(app_runtime, "clients", {})
    monkeypatch.setattr(app_runtime, "daily_limit", 1_000_000)
    monkeypatch.setattr(app_runtime, "global_daily_limit", 3_000_000)
    monkeypatch.setattr(app_runtime, "max_message_len", 2000)
    usage_store = _wire_usage_store(monkeypatch, app_runtime)

    results = list(
        app_module.respond(
            app_runtime,
            "1+1=?",
            [],
            "高中",
            "互动模式",
            False,
            "测试模型",
            "high",
            None,
            usage_module.new_usage_state(),
            _stage0(),
            None,
            None,
            _fake_request(),
        )
    )

    final_history, _, status, _, state, _, reasoning, _ = results[-1]

    assert final_history == [
        {"role": "user", "content": "1+1=?"},
    ]
    # 没有正式回答：没有任何保存尝试
    assert store._messages.get("user1") is None
    assert store.save_attempts == 0
    # 这个场景没有每日用量记录
    assert usage_store.add_usage_calls == []
    assert status
    assert "已保留生成的部分内容" not in status
    assert reasoning == "正在分析题目"

    assert state["turns"] == 1
    assert state["total_tokens"] == 0
    assert state["prompt_tokens"] == 0
    assert state["completion_tokens"] == 0

    assert len(logger.errors) == 1
    exc, details = logger.errors[0]
    assert isinstance(exc, RuntimeError)
    assert details["partial"] is False
    assert details["turn_total"] is None

    today = __import__("datetime").date.today().isoformat()
    assert usage_store.get_used(today, "user1") == 0
    assert usage_store.get_total(today) == 0


@pytest.mark.parametrize("reports_usage", [True, False])
def test_respond_truncated_answer_is_saved_and_shows_continue_hint(
    monkeypatch, tmp_path, reports_usage, app_runtime
):
    store = _use_memory_store(monkeypatch, app_runtime)

    class TruncatedClient:
        def chat_stream_with_usage(
            self, messages, thinking=None, reasoning_effort=None, model=None
        ):
            yield "部分答案", None, None, None
            yield None, None, _FakeUsage() if reports_usage else None, "length"

    monkeypatch.setattr(app_runtime, "client", TruncatedClient())
    monkeypatch.setattr(app_runtime, "clients", {})
    _use_memory_logger(monkeypatch, app_runtime)
    monkeypatch.setattr(app_runtime, "daily_limit", 1_000_000)
    monkeypatch.setattr(app_runtime, "global_daily_limit", 3_000_000)
    usage_store = _wire_usage_store(monkeypatch, app_runtime)

    results = list(
        app_module.respond(
            app_runtime,
            "1+1=?",
            [],
            "高中",
            "互动模式",
            False,
            "测试模型",
            "high",
            None,
            usage_module.new_usage_state(),
            _stage0(),
            None,
            None,
            _fake_request(),
        )
    )

    final_history, _, status, _, state, _, _, _ = results[-1]
    assert final_history == [
        {"role": "user", "content": "1+1=?"},
        {"role": "assistant", "content": "部分答案"},
    ]
    assert store._messages["user1"] == final_history
    assert store.save_attempts == 1
    assert "截断" in status and "继续" in status
    if not reports_usage:
        assert "0 tokens" not in status
    assert state["turns"] == 1

    today = __import__("datetime").date.today().isoformat()
    expected_total = 123 if reports_usage else 0
    assert state["total_tokens"] == expected_total
    # 每日只尝试记录一次
    assert usage_store.add_usage_calls == (
        [(today, "user1", 123)] if reports_usage else []
    )
    assert usage_store.get_used(today, "user1") == expected_total
    assert usage_store.get_total(today) == expected_total


def test_respond_save_failure_still_shows_answer_and_settles_usage(
    monkeypatch, tmp_path, app_runtime
):
    """正常回答保存失败：回答仍显示，用量结算一次，不重试模型。"""
    fake_client = _FakeClient()
    store = FailingSaveStore()
    monkeypatch.setattr(app_runtime, "client", fake_client)
    monkeypatch.setattr(app_runtime, "history_store", store)
    _use_memory_logger(monkeypatch, app_runtime)
    monkeypatch.setattr(app_runtime, "daily_limit", 1_000_000)
    monkeypatch.setattr(app_runtime, "global_daily_limit", 3_000_000)
    usage_store = _wire_usage_store(monkeypatch, app_runtime)

    results = list(
        app_module.respond(
            app_runtime,
            "1+1=?",
            [],
            "高中",
            "互动模式",
            False,
            "测试模型",
            "high",
            None,
            usage_module.new_usage_state(),
            _stage0(),
            None,
            None,
            _fake_request(),
        )
    )
    final_history, _, status, _, state, _, _, _ = results[-1]

    assert final_history[-1]["content"] == "你好世界"
    assert "历史保存失败，刷新后可能丢失" in status
    assert "已保存" not in status
    assert fake_client.calls == 1  # 不重新调用模型、不自动重试
    assert store.save_attempts == 1  # 只尝试保存一次

    # 用量仍结算一次：会话与每日用量都正常累计
    assert state["turns"] == 1
    assert state["total_tokens"] == 123
    today = __import__("datetime").date.today().isoformat()
    assert usage_store.get_used(today, "user1") == 123
    assert usage_store.get_total(today) == 123

    # 保存失败不增加次数：同一标记仍可成功保存
    store.fail = False
    assert store.save([{"role": "user", "content": "探针"}], "user1", 0)


def test_respond_log_write_failure_still_saves_and_settles(monkeypatch, tmp_path, app_runtime):
    """日志写入失败（数据目录被文件占用）不影响回答保存、显示与用量结算。"""
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("占用数据目录位置的普通文件", encoding="utf-8")
    monkeypatch.setattr(app_runtime, "event_logger", FileEventLogger(blocker))
    fake_client = _FakeClient()
    store = _use_memory_store(monkeypatch, app_runtime)
    monkeypatch.setattr(app_runtime, "client", fake_client)
    monkeypatch.setattr(app_runtime, "daily_limit", 1_000_000)
    monkeypatch.setattr(app_runtime, "global_daily_limit", 3_000_000)
    usage_store = _wire_usage_store(monkeypatch, app_runtime)

    results = list(
        app_module.respond(
            app_runtime,
            "1+1=?",
            [],
            "高中",
            "互动模式",
            False,
            "测试模型",
            "high",
            None,
            usage_module.new_usage_state(),
            _stage0(),
            None,
            None,
            _fake_request(),
        )
    )
    final_history, _, status, _, state, _, _, _ = results[-1]

    # 聊天照常完成：回答保存、显示，会话与每日用量正常结算
    assert final_history[-1]["content"] == "你好世界"
    assert store._messages["user1"] == final_history
    assert fake_client.calls == 1
    assert state["turns"] == 1
    assert state["total_tokens"] == 123
    today = __import__("datetime").date.today().isoformat()
    assert usage_store.get_used(today, "user1") == 123
    assert usage_store.get_total(today) == 123
    # 日志写入一直失败，但未产生任何日志文件
    assert not (blocker / "chat.log").exists()


def test_respond_stale_save_shows_not_saved_and_settles_usage(
    monkeypatch, tmp_path, app_runtime
):
    """保存标记过期：跳过保存但回答仍显示、用量仍结算一次。"""
    fake_client = _FakeClient()
    store = StaleSaveStore()
    monkeypatch.setattr(app_runtime, "client", fake_client)
    monkeypatch.setattr(app_runtime, "history_store", store)
    _use_memory_logger(monkeypatch, app_runtime)
    monkeypatch.setattr(app_runtime, "daily_limit", 1_000_000)
    monkeypatch.setattr(app_runtime, "global_daily_limit", 3_000_000)
    usage_store = _wire_usage_store(monkeypatch, app_runtime)

    results = list(
        app_module.respond(
            app_runtime,
            "1+1=?",
            [],
            "高中",
            "互动模式",
            False,
            "测试模型",
            "high",
            None,
            usage_module.new_usage_state(),
            _stage0(),
            None,
            None,
            _fake_request(),
        )
    )
    final_history, _, status, _, state, _, _, _ = results[-1]

    assert final_history[-1]["content"] == "你好世界"
    assert "另一个操作已更新历史，本次回答未保存" in status
    assert fake_client.calls == 1
    assert store.save_attempts == 1
    assert state["turns"] == 1
    assert state["total_tokens"] == 123
    today = __import__("datetime").date.today().isoformat()
    assert usage_store.get_used(today, "user1") == 123
    assert usage_store.get_total(today) == 123


def test_respond_stream_error_save_failure_still_shows_partial(
    monkeypatch, tmp_path, app_runtime
):
    """异常前的部分回答保存失败：部分内容仍显示，用量按估算结算一次。"""
    partial_answer = "已经生成的部分回答"

    class InterruptedClient:
        def __init__(self):
            self.calls = 0

        def chat_stream_with_usage(
            self, messages, thinking=None, reasoning_effort=None, model=None
        ):
            self.calls += 1
            yield partial_answer, None, None, None
            raise RuntimeError("模拟流式中断")

    fake_client = InterruptedClient()
    store = FailingSaveStore()
    monkeypatch.setattr(app_runtime, "client", fake_client)
    monkeypatch.setattr(app_runtime, "clients", {})
    monkeypatch.setattr(app_runtime, "history_store", store)
    _use_memory_logger(monkeypatch, app_runtime)
    monkeypatch.setattr(app_runtime, "daily_limit", 1_000_000)
    monkeypatch.setattr(app_runtime, "global_daily_limit", 3_000_000)
    monkeypatch.setattr(app_runtime, "max_message_len", 2000)
    usage_store = _wire_usage_store(monkeypatch, app_runtime)

    results = list(
        app_module.respond(
            app_runtime,
            "1+1=?",
            [],
            "高中",
            "互动模式",
            False,
            "测试模型",
            "high",
            None,
            usage_module.new_usage_state(),
            _stage0(),
            None,
            None,
            _fake_request(),
        )
    )
    final_history, _, status, _, state, _, _, _ = results[-1]

    assert final_history[-1]["content"] == partial_answer
    # 保存失败时不再出现"已保留"的成功说明，明确未写入历史
    assert "已保留生成的部分内容" not in status
    assert "未写入历史" in status
    # 原模型异常提示不被掩盖，同时给出保存失败提示
    assert "历史保存失败，刷新后可能丢失" in status
    assert fake_client.calls == 1  # 不重新调用模型
    assert store.save_attempts == 1  # 只尝试保存一次

    expected_tokens = usage_module.estimate_tokens(partial_answer)
    assert state["turns"] == 1
    assert state["total_tokens"] == expected_tokens
    today = __import__("datetime").date.today().isoformat()
    assert (
        usage_store.get_used(today, "user1")
        == expected_tokens
    )
    assert (
        usage_store.get_total(today) == expected_tokens
    )


def test_respond_stream_error_stale_save_shows_not_saved(monkeypatch, tmp_path, app_runtime):
    """异常部分回答遇到保存冲突：不出现保存成功说明，用量按估算结算一次。"""
    partial_answer = "已经生成的部分回答"

    class InterruptedClient:
        def __init__(self):
            self.calls = 0

        def chat_stream_with_usage(
            self, messages, thinking=None, reasoning_effort=None, model=None
        ):
            self.calls += 1
            yield partial_answer, None, None, None
            raise RuntimeError("模拟流式中断")

    fake_client = InterruptedClient()
    store = StaleSaveStore()
    monkeypatch.setattr(app_runtime, "client", fake_client)
    monkeypatch.setattr(app_runtime, "clients", {})
    monkeypatch.setattr(app_runtime, "history_store", store)
    _use_memory_logger(monkeypatch, app_runtime)
    monkeypatch.setattr(app_runtime, "daily_limit", 1_000_000)
    monkeypatch.setattr(app_runtime, "global_daily_limit", 3_000_000)
    monkeypatch.setattr(app_runtime, "max_message_len", 2000)
    usage_store = _wire_usage_store(monkeypatch, app_runtime)

    results = list(
        app_module.respond(
            app_runtime,
            "1+1=?",
            [],
            "高中",
            "互动模式",
            False,
            "测试模型",
            "high",
            None,
            usage_module.new_usage_state(),
            _stage0(),
            None,
            None,
            _fake_request(),
        )
    )
    final_history, _, status, _, state, _, _, _ = results[-1]

    assert final_history[-1]["content"] == partial_answer  # 回答仍显示
    # 冲突时不出现保存成功说明，明确未写入历史
    assert "已保留生成的部分内容" not in status
    assert "未写入历史" in status
    assert "另一个操作已更新历史，本次回答未保存" in status
    assert fake_client.calls == 1  # 不重新调用模型
    assert store.save_attempts == 1  # 只尝试保存一次

    expected_tokens = usage_module.estimate_tokens(partial_answer)
    assert state["turns"] == 1
    assert state["total_tokens"] == expected_tokens
    today = __import__("datetime").date.today().isoformat()
    assert (
        usage_store.get_used(today, "user1")
        == expected_tokens
    )


def test_respond_truncated_save_failure_shows_both_hints(monkeypatch, tmp_path, app_runtime):
    """截断回答保存失败：截断提示与保存失败提示同时出现，用量结算一次。"""

    class TruncatedClient:
        def chat_stream_with_usage(
            self, messages, thinking=None, reasoning_effort=None, model=None
        ):
            yield "部分答案", None, None, None
            yield None, None, _FakeUsage(), "length"

    monkeypatch.setattr(app_runtime, "client", TruncatedClient())
    monkeypatch.setattr(app_runtime, "clients", {})
    store = FailingSaveStore()
    monkeypatch.setattr(app_runtime, "history_store", store)
    _use_memory_logger(monkeypatch, app_runtime)
    monkeypatch.setattr(app_runtime, "daily_limit", 1_000_000)
    monkeypatch.setattr(app_runtime, "global_daily_limit", 3_000_000)
    usage_store = _wire_usage_store(monkeypatch, app_runtime)

    results = list(
        app_module.respond(
            app_runtime,
            "1+1=?",
            [],
            "高中",
            "互动模式",
            False,
            "测试模型",
            "high",
            None,
            usage_module.new_usage_state(),
            _stage0(),
            None,
            None,
            _fake_request(),
        )
    )
    final_history, _, status, _, state, _, _, _ = results[-1]

    assert final_history[-1]["content"] == "部分答案"
    assert "截断" in status and "继续" in status
    assert "历史保存失败，刷新后可能丢失" in status
    assert store.save_attempts == 1
    assert state["turns"] == 1
    today = __import__("datetime").date.today().isoformat()
    # 每日照常记录一次
    assert usage_store.add_usage_calls == [(today, "user1", 123)]
    assert usage_store.get_used(today, "user1") == 123


def _patch_daily_env(monkeypatch, daily_store, client, app_runtime):
    """为每日失败场景准备隔离环境，返回 (内存日志对象, 历史假存储)。

    每日存储由调用方按场景给定；日志对象保留访问事件供断言失败留痕。
    """
    history_store = _use_memory_store(monkeypatch, app_runtime)
    monkeypatch.setattr(app_runtime, "client", client)
    monkeypatch.setattr(app_runtime, "daily_usage_store", daily_store)
    logger = _use_memory_logger(monkeypatch, app_runtime)
    monkeypatch.setattr(app_runtime, "daily_limit", 1_000_000)
    monkeypatch.setattr(app_runtime, "global_daily_limit", 3_000_000)
    monkeypatch.setattr(app_runtime, "max_message_len", 2000)
    return logger, history_store


def _run_respond_once(
    app_runtime,
    message: str = "1+1=?",
    history: list[dict] | None = None,
    thinking: bool = False,
    usage_state: dict | None = None,
):
    """用固定的默认参数驱动一轮 respond，返回最终一次 yield 的八元组。"""
    results = list(
        app_module.respond(
            app_runtime,
            message,
            history if history is not None else [],
            "高中",
            "互动模式",
            thinking,
            "测试模型",
            "high",
            None,
            (
                usage_state
                if usage_state is not None
                else usage_module.new_usage_state()
            ),
            _stage0(),
            None,
            None,
            _fake_request(),
        )
    )
    return results[-1]


def test_respond_quota_read_failure_skips_model(monkeypatch, tmp_path, app_runtime):
    """聊天开始时无法读取额度：明确提示并结束请求，不调用模型。"""
    from core.usage_store import DailyUsageStore

    corrupt = tmp_path / "usage_daily.json"
    corrupt.write_text("{损坏", encoding="utf-8")
    fake_client = _FakeClient()
    logger, _store = _patch_daily_env(
        monkeypatch, DailyUsageStore(corrupt), fake_client, app_runtime
    )

    state0 = {"total_tokens": 0, "turns": 0}
    final_history, _, status, _, usage_state, stage_state, _, _ = (
        _run_respond_once(app_runtime, usage_state=state0)
    )

    assert "每日额度暂时无法检查" in status
    assert final_history == []
    assert usage_state == state0
    assert fake_client.calls == 0  # 不调用模型
    assert any("额度检查失败" in str(args) for args in logger.access_events)
    # 损坏文件不被覆盖
    assert corrupt.read_text(encoding="utf-8") == "{损坏"


def test_respond_daily_write_failure_still_completes(monkeypatch, tmp_path, app_runtime):
    """正常回答遇到每日写入失败：回答照常保存与显示，提示每日用量未记录成功。"""
    fake_client = _FakeClient()
    usage_store = FailingDailyUsageStore()
    logger, store = _patch_daily_env(monkeypatch, usage_store, fake_client, app_runtime)

    final_history, _, status, usage_text, state, _, _, _ = _run_respond_once(app_runtime)

    # 回答仍保存并显示，会话结算照常完成，不重新调用模型
    assert final_history[-1]["content"] == "你好世界"
    assert store._messages["user1"] == final_history
    assert state["turns"] == 1
    assert state["total_tokens"] == 123
    assert fake_client.calls == 1
    # 只尝试记录一次，未成功
    today = __import__("datetime").date.today().isoformat()
    assert usage_store.add_usage_calls == [(today, "user1", 123)]
    assert usage_store.get_used(today, "user1") == 0
    # 明确提示，且不显示"今日已用"的假结果
    assert "每日用量未记录成功" in status
    assert "今日已用" not in usage_text


def test_respond_truncated_daily_write_failure_keeps_hints(
    monkeypatch, tmp_path, app_runtime
):
    """截断回答遇到每日写入失败：截断提示不被覆盖，同时提示每日未记录。"""

    class TruncatedClient:
        def chat_stream_with_usage(
            self, messages, thinking=None, reasoning_effort=None, model=None
        ):
            yield "部分答案", None, None, None
            yield None, None, _FakeUsage(), "length"

    usage_store = FailingDailyUsageStore()
    logger, store = _patch_daily_env(monkeypatch, usage_store, TruncatedClient(), app_runtime)

    final_history, _, status, _, state, _, _, _ = _run_respond_once(app_runtime)

    assert final_history[-1]["content"] == "部分答案"
    # 截断提示与每日失败提示同时出现，互不掩盖
    assert "截断" in status and "继续" in status
    assert "每日用量未记录成功" in status
    assert state["turns"] == 1
    assert state["total_tokens"] == 123
    today = __import__("datetime").date.today().isoformat()
    assert usage_store.add_usage_calls == [(today, "user1", 123)]


def test_respond_stream_error_daily_write_failure_still_finishes(
    monkeypatch, tmp_path, app_runtime
):
    """异常部分回答遇到每日写入失败：按估算结算一次并完整结束，不重试。"""
    partial_answer = "已经生成的部分回答"

    class InterruptedClient:
        def __init__(self):
            self.calls = 0

        def chat_stream_with_usage(
            self, messages, thinking=None, reasoning_effort=None, model=None
        ):
            self.calls += 1
            yield partial_answer, None, None, None
            raise RuntimeError("模拟流式中断")

    fake_client = InterruptedClient()
    usage_store = FailingDailyUsageStore()
    logger, store = _patch_daily_env(monkeypatch, usage_store, fake_client, app_runtime)
    expected_tokens = usage_module.estimate_tokens(partial_answer)

    final_history, _, status, _, state, _, _, _ = _run_respond_once(app_runtime)

    # 部分回答仍保存并显示，原模型异常提示保留
    assert final_history[-1]["content"] == partial_answer
    assert store._messages["user1"] == final_history
    assert "已保留生成的部分内容" in status
    assert "每日用量未记录成功" in status
    assert fake_client.calls == 1
    # 只尝试记录一次；会话按估算结算一次
    today = __import__("datetime").date.today().isoformat()
    assert usage_store.add_usage_calls == [(today, "user1", expected_tokens)]
    assert state["turns"] == 1
    assert state["total_tokens"] == expected_tokens


def test_respond_empty_answer_daily_write_failure_still_finishes(
    monkeypatch, tmp_path, app_runtime
):
    """空回答遇到每日写入失败：临时提示不保存，仍提示每日未记录。"""

    class EmptyAnswerUsage:
        total_tokens = 123

    class EmptyAnswerClient:
        def chat_stream_with_usage(
            self, messages, thinking=None, reasoning_effort=None, model=None
        ):
            yield None, "正在分析题目", None, None
            yield None, None, EmptyAnswerUsage(), "length"

    usage_store = FailingDailyUsageStore()
    logger, store = _patch_daily_env(monkeypatch, usage_store, EmptyAnswerClient(), app_runtime)

    final_history, _, status, _, state, _, _, _ = _run_respond_once(app_runtime, thinking=True)

    # 空回答的临时提示不保存为助手回答
    assert final_history[-1]["content"] == "（未收到正式回答）"
    assert store._messages.get("user1") is None
    assert store.save_attempts == 0
    assert "没能生成正式回答" in status
    assert "每日用量未记录成功" in status
    # 用量仍结算一次，每日只尝试记录一次
    assert state["turns"] == 1
    assert state["total_tokens"] == 123
    today = __import__("datetime").date.today().isoformat()
    assert usage_store.add_usage_calls == [(today, "user1", 123)]


def test_respond_question_count_failure_does_not_break_chat(
    monkeypatch, tmp_path, app_runtime
):
    """提问次数记录失败不打断聊天，留下明确失败信息。"""

    class FailingQuestionStore(CountingDailyUsageStore):
        def add_question(self, date):
            raise OSError("模拟提问次数写盘失败")

    usage_store = FailingQuestionStore()
    logger, store = _patch_daily_env(monkeypatch, usage_store, _FakeClient(), app_runtime)

    final_history, _, status, _, state, _, _, _ = _run_respond_once(app_runtime)

    # 聊天照常完成：回答显示、历史保存、每日用量正常记录
    assert final_history[-1]["content"] == "你好世界"
    assert store._messages["user1"] == final_history
    today = __import__("datetime").date.today().isoformat()
    assert usage_store.get_used(today, "user1") == 123
    assert usage_store.get_questions(today) == 0
    assert any("提问次数记录失败" in str(args) for args in logger.access_events)


def test_respond_daily_recorded_but_global_refresh_fails(monkeypatch, tmp_path, app_runtime):
    """写入成功、全局统计刷新失败：保留已记录用量，提示统计暂不可用。"""
    from core.usage_store import DailyUsageReadError

    class GlobalRefreshFailsStore(CountingDailyUsageStore):
        """第一次 get_total（开始时的额度检查）正常，之后的统计读取失败。"""

        def __init__(self):
            super().__init__()
            self.total_reads = 0

        def get_total(self, date):
            self.total_reads += 1
            if self.total_reads > 1:
                raise DailyUsageReadError("模拟全局统计读取失败")
            return super().get_total(date)

    fake_client = _FakeClient()
    usage_store = GlobalRefreshFailsStore()
    logger, store = _patch_daily_env(monkeypatch, usage_store, fake_client, app_runtime)

    final_history, _, status, usage_text, state, _, _, _ = _run_respond_once(app_runtime)

    # 回答仍保存和显示，不重新调用模型
    assert final_history[-1]["content"] == "你好世界"
    assert store._messages["user1"] == final_history
    assert fake_client.calls == 1
    # 实际用量只记入一次，并保留已写入结果
    today = __import__("datetime").date.today().isoformat()
    assert usage_store.add_usage_calls == [(today, "user1", 123)]
    assert usage_store.get_used(today, "user1") == 123
    # 会话只结算一次
    assert state["turns"] == 1
    assert state["total_tokens"] == 123
    # 提示统计刷新失败，不出现"每日用量未记录成功"
    assert "每日用量已记录，但全局统计暂时无法刷新" in status
    assert "每日用量未记录成功" not in status
    # 全局统计不可用：只显示账号累计，不格式化 None、不输出完整正常统计行
    assert "今日已用 123 / 1,000,000（本账号）" in usage_text
    assert "全局" not in usage_text
    # 日志区分两种失败
    assert any("全局统计刷新失败" in str(args) for args in logger.access_events)
    assert not any("每日用量记录失败" in str(args) for args in logger.access_events)


def test_respond_no_client_preserves_startup_hint(monkeypatch, app_runtime):
    import datetime

    daily_store = CountingDailyUsageStore()
    _logger, history_store = _patch_daily_env(
        monkeypatch, daily_store, None, app_runtime
    )
    monkeypatch.setattr(app_runtime, "clients", {})
    monkeypatch.setattr(
        app_runtime, "startup_hint", "测试：客户端未配置"
    )

    initial_state = usage_module.new_usage_state()
    initial_state.update(total_tokens=10, turns=2)

    (
        final_history,
        cleared_input,
        status,
        usage_text,
        state,
        stage,
        reasoning,
        image,
    ) = _run_respond_once(app_runtime, usage_state=initial_state)

    assert final_history == [
        {"role": "user", "content": "1+1=?"},
        {"role": "assistant", "content": "⚠️ 测试：客户端未配置"},
    ]
    assert cleared_input == status == usage_text == reasoning == ""
    assert state == initial_state
    assert stage["stage"] == "initial_hint"
    assert image == {"path": None, "fresh": False}

    assert history_store.save_attempts == 0
    assert history_store.load("user1") == []
    assert daily_store.add_usage_calls == []

    today = datetime.date.today().isoformat()
    assert daily_store.get_questions(today) == 1
