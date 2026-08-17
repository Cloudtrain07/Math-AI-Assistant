# -*- coding: utf-8 -*-
"""respond 主流程测试（mock 客户端与用量文件）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module
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
    monkeypatch.setattr(app_module, "client", fake_client)
    monkeypatch.setattr(app_module, "save_history", lambda *a, **k: None)
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
            _fake_request(),
        )
    )
    final_history, _, status_text, usage_text, usage_state, stage_state, _ = results[-1]

    assert final_history[-1]["content"] == "你好世界"
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
    monkeypatch.setattr(app_module, "save_history", lambda *a, **k: None)
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
            _fake_request(),
        )
    )
    final_history, _, status_text, _, usage_state, stage_state, _ = results[-1]

    assert "额度已用尽" in status_text
    assert final_history == []
    assert usage_state == state0
    assert stage_state["stage"] == "initial_hint"
    assert fake_client.calls == 0


def test_respond_overlong_message_rejected(monkeypatch):
    fake_client = _FakeClient()
    monkeypatch.setattr(app_module, "client", fake_client)
    monkeypatch.setattr(app_module, "save_history", lambda *a, **k: None)
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
            _fake_request(),
        )
    )
    final_history, _, status_text, _, usage_state, stage_state, _ = results[-1]

    assert "过长" in status_text
    assert final_history == []
    assert usage_state == state0
    assert stage_state["stage"] == "initial_hint"
    assert fake_client.calls == 0


def test_respond_escape_keyword_full_solution(monkeypatch, tmp_path):
    fake_client = _FakeClient()
    monkeypatch.setattr(app_module, "client", fake_client)
    monkeypatch.setattr(app_module, "save_history", lambda *a, **k: None)
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
            _fake_request(),
        )
    )
    final_history, _, _, _, _, stage_state, _ = results[-1]
    assert stage_state["stage"] == "full_solution"
    assert final_history[-1]["content"] == "你好世界"


def test_respond_practice_keyword_practice_wait(monkeypatch, tmp_path):
    fake_client = _FakeClient()
    monkeypatch.setattr(app_module, "client", fake_client)
    monkeypatch.setattr(app_module, "save_history", lambda *a, **k: None)
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
            _fake_request(),
        )
    )
    _, _, _, _, _, stage_state, _ = results[-1]
    assert stage_state["stage"] == "practice_wait"


def test_respond_answer_mode_stage_none(monkeypatch, tmp_path):
    fake_client = _FakeClient()
    monkeypatch.setattr(app_module, "client", fake_client)
    monkeypatch.setattr(app_module, "save_history", lambda *a, **k: None)
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
            _fake_request(),
        )
    )
    _, _, _, _, _, stage_state, _ = results[-1]
    assert stage_state["stage"] is None
    assert stage_state["last_mode"] == "解答模式"


def test_respond_new_problem_resets_stage(monkeypatch, tmp_path):
    fake_client = _FakeClient()
    monkeypatch.setattr(app_module, "client", fake_client)
    monkeypatch.setattr(app_module, "save_history", lambda *a, **k: None)
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
            _fake_request(),
        )
    )
    _, _, _, _, _, new_stage, _ = results[-1]
    assert new_stage["stage"] == "initial_hint"
    assert new_stage["hints"] == 0


def test_respond_reasoning_panel_and_status(monkeypatch, tmp_path):
    fake_client = _ReasoningClient()
    monkeypatch.setattr(app_module, "client", fake_client)
    monkeypatch.setattr(app_module, "save_history", lambda *a, **k: None)
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
    monkeypatch.setattr(app_module, "save_history", lambda *a, **k: None)
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
            _fake_request(),
        )
    )
    final_history, _, _, _, _, _, _ = results[-1]
    last_user = next(
        m["content"] for m in reversed(final_history) if m["role"] == "user"
    )
    assert "求导" in last_user and "继续" in last_user
    assert final_history[-1]["role"] == "assistant"
    assert final_history[-1]["content"] == "你好世界"



def test_continue_last_answer_keeps_stage(monkeypatch, tmp_path):
    fake_client = _FakeClient()
    monkeypatch.setattr(app_module, "client", fake_client)
    monkeypatch.setattr(app_module, "save_history", lambda *a, **k: None)
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
            _fake_request(),
        )
    )
    assert "还没有可继续" in results[-1][2]
