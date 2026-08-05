# -*- coding: utf-8 -*-
"""配置读取与校验测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import _env_int


def test_env_int_default_when_missing(monkeypatch):
    monkeypatch.delenv("TEST_INT_X", raising=False)
    assert _env_int("TEST_INT_X", 42) == 42


def test_env_int_valid(monkeypatch):
    monkeypatch.setenv("TEST_INT_X", "123")
    assert _env_int("TEST_INT_X", 42) == 123


def test_env_int_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("TEST_INT_X", "abc")
    assert _env_int("TEST_INT_X", 42) == 42
