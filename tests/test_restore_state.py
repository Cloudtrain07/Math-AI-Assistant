# -*- coding: utf-8 -*-
"""登录后的历史记录与学段恢复测试"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module


def _fake_request():
    client = type("Client", (), {"host": "127.0.0.1"})()
    return type("Request", (), {"client": client, "username": "user1"})()


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
