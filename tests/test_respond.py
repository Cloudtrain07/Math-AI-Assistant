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

    def chat_stream_with_usage(self, messages, thinking=None):
        self.calls += 1
        yield "你好", None, None
        yield "世界", _FakeUsage(), "stop"


def _fake_request():
    client = type("Client", (), {"host": "127.0.0.1"})()
    return type("Request", (), {"client": client})()


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
            "讲解模式",
            False,
            None,
            {"total_tokens": 0, "turns": 0},
            _fake_request(),
        )
    )
    final_history, _, status_text, usage_state = results[-1]

    assert final_history[-1]["content"] == "你好世界"
    assert usage_state == {"total_tokens": 123, "turns": 1}
    assert "累计" in status_text and "123" in status_text
    assert "本访客" in status_text
    assert fake_client.calls == 1
    today = __import__("datetime").date.today().isoformat()
    assert usage_module.get_daily_used(today, "127.0.0.1", path=p) == 123
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
            "讲解模式",
            False,
            None,
            state0,
            _fake_request(),
        )
    )
    final_history, _, status_text, usage_state = results[-1]

    assert "额度已用尽" in status_text
    assert final_history == []
    assert usage_state == state0
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
            "讲解模式",
            False,
            None,
            state0,
            _fake_request(),
        )
    )
    final_history, _, status_text, usage_state = results[-1]

    assert "过长" in status_text
    assert final_history == []
    assert usage_state == state0
    assert fake_client.calls == 0
