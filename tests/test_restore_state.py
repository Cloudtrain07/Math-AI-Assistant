# -*- coding: utf-8 -*-
"""登录后的历史记录与学段恢复测试"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module
from core.storage import HistoryStore


def _fake_request():
    client = type("Client", (), {"host": "127.0.0.1"})()
    return type("Request", (), {"client": client, "username": "user1"})()


def test_restore_history(monkeypatch, tmp_path):
    store = HistoryStore(tmp_path)
    monkeypatch.setattr(app_module, "history_store", store)
    store.save(
        [
            {"role": "user", "content": "旧题"},
            {"role": "assistant", "content": "旧解"},
        ],
        "user1",
        store.start_turn("user1"),
    )
    history, message = app_module.restore_history(_fake_request())
    assert [m["content"] for m in history] == ["旧题", "旧解"]
    assert message == ""


def test_restore_history_unknown_account(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "history_store", HistoryStore(tmp_path))
    request = type("R", (), {"client": None, "username": None})()
    history, message = app_module.restore_history(request)
    assert history == []
    assert message == ""


def test_restore_history_read_failure_shows_notice(monkeypatch, tmp_path):
    """文件损坏时明确报告读取失败，不能伪装成成功读取了空历史。"""
    store = HistoryStore(tmp_path)
    monkeypatch.setattr(app_module, "history_store", store)
    history_dir = tmp_path / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    (history_dir / "user1.json").write_text("{损坏", encoding="utf-8")

    history, message = app_module.restore_history(_fake_request())
    assert history == []
    assert "读取失败" in message

    # 读取失败后，后续保存被阻止（不覆盖损坏文件）
    import pytest
    from core.storage import HistoryReadError

    with pytest.raises(HistoryReadError):
        store.save(
            [{"role": "user", "content": "新"}],
            "user1",
            store.start_turn("user1"),
        )
    assert (history_dir / "user1.json").read_text(encoding="utf-8") == "{损坏"


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
