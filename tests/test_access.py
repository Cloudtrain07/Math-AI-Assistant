# -*- coding: utf-8 -*-
"""公网访问密码与信息文件测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.access import (
    _PASSWORD_ALPHABET,
    generate_password,
    is_owner_host,
    save_access_info,
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
        [
            {
                "username": "user1",
                "password": "Ab3dEf9k",
                "display_name": "小明",
            }
        ],
        lan_url="http://192.168.1.5:7860",
        target=target,
    )
    assert result == target
    text = target.read_text(encoding="utf-8")
    assert "user1" in text
    assert "Ab3dEf9k" in text
    assert "小明" in text
    assert "https://abc.gradio.live" in text
    assert "http://192.168.1.5:7860" in text


def test_is_owner_host():
    assert is_owner_host("127.0.0.1")
    assert is_owner_host("::1")
    assert is_owner_host("localhost")
    assert not is_owner_host("192.168.3.83")
    assert not is_owner_host(None)
