# -*- coding: utf-8 -*-
"""习题库存储与逻辑测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import exercise_bank as bank


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(bank, "DATA_DIR", tmp_path / "exercise_bank")


def test_add_load_entry(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    entry = bank.add_entry(
        {
            "question": "解 $x^2-5x+6=0$",
            "mistake": "算错",
            "tags": ["二次函数"],
        },
        username="user1",
    )
    assert entry["id"]
    assert entry["created_at"]
    loaded = bank.load_entries("user1")
    assert len(loaded) == 1
    assert loaded[0]["question"] == "解 $x^2-5x+6=0$"
    assert loaded[0]["tags"] == ["二次函数"]


def test_per_account_isolated(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    bank.add_entry({"question": "A"}, username="user1")
    bank.add_entry({"question": "B"}, username="user2")
    assert [e["question"] for e in bank.load_entries("user1")] == ["A"]
    assert [e["question"] for e in bank.load_entries("user2")] == ["B"]


def test_update_and_delete(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    entry = bank.add_entry({"question": "A", "tags": ["x"]}, username="user1")
    updated = bank.update_entry(
        entry["id"],
        {"question": "A2", "tags": ["x", "y"]},
        username="user1",
    )
    assert updated["question"] == "A2"
    assert updated["tags"] == ["x", "y"]
    assert bank.delete_entry(entry["id"], username="user1") is True
    assert bank.load_entries("user1") == []


def test_find_duplicate_normalized(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    bank.add_entry({"question": "解  $x^2=4$  ", "id": "a"}, username="user1")
    bank.add_entry({"question": "不同", "id": "b"}, username="user1")
    entries = bank.load_entries("user1")
    dup = bank.find_duplicate("解 $x^2=4$", entries)
    assert dup["id"] == "a"
    assert bank.find_duplicate("解 $x^2=4$", entries, exclude_id="a") is None
    assert bank.find_duplicate("全新题目", entries) is None


def test_filter_entries(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    bank.add_entry(
        {"question": "二次函数求根", "tags": ["二次函数"]}, username="u"
    )
    bank.add_entry({"question": "导数题", "tags": ["导数"]}, username="u")
    entries = bank.load_entries("u")
    assert len(bank.filter_entries(entries, tag="二次函数")) == 1
    assert len(bank.filter_entries(entries, keyword="导数")) == 1
    assert len(bank.filter_entries(entries, keyword="不存在")) == 0
    assert len(bank.filter_entries(entries)) == 2


def test_entry_label(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    entry = bank.add_entry(
        {
            "question": "$x^2-5x+6=0$ 求根",
            "created_at": "2026-08-08 10:00:00",
        },
        username="u",
    )
    label = bank.entry_label(entry)
    assert "（无索引）" in label
    assert "08-08" in label


def test_entry_label_uses_title(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    entry = bank.add_entry(
        {
            "title": "讨论反常积分的收敛条件",
            "question": "$\\int$ 讨论",
            "created_at": "2026-08-08 10:00:00",
        },
        username="u",
    )
    assert bank.entry_label(entry) == "讨论反常积分的收敛条件（08-08）"


def test_filter_keyword_matches_title(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    bank.add_entry(
        {"title": "反常积分敛散性", "question": "讨论积分", "tags": []},
        username="u",
    )
    bank.add_entry(
        {"title": "导数题", "question": "求导", "tags": []},
        username="u",
    )
    entries = bank.load_entries("u")
    assert len(bank.filter_entries(entries, keyword="反常积分")) == 1
