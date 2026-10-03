# -*- coding: utf-8 -*-
"""新对话二次确认与按钮复位测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gradio as gr

import app as app_module
from conftest import (
    FailingClearStore,
    MemoryDailyUsageStore,
    MemoryEventLogger,
    MemoryHistoryStore,
)


def _fake_request():
    client = type("Client", (), {"host": "127.0.0.1"})()
    return type("Request", (), {"client": client, "username": "user1"})()


def _patch_env(monkeypatch, tmp_path, app_runtime):
    monkeypatch.setattr(app_runtime, "client", None)
    monkeypatch.setattr(app_runtime, "history_store", MemoryHistoryStore())
    monkeypatch.setattr(app_runtime, "daily_usage_store", MemoryDailyUsageStore())
    monkeypatch.setattr(app_runtime, "event_logger", MemoryEventLogger())
    monkeypatch.setattr(app_runtime, "daily_limit", 1_000_000)
    monkeypatch.setattr(app_runtime, "global_daily_limit", 3_000_000)


def test_confirm_clear_two_step(monkeypatch, tmp_path, app_runtime):
    _patch_env(monkeypatch, tmp_path, app_runtime)
    r1 = app_module.confirm_clear(app_runtime, _fake_request(), [], "")
    assert r1[8] == "armed"
    assert "再点一次确认" in r1[2]
    r2 = app_module.confirm_clear(app_runtime, _fake_request(), [], "armed")
    assert r2[8] == ""
    assert "已开启新对话" in r2[2]


def test_clear_success_resets_state_and_summary(monkeypatch, tmp_path, app_runtime):
    _patch_env(monkeypatch, tmp_path, app_runtime)
    resets = []
    monkeypatch.setattr(
        app_runtime.history_compressor, "reset", lambda u: resets.append(u)
    )

    result = app_module.clear_conversation(app_runtime, _fake_request())

    new_history, _text, msg, _usage_msg, usage, stage, reasoning, image = result
    # 界面历史、会话用量、阶段、思考与图片全部重置
    assert new_history["value"] == []
    assert "已开启新对话" in msg
    assert usage == {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0, "turns": 0}
    assert stage["stage"] == "initial_hint"
    assert reasoning == "（本轮无思考过程）"
    assert image == {"path": None, "fresh": False}
    # 空历史成功保存后，才清除摘要
    assert resets == ["user1"]
    assert app_runtime.history_store._messages.get("user1") == []
    assert app_runtime.history_store.clear_calls == 1


def test_clear_failure_keeps_state_and_summary(monkeypatch, tmp_path, app_runtime):
    _patch_env(monkeypatch, tmp_path, app_runtime)
    store = FailingClearStore()
    monkeypatch.setattr(app_runtime, "history_store", store)
    resets = []
    monkeypatch.setattr(
        app_runtime.history_compressor, "reset", lambda u: resets.append(u)
    )

    result = app_module.clear_conversation(app_runtime, _fake_request())

    new_history, _text, msg, _usage_msg, usage, stage, reasoning, image = result
    assert "清空未成功" in msg
    # 失败时不改动界面状态与摘要（gr.update() 表示保持原值）
    assert new_history == gr.update()
    assert usage == gr.update()
    assert stage == gr.update()
    assert image == gr.update()
    assert reasoning == gr.update()
    assert resets == []
    assert store._messages.get("user1") is None
    assert store.clear_calls == 1  # 尝试过清空但失败


def test_reset_confirm_buttons():
    clear_state, clear_btn = app_module.reset_confirm_buttons("armed")
    assert clear_state["value"] == ""
    assert clear_btn["value"] == "🗑️ 新对话"
