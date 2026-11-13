# -*- coding: utf-8 -*-
"""提示词 v2（双模式）结构测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.prompts import (
    LEVEL_METHOD_RULES,
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
    assert "忽略引导式教学的节奏控制" in prompt
    assert "你具体卡在哪一步" not in prompt


def test_answer_mode_defers_to_level_rules():
    """解答模式的「一轮输出成品」把跨学段确认列为唯一例外，且总规则在前。"""
    prompt = build_system_prompt("高中", MODE_ANSWER)
    assert "一轮输出成品、不发问" in prompt
    assert "唯一例外：当【学段与方法总规则】适用" in prompt
    # 未获同意时的例外：指出超纲知识、询问、结束本轮
    assert "只指出超纲知识" in prompt
    assert "然后结束回复" in prompt
    assert "这不算引导式追问" in prompt
    scope_pos = prompt.index("学段与方法总规则")
    mode_pos = prompt.index("【解答模式】")
    assert scope_pos < mode_pos


def test_interactive_stage_c_defers_to_level_rules():
    """互动模式阶段 C（含「直接讲」触发）先服从学段确认规则。"""
    prompt = build_system_prompt("初中", MODE_INTERACTIVE, stage="full_solution")
    assert "先按【学段与方法总规则】检查" in prompt
    assert "本轮改为只说明超纲知识并询问" in prompt
    assert "用户同意后" in prompt


def test_stage_hint_defers_to_level_rules():
    """每条阶段提示都附带服从学段总规则的后缀。"""
    prompt = build_system_prompt("小学", MODE_INTERACTIVE, stage="targeted_hint")
    assert "阶段提示服从【学段与方法总规则】" in prompt
    assert "本轮只说明超纲知识并询问后结束" in prompt


def test_primary_and_junior_rules_have_no_rhythm_conflict():
    """小学/初中方法规则不含节奏类指令；旧冲突文案已从模块中移除。"""
    for level in ("小学", "初中"):
        assert "一轮输出成品" not in LEVEL_METHOD_RULES[level]
        assert "不追问" not in LEVEL_METHOD_RULES[level]
        assert "忽略任何引导式教学规则" not in LEVEL_METHOD_RULES[level]


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


# ===================== 学段方法规则 =====================


def test_restricted_levels_include_scope_and_method_rules():
    for level in ("小学", "初中", "高中"):
        prompt = build_system_prompt(level, MODE_INTERACTIVE)
        assert "学段与方法总规则" in prompt
        # 常驻规则：每轮以当前学段为准，历史/摘要中的高阶解法只作背景
        assert "只作背景参考" in prompt
        # 找不到学段内解法：本轮只说明+询问即止
        assert "然后结束回复" in prompt
        assert "不写任何推导、步骤或答案" in prompt
        # 用户要求完整推导也适用；仅明确同意后下一轮才展开
        assert "给出完整推导" in prompt
        assert "明确同意使用高阶方法" in prompt
        assert "下一轮才展开完整推导" in prompt
        assert "未明确同意时不得展开" in prompt
        # 总括：唯一例外来源，压制模式与阶段要求
        assert "唯一例外来源" in prompt
        assert "以跨学段确认为前提" in prompt
        # 用户自带高阶解法：点评并标明学段
        assert "这是 XX 学段的方法" in prompt


def test_restricted_levels_ask_rule_precedes_stage_hints():
    """总规则在模式规则与阶段提示之前注入，且声明对「完整解答」阶段同样适用。"""
    prompt = build_system_prompt(
        "初中", MODE_INTERACTIVE, stage="full_solution"
    )
    scope_pos = prompt.index("学段与方法总规则")
    stage_pos = prompt.index("当前阶段：完整解答")
    assert scope_pos < stage_pos
    assert "即使阶段提示要求「完整解答」" in prompt


def test_primary_level_method_rules():
    prompt = build_system_prompt("小学", MODE_ANSWER)
    assert "小学方法规则" in prompt
    assert "分步算术" in prompt          # 应用题优先分步算术
    assert "3x=18" in prompt            # 课内简易方程示例
    assert "平面直角坐标系" in prompt    # 坐标系列为禁用工具


def test_junior_level_geometry_rules():
    prompt = build_system_prompt("初中", MODE_INTERACTIVE)
    assert "初中方法规则" in prompt
    assert "全等、相似、勾股" in prompt   # 几何证明优先课内方法
    assert "斜率" in prompt              # 明确不引入
    assert "可以正常使用坐标法" in prompt  # 课内坐标题可用
    # 全等/相似必须写齐判定条件
    assert "写齐判定条件" in prompt
    assert "SAS" in prompt and "HL" in prompt
    assert "对应的顶点与边" in prompt


def test_senior_level_derivative_rules():
    prompt = build_system_prompt("高中", MODE_ANSWER)
    assert "高中方法规则" in prompt
    assert "导数" in prompt              # 课内导数可用
    assert "积分" in prompt              # 大学方法不作为默认解法
    assert "本轮只说明并询问" in prompt  # 本轮只询问
    assert "用户明确同意后下一轮才展开" in prompt  # 同意后下一轮才展开


def test_university_level_not_restricted():
    """大学解法不限：总规则与分学段规则的本体不注入，跨学段确认条款不出现。"""
    p_answer = build_system_prompt("大学", MODE_ANSWER)
    assert "只作背景参考" not in p_answer
    assert "小学方法规则" not in p_answer
    assert "初中方法规则" not in p_answer
    assert "高中方法规则" not in p_answer
    assert "只指出超纲知识" not in p_answer
    assert "写齐判定条件" not in p_answer
    assert "严谨的数学语言" in p_answer

    p_inter = build_system_prompt("大学", MODE_INTERACTIVE, stage="full_solution")
    assert "只说明超纲知识" not in p_inter
    assert "学段与方法总规则" not in p_inter
    assert "严谨的数学语言" in p_inter


def test_type_templates_defer_to_level_rules():
    prompt = build_system_prompt("小学", MODE_ANSWER)
    assert "具体方法按当前学段选择" in prompt
    assert "小学优先分步算术" in prompt
