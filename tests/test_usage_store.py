# -*- coding: utf-8 -*-
"""每日用量存储测试：DailyUsageStore（文件）与 MemoryDailyUsageStore（内存）。"""

import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.usage_store as usage_store_module
from conftest import MemoryDailyUsageStore
from core.usage_store import DailyUsageReadError, DailyUsageStore

DATE = "2026-08-05"


# ---------- 文件实现：基本累计与统计 ----------


def test_daily_usage_accumulates(tmp_path):
    store = DailyUsageStore(tmp_path / "usage_daily.json")
    store.add_usage(DATE, "1.2.3.4", 100)
    store.add_usage(DATE, "1.2.3.4", 250)
    store.add_usage(DATE, "5.6.7.8", 50)
    assert store.get_used(DATE, "1.2.3.4") == 350
    assert store.get_used(DATE, "5.6.7.8") == 50


def test_daily_usage_resets_on_new_date(tmp_path):
    store = DailyUsageStore(tmp_path / "usage_daily.json")
    store.add_usage(DATE, "1.2.3.4", 100)
    assert store.get_used("2026-08-06", "1.2.3.4") == 0


def test_add_daily_usage_updates_global_total(tmp_path):
    store = DailyUsageStore(tmp_path / "usage_daily.json")
    store.add_usage(DATE, "1.1.1.1", 100)
    store.add_usage(DATE, "2.2.2.2", 250)
    assert store.get_total(DATE) == 350


def test_get_daily_total_backward_compat(tmp_path):
    path = tmp_path / "u.json"
    path.write_text(
        '{"2026-08-05": {"1.1.1.1": 100, "2.2.2.2": 200}}',
        encoding="utf-8",
    )
    assert DailyUsageStore(path).get_total(DATE) == 300


def test_daily_concurrent_add_no_loss(tmp_path):
    """每日用量是累加：并发完成的多笔都记入，账号与全局累计正确。"""
    store = DailyUsageStore(tmp_path / "usage_daily.json")

    def worker():
        for _ in range(50):
            store.add_usage(DATE, "9.9.9.9", 1)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.get_used(DATE, "9.9.9.9") == 200
    assert store.get_total(DATE) == 200


def test_daily_logins_and_questions(tmp_path):
    store = DailyUsageStore(tmp_path / "u.json")
    store.add_login(DATE)
    store.add_login(DATE)
    store.add_question(DATE)
    assert store.get_logins(DATE) == 2
    assert store.get_questions(DATE) == 1


def test_get_account_usage_excludes_reserved(tmp_path):
    store = DailyUsageStore(tmp_path / "u.json")
    store.add_usage(DATE, "1.1.1.1", 100)
    store.add_login(DATE)
    assert store.get_account_usage(DATE) == {"1.1.1.1": 100}


def test_missing_file_reads_as_empty(tmp_path):
    store = DailyUsageStore(tmp_path / "absent.json")
    assert store.get_used(DATE, "1.1.1.1") == 0
    assert store.get_total(DATE) == 0
    assert store.get_account_usage(DATE) == {}
    assert store.get_logins(DATE) == 0
    assert store.get_questions(DATE) == 0


def test_atomic_write_no_tmp_left(tmp_path):
    store = DailyUsageStore(tmp_path / "u.json")
    store.add_usage(DATE, "1.1.1.1", 5)
    assert (tmp_path / "u.json").exists()
    assert list(tmp_path.glob("*.tmp")) == []


# ---------- 文件实现：失败处理 ----------


def test_corrupt_file_reads_raise_and_preserve_file(tmp_path):
    path = tmp_path / "usage_daily.json"
    corrupt = "{损坏"
    path.write_text(corrupt, encoding="utf-8")
    store = DailyUsageStore(path)

    for read in (
        lambda: store.get_used(DATE, "1.1.1.1"),
        lambda: store.get_total(DATE),
        lambda: store.get_account_usage(DATE),
        lambda: store.get_logins(DATE),
        lambda: store.get_questions(DATE),
    ):
        with pytest.raises(DailyUsageReadError):
            read()

    # 记录也要求先读取成功：损坏时明确报错，不假装为零、不覆盖原文件
    with pytest.raises(DailyUsageReadError):
        store.add_usage(DATE, "1.1.1.1", 100)
    assert path.read_text(encoding="utf-8") == corrupt


