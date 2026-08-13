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


def test_category_default_and_legacy(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    # 旧数据兼容：无 category 字段时显示未分类
    bank.add_entry({"question": "A"}, username="u")
    assert bank.load_entries("u")[0]["category"] == ""
    # 默认预设
    assert bank.load_categories("u") == bank.DEFAULT_CATEGORIES


def test_add_category_duplicate_and_limit(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    ok, cats = bank.add_category("三角函数", "u")
    assert ok and "三角函数" in cats
    ok2, cats2 = bank.add_category("三角函数", "u")
    assert not ok2
    ok3, cats3 = bank.add_category("  ", "u")
    assert not ok3
    assert len(cats3) == len(bank.DEFAULT_CATEGORIES) + 1


def test_rename_category_updates_entries(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    bank.add_entry({"question": "A", "category": "代数"}, username="u")
    cats = bank.rename_category("代数", "代数与方程", "u")
    assert "代数" not in cats and "代数与方程" in cats
    assert bank.load_entries("u")[0]["category"] == "代数与方程"


def test_delete_category_uncategorizes_entries(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    bank.add_entry({"question": "A", "category": "几何"}, username="u")
    cats = bank.delete_category("几何", "u")
    assert "几何" not in cats
    assert bank.load_entries("u")[0]["category"] == ""


def test_filter_entries_by_category(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    bank.add_entry({"question": "A", "category": "代数"}, username="u")
    bank.add_entry({"question": "B", "category": "几何"}, username="u")
    bank.add_entry({"question": "C"}, username="u")
    entries = bank.load_entries("u")
    assert len(bank.filter_entries(entries, category="代数")) == 1
    assert len(bank.filter_entries(entries, category="")) == 1
    assert len(bank.filter_entries(entries, category=None)) == 3
    assert len(bank.filter_entries(entries, category="代数", tag=None)) == 1


def test_mastery_default_and_legacy(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    bank.add_entry({"question": "A"}, username="u")
    e = bank.load_entries("u")[0]
    assert e["mastery"] == ""
    assert e["practice_right"] == 0 and e["practice_wrong"] == 0


def test_record_practice_upgrade_and_downgrade(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    entry = bank.add_entry({"question": "A"}, username="u")
    # 答对一次：连续 1 次未达 2，保持未掌握
    e = bank.record_practice(entry["id"], True, "u")
    assert e["mastery"] == "未掌握"
    assert e["consecutive_right"] == 1
    # 连续第二次答对：升级到学习中
    e = bank.record_practice(entry["id"], True, "u")
    assert e["mastery"] == "学习中"
    assert e["consecutive_right"] == 0
    # 再答对两次：学习中 -> 已掌握
    e = bank.record_practice(entry["id"], True, "u")
    e = bank.record_practice(entry["id"], True, "u")
    assert e["mastery"] == "已掌握"
    # 答错：降级到学习中，连续计数清零
    e = bank.record_practice(entry["id"], False, "u")
    assert e["mastery"] == "学习中"
    assert e["consecutive_right"] == 0
    assert e["practice_right"] == 4 and e["practice_wrong"] == 1
    assert e["last_practiced"]


def test_record_practice_missing_entry(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    assert bank.record_practice("nope", True, "u") is None


def test_filter_entries_by_mastery(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    bank.add_entry({"question": "A"}, username="u")
    bank.add_entry({"question": "B", "mastery": "已掌握"}, username="u")
    entries = bank.load_entries("u")
    assert len(bank.filter_entries(entries, mastery="未掌握")) == 1
    assert len(bank.filter_entries(entries, mastery="已掌握")) == 1
    assert len(bank.filter_entries(entries, mastery=None)) == 2
    assert len(bank.filter_entries(entries, mastery="学习中")) == 0
