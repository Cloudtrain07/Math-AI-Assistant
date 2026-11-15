# -*- coding: utf-8 -*-
"""隔离 HTTP 假上游 × 真实 LLMClient：上游静默期的心跳与取消行为。

假上游是本进程内的真实 HTTP 服务器（127.0.0.1 随机端口），按脚本控制三段
静默：响应头前（headers_delay）、首个模型分片前（first_chunk_delay）、
生成中途（分段间隔）。LLMClient 用真实 OpenAI SDK 指向该地址，不 mock
任何客户端内部，也不调用任何真实服务商。

被证明的行为契约：
- 上游静默（任意一段）时，生成器仍按 SILENT_HEARTBEAT_SECONDS 间隔产出
  (None, None, None, None) 心跳——下游据此转成 SSE ": ping" 注释行，
  避免 Cloudflare 等代理的空闲读超时（官方默认 125 秒，524）掐断连接；
- 分片正常流动时行为不变：无多余心跳、内容顺序、usage 与 finish_reason
  照旧交付（实时 token 与速度显示的输入不变）；
- 总时长上限不变：超过 effective_max_wait 抛 MaxWaitTimeout；
- 消费端 close（停止/断连）时上游连接被取消：假上游观测到断开。
"""

import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from openai import RateLimitError

from core import llm_client as llm_module
from core.llm_client import LLMClient, MaxWaitTimeout


TEST_KEY_ENV = "TEST_SILENT_UPSTREAM_KEY"
MODEL = "fake-silent-model"


# ===================== 假上游：进程内真实 HTTP SSE 服务器 =====================