def test_read_permission_error_raises(tmp_path, monkeypatch):
    path = tmp_path / "usage_daily.json"
    path.write_text("{}", encoding="utf-8")
    store = DailyUsageStore(path)

    original = Path.read_text

    def denied(self, *args, **kwargs):
        if self.name == "usage_daily.json":
            raise PermissionError("模拟权限不足")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(DailyUsageReadError):
        store.get_total(DATE)
    monkeypatch.undo()
    assert path.read_text(encoding="utf-8") == "{}"


@pytest.mark.parametrize("fail_replace", [True, False])
def test_write_failure_preserves_file_and_cleans_tmp(
    tmp_path, monkeypatch, fail_replace
):
    path = tmp_path / "usage_daily.json"
    path.write_text(json.dumps({"old": {"1.1.1.1": 7}}), encoding="utf-8")
    store = DailyUsageStore(path)

    def broken_write(self, data, **kwargs):
        raise OSError("模拟临时文件写入失败")

    def broken_replace(src, dst):
        raise OSError("模拟替换失败")

    if fail_replace:
        monkeypatch.setattr(usage_store_module.os, "replace", broken_replace)
    else:
        monkeypatch.setattr(Path, "write_text", broken_write)

    with pytest.raises(OSError):
        store.add_usage(DATE, "1.1.1.1", 100)

    monkeypatch.undo()
    # 原文件保留，无临时文件残留；修复后同一实例可正常写入
    assert json.loads(path.read_text(encoding="utf-8")) == {"old": {"1.1.1.1": 7}}
    assert list(tmp_path.glob("*.tmp")) == []
    assert store.add_usage(DATE, "1.1.1.1", 100) == 100


def test_write_failure_keeps_other_tmp_files(tmp_path, monkeypatch):
    store = DailyUsageStore(tmp_path / "usage_daily.json")
    kept = tmp_path / "other.json.keep.tmp"
    kept.write_text("别的临时文件", encoding="utf-8")

    def broken_replace(src, dst):
        raise OSError("模拟替换失败")

    monkeypatch.setattr(usage_store_module.os, "replace", broken_replace)
    with pytest.raises(OSError, match="模拟替换失败"):
        store.add_usage(DATE, "1.1.1.1", 1)

    monkeypatch.undo()
    assert kept.read_text(encoding="utf-8") == "别的临时文件"
    assert list(tmp_path.glob("usage_daily.json.*.tmp")) == []


# ---------- 内存实现与一致性 ----------


def test_memory_store_rules_match_file_store(tmp_path):
    """同一串操作在文件与内存实现上得到相同的统计结果。"""
    file_store = DailyUsageStore(tmp_path / "usage_daily.json")
    memory = MemoryDailyUsageStore()

    for store in (file_store, memory):
        store.add_usage(DATE, "a", 100)
        store.add_usage(DATE, "a", 250)
        store.add_usage(DATE, "b", 50)
        store.add_login(DATE)
        store.add_login(DATE)
        store.add_question(DATE)

        assert store.get_used(DATE, "a") == 350
        assert store.get_used(DATE, "b") == 50
        assert store.get_total(DATE) == 400
        assert store.get_account_usage(DATE) == {"a": 350, "b": 50}
        assert store.get_logins(DATE) == 2
        assert store.get_questions(DATE) == 1
        assert store.get_used("2026-08-06", "a") == 0


def test_memory_store_accumulates_concurrently():
    store = MemoryDailyUsageStore()

    def worker():
        for _ in range(50):
            store.add_usage(DATE, "9.9.9.9", 1)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.get_used(DATE, "9.9.9.9") == 200
    assert store.get_total(DATE) == 200
