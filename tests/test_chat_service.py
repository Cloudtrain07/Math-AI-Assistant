# -*- coding: utf-8 -*-
"""继续生成消息的独立测试。"""

from core.chat_service import build_continue_message, prepare_api_messages,stream_reply
import core.chat_service as chat_service
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


def test_route_image_attaches_fresh_image(monkeypatch):
    monkeypatch.setattr(
        chat_service,
        "image_to_data_url",
        lambda path: "data:image/jpeg;base64,TEST",
    )
    history = [{"role": "user", "content": "求解 x=1"}]
    image_state = {"path": "question.png", "fresh": True}

    result = chat_service.route_image_for_turn(
        message="求解 x=1",
        history=history,
        image_state=image_state,
        previous_stage="summary",
        supports_vision=True,
        force_continue=False,
        attach_image=True,
        image_note="[附图]",
    )

    assert result["image_data_url"] == "data:image/jpeg;base64,TEST"
    assert result["history"] == [
        {"role": "user", "content": "求解 x=1\n[附图]"}
    ]
    assert result["image_state"] == {"path": "question.png", "fresh": False}
    assert result["image_status"] == ""
    assert result["reject_message"] is None
    assert history == [{"role": "user", "content": "求解 x=1"}]  # 不改原历史
    assert image_state == {"path": "question.png", "fresh": True}


def test_route_image_clears_old_image_for_new_problem(monkeypatch):
    def should_not_read_image(path):
        raise AssertionError("旧图不应该被读取")

    monkeypatch.setattr(chat_service, "image_to_data_url", should_not_read_image)
    history = [{"role": "user", "content": "求解 x=1"}]

    result = chat_service.route_image_for_turn(
        message="求解 x=1",
        history=history,
        image_state={"path": "old.png", "fresh": False},
        previous_stage="summary",
        supports_vision=True,
        force_continue=False,
        attach_image=True,
        image_note="[附图]",
    )

    assert result["history"] == history
    assert result["image_data_url"] is None
    assert result["image_state"] == {"path": None, "fresh": False}
    assert result["reject_message"] is None


def test_route_image_rejects_text_client_and_keeps_pending_image():
    history = [{"role": "user", "content": "这道题怎么做"}]
    image_state = {"path": "question.png", "fresh": True}

    result = chat_service.route_image_for_turn(
        message="这道题怎么做",
        history=history,
        image_state=image_state,
        previous_stage="initial_hint",
        supports_vision=False,
        force_continue=False,
        attach_image=True,
        image_note="[附图]",
    )

    assert "不支持图片输入" in result["reject_message"]
    assert result["image_data_url"] is None
    assert result["image_state"] == image_state
    assert result["history"] == history


def test_route_image_read_failure_falls_back_to_text(monkeypatch):
    def fail_to_read(path):
        raise ValueError("文件损坏")

    monkeypatch.setattr(chat_service, "image_to_data_url", fail_to_read)
    history = [{"role": "user", "content": "这道题怎么做"}]

    result = chat_service.route_image_for_turn(
        message="这道题怎么做",
        history=history,
        image_state={"path": "broken.png", "fresh": True},
        previous_stage="initial_hint",
        supports_vision=True,
        force_continue=False,
        attach_image=True,
        image_note="[附图]",
    )

    assert result["image_data_url"] is None
    assert result["history"] == history
    assert result["image_state"] == {"path": None, "fresh": False}
    assert "图片读取失败" in result["image_status"]
    assert "已按纯文本发送" in result["image_status"]
    assert result["reject_message"] is None  # 提示错误，但仍允许发文字


def test_route_image_skips_attachment_for_continue_or_text_only(monkeypatch):
    def should_not_read_image(path):
        raise AssertionError("本轮不应该读取图片")

    monkeypatch.setattr(chat_service, "image_to_data_url", should_not_read_image)
    history = [{"role": "user", "content": "求解 x=1"}]
    image_state = {"path": "question.png", "fresh": True}

    for force_continue, attach_image in [(True, True), (False, False)]:
        result = chat_service.route_image_for_turn(
            message="求解 x=1",
            history=history,
            image_state=image_state,
            previous_stage="summary",
            supports_vision=True,
            force_continue=force_continue,
            attach_image=attach_image,
            image_note="[附图]",
        )

        assert result["history"] == history
        assert result["image_data_url"] is None
        assert result["image_state"] == image_state
        assert result["reject_message"] is None


def test_prepare_user_message_preserves_normal_text():
    result = chat_service.prepare_user_message(
        message="  这道题怎么做？  ",
        has_pending_image=False,
        max_message_len=100,
        image_only_prompt="请解答图中的题目。",
    )

    assert result == {
        "message": "  这道题怎么做？  ",
        "error": None,
    }


def test_prepare_user_message_rejects_blank_without_image():
    for message in ["", "   ", "\n\t"]:
        result = chat_service.prepare_user_message(
            message=message,
            has_pending_image=False,
            max_message_len=100,
            image_only_prompt="请解答图中的题目。",
        )

        assert result == {"message": message, "error": "empty"}


def test_prepare_user_message_uses_prompt_for_image_only():
    result = chat_service.prepare_user_message(
        message="   ",
        has_pending_image=True,
        max_message_len=100,
        image_only_prompt="请解答图中的题目。",
    )

    assert result == {
        "message": "请解答图中的题目。",
        "error": None,
    }


def test_prepare_user_message_accepts_exact_length_limit():
    result = chat_service.prepare_user_message(
        message="12345",
        has_pending_image=False,
        max_message_len=5,
        image_only_prompt="请解答图中的题目。",
    )

    assert result == {"message": "12345", "error": None}


def test_prepare_user_message_rejects_overlong_text():
    result = chat_service.prepare_user_message(
        message="123456",
        has_pending_image=False,
        max_message_len=5,
        image_only_prompt="请解答图中的题目。",
    )

    assert result == {"message": "123456", "error": "too_long"}


def test_prepare_user_message_checks_generated_prompt_length():
    result = chat_service.prepare_user_message(
        message="",
        has_pending_image=True,
        max_message_len=5,
        image_only_prompt="请解答图中的题目。",
    )

    assert result == {
        "message": "请解答图中的题目。",
        "error": "too_long",
    }