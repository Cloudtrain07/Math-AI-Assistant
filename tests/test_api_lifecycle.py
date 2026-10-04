# -*- coding: utf-8 -*-
"""停止/断连生命周期测试：真实 uvicorn HTTP 流 + 可控慢速假客户端。

验证点（不把垃圾回收当作清理手段）：
- 客户端中断读取后，核心同步生成器链被确定性 close（假客户端 finally 记录）；
- busy 随之释放，同账号后续请求正常执行；
- 流进行中同账号新请求被 busy 拒绝；
- 正常完成路径同样被 close，且 done 事件契约完整。
"""

import threading
import time
from types import SimpleNamespace

import httpx
import pytest
import uvicorn

from api import ChatRegistry, create_api_app
from conftest import CountingDailyUsageStore, MemoryHistoryStore


class SlowClient:
    """可控慢速假客户端：慢速分片输出，并记录上游迭代是否被关闭。"""

    supports_vision = False
    model = "slow-model"

    def __init__(self, pieces: int = 60, delay: float = 0.02) -> None:
        self.pieces = pieces
        self.delay = delay
        self.calls = 0
        self.closed = True  # 尚未开始迭代时视为已关闭
        self.started = threading.Event()

    def chat_stream_with_usage(self, messages, **kwargs):
        self.calls += 1
        self.closed = False
        try:
            self.started.set()
            for i in range(self.pieces):
                yield f"这是第{i}片内容。", None, None, None
                time.sleep(self.delay)
            yield None, None, SimpleNamespace(
                total_tokens=11, prompt_tokens=5, completion_tokens=6
            ), "stop"
        finally:
            self.closed = True


@pytest.fixture
def server(api_runtime):
    """进程内真实 uvicorn 服务器（随机端口），假客户端与内存存储。"""
    api_runtime.client = SlowClient()
    api_runtime.history_store = MemoryHistoryStore()
    api_runtime.daily_usage_store = CountingDailyUsageStore()
    api = create_api_app(api_runtime, registry=ChatRegistry())

    sock = socket_tcp()
    port = sock.getsockname()[1]
    sock.close()
    config = uvicorn.Config(api, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn 未能在时限内启动"

    yield f"http://127.0.0.1:{port}", api_runtime

    server.should_exit = True
    thread.join(timeout=10)


def socket_tcp():
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    return sock


def login(client: httpx.Client, account: dict) -> None:
    resp = client.post(
        "/api/login",
        json={"username": account["username"], "password": account["password"]},
    )
    assert resp.status_code == 200


def test_disconnect_closes_upstream_and_releases_busy(server):
    base, api_runtime = server
    fake: SlowClient = api_runtime.client
    account = api_runtime.accounts[0]

    with httpx.Client(base_url=base) as client:
        login(client, account)
        with client.stream(
            "POST", "/api/chat", json={"message": "会被中断的问题"}
        ) as stream:
            assert stream.status_code == 200
            got_delta = False
            # 注意：必须保留迭代器引用。httpx 的流式迭代器被丢弃时
            # 会经引用计数立即关闭响应（真实断连），连接就不在了。
            lines = stream.iter_lines()
            for line in lines:
                if line.startswith("event: delta"):
                    got_delta = True
                    break
            assert got_delta, "中断前未收到任何增量"

            # 流进行中：同账号新请求被 busy 拒绝（真实 HTTP 路径）
            with httpx.Client(base_url=base) as other:
                login(other, account)
                busy_resp = other.post("/api/chat", json={"message": "并发请求"})
                assert "event: rejected" in busy_resp.text
                assert '"status": "busy"' in busy_resp.text
            # 此后放弃迭代器 → 连接关闭 → 服务端检测断连

        # 退出 with：客户端停止读取 → 连接中断
        deadline = time.time() + 5
        while not fake.closed and time.time() < deadline:
            time.sleep(0.05)
        assert fake.closed, (
            "断连后上游生成器未被确定性关闭（仍在依赖垃圾回收或未关闭）"
        )

    # busy 已释放：中断后同账号新请求正常完成
    with httpx.Client(base_url=base) as client:
        login(client, account)
        resp = client.post("/api/chat", json={"message": "中断后的新问题"})
        assert resp.status_code == 200
        assert "event: done" in resp.text
        assert "event: rejected" not in resp.text


def test_normal_completion_closes_and_full_contract(server):
    base, api_runtime = server
    fake: SlowClient = api_runtime.client
    account = api_runtime.accounts[0]

    with httpx.Client(base_url=base, timeout=30) as client:
        login(client, account)
        resp = client.post("/api/chat", json={"message": "完整走完的问题"})
        assert resp.status_code == 200
        assert "event: start" in resp.text
        assert "event: done" in resp.text
        # done 契约：save_status 与用量字段在真实 HTTP 流中完整
        assert '"save_status": "saved"' in resp.text
        assert '"turn_total": 11' in resp.text
        # 正常完成后生成器被 close（幂等，不依赖 GC）
        deadline = time.time() + 5
        while not fake.closed and time.time() < deadline:
            time.sleep(0.05)
        assert fake.closed
