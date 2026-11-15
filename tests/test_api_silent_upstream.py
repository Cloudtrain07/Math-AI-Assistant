# -*- coding: utf-8 -*-
"""api 层 × 隔离 HTTP 假上游：真实 LLMClient 走完整 SSE 链路的行为契约。

与服务商无关的端到端验证（真实 uvicorn + 真实 OpenAI SDK 指向进程内假上游，
不 mock 客户端内部，不调用任何真实服务商）：
- 完整轮次：SSE 事件齐全、实时 usage 转发、done 契约（速度字段真实口径）；
- 上游静默（首个模型分片前 / 生成中途）：SSE ": ping" 注释行按静默心跳
  间隔产出（改前这些时段为零字节，隧道会被代理空闲读超时掐断）；
- 停止/断连：假上游观测到连接断开（上游被取消）、busy 随之释放、
  未完成的回答不写入历史；
- 已保存历史：继续生成以服务端历史构造指令并正常收尾；
- 真实 uvicorn 的转发头信任边界：回环代理的 X-Forwarded-Proto 被采纳
  （Cookie 带 Secure）、X-Forwarded-For 改写来源（local_admin=False）。
"""

import json
import socket
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlparse

import httpx
import pytest
import uvicorn

from api import create_api_app
from conftest import API_TEST_ACCOUNTS
from core.runtime import build_runtime
from test_llm_silent_upstream import FakeUpstream

TEST_KEY_ENV = "TEST_SILENT_UPSTREAM_KEY"


@pytest.fixture(autouse=True)
def short_heartbeat_and_key(monkeypatch):
    """静默心跳间隔调短（真实 10 秒节奏太慢）；假上游密钥注入环境。"""
    monkeypatch.setattr(
        "core.llm_client.SILENT_HEARTBEAT_SECONDS", 0.4
    )
    monkeypatch.setenv(TEST_KEY_ENV, "dummy-key")


@contextmanager
def running_api(upstream: FakeUpstream, tmp_path):
    """组装 runtime（providers 指向假上游）+ 真实 uvicorn（随机端口）。"""
    providers = [
        {
            "id": "fake-upstream",
            "name": "假上游",
            "base_url": upstream.base_url,
            "api_key_env": TEST_KEY_ENV,
            "models": ["fake-silent-model"],
            "default_model": "fake-silent-model",
            "thinking_param": "none",
        }
    ]
    providers_path = tmp_path / "providers.json"
    providers_path.write_text(json.dumps(providers), encoding="utf-8")
    accounts_path = tmp_path / "accounts.json"
    accounts_path.write_text(
        json.dumps(API_TEST_ACCOUNTS, ensure_ascii=False), encoding="utf-8"
    )
    runtime = build_runtime(
        providers_file=providers_path,
        accounts_path=accounts_path,
        # 每个测试独立数据目录：真实文件历史存储不跨测试串数据
        data_dir=tmp_path / "data",
    )
    app = create_api_app(runtime)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="maa-test-api")
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn 未能在时限内启动"
    try:
        yield runtime, f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        upstream.stop()


def login(client: httpx.Client, base: str, account: dict, **headers) -> httpx.Response:
    resp = client.post(
        f"{base}/api/login",
        json={"username": account["username"], "password": account["password"]},
        headers=headers or None,
    )
    assert resp.status_code == 200
    return resp


# ===================== 完整轮次与实时统计 =====================


def test_full_turn_streams_with_live_usage(tmp_path):
    """真实客户端完整轮次：事件齐全、usage 转发、done 契约与历史保存。"""
    upstream = FakeUpstream(
        {"pieces": [("第一段", 0), ("第二段", 0.01), ("第三段", 0.01)]}
    )
    upstream.start()
    with running_api(upstream, tmp_path) as (runtime, base):
        account = runtime.accounts[0]
        with httpx.Client(base_url=base, timeout=30, trust_env=False) as client:
            login(client, base, account)
            resp = client.post("/api/chat", json={"message": "解方程 x+1=2"})
            assert resp.status_code == 200
            text = resp.text
            assert "event: start" in text
            assert "event: delta" in text
            assert "event: usage" in text          # 实时 token 显示的数据源
            assert '"completion_tokens": 7' in text
            assert "event: done" in text
            assert '"save_status": "saved"' in text
            assert '"turn_total": 11' in text      # 假上游 usage 的 total_tokens
            assert '"tok_s_estimated": false' in text  # 真实用量口径，非估算
            history = client.get("/api/history").json()["history"]
            assert [m["role"] for m in history] == ["user", "assistant"]
            assert "解方程" in history[0]["content"]


