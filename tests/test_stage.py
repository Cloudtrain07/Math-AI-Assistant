# -*- coding: utf-8 -*-
"""advance_stage 阶段转移纯函数测试。"""

import sys
from pathlib import Path

import pytest

from core import dialogue_state

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.dialogue_state import advance_stage


def _st(stage="initial_hint", hints=0, last_mode="互动模式"):
    return {"stage": stage, "hints": hints, "last_mode": last_mode}


def test_answer_mode_always_none():
    result = advance_stage("解答模式", "随便说点什么", _st())
    assert result["stage"] is None
    assert result["mode_changed"] is True


def test_escape_keyword_full_solution():
    result = advance_stage("互动模式", "没时间了，直接讲答案", _st())
    assert result["stage"] == "full_solution"


def test_explicit_single_hint_overrides_previous_hint_count():
    result = advance_stage(
        "互动模式", "先不要给答案，只提示我下一步。",
        _st("targeted_hint", hints=3), has_history=True,
    )
    assert result["stage"] == "single_hint"


@pytest.mark.parametrize(("message", "stage"), [
    ("不要直接讲答案，我想自己试试。", "single_hint"),
    ("不用提示了，直接给我答案。", "full_solution"),
    ("不用先直接给我答案。", "single_hint"),
    ("不需要最终答案。", "single_hint"),
    ("直接给我答案，不需要提示。", "full_solution"),
    ("我只需要一点提示。", "single_hint"),
    ("我只要提示。", "single_hint"),
    ("只需要一点提示。还是直接给我答案吧。", "full_solution"),
    ("直接给我答案，算了，先不要答案。", "single_hint"),
    ("先不要答案，还是给出最终答案吧。", "full_solution"),
    ("先提示我一步，还是直接给答案吧。", "full_solution"),
    ("直接讲答案吧，等等，只提示一步。", "single_hint"),
    ("谢谢刚才的提示，我的做法是 x=3，对吗？", "grading"),
    ("之前的提示里，为什么这一步可以写成 x=3？", "targeted_hint"),
    ("之前只提示了一步，为什么这一步可以写成 x=3？", "targeted_hint"),
])
def test_reply_directives_and_ordinary_followups(message, stage):
    assert advance_stage(
        "互动模式", message, _st("full_solution"), has_history=True
    )["stage"] == stage


def test_switch_from_hint_to_full_solution_and_keep_stage_when_continuing():
    hint = advance_stage("互动模式", "只提示一步", _st(), has_history=True)
    assert hint["stage"] == "single_hint"
    assert advance_stage(
        "互动模式", "只提示一步", hint, has_history=True, force_continue=True
    ) == hint
    assert advance_stage(
        "互动模式", "现在可以讲完整解法了", hint, has_history=True
    )["stage"] == "full_solution"


def test_practice_keyword_practice_wait():
    result = advance_stage("互动模式", "出道题练练", _st())
    assert result["stage"] == "practice_wait"


def test_grading_keyword_grading():
    result = advance_stage(
        "互动模式", "我的做法：设 g(x)=x-1-ln x，对吗", _st()
    )
    assert result["stage"] == "grading"


def test_new_problem_resets_to_initial():
    result = advance_stage(
        "互动模式", "求解方程 x²-5x+6=0", _st("full_solution"), has_history=True
    )
    assert result["stage"] == "initial_hint"
    assert result["hints"] == 0


@pytest.mark.parametrize("separator", ["：", ":", "，", ",", "、", "；", " ", "\n"])
def test_next_problem_recognizes_common_separators(separator):
    assert dialogue_state.explicitly_switches_problem(f"下一题{separator}计算 2+2")


@pytest.mark.parametrize(("message", "is_new"), [
    ("计算图上标出的线段长度。", False),
    ("证明图上标出的两角相等。", False),
    ("计算 2+2 的结果。", True),
    ("证明三角形的内角和等于 180 度。", True),
    ("换一道题：求图上标出的角。", True),
    ("别换题，继续讲这里。", False),
])
def test_problem_statements_and_image_references(message, is_new):
    assert dialogue_state.looks_like_new_problem(message) is is_new


def test_first_message_initial_hint():
    result = advance_stage("互动模式", "什么是导数", _st(), has_history=False)
    assert result["stage"] == "initial_hint"