class _UpstreamState:
    """假上游的观测记录：线程安全，供测试断言用。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[float] = []
        self.bodies: list[dict] = []
        self.headers_sent_at: float | None = None
        self.first_chunk_at: float | None = None
        self.done_at: float | None = None
        self.disconnected = threading.Event()
        self.disconnected_at: float | None = None


def _chunk(text: str) -> dict:
    return {
        "id": "f1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": MODEL,
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }


def _finish_chunk() -> dict:
    return {
        "id": "f1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": MODEL,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }


def _usage_chunk() -> dict:
    return {
        "id": "f1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": MODEL,
        "choices": [],
        "usage": {"prompt_tokens": 4, "completion_tokens": 7, "total_tokens": 11},
    }


def _make_handler(state: _UpstreamState, plan: dict):
    """构造绑定观测状态与脚本的请求处理器。

    plan 字段：
    - headers_delay：收到请求后、发出响应头前的静默秒数；
    - first_chunk_delay：响应头后、首个分片前的静默秒数；
    - pieces：[(文本, 该分片前静默秒数), ...]，生成中途静默写在间隔里；
    - hold_headers：扣住响应头不放（秒数安全阀），对所有请求生效（含摘要），
      期间轮询探测客户端断开；
    - 非流式请求（body.stream=false，摘要等 complete() 调用）：
      complete_delay 延迟秒数、complete_content 返回的补全文本、
      complete_status 状态码（默认 200，429 时回错误 JSON）、
      complete_retry_after 响应头 Retry-After 值。
    """

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:  # 静默测试服务器，不刷测试输出
            pass

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw_body = self.rfile.read(length) if length else b""
            with state.lock:
                state.requests.append(time.monotonic())
                try:
                    state.bodies.append(json.loads(raw_body.decode("utf-8")))
                except Exception:
                    state.bodies.append({})
            request_body = state.bodies[-1]

            # 扣住响应头：对所有请求（摘要/聊天）生效；轮询等待客户端断开
            # （取消证据）或安全阀到期
            if plan.get("hold_headers"):
                deadline = time.monotonic() + float(plan["hold_headers"])
                while time.monotonic() < deadline:
                    if self._client_gone():
                        return
                    time.sleep(0.05)
                return

            # 非流式请求（摘要等 complete() 调用）：按脚本延迟/状态码返回；
            # 延迟分段轮询，客户端断开（取消）可被及时观测
            if request_body.get("stream") is False:
                if plan.get("complete_delay"):
                    delay_deadline = time.monotonic() + float(plan["complete_delay"])
                    while time.monotonic() < delay_deadline:
                        if self._client_gone():
                            return
                        time.sleep(0.05)
                if self._client_gone():
                    return
                status = int(plan.get("complete_status") or 200)
                if status != 200:
                    error_body = json.dumps(
                        {
                            "error": {
                                "message": plan.get(
                                    "complete_error_message", "假上游错误响应"
                                ),
                                "type": (
                                    "rate_limit_error" if status == 429 else "api_error"
                                ),
                            }
                        },
                        ensure_ascii=False,
                    ).encode("utf-8")
                    self.send_response(status)
                    if plan.get("complete_retry_after") is not None:
                        self.send_header(
                            "Retry-After", str(plan["complete_retry_after"])
                        )
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(error_body)))
                    self.end_headers()
                    self.wfile.write(error_body)
                    self.wfile.flush()
                    with state.lock:
                        state.done_at = time.monotonic()
                    return
                payload = {
                    "id": "c1",
                    "object": "chat.completion",
                    "created": 1,
                    "model": MODEL,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": plan.get("complete_content", "摘要内容"),
                                "finish_reason": "stop",
                            },
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 2,
                        "total_tokens": 5,
                    },
                }
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
                with state.lock:
                    state.done_at = time.monotonic()
                return

            if plan.get("headers_delay"):
                time.sleep(plan["headers_delay"])
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            with state.lock:
                state.headers_sent_at = time.monotonic()

            if plan.get("first_chunk_delay"):
                time.sleep(plan["first_chunk_delay"])
            for text, delay in plan.get("pieces") or []:
                if delay:
                    time.sleep(delay)
                if self._client_gone():
                    return
                if not self._write_event(_chunk(text)):
                    return
            with state.lock:
                if state.first_chunk_at is None:
                    state.first_chunk_at = time.monotonic()
            self._write_event(_finish_chunk())
            self._write_event(_usage_chunk())
            self._write_raw(b"data: [DONE]\n\n")
            with state.lock:
                state.done_at = time.monotonic()
            # Connection: close：随处理结束关闭连接

        def _write_event(self, payload: dict) -> bool:
            return self._write_raw(
                f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
            )

        def _write_raw(self, data: bytes) -> bool:
            try:
                self.wfile.write(data)
                self.wfile.flush()
                return True
            except OSError:
                self._mark_disconnected()
                return False

        def _client_gone(self) -> bool:
            """写间隙探测客户端是否已断开：读到 EOF/错误即断开。"""
            try:
                self.connection.settimeout(0.05)
                data = self.connection.recv(1)
            except (TimeoutError, socket.timeout):
                return False
            except OSError:
                self._mark_disconnected()
                return True
            finally:
                try:
                    self.connection.settimeout(None)
                except OSError:
                    pass
            if data == b"":
                self._mark_disconnected()
                return True
            return False

        def _mark_disconnected(self) -> None:
            if not state.disconnected.is_set():
                state.disconnected_at = time.monotonic()
                state.disconnected.set()

    return Handler


class FakeUpstream:
    """按脚本响应 /v1/chat/completions 的 SSE 假上游（每个实例一个端口）。

    plan 为同一字典对象的引用，处理器按请求时刻取值——测试可在两次
    请求之间改写 self.plan 切换脚本（如扣头→正常流式）。
    """

    def __init__(self, plan: dict) -> None:
        self.plan = plan
        self.state = _UpstreamState()
        handler = _make_handler(self.state, self.plan)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(
            target=self.server.serve_forever, daemon=True, name="maa-fake-upstream"
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"


def make_client(port: int) -> LLMClient:
    """真实 LLMClient（真实 OpenAI SDK），仅指向本进程内的假上游。"""
    return LLMClient(
        provider={
            "id": "fake-upstream",
            "name": "假上游",
            "base_url": f"http://127.0.0.1:{port}/v1",
            "api_key_env": TEST_KEY_ENV,
            "models": [MODEL],
            "default_model": MODEL,
            "thinking_param": "none",
        }
    )


@pytest.fixture
def short_heartbeat(monkeypatch):
    """把静默心跳间隔调短：测试不必等待真实的 10 秒节奏。

    raising=False：对未实现独立心跳的旧代码本 fixture 等于无操作——
    此时静默段内没有任何输出，正好作为改前基线被下面的断言记录。
    """
    monkeypatch.setattr(llm_module, "SILENT_HEARTBEAT_SECONDS", 0.4, raising=False)


@pytest.fixture
def upstream_key(monkeypatch):
    monkeypatch.setenv(TEST_KEY_ENV, "dummy-key")


class TimedStream:
    """带超时的生成器消费器：每次 next 在独立线程执行，可按步限时。

    next() 抛出的异常（含 StopIteration 与 MaxWaitTimeout）在调用线程
    原样重抛，保证断言语义与直接迭代一致。
    """

    def __init__(self, gen) -> None:
        self._gen = gen
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="maa-pull")

    def next_with_timeout(self, timeout: float):
        future = self._pool.submit(next, self._gen)
        try:
            return future.result(timeout=timeout)
        except StopIteration:
            raise

    def close(self) -> None:
        self._pool.shutdown(wait=False)
        self._gen.close()

    def drain(self) -> list[tuple]:
        out = []
        while True:
            try:
                out.append(self.next_with_timeout(timeout=20))
            except StopIteration:
                return out


def is_heartbeat(t: tuple) -> bool:
    return t[0] is None and t[1] is None and t[2] is None and t[3] is None


def content_of(t: tuple) -> str:
    return t[0] or ""


# ===================== 静默期心跳 =====================


def test_heartbeat_while_waiting_for_response_headers(
    short_heartbeat, upstream_key
):
    """响应头前静默：心跳先于任何内容到达（改前此段完全无输出）。"""
    upstream = FakeUpstream(
        {
            "headers_delay": 1.5,
            "pieces": [("第一段", 0), ("第二段", 0)],
        }
    )
    upstream.start()
    try:
        client = make_client(upstream.port)
        stream = TimedStream(
            client.chat_stream_with_usage(
                [{"role": "user", "content": "问题"}], max_wait_seconds=30
            )
        )
        t0 = time.monotonic()
        first = stream.next_with_timeout(timeout=10)
        elapsed = time.monotonic() - t0
        # 响应头 1.5 秒后才可能发出：更早到达的只能是静默心跳
        assert elapsed < 1.4, f"静默期内无心跳（首个输出在 {elapsed:.2f}s）"
        assert is_heartbeat(first), f"静默期首输出不是心跳：{first!r}"
        rest = stream.drain()
        assert "".join(content_of(t) for t in rest) == "第一段第二段"
        usage = [t for t in rest if t[2] is not None]
        assert len(usage) == 1 and usage[0][3] == "stop"
    finally:
        stream.close()
        upstream.stop()


def test_heartbeat_before_first_chunk(short_heartbeat, upstream_key):
    """首分片前静默：响应头已发出但模型迟迟不出字，心跳照常产出。"""
    upstream = FakeUpstream(
        {
            "first_chunk_delay": 1.5,
            "pieces": [("答案A", 0), ("答案B", 0)],
        }
    )
    upstream.start()
    try:
        client = make_client(upstream.port)
        stream = TimedStream(
            client.chat_stream_with_usage(
                [{"role": "user", "content": "问题"}], max_wait_seconds=30
            )
        )
        t0 = time.monotonic()
        first = stream.next_with_timeout(timeout=10)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.4, f"静默期内无心跳（首个输出在 {elapsed:.2f}s）"
        assert is_heartbeat(first), f"静默期首输出不是心跳：{first!r}"
        rest = stream.drain()
        assert "".join(content_of(t) for t in rest) == "答案A答案B"
    finally:
        stream.close()
        upstream.stop()


def test_heartbeat_during_mid_generation_gap(short_heartbeat, upstream_key):
    """生成中途静默：分片之间长时间无字，间隙里产出心跳。"""
    upstream = FakeUpstream(
        {"pieces": [("前半", 0), ("后半", 1.5)]}
    )
    upstream.start()
    try:
        client = make_client(upstream.port)
        stream = TimedStream(
            client.chat_stream_with_usage(
                [{"role": "user", "content": "问题"}], max_wait_seconds=30
            )
        )
        first = stream.next_with_timeout(timeout=10)
        assert content_of(first) == "前半"
        t0 = time.monotonic()
        gap_item = stream.next_with_timeout(timeout=10)
        elapsed = time.monotonic() - t0
        # 间隙 1.5 秒：心跳间隔 0.4 秒，应先于后半内容到达
        assert is_heartbeat(gap_item), f"生成中途静默无心跳：{gap_item!r}"
        assert elapsed < 1.2, f"间隙内首个输出迟至 {elapsed:.2f}s"
        rest = stream.drain()
        assert "".join(content_of(t) for t in [first] + rest) == "前半后半"
        usage = [t for t in rest if t[2] is not None]
        assert len(usage) == 1 and usage[0][3] == "stop"
    finally:
        stream.close()
        upstream.stop()


def test_no_extra_heartbeat_when_chunks_flow(short_heartbeat, upstream_key):
    """分片连续到达时行为不变：不注入额外心跳（既有 1 秒节奏不触发）。"""
    upstream = FakeUpstream({"pieces": [("一", 0), ("二", 0), ("三", 0)]})
    upstream.start()
    try:
        client = make_client(upstream.port)
        tuples = list(
            client.chat_stream_with_usage([{"role": "user", "content": "问题"}])
        )
        assert not any(is_heartbeat(t) for t in tuples), "连续分片流中出现了多余心跳"
        assert "".join(content_of(t) for t in tuples) == "一二三"
        usage = [t for t in tuples if t[2] is not None]
        assert len(usage) == 1 and usage[0][3] == "stop"
    finally:
        upstream.stop()


# ===================== 等待上限与取消（既有行为保持） =====================


def test_max_wait_bounds_total_silence(short_heartbeat, upstream_key):
    """全程静默超过覆盖等待值：抛 MaxWaitTimeout（不再依赖 SDK 读超时+重试）。"""
    upstream = FakeUpstream({"headers_delay": 10, "pieces": []})
    upstream.start()
    try:
        client = make_client(upstream.port)
        stream = TimedStream(
            client.chat_stream_with_usage(
                [{"role": "user", "content": "问题"}], max_wait_seconds=2
            )
        )
        t0 = time.monotonic()
        with pytest.raises(MaxWaitTimeout):
            while True:
                stream.next_with_timeout(timeout=15)
        elapsed = time.monotonic() - t0
        assert elapsed < 8, f"等待上限未生效（{elapsed:.2f}s 才抛出）"
    finally:
        stream.close()
        upstream.stop()


def test_close_cancels_upstream_connection(short_heartbeat, upstream_key):
    """停止/断连：close 后假上游很快观测到连接断开（上游被取消）。"""
    upstream = FakeUpstream(
        {"pieces": [(f"第{i}段。", 0.15) for i in range(200)]}
    )
    upstream.start()
    try:
        client = make_client(upstream.port)
        gen = client.chat_stream_with_usage(
            [{"role": "user", "content": "问题"}], max_wait_seconds=60
        )
        pulled = [next(gen), next(gen)]
        assert "".join(content_of(t) for t in pulled) == "第0段。第1段。"
        t0 = time.monotonic()
        gen.close()
        close_seconds = time.monotonic() - t0
        assert close_seconds < 3, f"close 耗时 {close_seconds:.2f}s，疑似未即时取消"
        assert upstream.state.disconnected.wait(timeout=3), (
            "close 后假上游未观测到客户端断开：上游连接未被取消"
        )
    finally:
        upstream.stop()


# ===================== 建流阶段（响应头前）的停止与超时 =====================


def test_stop_while_upstream_holds_headers(short_heartbeat, upstream_key):
    """扣住响应头时停止：静默心跳照常产出；close 取消建流（TCP 断开）。"""
    upstream = FakeUpstream({"hold_headers": 30})
    upstream.start()
    try:
        client = make_client(upstream.port)
        stream = TimedStream(
            client.chat_stream_with_usage(
                [{"role": "user", "content": "问题"}], max_wait_seconds=60
            )
        )
        first = stream.next_with_timeout(timeout=5)
        assert is_heartbeat(first), f"建流静默期首个输出不是心跳：{first!r}"
        t0 = time.monotonic()
        stream.close()
        assert time.monotonic() - t0 < 5, "扣头期间的 close 未及时返回"
        assert upstream.state.disconnected.wait(timeout=5), (
            "close 后假上游未观测到 TCP 断开：建流阶段未被取消"
        )
    finally:
        stream.close()
        upstream.stop()


def test_preheader_timeout_cancels_request_and_retries(upstream_key):
    """默认 10 秒心跳间隔下的总超时：从建流起算，取消在途请求与重试。

    记录口径：实际耗时 ≈ 覆盖值 3 秒（非心跳量化）、假上游仅收到 1 次
    请求（SDK 重试因连接池关闭未再到达服务器）、异常类型 MaxWaitTimeout。
    """
    upstream = FakeUpstream({"hold_headers": 600})
    upstream.start()
    try:
        client = make_client(upstream.port)
        stream = TimedStream(
            client.chat_stream_with_usage(
                [{"role": "user", "content": "问题"}], max_wait_seconds=3
            )
        )
        t0 = time.monotonic()
        with pytest.raises(MaxWaitTimeout):
            while True:
                stream.next_with_timeout(timeout=15)
        elapsed = time.monotonic() - t0
        assert elapsed < 8, f"总超时未按覆盖值生效（耗时 {elapsed:.2f}s）"
        assert len(upstream.state.requests) == 1, (
            f"重试未被取消：假上游共收到 {len(upstream.state.requests)} 次请求"
        )
        assert upstream.state.disconnected.wait(timeout=5), (
            "超时后假上游未观测到连接断开"
        )
    finally:
        stream.close()
        upstream.stop()


def test_close_during_first_read_cancels(short_heartbeat, upstream_key):
    """建流完成后、首个分片前停止：经流登记处关闭真实连接。"""
    upstream = FakeUpstream(
        {"first_chunk_delay": 5.0, "pieces": [("晚到的答案", 0)]}
    )
    upstream.start()
    try:
        client = make_client(upstream.port)
        gen = client.chat_stream_with_usage(
            [{"role": "user", "content": "问题"}], max_wait_seconds=60
        )
        first = next(gen)  # 心跳：建流已完成并登记，首个分片尚在延迟中
        assert is_heartbeat(first)
        t0 = time.monotonic()
        gen.close()
        assert time.monotonic() - t0 < 3, "close 未及时返回（清理卡顿）"
        assert upstream.state.disconnected.wait(timeout=5)
    finally:
        upstream.stop()


class _RaceStream:
    """假流：首读阻塞直到被 close 打断，模拟真实流被关闭时的读中断。"""

    def __init__(self) -> None:
        self.closed = False
        self.gate = threading.Event()

    def __iter__(self):
        return self

    def __next__(self):
        while not self.gate.wait(timeout=0.05):
            if self.closed:
                raise OSError("connection aborted")
        raise StopIteration

    def close(self):
        self.closed = True


def test_stop_race_at_stream_creation(monkeypatch, upstream_key):
    """停止恰好发生在建流完成时：流被确定性关闭，close 及时返回。

    fake create 返回后测试立即 close：无论主侧先关（经流登记处）还是
    工作线程侧兜底关，closed 都必须成立；真实连接池的关闭兜底由
    HTTP 假上游用例覆盖。
    """
    monkeypatch.setattr(llm_module, "SILENT_HEARTBEAT_SECONDS", 0.2)
    created = threading.Event()
    release = threading.Event()
    race_box: list = []

    def fake_create(**kwargs):
        release.wait(timeout=10)  # 建流被扣住
        stream = _RaceStream()
        race_box.append(stream)
        created.set()  # create() 已返回（建流完成）
        return stream

    fake_sdk = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
    )
    monkeypatch.setattr(llm_module, "OpenAI", lambda **kwargs: fake_sdk)

    client = LLMClient(
        provider={
            "id": "race",
            "base_url": "https://example.invalid/v1",
            "api_key_env": TEST_KEY_ENV,
            "default_model": MODEL,
            "thinking_param": "none",
        }
    )
    gen = client.chat_stream_with_usage([{"role": "user", "content": "问题"}])
    puller = ThreadPoolExecutor(max_workers=1, thread_name_prefix="maa-pull")
    try:
        first = puller.submit(next, gen).result(timeout=5)
        assert is_heartbeat(first), "建流扣住期间未产出静默心跳"
        release.set()
        assert created.wait(timeout=5), "fake create 未完成"
        time.sleep(0.05)  # 留出工作线程登记流并进入首读的时间
        t0 = time.monotonic()
        gen.close()  # 恰在建流完成后的首读阻塞期停止
        assert time.monotonic() - t0 < 3, "close 未及时返回（清理卡顿）"
        assert race_box[0].closed, "建流完成瞬间停止：上游流未被关闭"
    finally:
        puller.shutdown(wait=False)
        gen.close()


def test_complete_total_timeout_bounds_summary(monkeypatch, upstream_key):
    """摘要总时限为单一截止：到期取消在途请求，重试不被发出。

    预算压到 0.5 秒、摘要响应延迟 2 秒（响应头已到、正文迟迟不来）：
    实际耗时 ≈0.5 秒、仅 1 次请求（SDK 自动重试已关闭）、到期后连接被取消。
    """
    monkeypatch.setattr(llm_module, "COMPLETE_TOTAL_TIMEOUT_SECONDS", 0.5)
    upstream = FakeUpstream({"complete_delay": 2.0})
    upstream.start()
    try:
        client = make_client(upstream.port)
        t0 = time.monotonic()
        with pytest.raises(Exception) as excinfo:
            client.complete(
                [{"role": "user", "content": "压缩这些消息"}],
                thinking=False,
                max_tokens=600,
            )
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0, f"摘要总截止未生效（耗时 {elapsed:.2f}s）"
        assert isinstance(excinfo.value, TimeoutError), (
            f"异常类型：{type(excinfo.value).__name__}"
        )
        assert len(upstream.state.requests) == 1, (
            f"到期后不得发出后续请求，实际 {len(upstream.state.requests)} 次"
        )
        assert upstream.state.disconnected.wait(timeout=5), (
            "到期后在途连接未被取消"
        )
    finally:
        upstream.stop()


def test_complete_retry_after_does_not_extend_budget(monkeypatch, upstream_key):
    """429 + Retry-After 不再拖长摘要：SDK 自动重试已关闭，预算独立成立。

    预算 1 秒，上游返回 429 且 Retry-After: 2：修复前 SDK 会按 Retry-After
    睡眠并重试，总耗时被放大（约 4.3 秒、3 次请求）；现在 429 到达即抛错，
    仅 1 次请求，总耗时远低于 Retry-After 本身。
    """
    monkeypatch.setattr(llm_module, "COMPLETE_TOTAL_TIMEOUT_SECONDS", 1.0)
    upstream = FakeUpstream({"complete_status": 429, "complete_retry_after": 2})
    upstream.start()
    try:
        client = make_client(upstream.port)
        t0 = time.monotonic()
        with pytest.raises(RateLimitError):
            client.complete(
                [{"role": "user", "content": "压缩这些消息"}],
                thinking=False,
                max_tokens=600,
            )
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0, f"Retry-After 仍在拖长总时长（耗时 {elapsed:.2f}s）"
        assert len(upstream.state.requests) == 1, (
            f"429 后不得重试，实际 {len(upstream.state.requests)} 次"
        )
    finally:
        upstream.stop()


def test_complete_deadline_cancels_held_headers(monkeypatch, upstream_key):
    """上游收到摘要请求后一直不返回响应头：截止到期取消在途请求。"""
    monkeypatch.setattr(llm_module, "COMPLETE_TOTAL_TIMEOUT_SECONDS", 1.0)
    upstream = FakeUpstream({"hold_headers": 60})
    upstream.start()
    try:
        client = make_client(upstream.port)
        t0 = time.monotonic()
        with pytest.raises(Exception) as excinfo:
            client.complete(
                [{"role": "user", "content": "压缩这些消息"}],
                thinking=False,
                max_tokens=600,
            )
        elapsed = time.monotonic() - t0
        assert elapsed < 5.0, f"摘要总截止未生效（耗时 {elapsed:.2f}s）"
        assert isinstance(excinfo.value, TimeoutError), (
            f"异常类型：{type(excinfo.value).__name__}"
        )
        assert len(upstream.state.requests) == 1, (
            f"到期后不得发出后续请求，实际 {len(upstream.state.requests)} 次"
        )
        assert upstream.state.disconnected.wait(timeout=5), (
            "到期后在途连接未被取消"
        )
    finally:
        upstream.stop()


def test_abort_wakes_blocked_pull_without_heartbeat_wait(upstream_key):
    """默认心跳间隔下，中止句柄立即唤醒阻塞中的拉取（不等下一个心跳）。"""
    upstream = FakeUpstream({"hold_headers": 60})
    upstream.start()
    try:
        handle = llm_module.TurnAbort()
        client = make_client(upstream.port)
        gen = client.chat_stream_with_usage(
            [{"role": "user", "content": "问题"}],
            max_wait_seconds=60,
            abort=handle,
        )
        puller = ThreadPoolExecutor(max_workers=1, thread_name_prefix="maa-pull")
        try:
            fut = puller.submit(next, gen)  # 阻塞在事件队列（默认 10 秒心跳）
            # 等假上游明确收到请求再触发停止：固定 sleep 在回环下也可能早于
            # 请求到达，导致"上游 TCP 已断开"断言无从谈起
            deadline = time.monotonic() + 5
            with upstream.state.lock:
                got_request = bool(upstream.state.requests)
            while not got_request and time.monotonic() < deadline:
                time.sleep(0.05)
                with upstream.state.lock:
                    got_request = bool(upstream.state.requests)
            assert got_request, "假上游未在时限内收到请求"
            t0 = time.monotonic()
            handle.abort()
            with pytest.raises(llm_module.TurnAborted):
                fut.result(timeout=5)
            wake_seconds = time.monotonic() - t0
            assert wake_seconds < 2.0, (
                f"中止唤醒滞后 {wake_seconds:.2f}s：仍在等下一个心跳"
            )
        finally:
            puller.shutdown(wait=False)
            gen.close()
        assert upstream.state.disconnected.wait(timeout=5), (
            "abort 后假上游未观测到 TCP 断开"
        )
    finally:
        upstream.stop()


def test_complete_abort_cancels_in_flight_request(monkeypatch, upstream_key):
    """中止句柄传入摘要：在途请求被取消，complete 立即返回 TurnAborted。"""
    monkeypatch.setattr(llm_module, "COMPLETE_TOTAL_TIMEOUT_SECONDS", 30.0)
    upstream = FakeUpstream({"complete_delay": 30.0})
    upstream.start()
    try:
        handle = llm_module.TurnAbort()
        client = make_client(upstream.port)
        box: dict = {}

        def _call() -> None:
            try:
                client.complete(
                    [{"role": "user", "content": "压缩这些消息"}],
                    thinking=False,
                    max_tokens=600,
                    abort=handle,
                )
                box["exc"] = None
            except BaseException as exc:  # 工作线程内异常原样收集供断言
                box["exc"] = exc

        worker = threading.Thread(target=_call, daemon=True, name="maa-complete")
        worker.start()
        # 等假上游明确收到摘要请求再中止（不用固定 sleep）
        deadline = time.monotonic() + 5
        with upstream.state.lock:
            got_request = bool(upstream.state.requests)
        while not got_request and time.monotonic() < deadline:
            time.sleep(0.05)
            with upstream.state.lock:
                got_request = bool(upstream.state.requests)
        assert got_request, "假上游未在时限内收到摘要请求"

        t0 = time.monotonic()
        handle.abort()
        while worker.is_alive() and time.monotonic() - t0 < 5:
            time.sleep(0.05)
        abort_seconds = time.monotonic() - t0
        assert not worker.is_alive(), "中止后 complete 未及时返回"
        assert abort_seconds < 2.0, f"中止响应滞后 {abort_seconds:.2f}s"
        assert isinstance(box.get("exc"), llm_module.TurnAborted), (
            f"异常类型：{type(box.get('exc')).__name__}"
        )
        assert len(upstream.state.requests) == 1, "中止后不得发出后续请求"
        assert upstream.state.disconnected.wait(timeout=5), (
            "中止后在途连接未被取消"
        )
    finally:
        upstream.stop()
