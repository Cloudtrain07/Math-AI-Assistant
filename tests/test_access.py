# -*- coding: utf-8 -*-
"""公网访问密码与信息文件测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.access import (
    _PASSWORD_ALPHABET,
    generate_password,
    is_owner_host,
    log_access_event,
    save_access_info,
    trim_log_file,
)


def test_password_length():
    assert len(generate_password()) == 8


def test_password_uses_safe_alphabet():
    pwd = generate_password(64)
    assert set(pwd) <= set(_PASSWORD_ALPHABET)
    assert not any(ch in pwd for ch in "0O1Il")


def test_password_random_different():
    assert generate_password() != generate_password()


def test_save_access_info_writes_file(tmp_path):
    target = tmp_path / "当前访问密码.txt"
    result = save_access_info(
        "https://abc.gradio.live",
        "math",
        "Ab3dEf9k",
        lan_url="http://192.168.1.5:7860",
        target=target,
    )
    assert result == target
    text = target.read_text(encoding="utf-8")
    assert "math" in text
    assert "Ab3dEf9k" in text
    assert "https://abc.gradio.live" in text
    assert "http://192.168.1.5:7860" in text


def test_trim_log_file_keeps_last_n(tmp_path):
    target = tmp_path / "access.log"
    target.write_text(
        "\n".join(f"line{i}" for i in range(1, 9)) + "\n", encoding="utf-8"
    )
    trim_log_file(target, max_lines=5)
    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines == ["line4", "line5", "line6", "line7", "line8"]


def test_log_access_event_trims_to_5(tmp_path):
    target = tmp_path / "access.log"
    for i in range(6):
        log_access_event("事件", f"n={i}", target=target)
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5
    assert "n=5" in lines[-1]
    assert "n=0" not in "".join(lines)


def test_trim_log_file_missing_ok(tmp_path):
    trim_log_file(tmp_path / "nope.log")


def test_is_owner_host():
    assert is_owner_host("127.0.0.1")
    assert is_owner_host("::1")
    assert is_owner_host("localhost")
    assert not is_owner_host("192.168.3.83")
    assert not is_owner_host(None)
