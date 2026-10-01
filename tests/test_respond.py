# -*- coding: utf-8 -*-
"""respond 主流程测试（mock 客户端与用量文件）。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module
from conftest import FailingSaveStore, MemoryHistoryStore, StaleSaveStore
from core import usage as usage_module


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


def _use_memory_store(monkeypatch) -> MemoryHistoryStore:
    """用内存假存储替换文件存储，返回实例供断言。"""
    store = MemoryHistoryStore()
    monkeypatch.setattr(app_module, "history_store", store)
    return store


def _stage0():
    return {"stage": "initial_hint", "hints": 0, "last_mode": "互动模式"}


def _wire_usage_to_tmp(monkeypatch, tmp_path):
    p = tmp_path / "u.json"
    monkeypatch.setattr(
        app_module,
        "get_daily_used",
        lambda d, ip: usage_module.get_daily_used(d, ip, path=p),
    )
    monkeypatch.setattr(
        app_module,
        "get_daily_total",
        lambda d: usage_module.get_daily_total(d, path=p),
    )
    monkeypatch.setattr(
        app_module,
        "add_daily_usage",
        lambda d, ip, t: usage_module.add_daily_usage(d, ip, t, path=p),
    )
    monkeypatch.setattr(
        app_module,
        "add_daily_question",
        lambda d: usage_module.add_daily_question(d, path=p),
    )
    return p


def test_respond_success_path(monkeypatch, tmp_path):
    fake_client = _FakeClient()
    store = _use_memory_store(monkeypatch)
    monkeypatch.setattr(app_module, "client", fake_client)
    monkeypatch.setattr(app_module, "_log_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "log_access_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "DAILY_LIMIT", 1_000_000)
    monkeypatch.setattr(app_module, "GLOBAL_DAILY_LIMIT", 3_000_000)
    p = _wire_usage_to_tmp(monkeypatch, tmp_path)

    results = list(
        app_module.respond(
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
    assert usage_module.get_daily_used(today, "user1", path=p) == 123
    assert usage_module.get_daily_total(today, path=p) == 123


def test_respond_quota_rejected(monkeypatch, tmp_path):
    fake_client = _FakeClient()
    monkeypatch.setattr(app_module, "client", fake_client)
    _use_memory_store(monkeypatch)
    monkeypatch.setattr(app_module, "_log_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "log_access_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "DAILY_LIMIT", 1_000_000)
    monkeypatch.setattr(app_module, "GLOBAL_DAILY_LIMIT", 3_000_000)
    monkeypatch.setattr(
        app_module, "get_daily_used", lambda d, ip: 1_000_000
    )
    monkeypatch.setattr(app_module, "get_daily_total", lambda d: 0)

    state0 = {"total_tokens": 0, "turns": 0}
    results = list(
        app_module.respond(
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


def test_respond_overlong_message_rejected(monkeypatch):
    fake_client = _FakeClient()
    monkeypatch.setattr(app_module, "client", fake_client)
    _use_memory_store(monkeypatch)
    monkeypatch.setattr(app_module, "_log_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "log_access_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "MAX_MESSAGE_LEN", 5)

    state0 = {"total_tokens": 0, "turns": 0}
    results = list(
        app_module.respond(
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


def test_respond_escape_keyword_full_solution(monkeypatch, tmp_path):
    fake_client = _FakeClient()
    monkeypatch.setattr(app_module, "client", fake_client)
    _use_memory_store(monkeypatch)
    monkeypatch.setattr(app_module, "_log_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "log_access_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "DAILY_LIMIT", 1_000_000)
    monkeypatch.setattr(app_module, "GLOBAL_DAILY_LIMIT", 3_000_000)
    _wire_usage_to_tmp(monkeypatch, tmp_path)

    results = list(
        app_module.respond(
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


def test_respond_practice_keyword_practice_wait(monkeypatch, tmp_path):
    fake_client = _FakeClient()
    monkeypatch.setattr(app_module, "client", fake_client)
    _use_memory_store(monkeypatch)
    monkeypatch.setattr(app_module, "_log_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "log_access_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "DAILY_LIMIT", 1_000_000)
    monkeypatch.setattr(app_module, "GLOBAL_DAILY_LIMIT", 3_000_000)
    _wire_usage_to_tmp(monkeypatch, tmp_path)

    results = list(
        app_module.respond(
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


def test_respond_answer_mode_stage_none(monkeypatch, tmp_path):
    fake_client = _FakeClient()
    monkeypatch.setattr(app_module, "client", fake_client)
    _use_memory_store(monkeypatch)
    monkeypatch.setattr(app_module, "_log_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "log_access_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "DAILY_LIMIT", 1_000_000)
    monkeypatch.setattr(app_module, "GLOBAL_DAILY_LIMIT", 3_000_000)
    _wire_usage_to_tmp(monkeypatch, tmp_path)

    results = list(
        app_module.respond(
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


def test_respond_new_problem_resets_stage(monkeypatch, tmp_path):
    fake_client = _FakeClient()
    monkeypatch.setattr(app_module, "client", fake_client)
    _use_memory_store(monkeypatch)
    monkeypatch.setattr(app_module, "_log_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "log_access_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "DAILY_LIMIT", 1_000_000)
    monkeypatch.setattr(app_module, "GLOBAL_DAILY_LIMIT", 3_000_000)
    _wire_usage_to_tmp(monkeypatch, tmp_path)

    history = [
        {"role": "user", "content": "上一题"},
        {"role": "assistant", "content": "上一题的完整解答"},
    ]
    stage_state = {"stage": "full_solution", "hints": 0, "last_mode": "互动模式"}
    results = list(
        app_module.respond(
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


def test_respond_reasoning_panel_and_status(monkeypatch, tmp_path):
    fake_client = _ReasoningClient()
    monkeypatch.setattr(app_module, "client", fake_client)
    _use_memory_store(monkeypatch)
    monkeypatch.setattr(app_module, "_log_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "log_access_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "DAILY_LIMIT", 1_000_000)
    monkeypatch.setattr(app_module, "GLOBAL_DAILY_LIMIT", 3_000_000)
    _wire_usage_to_tmp(monkeypatch, tmp_path)

    results = list(
        app_module.respond(
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


def test_continue_last_answer(monkeypatch, tmp_path):
    fake_client = _FakeClient()
    monkeypatch.setattr(app_module, "client", fake_client)
    _use_memory_store(monkeypatch)
    monkeypatch.setattr(app_module, "_log_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "log_access_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "DAILY_LIMIT", 1_000_000)
    monkeypatch.setattr(app_module, "GLOBAL_DAILY_LIMIT", 3_000_000)
    _wire_usage_to_tmp(monkeypatch, tmp_path)

    history = [
        {"role": "user", "content": "求导"},
        {"role": "assistant", "content": "先写一半"},
    ]
    results = list(
        app_module.continue_last_answer(
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



def test_continue_last_answer_keeps_stage(monkeypatch, tmp_path):
    fake_client = _FakeClient()
    monkeypatch.setattr(app_module, "client", fake_client)
    _use_memory_store(monkeypatch)
    monkeypatch.setattr(app_module, "_log_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "log_access_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "DAILY_LIMIT", 1_000_000)
    monkeypatch.setattr(app_module, "GLOBAL_DAILY_LIMIT", 3_000_000)
    _wire_usage_to_tmp(monkeypatch, tmp_path)

    history = [
        {"role": "user", "content": "出道题"},
        {"role": "assistant", "content": "题目写到一半"},
    ]
    results = list(
        app_module.continue_last_answer(
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


def test_continue_last_answer_no_history():
    results = list(
        app_module.continue_last_answer(
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


def test_respond_empty_answer_still_records_usage(monkeypatch, tmp_path):
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

    store = _use_memory_store(monkeypatch)
    monkeypatch.setattr(app_module, "client", EmptyAnswerClient())
    monkeypatch.setattr(app_module, "clients", {})
    monkeypatch.setattr(app_module, "_log_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "_log_empty_reply", lambda **k: None)
    monkeypatch.setattr(app_module, "log_access_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "DAILY_LIMIT", 1_000_000)
    monkeypatch.setattr(app_module, "GLOBAL_DAILY_LIMIT", 3_000_000)
    usage_path = _wire_usage_to_tmp(monkeypatch, tmp_path)

    results = list(
        app_module.respond(
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
    assert usage_module.get_daily_used(today, "user1", path=usage_path) == 123
    assert usage_module.get_daily_total(today, path=usage_path) == 123


# 没有返回usage，但是输出部分回复
def test_respond_stream_error_keeps_partial_and_estimates_usage(
    monkeypatch, tmp_path
):
    partial_answer = "已经生成的部分回答"
    logged_errors = []

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
    store = _use_memory_store(monkeypatch)

    def capture_error(exc, **details):
        logged_errors.append((exc, details))

    monkeypatch.setattr(app_module, "client", fake_client)
    monkeypatch.setattr(app_module, "clients", {})
    monkeypatch.setattr(app_module, "_log_error", capture_error)
    monkeypatch.setattr(app_module, "_log_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "log_access_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "DAILY_LIMIT", 1_000_000)
    monkeypatch.setattr(app_module, "GLOBAL_DAILY_LIMIT", 3_000_000)
    monkeypatch.setattr(app_module, "MAX_MESSAGE_LEN", 2000)
    usage_path = _wire_usage_to_tmp(monkeypatch, tmp_path)

    results = list(
        app_module.respond(
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
        usage_module.get_daily_used(today, "user1", path=usage_path)
        == expected_tokens
    )
    assert (
        usage_module.get_daily_total(today, path=usage_path)
        == expected_tokens
    )

    # 确认走了异常分支，没有重新调用模型。
    assert fake_client.calls == 1
    assert len(logged_errors) == 1
    exc, details = logged_errors[0]
    assert isinstance(exc, RuntimeError)
    assert details["partial"] is True
    assert details["turn_total"] == expected_tokens


# 成功返回usage,随后流式中断
def test_respond_stream_error_uses_received_usage(monkeypatch, tmp_path):
    partial_answer = "已经生成的部分回答"
    store = _use_memory_store(monkeypatch)

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

    monkeypatch.setattr(app_module, "client", InterruptedClient())
    monkeypatch.setattr(app_module, "clients", {})
    monkeypatch.setattr(
        app_module.chat_service,
        "estimate_tokens",
        unexpected_estimate,
    )
    monkeypatch.setattr(app_module, "_log_error", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "_log_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "log_access_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "DAILY_LIMIT", 1_000_000)
    monkeypatch.setattr(app_module, "GLOBAL_DAILY_LIMIT", 3_000_000)
    monkeypatch.setattr(app_module, "MAX_MESSAGE_LEN", 2000)
    usage_path = _wire_usage_to_tmp(monkeypatch, tmp_path)

    results = list(
        app_module.respond(
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
    assert usage_module.get_daily_used(
        today, "user1", path=usage_path
    ) == 123
    assert usage_module.get_daily_total(
        today, path=usage_path
    ) == 123


# 没有输出正式回复，也没有返回usage就报错了
def test_respond_stream_error_without_answer_or_usage(monkeypatch, tmp_path):
    daily_usage_calls = []
    logged_errors = []
    store = _use_memory_store(monkeypatch)

    class FailedClient:
        def chat_stream_with_usage(
            self, messages, thinking=None, reasoning_effort=None, model=None
        ):
            yield None, "正在分析题目", None, None
            raise RuntimeError("正式回答前模拟中断")

    monkeypatch.setattr(app_module, "client", FailedClient())
    monkeypatch.setattr(app_module, "clients", {})
    monkeypatch.setattr(
        app_module,
        "_log_error",
        lambda exc, **details: logged_errors.append((exc, details)),
    )
    monkeypatch.setattr(app_module, "_log_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "log_access_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "DAILY_LIMIT", 1_000_000)
    monkeypatch.setattr(app_module, "GLOBAL_DAILY_LIMIT", 3_000_000)
    monkeypatch.setattr(app_module, "MAX_MESSAGE_LEN", 2000)
    usage_path = _wire_usage_to_tmp(monkeypatch, tmp_path)

    # 这个场景不应该增加每日 token 用量。
    monkeypatch.setattr(
        app_module,
        "add_daily_usage",
        lambda *a, **k: daily_usage_calls.append((a, k)),
    )

    results = list(
        app_module.respond(
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
    assert daily_usage_calls == []
    assert status
    assert "已保留生成的部分内容" not in status
    assert reasoning == "正在分析题目"

    assert state["turns"] == 1
    assert state["total_tokens"] == 0
    assert state["prompt_tokens"] == 0
    assert state["completion_tokens"] == 0

    assert len(logged_errors) == 1
    exc, details = logged_errors[0]
    assert isinstance(exc, RuntimeError)
    assert details["partial"] is False
    assert details["turn_total"] is None

    today = __import__("datetime").date.today().isoformat()
    assert usage_module.get_daily_used(
        today, "user1", path=usage_path
    ) == 0
    assert usage_module.get_daily_total(
        today, path=usage_path
    ) == 0


@pytest.mark.parametrize("reports_usage", [True, False])
def test_respond_truncated_answer_is_saved_and_shows_continue_hint(
    monkeypatch, tmp_path, reports_usage
):
    daily_usage_calls = []
    store = _use_memory_store(monkeypatch)

    class TruncatedClient:
        def chat_stream_with_usage(
            self, messages, thinking=None, reasoning_effort=None, model=None
        ):
            yield "部分答案", None, None, None
            yield None, None, _FakeUsage() if reports_usage else None, "length"

    monkeypatch.setattr(app_module, "client", TruncatedClient())
    monkeypatch.setattr(app_module, "clients", {})
    monkeypatch.setattr(app_module, "_log_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "log_access_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "DAILY_LIMIT", 1_000_000)
    monkeypatch.setattr(app_module, "GLOBAL_DAILY_LIMIT", 3_000_000)
    usage_path = _wire_usage_to_tmp(monkeypatch, tmp_path)

    def count_daily_usage(day, username, tokens):
        daily_usage_calls.append(tokens)
        return usage_module.add_daily_usage(
            day, username, tokens, path=usage_path
        )

    monkeypatch.setattr(app_module, "add_daily_usage", count_daily_usage)

    results = list(
        app_module.respond(
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
    assert daily_usage_calls == ([123] if reports_usage else [])
    assert usage_module.get_daily_used(
        today, "user1", path=usage_path
    ) == expected_total
    assert usage_module.get_daily_total(today, path=usage_path) == expected_total


def test_respond_save_failure_still_shows_answer_and_settles_usage(
    monkeypatch, tmp_path
):
    """正常回答保存失败：回答仍显示，用量结算一次，不重试模型。"""
    fake_client = _FakeClient()
    store = FailingSaveStore()
    monkeypatch.setattr(app_module, "client", fake_client)
    monkeypatch.setattr(app_module, "history_store", store)
    monkeypatch.setattr(app_module, "_log_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "log_access_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "DAILY_LIMIT", 1_000_000)
    monkeypatch.setattr(app_module, "GLOBAL_DAILY_LIMIT", 3_000_000)
    usage_path = _wire_usage_to_tmp(monkeypatch, tmp_path)

    results = list(
        app_module.respond(
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
    assert usage_module.get_daily_used(today, "user1", path=usage_path) == 123
    assert usage_module.get_daily_total(today, path=usage_path) == 123

    # 保存失败不增加次数：同一标记仍可成功保存
    store.fail = False
    assert store.save([{"role": "user", "content": "探针"}], "user1", 0)


def test_respond_stale_save_shows_not_saved_and_settles_usage(
    monkeypatch, tmp_path
):
    """保存标记过期：跳过保存但回答仍显示、用量仍结算一次。"""
    fake_client = _FakeClient()
    store = StaleSaveStore()
    monkeypatch.setattr(app_module, "client", fake_client)
    monkeypatch.setattr(app_module, "history_store", store)
    monkeypatch.setattr(app_module, "_log_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "log_access_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "DAILY_LIMIT", 1_000_000)
    monkeypatch.setattr(app_module, "GLOBAL_DAILY_LIMIT", 3_000_000)
    usage_path = _wire_usage_to_tmp(monkeypatch, tmp_path)

    results = list(
        app_module.respond(
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
    assert usage_module.get_daily_used(today, "user1", path=usage_path) == 123
    assert usage_module.get_daily_total(today, path=usage_path) == 123


def test_respond_stream_error_save_failure_still_shows_partial(
    monkeypatch, tmp_path
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
    monkeypatch.setattr(app_module, "client", fake_client)
    monkeypatch.setattr(app_module, "clients", {})
    monkeypatch.setattr(app_module, "history_store", store)
    monkeypatch.setattr(app_module, "_log_error", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "_log_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "log_access_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "DAILY_LIMIT", 1_000_000)
    monkeypatch.setattr(app_module, "GLOBAL_DAILY_LIMIT", 3_000_000)
    monkeypatch.setattr(app_module, "MAX_MESSAGE_LEN", 2000)
    usage_path = _wire_usage_to_tmp(monkeypatch, tmp_path)

    results = list(
        app_module.respond(
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
        usage_module.get_daily_used(today, "user1", path=usage_path)
        == expected_tokens
    )
    assert (
        usage_module.get_daily_total(today, path=usage_path) == expected_tokens
    )


def test_respond_stream_error_stale_save_shows_not_saved(monkeypatch, tmp_path):
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
    monkeypatch.setattr(app_module, "client", fake_client)
    monkeypatch.setattr(app_module, "clients", {})
    monkeypatch.setattr(app_module, "history_store", store)
    monkeypatch.setattr(app_module, "_log_error", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "_log_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "log_access_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "DAILY_LIMIT", 1_000_000)
    monkeypatch.setattr(app_module, "GLOBAL_DAILY_LIMIT", 3_000_000)
    monkeypatch.setattr(app_module, "MAX_MESSAGE_LEN", 2000)
    usage_path = _wire_usage_to_tmp(monkeypatch, tmp_path)

    results = list(
        app_module.respond(
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
        usage_module.get_daily_used(today, "user1", path=usage_path)
        == expected_tokens
    )


def test_respond_truncated_save_failure_shows_both_hints(monkeypatch, tmp_path):
    """截断回答保存失败：截断提示与保存失败提示同时出现，用量结算一次。"""
    daily_usage_calls = []

    class TruncatedClient:
        def chat_stream_with_usage(
            self, messages, thinking=None, reasoning_effort=None, model=None
        ):
            yield "部分答案", None, None, None
            yield None, None, _FakeUsage(), "length"

    monkeypatch.setattr(app_module, "client", TruncatedClient())
    monkeypatch.setattr(app_module, "clients", {})
    store = FailingSaveStore()
    monkeypatch.setattr(app_module, "history_store", store)
    monkeypatch.setattr(app_module, "_log_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "log_access_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "DAILY_LIMIT", 1_000_000)
    monkeypatch.setattr(app_module, "GLOBAL_DAILY_LIMIT", 3_000_000)
    usage_path = _wire_usage_to_tmp(monkeypatch, tmp_path)

    def count_daily_usage(day, username, tokens):
        daily_usage_calls.append(tokens)
        return usage_module.add_daily_usage(
            day, username, tokens, path=usage_path
        )

    monkeypatch.setattr(app_module, "add_daily_usage", count_daily_usage)

    results = list(
        app_module.respond(
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
    assert daily_usage_calls == [123]
    today = __import__("datetime").date.today().isoformat()
    assert usage_module.get_daily_used(today, "user1", path=usage_path) == 123
