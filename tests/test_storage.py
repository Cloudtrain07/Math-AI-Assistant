# -*- coding: utf-8 -*-
"""按账号隔离的会话历史持久化测试（HistoryStore / MemoryHistoryStore）。"""

import copy
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.storage as storage
from conftest import FailingSaveStore, MemoryHistoryStore
from core.storage import (
    HistoryReadError,
    HistoryStore,
    get_history_store,
)


def _user_messages(path: Path) -> list:
    return json.loads(path.read_text(encoding="utf-8"))


def _history_dir(tmp_path: Path) -> Path:
    return tmp_path / "history"


# ---------- 基本读写 ----------


def test_save_load_per_account(tmp_path):
    store = HistoryStore(tmp_path)
    token = store.start_turn("user1")
    assert store.save(
        [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ],
        "user1",
        token,
    )
    token2 = store.start_turn("user2")
    assert store.save([{"role": "user", "content": "x"}], "user2", token2)

    assert [i["content"] for i in store.load("user1")] == ["a", "b"]
    assert [i["content"] for i in store.load("user2")] == ["x"]


def test_history_files_isolated(tmp_path):
    store = HistoryStore(tmp_path)
    for name in ("user1", "user2"):
        store.save([{"role": "user", "content": name}], name, store.start_turn(name))
    assert (_history_dir(tmp_path) / "user1.json").exists()
    assert (_history_dir(tmp_path) / "user2.json").exists()
    assert (_history_dir(tmp_path) / "user1.json").read_text(encoding="utf-8") != (
        _history_dir(tmp_path) / "user2.json"
    ).read_text(encoding="utf-8")


def test_missing_history_returns_empty(tmp_path):
    assert HistoryStore(tmp_path).load("nobody") == []


def test_unsafe_username_sanitized(tmp_path):
    store = HistoryStore(tmp_path)
    store.save(
        [{"role": "user", "content": "x"}], "../evil", store.start_turn("../evil")
    )
    # 不会被写出目录：文件名被替换为安全字符
    assert not (tmp_path.parent / "evil.json").exists()
    assert (_history_dir(tmp_path) / "___evil.json").exists()


def test_saved_file_is_plain_json_message_list(tmp_path):
    store = HistoryStore(tmp_path)
    store.save(
        [{"role": "user", "content": "你好"}], "user1", store.start_turn("user1")
    )
    raw = _user_messages(_history_dir(tmp_path) / "user1.json")
    assert raw == [{"role": "user", "content": "你好"}]


def test_save_filters_invalid_messages(tmp_path):
    store = HistoryStore(tmp_path)
    history = [
        {"role": "system", "content": "不应保存"},
        {"role": "user", "content": "保留"},
        "not-a-dict",
        {"role": "user", "content": 42},
        {"role": "assistant", "content": "保留回答"},
    ]
    store.save(history, "user1", store.start_turn("user1"))
    assert [m["role"] for m in store.load("user1")] == ["user", "assistant"]


def test_load_normalizes_unclosed_formula(tmp_path):
    path = _history_dir(tmp_path) / "user1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([{"role": "assistant", "content": "结果为 $x^2+1"}]),
        encoding="utf-8",
    )
    assert HistoryStore(tmp_path).load("user1") == [
        {"role": "assistant", "content": "结果为 $x^2+1$"}
    ]


def test_load_ignores_non_list_json(tmp_path):
    path = _history_dir(tmp_path) / "user1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"role": "user"}), encoding="utf-8")
    assert HistoryStore(tmp_path).load("user1") == []


# ---------- 读取失败与文件保护 ----------


def test_corrupt_json_raises_and_preserves_file(tmp_path):
    path = _history_dir(tmp_path) / "user1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    corrupt = '{"role": "user", "content": '  # 非 JSON 片段
    path.write_text(corrupt, encoding="utf-8")

    with pytest.raises(HistoryReadError):
        HistoryStore(tmp_path).load("user1")
    assert path.read_text(encoding="utf-8") == corrupt


def test_read_permission_error_raises(tmp_path, monkeypatch):
    path = _history_dir(tmp_path) / "user1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[]", encoding="utf-8")

    original = Path.read_text

    def denied(self, *args, **kwargs):
        if self.name.endswith(".json"):
            raise PermissionError("模拟权限不足")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(HistoryReadError):
        HistoryStore(tmp_path).load("user1")
    monkeypatch.undo()
    assert path.read_text(encoding="utf-8") == "[]"


