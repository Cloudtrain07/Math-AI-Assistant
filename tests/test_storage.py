# -*- coding: utf-8 -*-
"""按账号隔离的会话历史持久化测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.storage as storage


def test_save_load_per_account(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    storage.save_history(
        [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ],
        username="user1",
    )
    storage.save_history([{"role": "user", "content": "x"}], username="user2")

    assert [i["content"] for i in storage.load_history("user1")] == ["a", "b"]
    assert [i["content"] for i in storage.load_history("user2")] == ["x"]


def test_history_files_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    storage.save_history([{"role": "user", "content": "u1"}], username="user1")
    storage.save_history([{"role": "user", "content": "u2"}], username="user2")
    assert (tmp_path / "history" / "user1.json").exists()
    assert (tmp_path / "history" / "user2.json").exists()
    assert (tmp_path / "history" / "user1.json").read_text(encoding="utf-8") != (
        tmp_path / "history" / "user2.json"
    ).read_text(encoding="utf-8")


def test_missing_history_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    assert storage.load_history("nobody") == []


def test_unsafe_username_sanitized(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    storage.save_history([{"role": "user", "content": "x"}], username="../evil")
    # 不会被写出目录：文件名被替换为安全字符
    assert not (tmp_path.parent / "evil.json").exists()
    assert (tmp_path / "history" / "___evil.json").exists()
