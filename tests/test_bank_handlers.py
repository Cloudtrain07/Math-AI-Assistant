# -*- coding: utf-8 -*-
"""习题库界面处理器测试（mock 客户端与数据目录）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module
from core import exercise_bank as bank


class _FakeUsage:
    total_tokens = 123


class _FakeClient:
    def __init__(self):
        self.calls = 0

    def chat_stream_with_usage(
        self, messages, thinking=None, reasoning_effort=None, model=None
    ):
        self.calls += 1
        yield "变式题来了", None, None, None
        yield None, None, _FakeUsage(), "stop"


def _fake_request():
    client = type("Client", (), {"host": "127.0.0.1"})()
    return type("Request", (), {"client": client, "username": "user1"})()


def _patch_env(monkeypatch, tmp_path):
    monkeypatch.setattr(bank, "DATA_DIR", tmp_path / "exercise_bank")
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


def test_collect_current_problem(monkeypatch, tmp_path):
    _patch_env(monkeypatch, tmp_path)
    history = [
        {"role": "user", "content": "解方程 $x^2-5x+6=0$"},
        {"role": "assistant", "content": "答案是 2 和 3。"},
    ]
    results = list(app_module.collect_current_problem(history, _fake_request()))
    (
        _tabs,
        _form,
        title,
        question,
        mistake,
        solution,
        _err,
        tags,
        edit_id,
        bank_msg,
        _form_msg,
    ) = results[-1]
    assert title == ""  # 索引描述由用户自己填，不自动预填
    assert "解方程" in question
    assert bank_msg
    assert tags == ""  # client 为 None 时无模型建议
    assert edit_id == ""


def test_collect_no_history(monkeypatch, tmp_path):
    _patch_env(monkeypatch, tmp_path)
    results = list(app_module.collect_current_problem([], _fake_request()))
    assert results[-1][2] == ""
    assert "还没有题目" in results[-1][9]


def test_save_and_duplicate_confirm(monkeypatch, tmp_path):
    _patch_env(monkeypatch, tmp_path)
    r1 = list(
        app_module.save_entry(
            _fake_request(), "", "索引A", "题目A", "", "", "", "标签1", ""
        )
    )
    form_msg, force_id, *_ = r1[-1]
    assert "已保存" in form_msg
    assert force_id == ""
    assert len(bank.load_entries("user1")) == 1

    r2 = list(
        app_module.save_entry(
            _fake_request(), "", "索引A", "题目A", "", "", "", "标签1", ""
        )
    )
    dup_msg, dup_force, *_ = r2[-1]
    assert "已在库中" in dup_msg
    assert dup_force

    r3 = list(
        app_module.save_entry(
            _fake_request(), "", "索引A", "题目A", "", "", "", "标签1", dup_force
        )
    )
    assert "已保存" in r3[-1][0]
    assert len(bank.load_entries("user1")) == 2


def test_save_empty_question(monkeypatch, tmp_path):
    _patch_env(monkeypatch, tmp_path)
    r = list(
        app_module.save_entry(
            _fake_request(), "", "索引A", "  ", "", "", "", "", ""
        )
    )
    assert "不能为空" in r[-1][0]


def test_save_empty_title_rejected(monkeypatch, tmp_path):
    _patch_env(monkeypatch, tmp_path)
    r = list(
        app_module.save_entry(
            _fake_request(), "", "  ", "题目A", "", "", "", "", ""
        )
    )
    assert "索引描述不能为空" in r[-1][0]
    assert bank.load_entries("user1") == []


def test_edit_and_delete(monkeypatch, tmp_path):
    _patch_env(monkeypatch, tmp_path)
    entry = bank.add_entry({"question": "题1", "tags": ["a"]}, username="user1")
    (
        _form_u,
        bank_msg,
        title,
        question,
        _mistake,
        _solution,
        _err,
        tags,
        edit_id,
        _form_msg,
    ) = app_module.edit_entry(entry["id"], _fake_request())
    assert title == ""
    assert question == "题1"
    assert tags == "a"
    assert edit_id == entry["id"]

    r2 = list(
        app_module.save_entry(
            _fake_request(), entry["id"], "索引1", "题1改", "", "", "", "a,b", ""
        )
    )
    assert "已更新" in r2[-1][0]
    assert bank.get_entry(entry["id"], "user1")["question"] == "题1改"
    assert bank.get_entry(entry["id"], "user1")["title"] == "索引1"
    assert bank.get_entry(entry["id"], "user1")["tags"] == ["a", "b"]

    r3 = app_module.delete_selected_entry(entry["id"], _fake_request())
    assert "已删除" in r3[0]
    assert bank.load_entries("user1") == []


def test_select_and_toggle_solution(monkeypatch, tmp_path):
    _patch_env(monkeypatch, tmp_path)
    entry = bank.add_entry(
        {
            "title": "我的索引",
            "question": "题A",
            "solution": "我的标准答案",
            "tags": ["t"],
        },
        username="user1",
    )
    detail, sel_id, _sol, _msg = app_module.select_entry(
        entry["id"], False, _fake_request()
    )
    assert "题A" in detail
    assert "我的索引" in detail
    assert "我的标准答案" not in detail
    assert "勾选「显示正确解」查看" in detail
    assert sel_id == entry["id"]

    detail2, msg2 = app_module.toggle_solution(
        True, entry["id"], _fake_request()
    )
    assert "我的标准答案" in detail2
    assert "复习时间" in msg2
    assert bank.get_entry(entry["id"], "user1")["last_reviewed"]


def test_detail_renders_normalized_math(monkeypatch, tmp_path):
    _patch_env(monkeypatch, tmp_path)
    entry = bank.add_entry(
        {
            "question": "求 $\\frac{a}{2}",
            "solution": "答案是 $\\frac{a}{2}",
        },
        username="user1",
    )
    detail, _, _, _ = app_module.select_entry(entry["id"], False, _fake_request())
    # 未闭合的行内公式在展示时被自动补上闭合符
    assert "\\frac{a}{2}$" in detail
    detail2, _ = app_module.toggle_solution(True, entry["id"], _fake_request())
    assert "\\frac{a}{2}$" in detail2


def test_generate_similar_no_selection(monkeypatch, tmp_path):
    _patch_env(monkeypatch, tmp_path)
    results = list(
        app_module.generate_similar(
            "",
            "进阶",
            [],
            "高中",
            "解答模式",
            False,
            "deepseek-v4-flash",
            "high",
            None,
            {"total_tokens": 0, "turns": 0},
            {"stage": None, "hints": 0, "last_mode": "解答模式"},
            _fake_request(),
        )
    )
    assert "请先在习题库选择一道题" in results[-1][2]


def test_generate_similar_with_entry(monkeypatch, tmp_path):
    _patch_env(monkeypatch, tmp_path)
    app_module.client = _FakeClient()
    entry = bank.add_entry({"question": "求 $x$ 的值"}, username="user1")
    results = list(
        app_module.generate_similar(
            entry["id"],
            "挑战",
            [],
            "高中",
            "解答模式",
            False,
            "deepseek-v4-flash",
            "high",
            None,
            {"total_tokens": 0, "turns": 0},
            {"stage": None, "hints": 0, "last_mode": "解答模式"},
            _fake_request(),
        )
    )
    last = results[-1]
    assert "收藏" in last[2]
    user_msgs = [m["content"] for m in last[0] if m["role"] == "user"]
    assert any("变式" in u for u in user_msgs)
    assert any("挑战" in u for u in user_msgs)


def test_confirm_clear_two_step(monkeypatch, tmp_path):
    _patch_env(monkeypatch, tmp_path)
    r1 = app_module.confirm_clear(_fake_request(), [], "")
    assert r1[8] == "armed"
    assert "再点一次确认" in r1[2]
    r2 = app_module.confirm_clear(_fake_request(), [], "armed")
    assert r2[8] == ""
    assert "已开启新对话" in r2[2]


def test_confirm_delete_two_step(monkeypatch, tmp_path):
    _patch_env(monkeypatch, tmp_path)
    entry = bank.add_entry(
        {"title": "索引", "question": "题"}, username="user1"
    )
    r1 = app_module.confirm_delete(_fake_request(), entry["id"], "")
    assert r1[0] == "armed"
    assert "再点一次确认删除" in r1[2]
    r2 = app_module.confirm_delete(_fake_request(), entry["id"], "armed")
    assert r2[0] == ""
    assert "已删除" in r2[2]
    assert bank.load_entries("user1") == []


def test_reset_confirm_buttons():
    clear_state, delete_state, clear_btn, delete_btn = (
        app_module.reset_confirm_buttons("armed", "")
    )
    assert clear_state["value"] == ""
    assert clear_btn["value"] == "🗑️ 新对话"
    assert delete_state.get("value") is None
    assert delete_btn.get("value") is None