def test_initial_hint_to_targeted():
    result = advance_stage(
        "互动模式", "我不知道为什么要用对称轴", _st(), has_history=True
    )
    assert result["stage"] == "targeted_hint"
    assert result["hints"] == 1


def test_targeted_hint_hints_accumulate_then_full_solution():
    result = advance_stage(
        "互动模式", "还是不会", _st("targeted_hint", hints=1), has_history=True
    )
    assert result["stage"] == "targeted_hint"
    assert result["hints"] == 2

    result2 = advance_stage(
        "互动模式", "还是不会", _st("targeted_hint", hints=2), has_history=True
    )
    assert result2["stage"] == "full_solution"
    assert result2["hints"] == 0


def test_practice_wait_to_grade():
    result = advance_stage(
        "互动模式", "答案是 3", _st("practice_wait"), has_history=True
    )
    assert result["stage"] == "practice_grade"


def test_grading_to_summary():
    result = advance_stage(
        "互动模式", "清楚了", _st("grading"), has_history=True
    )
    assert result["stage"] == "summary"


def test_mode_switch_back_to_interactive():
    result = advance_stage(
        "互动模式", "继续讲", _st(last_mode="解答模式"), has_history=True
    )
    assert result["stage"] == "targeted_hint"
    assert result["mode_changed"] is True


def test_force_continue_keeps_interactive_stage():
    current = _st("targeted_hint", hints=2, last_mode="互动模式")
    original = current.copy()

    result = advance_stage(
        "互动模式", "继续", current, has_history=True, force_continue=True
    )

    assert result["stage"] == "targeted_hint"
    assert result["hints"] == 2
    assert result["last_mode"] == "互动模式"
    assert result["mode_changed"] is False
    assert current == original


def test_force_continue_clears_non_interactive_stage():
    current = _st("targeted_hint", hints=2, last_mode="互动模式")
    original = current.copy()

    result = advance_stage(
        "解答模式", "继续", current, has_history=True, force_continue=True
    )

    assert result["stage"] is None
    assert result["hints"] == 2
    assert result["last_mode"] == "解答模式"
    assert result["mode_changed"] is False
    assert current == original


def test_prepare_stage_state_creates_defaults():
    result = dialogue_state.prepare_stage_state(
        mode=dialogue_state.MODE_INTERACTIVE,
        current=None,
    )

    assert result == {
        "stage": "initial_hint",
        "hints": 0,
        "last_mode": dialogue_state.MODE_INTERACTIVE,
    }


def test_prepare_stage_state_fills_missing_keys():
    result = dialogue_state.prepare_stage_state(
        mode=dialogue_state.MODE_INTERACTIVE,
        current={"hints": 2},
    )

    assert result == {
        "stage": "initial_hint",
        "hints": 2,
        "last_mode": dialogue_state.MODE_INTERACTIVE,
    }


def test_prepare_stage_state_preserves_valid_state_without_mutation():
    current = {
        "stage": "targeted_hint",
        "hints": 3,
        "last_mode": dialogue_state.MODE_INTERACTIVE,
        "extra": "保留其他字段",
    }
    original = dict(current)

    result = dialogue_state.prepare_stage_state(
        mode=dialogue_state.MODE_INTERACTIVE,
        current=current,
    )

    assert result == original
    assert current == original
    assert result is not current


def test_prepare_stage_state_repairs_invalid_interactive_stage():
    for stage in [None, "initial_hnit", "abc"]:
        current = {"stage": stage}
        result = dialogue_state.prepare_stage_state(
            mode=dialogue_state.MODE_INTERACTIVE,
            current=current,
        )

        assert result["stage"] == "initial_hint"
        assert current["stage"] == stage  # 原字典没有被修改


def test_prepare_stage_state_does_not_repair_stage_in_answer_mode():
    current = {
        "stage": "abc",
        "hints": None,
        "last_mode": None,
    }

    result = dialogue_state.prepare_stage_state(
        mode="解答模式",
        current=current,
    )

    # 非互动模式不在这里修正阶段；
    # 已存在的键即使值是 None，也不会被 setdefault 覆盖。
    assert result == current
    assert result is not current
