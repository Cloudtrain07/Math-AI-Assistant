# -*- coding: utf-8 -*-
"""继续生成消息的独立测试。"""

from core.chat_service import build_continue_message


def test_uses_latest_user_message_and_removes_image_note():
    history = [
        {"role": "user", "content": "旧题目"},
        {"role": "assistant", "content": "旧回答"},
        {"role": "user", "content": "这是新题目\n[附图]"},
        {"role": "assistant", "content": "尚未完成的回答"},
    ]

    result = build_continue_message(history, "[附图]")

    assert result == "这是新题目\n（请继续完成上一条回答）"


def test_no_user_message_returns_none():
    assert build_continue_message([], "[附图]") is None
    assert build_continue_message(
        [{"role": "assistant", "content": "只有助手的回复"}],
        "[附图]",
    ) is None


def test_latest_user_message_only_has_image_note():
    history = [
        {"role": "user", "content": "更早的题目"},
        {"role": "user", "content": "[附图]"},
        {"role": "assistant", "content": "尚未完成的回答"},
    ]

    assert build_continue_message(history, "[附图]") is None