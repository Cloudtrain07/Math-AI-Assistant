# -*- coding: utf-8 -*-
"""日志轮转测试（chat.log / error.log 超限自动裁剪）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module


def test_chat_log_trimmed(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path)
    for i in range(2001):
        app_module._log_event("测试", f"n={i}")
    lines = (tmp_path / "chat.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2000
    assert "n=0" not in "".join(lines)
    assert "n=2000" in lines[-1]


def test_error_log_trimmed(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path)
    for i in range(2001):
        app_module._write_error_log({"type": "t", "i": i, "message": f"n={i}"})
    lines = (tmp_path / "error.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2000
