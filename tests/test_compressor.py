# -*- coding: utf-8 -*-
"""上下文压缩器测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.compressor import HistoryCompressor


class _FakeClient:
    def __init__(self, reply="摘要内容"):
        self.reply = reply
        self.calls = 0

    def complete(self, messages, thinking=None, reasoning_effort=None, max_tokens=500):
        self.calls += 1
        return self.reply


def _history(n):
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"消息{i}"}
        for i in range(n)
    ]


def test_compressor_generates_and_caches_summary():
    client = _FakeClient("已压缩的早期对话")
    compressor = HistoryCompressor()
    history = _history(5)

    summary = compressor.get_summary("user1", history, window_size=3, client=client)
    assert summary == "已压缩的早期对话"
    assert client.calls == 1

    # 同一份历史再次读取时直接命中缓存，不重复调用模型
    summary2 = compressor.get_summary("user1", history, window_size=3, client=client)
    assert summary2 == "已压缩的早期对话"
    assert client.calls == 1


def test_compressor_incremental_update():
    client = _FakeClient("摘要")
    compressor = HistoryCompressor()

    compressor.get_summary("user1", _history(4), window_size=2, client=client)
    assert client.calls == 1

    # 多了一条新消息后，只需把新掉出窗口的消息并入摘要
    compressor.get_summary("user1", _history(5), window_size=2, client=client)
    assert client.calls == 2


def test_compressor_reset():
    client = _FakeClient("摘要")
    compressor = HistoryCompressor()
    compressor.get_summary("user1", _history(4), window_size=2, client=client)
    compressor.reset("user1")

    compressor.get_summary("user1", _history(4), window_size=2, client=client)
    assert client.calls == 2


def test_compressor_fallback_when_model_fails():
    client = _FakeClient("")
    compressor = HistoryCompressor()
    summary = compressor.get_summary("user1", _history(4), window_size=2, client=client)
    assert "用户" in summary or "助手" in summary
