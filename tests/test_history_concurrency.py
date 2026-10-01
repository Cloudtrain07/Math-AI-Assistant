# -*- coding: utf-8 -*-
"""应用层并发与清空竞态测试。

存储层的标记/清空/并发语义统一在 tests/test_storage.py 验证；
本文件只覆盖经 respond 调用链的应用层行为（生成期间清空、先后两轮覆盖）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module
from conftest import MemoryDailyUsageStore, MemoryHistoryStore
from core import usage as usage_module


def _wire_usage_store(monkeypatch) -> MemoryDailyUsageStore:
    """用内存每日用量存储替换文件实现，返回实例供断言。"""
    store = MemoryDailyUsageStore()
    monkeypatch.setattr(app_module, "daily_usage_store", store)
    return store


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


def _fake_request():
    client = type("Client", (), {"host": "127.0.0.1"})()
    return type("Request", (), {"client": client, "username": "user1"})()


def _stage0():
    return {"stage": "initial_hint", "hints": 0, "last_mode": "互动模式"}


def _patch_respond_env(monkeypatch, tmp_path, store):
    monkeypatch.setattr(app_module, "client", _FakeClient())
    monkeypatch.setattr(app_module, "history_store", store)
    monkeypatch.setattr(app_module, "_log_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "log_access_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "DAILY_LIMIT", 1_000_000)
    monkeypatch.setattr(app_module, "GLOBAL_DAILY_LIMIT", 3_000_000)
    return _wire_usage_store(monkeypatch)


def test_clear_during_generation_blocks_writeback_and_settles_usage(
    monkeypatch, tmp_path
):
    """respond 生成期间清空成功：旧回答不能写回，用量仍结算一次。"""
    store = MemoryHistoryStore()
    usage_store = _patch_respond_env(monkeypatch, tmp_path, store)

    stream = app_module.respond(
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
    next(stream)  # 模型已开始生成（保存标记已取得）
    app_module.history_store.clear("user1")  # 生成期间清空成功
    final = list(stream)[-1]

    final_history, _, status, _, state, _, _, _ = final

    # 回答仍显示，但提示未被保存
    assert final_history[-1]["content"] == "你好世界"
    assert "另一个操作已更新历史，本次回答未保存" in status
    # 旧回答没有写回；本轮只尝试保存一次（被标记检查跳过）
    assert store._messages.get("user1") == []
    assert store.save_attempts == 1
    # 用量仍结算一次
    assert state["turns"] == 1
    assert state["total_tokens"] == 123
    today = __import__("datetime").date.today().isoformat()
    assert usage_store.get_used(today, "user1") == 123
    assert usage_store.get_total(today) == 123


def test_two_turns_same_account_second_overwrites_after_first_saved(
    monkeypatch, tmp_path
):
    """同一账号先后开始的两轮：第一轮保存完成后，第二轮正常覆盖。"""
    store = MemoryHistoryStore()
    _patch_respond_env(monkeypatch, tmp_path, store)

    first = list(
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
    assert "已更新历史" not in first[-1][2]
    assert [m["content"] for m in store._messages["user1"]] == ["1+1=?", "你好世界"]

    second = list(
        app_module.respond(
            "再来一题",
            store._messages["user1"],
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
    assert "已更新历史" not in second[-1][2]
    assert [m["content"] for m in store._messages["user1"]] == [
        "1+1=?",
        "你好世界",
        "再来一题",
        "你好世界",
    ]
    # 两轮各只尝试保存一次，均成功
    assert store.save_attempts == 2