# ===================== 静默期的 SSE 保活 =====================


def _collect_stream_lines(client: httpx.Client, base: str, payload: dict):
    """流式读取一轮 SSE，返回行列表与响应头（读到流结束）。"""
    lines = []
    headers = None
    with client.stream("POST", f"{base}/api/chat", json=payload) as stream:
        assert stream.status_code == 200
        headers = stream.headers
        for line in stream.iter_lines():
            lines.append(line)
    return headers, lines


def test_sse_pings_before_first_chunk(tmp_path):
    """首个模型分片前静默：start 之后、首个 delta 之前有 ": ping" 注释行。"""
    upstream = FakeUpstream(
        {"first_chunk_delay": 1.5, "pieces": [("静默后答案", 0)]}
    )
    upstream.start()
    with running_api(upstream, tmp_path) as (runtime, base):
        account = runtime.accounts[0]
        with httpx.Client(base_url=base, timeout=30, trust_env=False) as client:
            login(client, base, account)
            headers, lines = _collect_stream_lines(
                client, base, {"message": "会静默一阵的题目"}
            )
    assert headers["content-type"].startswith("text/event-stream")
    assert headers["cache-control"] == "no-cache"

    first_delta = lines.index("event: delta")
    start_index = lines.index("event: start")
    pings = [i for i, line in enumerate(lines) if line == ": ping"]
    assert pings, "整个流中没有任何 SSE 心跳"
    # 静默期（start 与首个 delta 之间）必须有 ping；delta 之后仍可能有 ping
    # （finish/usage 分片同样映射为注释行，属既有语义），不做限制
    pings_in_silence = [i for i in pings if start_index < i < first_delta]
    assert pings_in_silence, "首个分片前静默期没有 SSE 心跳"
    # 静默后的内容照常完整到达
    assert any("静默后答案" in line for line in lines)


def test_sse_pings_during_mid_generation_gap(tmp_path):
    """生成中途静默：两个 delta 之间出现 ": ping"，内容顺序不变。"""
    upstream = FakeUpstream({"pieces": [("前半。", 0), ("后半。", 1.5)]})
    upstream.start()
    with running_api(upstream, tmp_path) as (runtime, base):
        account = runtime.accounts[0]
        with httpx.Client(base_url=base, timeout=30, trust_env=False) as client:
            login(client, base, account)
            _, lines = _collect_stream_lines(
                client, base, {"message": "中途会停顿的题目"}
            )
    first_half = next(i for i, ln in enumerate(lines) if "前半。" in ln)
    second_half = next(i for i, ln in enumerate(lines) if "后半。" in ln)
    pings_between = [
        i for i, line in enumerate(lines) if line == ": ping" and first_half < i < second_half
    ]
    assert pings_between, "生成中途静默期没有 SSE 心跳"
    assert second_half > first_half


# ===================== 停止/断连：取消上游、释放 busy、不写历史 =====================


def test_disconnect_cancels_upstream_releases_busy_keeps_history_clean(tmp_path):
    """断连 = 上游取消 + busy 释放 + 未完成回答不进历史；随后新轮次正常。"""
    upstream = FakeUpstream(
        {"pieces": [(f"第{i}段。", 0.15) for i in range(100)]}
    )
    upstream.start()
    with running_api(upstream, tmp_path) as (runtime, base):
        account = runtime.accounts[0]
        with httpx.Client(base_url=base, timeout=30, trust_env=False) as client:
            login(client, base, account)
            with client.stream(
                "POST", f"{base}/api/chat", json={"message": "会被中断的问题"}
            ) as stream:
                assert stream.status_code == 200
                got_delta = False
                # 保留迭代器引用（与 test_api_lifecycle 相同的注意事项）
                for line in stream.iter_lines():
                    if line.startswith("event: delta"):
                        got_delta = True
                        break
                assert got_delta, "中断前未收到任何增量"
            # 退出 with：客户端停止读取 → 断连

        # 假上游观测到连接被取消（真实 TCP 断开，而非仅生成器关闭）
        assert upstream.state.disconnected.wait(timeout=5), (
            "断连后假上游未观测到客户端断开：上游连接未被取消"
        )

        with httpx.Client(base_url=base, timeout=30, trust_env=False) as client:
            login(client, base, account)
            # 未完成的回答不写入历史
            history = client.get("/api/history").json()["history"]
            assert history == []
            # busy 已释放：新轮次立即正常完成
            resp = client.post("/api/chat", json={"message": "中断后的新问题"})
            assert resp.status_code == 200
            assert "event: done" in resp.text
            assert "event: rejected" not in resp.text
            saved = client.get("/api/history").json()["history"]
            assert [m["role"] for m in saved] == ["user", "assistant"]
            assert "中断后的新问题" in saved[0]["content"]


