# -*- coding: utf-8 -*-
"""自动验算闭环与登录恢复历史测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module


class _FakeUsage:
    total_tokens = 123


class _WrongClient:
    """流式给出错误等式，complete 返回修正。"""

    def __init__(self):
        self.calls = 0
        self.complete_calls = 0

    def chat_stream_with_usage(
        self, messages, thinking=None, reasoning_effort=None, model=None
    ):
        self.calls += 1
        yield "答案：$2+2=5$", None, None, None
        yield None, None, _FakeUsage(), "stop"

    def complete(self, messages, thinking=None, reasoning_effort=None, max_tokens=500):
        self.complete_calls += 1
        return "修正：$2+2=4$"


class _GoodClient:
    """流式给出正确等式，不应触发复核。"""

    def chat_stream_with_usage(
        self, messages, thinking=None, reasoning_effort=None, model=None
    ):
        yield "答案：$2+2=4$", None, None, None
        yield None, None, _FakeUsage(), "stop"


def _fake_request():
    client = type("Client", (), {"host": "127.0.0.1"})()
    return type("Request", (), {"client": client, "username": "user1"})()


def _patch(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "save_history", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "_log_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "log_access_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "add_daily_question", lambda d: None)
    monkeypatch.setattr(app_module, "add_daily_usage", lambda d, u, t: 0)
    monkeypatch.setattr(app_module, "get_daily_used", lambda d, u: 0)
    monkeypatch.setattr(app_module, "get_daily_total", lambda d: 0)
    monkeypatch.setattr(app_module, "DAILY_LIMIT", 1_000_000)
    monkeypatch.setattr(app_module, "GLOBAL_DAILY_LIMIT", 3_000_000)


def _stage():
    return {"stage": None, "hints": 0, "last_mode": "解答模式"}


def test_auto_verify_detects_wrong_and_rechecks(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)
    fake = _WrongClient()
    monkeypatch.setattr(app_module, "client", fake)
    results = list(
        app_module.respond(
            "1+1=?",
            [],
            "高中",
            "解答模式",
            False,
            "deepseek-v4-flash",
            "high",
            None,
            {"total_tokens": 0, "turns": 0},
            _stage(),
            None,
            None,
            _fake_request(),
        )
    )
    last = results[-1]
    assert "自动验算" in last[2]
    assert "不成立" in last[2]
    assert "已自动复核" in last[2]
    assert fake.complete_calls == 1
    # 两段式输出：先出答案（不含验算），随后刷新验算结果
    assert "自动验算" not in results[-2][2]
    assert "自动验算" in results[-1][2]
    assistant_msgs = [m["content"] for m in last[0] if m["role"] == "assistant"]
    assert any("自动复核" in m and "2+2=4" in m for m in assistant_msgs)


def test_auto_verify_correct_answer_no_recheck(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)
    monkeypatch.setattr(app_module, "client", _GoodClient())
    results = list(
        app_module.respond(
            "1+1=?",
            [],
            "高中",
            "解答模式",
            False,
            "deepseek-v4-flash",
            "high",
            None,
            {"total_tokens": 0, "turns": 0},
            _stage(),
            None,
            None,
            _fake_request(),
        )
    )
    status = results[-1][2]
    assert "自动验算" in status
    assert "成立" in status
    assert "自动复核" not in status


def test_restore_history(monkeypatch, tmp_path):
    import core.storage as storage

    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    storage.save_history(
        [
            {"role": "user", "content": "旧题"},
            {"role": "assistant", "content": "旧解"},
        ],
        username="user1",
    )
    history = app_module.restore_history(_fake_request())
    assert [m["content"] for m in history] == ["旧题", "旧解"]


def test_restore_history_unknown_account(monkeypatch, tmp_path):
    import core.storage as storage

    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    request = type("R", (), {"client": None, "username": None})()
    assert app_module.restore_history(request) == []


def test_restore_level_from_account(monkeypatch, tmp_path):
    import core.accounts as accounts_mod

    path = tmp_path / "accounts.json"
    monkeypatch.setattr(accounts_mod, "ACCOUNTS_FILE", path)
    accounts_mod._write_accounts(
        [
            {
                "username": "user1",
                "password": "x",
                "level": "大学",
                "enabled": True,
            }
        ],
        path,
    )
    assert app_module.restore_level(_fake_request()) == "大学"


def test_restore_level_fallback():
    request = type("R", (), {"client": None, "username": None})()
    assert app_module.restore_level(request) == "高中"