def test_save_on_corrupt_file_without_prior_load(tmp_path):
    """验收案例：已有损坏文件，不先 load 直接 save——保存失败，原文件不变。"""
    path = _history_dir(tmp_path) / "user1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    corrupt = "{损坏"
    path.write_text(corrupt, encoding="utf-8")

    store = HistoryStore(tmp_path)
    token = store.start_turn("user1")
    with pytest.raises(HistoryReadError):
        store.save([{"role": "user", "content": "新"}], "user1", token)
    # 原文件保留，无临时文件残留，保存计数未推进
    assert path.read_text(encoding="utf-8") == corrupt
    assert list(_history_dir(tmp_path).glob("*.tmp")) == []
    assert store.start_turn("user1") == token

    # 显式清空仍允许：直接写空历史成功，之后可正常保存
    store.clear("user1")
    assert store.load("user1") == []
    assert store.save(
        [{"role": "user", "content": "新"}], "user1", store.start_turn("user1")
    )
    assert [m["content"] for m in store.load("user1")] == ["新"]


def test_save_blocked_on_unreadable_existing_file(tmp_path, monkeypatch):
    """现有文件存在但读取权限错误：保存失败，原文件不变。"""
    path = _history_dir(tmp_path) / "user1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[]", encoding="utf-8")
    store = HistoryStore(tmp_path)
    token = store.start_turn("user1")

    original = Path.read_text

    def denied(self, *args, **kwargs):
        if self.name.endswith(".json"):
            raise PermissionError("模拟权限不足")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(HistoryReadError):
        store.save([{"role": "user", "content": "新"}], "user1", token)
    monkeypatch.undo()
    assert path.read_text(encoding="utf-8") == "[]"


def test_readable_existing_file_allows_save(tmp_path):
    """现有文件可正常读取时，保存直接覆盖（无需先 load）。"""
    store = HistoryStore(tmp_path)
    store.save(
        [{"role": "user", "content": "旧"}], "user1", store.start_turn("user1")
    )
    assert store.save(
        [{"role": "user", "content": "新"}],
        "user1",
        store.start_turn("user1"),
    )
    assert [m["content"] for m in store.load("user1")] == ["新"]


# ---------- 写入失败与临时文件清理 ----------


@pytest.mark.parametrize("fail_replace", [True, False])
def test_write_failure_preserves_file_and_cleans_tmp(
    tmp_path, monkeypatch, fail_replace
):
    path = _history_dir(tmp_path) / "user1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([{"role": "user", "content": "旧"}]), encoding="utf-8"
    )
    store = HistoryStore(tmp_path)

    def broken_write(self, data, **kwargs):
        raise OSError("模拟临时文件写入失败")

    def broken_replace(src, dst):
        raise OSError("模拟替换失败")

    if fail_replace:
        monkeypatch.setattr(storage.os, "replace", broken_replace)
    else:
        monkeypatch.setattr(Path, "write_text", broken_write)

    with pytest.raises(OSError):
        store.save(
            [{"role": "user", "content": "新"}], "user1", store.start_turn("user1")
        )

    monkeypatch.undo()
    # 原文件保留，无临时文件残留
    assert _user_messages(path) == [{"role": "user", "content": "旧"}]
    assert list(_history_dir(tmp_path).glob("*.tmp")) == []

    # 保存失败不推进计数：同一标记仍可成功保存
    assert store.save(
        [{"role": "user", "content": "新"}], "user1", store.start_turn("user1")
    )
    assert _user_messages(path) == [{"role": "user", "content": "新"}]


def test_write_failure_keeps_original_error_and_own_tmp_only(
    tmp_path, monkeypatch
):
    """替换失败时只清理自己的临时文件，并抛出原始错误。"""
    store = HistoryStore(tmp_path)
    kept_tmp = _history_dir(tmp_path) / "user2.json.keep.tmp"
    kept_tmp.parent.mkdir(parents=True, exist_ok=True)
    kept_tmp.write_text("别的进程的临时文件", encoding="utf-8")

    def broken_replace(src, dst):
        raise OSError("模拟替换失败")

    monkeypatch.setattr(storage.os, "replace", broken_replace)
    with pytest.raises(OSError, match="模拟替换失败"):
        store.save([{"role": "user", "content": "新"}], "user1", 0)

    monkeypatch.undo()
    assert kept_tmp.read_text(encoding="utf-8") == "别的进程的临时文件"
    assert not (_history_dir(tmp_path) / "user1.json").exists()
    assert list(_history_dir(tmp_path).glob("user1.json.*.tmp")) == []


