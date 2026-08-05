# -*- coding: utf-8 -*-
"""calculator 验算逻辑回归测试。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.calculator import CALCULATOR_AVAILABLE, verify_math

pytestmark = pytest.mark.skipif(
    not CALCULATOR_AVAILABLE, reason="sympy/latex2sympy2 未安装"
)


def test_lt_inequality_parses():
    results = verify_math("当 $1\\lt a\\lt 2$ 时")
    assert results
    assert all(r["status"] != "解析失败" for r in results)


def test_textless_inequality_parses():
    results = verify_math("结论：$x\\textless 0$")
    assert results
    assert all(r["status"] != "解析失败" for r in results)


def test_quadratic_still_solves():
    results = verify_math("解方程 $x^2-5x+6=0$")
    assert any(r["status"] == "解为" for r in results)


def test_long_formula_skipped():
    long_formula = "$" + "a" * 600 + "$"
    results = verify_math(long_formula)
    assert results
    assert results[0]["status"] == "解析失败"
    assert "过长" in results[0]["detail"]