# ===================== 已保存历史：继续生成 =====================


def test_continue_uses_server_side_history(tmp_path):
    """继续生成：指令由服务端按已保存历史构造，第二轮正常收尾并保存。"""
    upstream = FakeUpstream(
        {
            "pieces": [
                ("第一轮回答：$x=1$", 0),
                ("（接上文）继续补充：$y=2$", 0),
            ]
        }
    )
    upstream.start()
    with running_api(upstream, tmp_path) as (runtime, base):
        account = runtime.accounts[0]
        with httpx.Client(base_url=base, timeout=30, trust_env=False) as client:
            login(client, base, account)
            first = client.post("/api/chat", json={"message": "解方程 x+1=2"})
            assert "event: done" in first.text

            resp = client.post("/api/continue", json={})
            assert resp.status_code == 200
            text = resp.text
            assert "event: start" in text
            assert "（请继续完成上一条回答）" in text  # 服务端构造的继续指令
            assert "event: done" in text
            assert '"save_status": "saved"' in text

            # 假上游收到两轮请求；第二轮最后一条消息是继续指令，携带第一问上下文
            assert len(upstream.state.bodies) == 2
            second_messages = upstream.state.bodies[1]["messages"]
            assert second_messages[-1]["content"].endswith("（请继续完成上一条回答）")
            assert "解方程 x+1=2" in second_messages[-1]["content"]

            history = client.get("/api/history").json()["history"]
            assert len(history) == 4  # 问 / 答 / 继续指令 / 续答
            assert "请继续完成上一条回答" in history[2]["content"]
            assert "继续补充" in history[3]["content"]


# ===================== 真实 uvicorn 的转发头信任边界 =====================


def test_forwarded_proto_enables_secure_cookie(tmp_path):
    """回环代理带 X-Forwarded-Proto: https → Cookie 带 Secure（隧道形状）。

    cloudflared 从本机回源时即此形状：uvicorn 默认信任回环来源的
    X-Forwarded-Proto 并改写 scheme。不带该头的本机/局域网 HTTP 直连
    不带 Secure，登录不受影响。
    """
    upstream = FakeUpstream({"pieces": [("答", 0)]})
    upstream.start()
    with running_api(upstream, tmp_path) as (runtime, base):
        account = runtime.accounts[0]
        with httpx.Client(base_url=base, trust_env=False) as client:
            plain = login(client, base, account)
            assert "Secure" not in plain.headers.get("set-cookie", "")
            with httpx.Client(base_url=base, trust_env=False) as client2:
                tunneled = login(
                    client2, base, account, **{"X-Forwarded-Proto": "https"}
                )
                assert "Secure" in tunneled.headers.get("set-cookie", "")


def test_forwarded_for_demotes_local_admin(tmp_path):
    """回环来源但带 X-Forwarded-For：来源被改写，local_admin 由真变假。

    与隧道路径一致：cloudflared 携带访客 IP 的转发头，远程用户即使
    已登录也拿不到管理员能力；不带转发头的本机直连仍为 True。
    """
    upstream = FakeUpstream({"pieces": [("答", 0)]})
    upstream.start()
    with running_api(upstream, tmp_path) as (runtime, base):
        account = runtime.accounts[0]
        with httpx.Client(base_url=base, trust_env=False) as client:
            login(client, base, account)
            me_direct = client.get("/api/me").json()
            assert me_direct["local_admin"] is True  # 本机直连管理员可用
            me_forwarded = client.get(
                "/api/me", headers={"X-Forwarded-For": "203.0.113.9"}
            ).json()
            assert me_forwarded["local_admin"] is False
            admin_via_forwarded = client.get(
                "/api/admin/stats", headers={"X-Forwarded-For": "203.0.113.9"}
            )
            assert admin_via_forwarded.status_code == 403
            admin_direct = client.get("/api/admin/stats")
            assert admin_direct.status_code == 200  # 本机直连不受影响