# ---------- 本轮保存标记 ----------


def test_stale_token_skips_save(tmp_path):
    store = HistoryStore(tmp_path)
    first_token = store.start_turn("user1")
    assert store.save([{"role": "user", "content": "第一轮"}], "user1", first_token)

    # 旧标记再保存：被跳过，文件保持上一轮内容
    assert not store.save(
        [{"role": "user", "content": "过时"}], "user1", first_token
    )
    assert [m["content"] for m in store.load("user1")] == ["第一轮"]

    # 新一轮重新取标记后可以覆盖
    second_token = store.start_turn("user1")
    assert second_token != first_token
    assert store.save([{"role": "user", "content": "第二轮"}], "user1", second_token)
    assert [m["content"] for m in store.load("user1")] == ["第二轮"]


def test_clear_bumps_counter_and_blocks_old_turn(tmp_path):
    store = HistoryStore(tmp_path)
    token = store.start_turn("user1")
    store.save([{"role": "user", "content": "旧"}], "user1", token)

    # 第一次保存已推进计数，重新取得清空前的当前标记
    stale_token = store.start_turn("user1")

    store.clear("user1")
    assert store.load("user1") == []
    # 使用清空前取得的标记保存：被跳过（证明 clear 推进了计数）
    assert not store.save(
        [{"role": "user", "content": "旧轮写回"}], "user1", stale_token
    )
    assert store.load("user1") == []
    # 清空完成后新发起的一轮取得新标记，可以正常保存
    assert store.save(
        [{"role": "user", "content": "新一轮"}],
        "user1",
        store.start_turn("user1"),
    )
    assert [m["content"] for m in store.load("user1")] == ["新一轮"]


def test_failed_save_does_not_bump_counter(tmp_path, monkeypatch):
    store = HistoryStore(tmp_path)
    token = store.start_turn("user1")

    def broken_replace(src, dst):
        raise OSError("模拟替换失败")

    monkeypatch.setattr(storage.os, "replace", broken_replace)
    with pytest.raises(OSError):
        store.save([{"role": "user", "content": "失败轮"}], "user1", token)
    monkeypatch.undo()

    # 同一账号的另一方（相同标记）仍可成功保存
    assert store.save([{"role": "user", "content": "成功轮"}], "user1", token)
    assert [m["content"] for m in store.load("user1")] == ["成功轮"]


def test_different_accounts_do_not_interfere(tmp_path):
    store = HistoryStore(tmp_path)
    token_a = store.start_turn("alice")
    token_b = store.start_turn("bob")
    assert token_a == token_b  # 各自独立计数，初始相同

    assert store.save([{"role": "user", "content": "A1"}], "alice", token_a)
    assert store.save([{"role": "user", "content": "B1"}], "bob", token_b)
    assert [m["content"] for m in store.load("alice")] == ["A1"]
    assert [m["content"] for m in store.load("bob")] == ["B1"]

    # alice 保存推进的只是 alice 的计数
    assert not store.save([{"role": "user", "content": "A2"}], "alice", token_a)
    assert store.save([{"role": "user", "content": "B2"}], "bob", store.start_turn("bob"))


# ---------- 实例共享 ----------


def test_get_history_store_shares_instance_per_data_dir(tmp_path):
    store_a = get_history_store(tmp_path)
    assert get_history_store(tmp_path) is store_a
    assert get_history_store(tmp_path / "other") is not store_a


# ---------- 并发（事件/屏障控制顺序，不依赖 sleep） ----------


