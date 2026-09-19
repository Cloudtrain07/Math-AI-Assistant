# -*- coding: utf-8 -*-
"""新对话二次确认与按钮复位测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module


def _fake_request():
    client = type("Client", (), {"host": "127.0.0.1"})()
    return type("Request", (), {"client": client, "username": "user1"})()


def _patch_env(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "client", None)
    monkeypatch.setattr(app_module, "save_history", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "_log_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "log_access_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "add_daily_question", lambda d: None)
    monkeypatch.setattr(app_module, "add_daily_usage", lambda d, u, t: 0)
    monkeypatch.setattr(app_module, "get_daily_used", lambda d, u: 0)
    monkeypatch.setattr(app_module, "get_daily_total", lambda d: 0)
    monkeypatch.setattr(app_module, "DAILY_LIMIT", 1_000_000)
    monkeypatch.setattr(app_module, "GLOBAL_DAILY_LIMIT", 3_000_000)


def test_confirm_clear_two_step(monkeypatch, tmp_path):
    _patch_env(monkeypatch, tmp_path)
    r1 = app_module.confirm_clear(_fake_request(), [], "")
    assert r1[8] == "armed"
    assert "再点一次确认" in r1[2]
    r2 = app_module.confirm_clear(_fake_request(), [], "armed")
    assert r2[8] == ""
    assert "已开启新对话" in r2[2]


def test_reset_confirm_buttons():
    clear_state, clear_btn = app_module.reset_confirm_buttons("armed")
    assert clear_state["value"] == ""
    assert clear_btn["value"] == "🗑️ 新对话"
