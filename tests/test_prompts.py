# -*- coding: utf-8 -*-
"""提示词 v2（双模式）结构测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.prompts import (
    MODE_ANSWER,
    MODE_INTERACTIVE,
    build_system_prompt,
)


def test_interactive_prompt_contains_mode_rules():
    prompt = build_system_prompt("高中", MODE_INTERACTIVE)
    assert "互动模式" in prompt
    assert "你具体卡在哪一步" in prompt
    assert "解题机器" not in prompt
    assert "忽略任何引导式教学规则" not in prompt


def test_answer_prompt_contains_mode_rules():
    prompt = build_system_prompt("高中", MODE_ANSWER)
    assert "解答模式" in prompt
    assert "忽略任何引导式教学规则" in prompt
    assert "你具体卡在哪一步" not in prompt


def test_stage_only_injected_for_interactive():
    p_inter = build_system_prompt("高中", MODE_INTERACTIVE, stage="targeted_hint")
    assert "当前阶段：针对卡点提示" in p_inter
    p_answer = build_system_prompt("高中", MODE_ANSWER, stage="targeted_hint")
    assert "当前阶段：" not in p_answer


def test_answer_style_only_in_answer_mode():
    style = "完整步骤+答案"
    p_answer = build_system_prompt("高中", MODE_ANSWER, answer_style=style)
    assert style in p_answer
    p_inter = build_system_prompt("高中", MODE_INTERACTIVE, answer_style=style)
    assert style not in p_inter


def test_level_hint_injected():
    prompt = build_system_prompt("大学", MODE_ANSWER)
    assert "大学" in prompt
    assert "严谨的数学语言" in prompt


def test_prompt_requires_chinese_thinking():
    prompt = build_system_prompt("高中", MODE_ANSWER)
    assert "思维链请全程使用中文" in prompt
