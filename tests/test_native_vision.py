# -*- coding: utf-8 -*-
"""原生视觉流程测试：providers 能力标记、多模态消息构造与 token 细分显示。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.providers import _sanitize_provider, get_provider_supports_vision
from core.message_builder import to_api_messages
from core.ui_text import format_usage


# ---------- providers 能力标记 ----------


def test_sanitize_provider_keeps_supports_vision():
    provider = _sanitize_provider(
        {
            "id": "glm",
            "base_url": "https://example.com",
            "models": ["m1"],
            "supports_vision": True,
        }
    )
    assert provider["supports_vision"] is True
    assert get_provider_supports_vision(provider) is True


def test_sanitize_provider_defaults_vision_false():
    provider = _sanitize_provider(
        {"id": "ds", "base_url": "https://example.com", "models": ["m1"]}
    )
    assert provider["supports_vision"] is False
    assert get_provider_supports_vision(provider) is False


# ---------- to_api_messages 多模态消息 ----------


def test_to_api_messages_without_image_stays_text():
    messages = to_api_messages(
        [{"role": "user", "content": "1+1=?"}],
        "高中",
        "解答模式",
    )
    assert messages[-1] == {"role": "user", "content": "1+1=?"}


def test_to_api_messages_with_image_builds_content_array():
    messages = to_api_messages(
        [{"role": "user", "content": "这道题怎么做"}],
        "高中",
        "解答模式",
        image_data_url="data:image/jpeg;base64,QUJD",
    )
    last = messages[-1]
    assert last["role"] == "user"
    assert isinstance(last["content"], list)
    assert last["content"][0]["type"] == "image_url"
    assert last["content"][0]["image_url"]["url"] == "data:image/jpeg;base64,QUJD"
    assert last["content"][1]["type"] == "text"
    assert "这道题怎么做" in last["content"][1]["text"]


def test_to_api_messages_image_without_user_message_is_ignored():
    messages = to_api_messages(
        [], "高中", "解答模式", image_data_url="data:image/jpeg;base64,QUJD"
    )
    assert all(m["role"] == "system" for m in messages)


# ---------- token 细分显示 ----------


def test_format_usage_shows_split():
    class _Usage:
        total_tokens = 123
        prompt_tokens = 100
        completion_tokens = 23

    text = format_usage(_Usage())
    assert "输入 100 + 输出 23" in text
    assert "123" in text


def test_format_usage_fallback_without_split():
    class _Usage:
        total_tokens = 123

    text = format_usage(_Usage())
    assert "本次约 123 tokens" in text
    assert "输入" not in text
