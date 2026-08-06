# -*- coding: utf-8 -*-
"""advance_stage 阶段转移纯函数测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module


def _st(stage="initial_hint", hints=0, last_mode="互动模式"):
    return {"stage": stage, "hints": hints, "last_mode": last_mode}


def test_answer_mode_always_none():
    result = app_module.advance_stage("解答模式", "随便说点什么", _st())
    assert result["stage"] is None
    assert result["mode_changed"] is True


def test_escape_keyword_full_solution():
    result = app_module.advance_stage("互动模式", "没时间了，直接讲答案", _st())
    assert result["stage"] == "full_solution"


def test_practice_keyword_practice_wait():
    result = app_module.advance_stage("互动模式", "出道题练练", _st())
    assert result["stage"] == "practice_wait"


def test_grading_keyword_grading():
    result = app_module.advance_stage(
        "互动模式", "我的做法：设 g(x)=x-1-ln x，对吗", _st()
    )
    assert result["stage"] == "grading"


def test_new_problem_resets_to_initial():
    result = app_module.advance_stage(
        "互动模式", "求解方程 x²-5x+6=0", _st("full_solution"), has_history=True
    )
    assert result["stage"] == "initial_hint"
    assert result["hints"] == 0


def test_first_message_initial_hint():
    result = app_module.advance_stage("互动模式", "什么是导数", _st(), has_history=False)
    assert result["stage"] == "initial_hint"


def test_initial_hint_to_targeted():
    result = app_module.advance_stage(
        "互动模式", "我不知道为什么要用对称轴", _st(), has_history=True
    )
    assert result["stage"] == "targeted_hint"
    assert result["hints"] == 1


def test_targeted_hint_hints_accumulate_then_full_solution():
    result = app_module.advance_stage(
        "互动模式", "还是不会", _st("targeted_hint", hints=1), has_history=True
    )
    assert result["stage"] == "targeted_hint"
    assert result["hints"] == 2

    result2 = app_module.advance_stage(
        "互动模式", "还是不会", _st("targeted_hint", hints=2), has_history=True
    )
    assert result2["stage"] == "full_solution"
    assert result2["hints"] == 0


def test_practice_wait_to_grade():
    result = app_module.advance_stage(
        "互动模式", "答案是 3", _st("practice_wait"), has_history=True
    )
    assert result["stage"] == "practice_grade"


def test_grading_to_summary():
    result = app_module.advance_stage(
        "互动模式", "清楚了", _st("grading"), has_history=True
    )
    assert result["stage"] == "summary"


def test_mode_switch_back_to_interactive():
    result = app_module.advance_stage(
        "互动模式", "继续讲", _st(last_mode="解答模式"), has_history=True
    )
    assert result["stage"] == "targeted_hint"
    assert result["mode_changed"] is True