def test_concurrent_turns_first_save_wins(tmp_path):
    """两轮都在第一次保存前开始：先成功保存者保留，另一方被跳过。"""
    store = HistoryStore(tmp_path)
    barrier = threading.Barrier(2)
    results = {}

    def worker(name: str, content: str):
        token = store.start_turn("user1")
        barrier.wait()  # 双方都取得标记后再保存
        results[name] = store.save(
            [{"role": "user", "content": content}], "user1", token
        )

    threads = [
        threading.Thread(target=worker, args=("a", "回答A")),
        threading.Thread(target=worker, args=("b", "回答B")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results.values()) == [False, True]
    winner = "a" if results["a"] else "b"
    expected = [{"role": "user", "content": f"回答{winner.upper()}"}]
    assert store.load("user1") == expected


def test_concurrent_saves_on_different_accounts(tmp_path):
    store = HistoryStore(tmp_path)
    barrier = threading.Barrier(2)
    results = {}

    def worker(name: str):
        token = store.start_turn(name)
        barrier.wait()
        results[name] = store.save(
            [{"role": "user", "content": name}], name, token
        )

    threads = [
        threading.Thread(target=worker, args=("alice",)),
        threading.Thread(target=worker, args=("bob",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == {"alice": True, "bob": True}


# ---------- 内存假存储 ----------


def test_memory_store_interface_parity():
    store = MemoryHistoryStore()
    token = store.start_turn("user1")
    assert store.save([{"role": "user", "content": "a"}], "user1", token)
    assert [m["content"] for m in store.load("user1")] == ["a"]
    assert not store.save([{"role": "user", "content": "过时"}], "user1", token)

    store.clear("user1")
    assert store.load("user1") == []
    assert not store.save([{"role": "user", "content": "旧"}], "user1", token)


def test_memory_store_does_not_mutate_caller_history():
    store = MemoryHistoryStore()
    history = [{"role": "user", "content": "a"}]
    store.save(history, "user1", store.start_turn("user1"))

    # 保存后修改调用方列表，不影响已存内容
    history.append({"role": "assistant", "content": "后加的"})
    assert [m["content"] for m in store.load("user1")] == ["a"]

    # 读取返回副本，修改返回值不影响已存内容
    loaded = store.load("user1")
    loaded.append({"role": "assistant", "content": "外部改动"})
    assert [m["content"] for m in store.load("user1")] == ["a"]


def test_failing_save_store_counts_each_save_once():
    """FailingSaveStore 失败/成功路径都只计数一次，保存行为与基类一致。"""
    store = FailingSaveStore()
    token = store.start_turn("user1")

    # 失败路径：计数一次，抛出模拟异常
    with pytest.raises(OSError):
        store.save([{"role": "user", "content": "失败轮"}], "user1", token)
    assert store.save_attempts == 1

    # 成功路径：由基类计数一次，不重复
    store.fail = False
    assert store.save([{"role": "user", "content": "成功轮"}], "user1", token)
    assert store.save_attempts == 2
    # 保存行为不变：内容已写入，且计数推进一次（旧标记被跳过）
    assert [m["content"] for m in store.load("user1")] == ["成功轮"]
    assert not store.save([{"role": "user", "content": "过时"}], "user1", token)
    assert store.save_attempts == 3


def test_file_and_memory_load_unclosed_formula_identically(tmp_path):
    """验收案例：两种存储读取同一份未闭合公式，结果一致（尾部自动闭合）。"""
    history = [{"role": "assistant", "content": "结果为 $x^2+1"}]

    file_store = HistoryStore(tmp_path)
    file_store.save(history, "user1", file_store.start_turn("user1"))
    memory = MemoryHistoryStore()
    memory.save(history, "user1", memory.start_turn("user1"))

    expected = [{"role": "assistant", "content": "结果为 $x^2+1$"}]
    assert file_store.load("user1") == expected
    assert memory.load("user1") == expected


def test_file_and_memory_load_filter_identically(tmp_path):
    """两种存储读取同一份含非法消息的数据，过滤结果一致。"""
    junk = [
        {"role": "system", "content": "不应出现"},
        "not-a-dict",
        {"role": "user", "content": 42},
        {"role": "user", "content": "保留"},
    ]
    path = _history_dir(tmp_path) / "user1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(junk), encoding="utf-8")

    memory = MemoryHistoryStore()
    memory._messages["user1"] = copy.deepcopy(junk)

    expected = [{"role": "user", "content": "保留"}]
    assert HistoryStore(tmp_path).load("user1") == expected
    assert memory.load("user1") == expected
