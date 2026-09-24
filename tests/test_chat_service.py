# -*- coding: utf-8 -*-
"""继续生成消息的独立测试。"""

from core.chat_service import build_continue_message, prepare_api_messages,stream_reply
from unittest.mock import Mock


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


def test_prepare_api_messages_without_compression():
    history = [
        {"role": "user", "content": "什么是导数？"},
        {"role": "assistant", "content": "导数描述变化率。"},
    ]
    compressor = Mock()

    messages = prepare_api_messages(
        username="tester",
        history=history,
        level="高中",
        mode="解答模式",
        answer_style=None,
        active_client=object(),
        history_compressor=compressor,
        image_data_url=None,
        stage=None,
        mode_changed=False,
        max_history_messages=2,
    )

    compressor.get_summary.assert_not_called()
    assert messages[-2:] == history
    assert not any(
        "【更早对话摘要】" in str(message["content"])
        for message in messages
    )


def test_prepare_api_messages_with_compression():
    history = [
        {"role": "user", "content": "第一条较早的问题"},
        {"role": "assistant", "content": "第一条较早的回答"},
        {"role": "user", "content": "最近的问题"},
    ]
    compressor = Mock()
    compressor.get_summary.return_value = "测试摘要"
    active_client = object()

    messages = prepare_api_messages(
        username="tester",
        history=history,
        level="高中",
        mode="解答模式",
        answer_style=None,
        active_client=active_client,
        history_compressor=compressor,
        image_data_url=None,
        stage=None,
        mode_changed=False,
        max_history_messages=2,
    )

    compressor.get_summary.assert_called_once_with(
        "tester", history, 2, active_client
    )
    assert any(
        message["role"] == "system"
        and "【更早对话摘要】\n测试摘要" in message["content"]
        for message in messages
    )
    assert messages[-2:] == history[-2:]


def test_stream_reply_accumulates_model_chunks():
    usage_marker = object()
    messages = [{"role": "user", "content": "请解题"}]

    class FakeClient:
        def chat_stream_with_usage(
            self, received_messages, thinking, reasoning_effort, model
        ):
            self.received = (
                received_messages, thinking, reasoning_effort, model
            )
            yield "先求导", None, None, None
            yield None, "正在分析", None, None
            yield "，得到结果", None, usage_marker, "length"

    client = FakeClient()
    snapshots = list(
        stream_reply(
            client,
            messages,
            thinking=True,
            reasoning_effort="high",
            model="test-model",
        )
    )

    assert client.received == (messages, True, "high", "test-model")
    assert snapshots == [
        {
            "answer": "先求导",
            "reasoning_text": "",
            "usage": None,
            "truncated": False,
        },
        {
            "answer": "先求导",
            "reasoning_text": "正在分析",
            "usage": None,
            "truncated": False,
        },
        {
            "answer": "先求导，得到结果",
            "reasoning_text": "正在分析",
            "usage": usage_marker,
            "truncated": True,
        },
    ]
