# -*- coding: utf-8 -*-
"""本机假代理回归：摘要单一截止时间覆盖代理 CONNECT 与 TLS 握手阶段。

Codex 复现的口子：complete() 的看门狗关闭连接池时关不掉握手期的连接，
异步取消作用域（anyio.fail_after）可以。本测试用本机 HTTP 假代理复现
同一阻塞形态——CONNECT 延迟应答，隧道建立后收到 TLS ClientHello 但
不完成握手——验证预算到期时 complete() 立即返回、代理观测到连接关闭、
且没有额外请求（SDK 自动重试保持关闭）。

代理与目标主机只在测试进程内生效（monkeypatch 环境变量），不影响
正式环境的代理设置；目标主机不在 conftest 的回环 NO_PROXY 名单内，
确保请求确实经由假代理。
"""

import socket
import threading
import time

import pytest

from core import llm_client as llm_module
from core.llm_client import LLMClient

TEST_KEY_ENV = "TEST_SILENT_UPSTREAM_KEY"
PROXY_TARGET = "https://proxy-target.example.invalid:9443/v1"


class FakeConnectProxy:
    """最小 HTTP CONNECT 假代理：延迟应答，隧道期轮询观测客户端断开。"""

    def __init__(self, connect_delay: float, hold: float = 60.0) -> None:
        self.connect_delay = connect_delay
        self.hold = hold
        self.lock = threading.Lock()
        self.connects: list[float] = []
        self.disconnected = threading.Event()
        self.disconnected_at: float | None = None
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(5)
        self.port = self._server.getsockname()[1]
        self._thread = threading.Thread(
            target=self._serve, daemon=True, name="maa-fake-proxy"
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.close()

    def _serve(self) -> None:
        while True:
            try:
                conn, _addr = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(5)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            with self.lock:
                self.connects.append(time.monotonic())
            if self.connect_delay:
                time.sleep(self.connect_delay)
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            # 隧道期：读到的 TLS ClientHello 直接忽略，轮询观测客户端断开
            deadline = time.monotonic() + self.hold
            conn.settimeout(0.05)
            while time.monotonic() < deadline:
                try:
                    if conn.recv(4096) == b"":
                        break
                except (TimeoutError, socket.timeout):
                    pass
                except OSError:
                    break
            if not self.disconnected.is_set():
                self.disconnected_at = time.monotonic()
                self.disconnected.set()
        finally:
            try:
                conn.close()
            except OSError:
                pass


def test_proxy_connect_and_tls_handshake_within_budget(monkeypatch):
    """CONNECT 延迟 0.8 秒 + TLS 握手不完成：预算 1 秒到期即返回。

    记录：complete 总耗时 ≈ 预算（修复前线程池看门狗关不掉握手期连接，
    实测约 2.09 秒）、代理恰收到 1 次 CONNECT（SDK 重试保持关闭）、
    预算到期后代理观测到连接关闭。
    """
    proxy = FakeConnectProxy(connect_delay=0.8)
    proxy.start()
    monkeypatch.setattr(llm_module, "COMPLETE_TOTAL_TIMEOUT_SECONDS", 1.0)
    # 仅测试进程内：目标主机经本机假代理（正式环境代理设置不受影响；
    # 目标主机不在 conftest 的回环 NO_PROXY 名单内）
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{proxy.port}")
    monkeypatch.setenv(TEST_KEY_ENV, "dummy-key")
    try:
        client = LLMClient(
            provider={
                "id": "proxy-target",
                "name": "假代理目标",
                "base_url": PROXY_TARGET,
                "api_key_env": TEST_KEY_ENV,
                "models": ["fake-silent-model"],
                "default_model": "fake-silent-model",
                "thinking_param": "none",
            }
        )
        t0 = time.monotonic()
        with pytest.raises(TimeoutError):
            client.complete(
                [{"role": "user", "content": "压缩这些消息"}],
                thinking=False,
                max_tokens=600,
            )
        elapsed = time.monotonic() - t0
        assert elapsed < 1.5, f"总预算未覆盖代理握手（耗时 {elapsed:.2f}s）"
        with proxy.lock:
            n_connects = len(proxy.connects)
        assert n_connects == 1, f"代理收到 {n_connects} 次 CONNECT，不应有重试"
        assert proxy.disconnected.wait(timeout=5), (
            "预算到期后代理未观测到连接关闭"
        )
    finally:
        proxy.stop()
