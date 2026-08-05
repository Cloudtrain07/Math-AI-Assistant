# -*- coding: utf-8 -*-
"""math_format 归一化逻辑的回归测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.math_format import normalize_math_text


def test_inline_angle_escaped():
    out = normalize_math_text("当 $1<a<2$ 时", auto_close=True)
    assert "\\lt " in out
    assert "<" not in out


def test_display_angle_escaped():
    out = normalize_math_text("$$-1<\\alpha<0$$", auto_close=True)
    assert "\\lt " in out
    assert "<" not in out


def test_text_command_not_corrupted():
    out = normalize_math_text(
        "$$f(x)\\ \\text{在}\\ (-1,\\alpha)\\ \\text{上单调递增}$$",
        auto_close=True,
    )
    assert "\\text{$$" not in out
    assert "\\text{在}" in out


def test_idempotent():
    raw = "当 $1<a<2$ 时，$$-1<\\alpha<0$$"
    once = normalize_math_text(raw, auto_close=True)
    twice = normalize_math_text(once, auto_close=True)
    assert once == twice


def test_inequality_fraction():
    out = normalize_math_text(
        "$\\frac{2}{n+2}<a_n \\leqslant \\frac{3}{n+2}$",
        auto_close=True,
    )
    assert "\\lt " in out
    assert "<" not in out