# ===================== 建流阶段停止与摘要静默 =====================


def test_stop_while_upstream_holds_headers(tmp_path):
    """扣住响应头：心跳照常下发；停止 → 假上游观测 TCP 断开 → busy 释放、历史干净。"""
    upstream = FakeUpstream({"hold_headers": 30})
    upstream.start()
    with running_api(upstream, tmp_path) as (runtime, base):
        account = runtime.accounts[0]
        with httpx.Client(base_url=base, timeout=30, trust_env=False) as client:
            login(client, base, account)
            with client.stream(
                "POST", f"{base}/api/chat", json={"message": "扣住响应头时停止"}
            ) as stream:
                assert stream.status_code == 200
                saw_ping = False
                for line in stream.iter_lines():
                    if line == ": ping":
                        saw_ping = True
                        break
                assert saw_ping, "上游扣住响应头期间未收到 SSE 心跳"
            # 退出 with：客户端断开 → 服务端取消建流

        assert upstream.state.disconnected.wait(timeout=5), (
            "假上游未观测到 TCP 断开：建流阶段的在途请求未被取消"
        )

        with httpx.Client(base_url=base, timeout=30, trust_env=False) as client:
            login(client, base, account)
            # 未完成回答不写入历史
            assert client.get("/api/history").json()["history"] == []
            # busy 释放略晚于 TCP 断开（清理需等读线程退出，上限 2 秒）：
            # 轮询同账号新问题，busy 拒绝消失并正常完成即为释放证据
            upstream.plan.clear()
            upstream.plan.update({"pieces": [("恢复后的回答", 0)]})
            deadline = time.time() + 10
            resp = None
            while time.time() < deadline:
                resp = client.post("/api/chat", json={"message": "释放后的新问题"})
                if "event: done" in resp.text:
                    break
                time.sleep(0.2)
            assert resp is not None and "event: done" in resp.text, (
                "busy 未在时限内释放"
            )
            assert "event: rejected" not in resp.text
            saved = client.get("/api/history").json()["history"]
            assert [m["role"] for m in saved] == ["user", "assistant"]
            assert "释放后的新问题" in saved[0]["content"]


def test_history_summary_silence_is_bounded(tmp_path, monkeypatch):
    """启用历史摘要：摘要总时限（含重试）有界（超时回退本地摘要），轮次照常完成。

    摘要发生在 SSE 首个事件之前（该段没有心跳可发），含重试的总时限必须
    远低于代理空闲读超时（125 秒）。本测试把总时限压到 0.5 秒、摘要请求
    静默 2 秒，验证超时回退后轮次正常收尾。
    """
    monkeypatch.setattr("core.llm_client.COMPLETE_TOTAL_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setenv("MAX_HISTORY_MESSAGES", "2")
    upstream = FakeUpstream(
        {
            "complete_delay": 2.0,
            "complete_content": "旧对话的摘要",
            "pieces": [("带摘要上下文的回答", 0)],
        }
    )
    upstream.start()
    with running_api(upstream, tmp_path) as (runtime, base):
        account = runtime.accounts[0]
        username = account["username"]
        from core.storage import get_history_store

        store = get_history_store(runtime.data_dir)
        store.save(
            [
                {"role": "user", "content": "旧问题一"},
                {"role": "assistant", "content": "旧回答一"},
                {"role": "user", "content": "旧问题二"},
                {"role": "assistant", "content": "旧回答二"},
            ],
            username=username,
            turn_token=store.start_turn(username),
        )
        with httpx.Client(base_url=base, timeout=30, trust_env=False) as client:
            login(client, base, account)
            t0 = time.monotonic()
            resp = client.post("/api/chat", json={"message": "新问题"})
            elapsed = time.monotonic() - t0
            assert resp.status_code == 200
            assert "event: done" in resp.text
            assert elapsed < 10, f"摘要静默未受上限约束（耗时 {elapsed:.2f}s）"

    # 摘要恰为 1 次非流式请求（SDK 自动重试已关闭，到期取消后无后续重试），
    # 最后一次是聊天补全（stream=true），回答正常收尾
    complete_bodies = [b for b in upstream.state.bodies if b.get("stream") is False]
    assert len(complete_bodies) == 1, (
        f"摘要请求应为 1 次（无重试），实际 {len(complete_bodies)} 次"
    )
    assert upstream.state.bodies[-1].get("stream") is True
    # 回退摘要生效：聊天请求携带本地回退摘要而非超时的模型摘要
    chat_content = json.dumps(
        upstream.state.bodies[-1]["messages"], ensure_ascii=False
    )
    assert "旧问题一" in chat_content
    assert "旧对话的摘要" not in chat_content


def test_stop_during_preheader_releases_immediately(tmp_path, monkeypatch):
    """默认 10 秒心跳下建流阶段停止：取消与 busy 释放不等待下一个心跳。

    复现口径（Codex 独立复验）：扣住响应头 + 默认 10 秒心跳，等首个心跳
    下发后立即停止。修复前取消要等下一个心跳 tick（≈10 秒）才断开上游、
    释放 busy，紧接重发被 busy 拒绝；修复后断开与重发应在秒级完成。
    """
    monkeypatch.setattr("core.llm_client.SILENT_HEARTBEAT_SECONDS", 10.0)
    upstream = FakeUpstream({"hold_headers": 60})
    upstream.start()
    with running_api(upstream, tmp_path) as (runtime, base):
        account = runtime.accounts[0]
        with httpx.Client(base_url=base, timeout=60, trust_env=False) as client:
            login(client, base, account)
            t_stop = None
            with client.stream(
                "POST", f"{base}/api/chat", json={"message": "建流阶段停止"}
            ) as stream:
                assert stream.status_code == 200
                for line in stream.iter_lines():
                    if line == ": ping":  # 首个心跳（≈10 秒），随后立即停止
                        t_stop = time.monotonic()
                        break
                assert t_stop is not None, "未在时限内收到首个心跳"
            # 退出 with：客户端断开，此刻同步链阻塞在下一个心跳的拉取里

        # 记录从停止到上游 TCP 断开的实际耗时（修复前 ≈10 秒一个心跳 tick）
        assert upstream.state.disconnected.wait(timeout=15), (
            "停止后 15 秒内上游仍未断开"
        )
        disconnect_after_stop = upstream.state.disconnected_at - t_stop
        assert disconnect_after_stop < 2.0, (
            f"上游断开滞后 {disconnect_after_stop:.2f}s：取消仍在等下一个心跳"
        )

        # busy 释放：切换脚本后轮询重发（修复前此处被 busy 拒绝约 10 秒）
        upstream.plan.clear()
        upstream.plan.update({"pieces": [("恢复后的回答", 0)]})
        resend_done = None
        resend_resp = None
        with httpx.Client(base_url=base, timeout=60, trust_env=False) as client:
            login(client, base, account)
            deadline = time.time() + 10
            while time.time() < deadline:
                resend_resp = client.post("/api/chat", json={"message": "停止后的重发"})
                if "event: done" in resend_resp.text:
                    resend_done = time.monotonic()
                    break
                time.sleep(0.1)
            assert resend_done is not None, "busy 未在时限内释放"
            assert "event: rejected" not in resend_resp.text
            assert resend_done - t_stop < 5.0, (
                f"停止到重发完成滞后 {resend_done - t_stop:.2f}s"
            )
            saved = client.get("/api/history").json()["history"]
            assert [m["role"] for m in saved] == ["user", "assistant"]
            assert "停止后的重发" in saved[0]["content"]


def test_stop_during_summary_releases_immediately(tmp_path, monkeypatch):
    """摘要期间停止：中止信号传到摘要请求，取消后 busy 立即释放。

    Codex 复现口径：摘要预算 4 秒、假上游收到摘要请求后客户端断开——
    修复前停止信号未传到摘要，busy 要等摘要预算耗尽（≈4 秒）才释放，
    紧接重发被 busy 拒绝；修复后取消在秒级完成，重发立即成功，旧轮
    不再发出主回答请求，历史未写入旧轮内容。
    """
    monkeypatch.setattr("core.llm_client.COMPLETE_TOTAL_TIMEOUT_SECONDS", 4.0)
    monkeypatch.setenv("MAX_HISTORY_MESSAGES", "2")  # 预置历史超过窗口触发摘要
    upstream = FakeUpstream(
        {
            "complete_delay": 30.0,  # 摘要请求迟迟不返回（在途）
            "pieces": [("恢复后的回答", 0)],
        }
    )
    upstream.start()
    with running_api(upstream, tmp_path) as (runtime, base):
        account = runtime.accounts[0]
        username = account["username"]
        from core.storage import get_history_store

        store = get_history_store(runtime.data_dir)
        store.save(
            [
                {"role": "user", "content": "旧问题一"},
                {"role": "assistant", "content": "旧回答一"},
                {"role": "user", "content": "旧问题二"},
                {"role": "assistant", "content": "旧回答二"},
            ],
            username=username,
            turn_token=store.start_turn(username),
        )

        with httpx.Client(base_url=base, timeout=60, trust_env=False) as client:
            login(client, base, account)
            cookie = client.cookies.get("maa_session")
        assert cookie, "登录会话 Cookie 未取得"

        # 原始 socket 发起聊天：摘要期间响应无任何字节，只有换用 raw socket
        # 才能在不读响应的前提下于任意时刻精确断开
        parsed = urlparse(base)
        body = json.dumps({"message": "会触发摘要的问题"}).encode("utf-8")
        request_bytes = (
            f"POST /api/chat HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n"
            f"Cookie: maa_session={cookie}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("utf-8") + body
        sock = socket.create_connection((parsed.hostname, parsed.port))
        sock.sendall(request_bytes)

        # 等假上游明确收到摘要请求再断开（不用固定 sleep）
        deadline = time.monotonic() + 5
        summary_seen = False
        while time.monotonic() < deadline:
            with upstream.state.lock:
                summary_seen = any(
                    b.get("stream") is False for b in upstream.state.bodies
                )
            if summary_seen:
                break
            time.sleep(0.05)
        assert summary_seen, "假上游未在时限内收到摘要请求"
        t_stop = time.monotonic()
        sock.close()  # 客户端断开（停止）

        # 上游断开耗时：停止 → 摘要请求所在连接被取消
        assert upstream.state.disconnected.wait(timeout=5), (
            "停止后 5 秒内摘要请求所在连接未断开"
        )
        disconnect_after_stop = upstream.state.disconnected_at - t_stop
        assert disconnect_after_stop < 2.0, (
            f"摘要请求取消滞后 {disconnect_after_stop:.2f}s"
        )

        # 同账号立即重发：不再被 busy 拒绝
        resend_done = None
        resend_resp = None
        with httpx.Client(base_url=base, timeout=60, trust_env=False) as client:
            login(client, base, account)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                resend_resp = client.post("/api/chat", json={"message": "停止后的重发"})
                if "event: done" in resend_resp.text:
                    resend_done = time.monotonic()
                    break
                time.sleep(0.1)
            assert resend_done is not None, "busy 未在时限内释放"
            assert "event: rejected" not in resend_resp.text
            assert resend_done - t_stop < 3.0, (
                f"停止到重发完成滞后 {resend_done - t_stop:.2f}s"
            )

        # 旧轮没有发出主回答请求：仅 1 次摘要（非流式）+ 1 次重发聊天
        with upstream.state.lock:
            complete_count = sum(
                1 for b in upstream.state.bodies if b.get("stream") is False
            )
            chat_count = sum(
                1 for b in upstream.state.bodies if b.get("stream") is True
            )
        assert complete_count == 1, f"摘要请求应为 1 次，实际 {complete_count} 次"
        assert chat_count == 1, (
            f"旧轮不应发出主回答请求：聊天请求共 {chat_count} 次（含重发 1 次）"
        )

        # 历史未写入旧轮内容：预置 4 条 + 重发轮 2 条，被中止的旧轮
        # （问题与未完成的回答）均不在历史中
        with httpx.Client(base_url=base, timeout=60, trust_env=False) as client:
            login(client, base, account)
            saved = client.get("/api/history").json()["history"]
        assert len(saved) == 6, f"历史应为预置 4 条 + 重发轮 2 条，实际 {len(saved)} 条"
        assert "停止后的重发" in saved[4]["content"]
        assert "恢复后的回答" in saved[5]["content"]
        assert "会触发摘要的问题" not in json.dumps(saved, ensure_ascii=False)
