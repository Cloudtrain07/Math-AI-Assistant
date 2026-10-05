# -*- coding: utf-8 -*-
"""--fake 开发链路的客户端替换测试。

核心保证：启用假模式后，设置解析可能选中的任何客户端都不能再是真实客户端。
用「一旦被调用就报错」的标记客户端，确认显式服务商选择与缺省请求都改走假客户端；
同时确认假客户端能力（supports_vision）与服务商声明一致，不把所有服务商伪装成视觉模型。
"""

from fastapi.testclient import TestClient

from api import ChatRegistry, ChatBody, create_api_app, enable_fake_clients
from conftest import CountingDailyUsageStore, MemoryHistoryStore
from dev_fake import FakeStreamClient
from test_api_chat import login, parse_sse


class RealMarkerClient:
    """代表真实客户端：一旦被选中并调用就立即失败，暴露替换不彻底。"""

    supports_vision = True
    model = "real-marker"

    def __init__(self) -> None:
        self.calls = 0

    def chat_stream_with_usage(self, *args, **kwargs):  # pragma: no cover - 不应被调用
        self.calls += 1
        raise AssertionError("真实客户端被调用：--fake 未能替换全部可选中客户端")


def _prepare_runtime_with_real_clients(api_runtime):
    """给每个可展示服务商挂上真实标记客户端，并设缺省回退也是真实客户端。"""
    marker = RealMarkerClient()
    api_runtime.clients = {p["id"]: marker for p in api_runtime.ui_providers}
    api_runtime.client = marker
    return marker


def test_fake_replaces_explicit_and_default_clients(api_runtime):
    assert api_runtime.ui_providers, "测试前提：需要有可展示的服务商"
    marker = _prepare_runtime_with_real_clients(api_runtime)

    enable_fake_clients(api_runtime)

    # 显式服务商选择路径：runtime.clients 已整体替换
    assert api_runtime.client is not marker
    for client in api_runtime.clients.values():
        assert isinstance(client, FakeStreamClient)
        assert client is not marker
    # 缺省回退路径：runtime.client 也已替换
    assert isinstance(api_runtime.client, FakeStreamClient)

    # 直接走设置解析：显式与缺省都不得返回标记对象
    first_id = api_runtime.ui_providers[0]["id"]
    explicit = api_runtime.clients[first_id]
    assert explicit is not marker

    username = api_runtime.accounts[0]["username"]
    resolved_explicit = _resolve(api_runtime, username, ChatBody(message="x", provider_id=first_id))
    resolved_default = _resolve(api_runtime, username, ChatBody(message="x"))
    assert resolved_explicit["client"] is not marker
    assert resolved_default["client"] is not marker
    assert isinstance(resolved_explicit["client"], FakeStreamClient)


def _resolve(api_runtime, username, body):
    from api import _resolve_turn_settings

    return _resolve_turn_settings(api_runtime, username, body)


def test_fake_capability_matches_provider_declaration(api_runtime):
    enable_fake_clients(api_runtime)
    vision_flags = {
        p["id"]: bool(p.get("supports_vision", False)) for p in api_runtime.ui_providers
    }
    assert any(vision_flags.values()), "测试前提：默认服务商清单含一个视觉服务商"
    assert not all(vision_flags.values()), "测试前提：不应所有服务商都是视觉模型"

    for provider_id, declares_vision in vision_flags.items():
        assert api_runtime.clients[provider_id].supports_vision is declares_vision


def test_fake_mode_end_to_end_never_calls_real_client(api_runtime):
    """完整链路：标记真实客户端存在时，显式与缺省请求都走假客户端并成功返回。"""
    marker = _prepare_runtime_with_real_clients(api_runtime)
    enable_fake_clients(api_runtime)
    api_runtime.history_store = MemoryHistoryStore()
    api_runtime.daily_usage_store = CountingDailyUsageStore()
    registry = ChatRegistry()
    api = create_api_app(api_runtime, registry=registry)
    tc = TestClient(api)
    login(tc, api_runtime.accounts[0])

    first_id = api_runtime.ui_providers[0]["id"]
    # 显式选择第一个服务商
    resp = tc.post("/api/chat", json={"message": "显式服务商", "provider_id": first_id})
    assert resp.status_code == 200
    events = parse_sse(resp.text)
    assert events[0][0] == "start" and events[-1][0] == "done"

    # 缺省请求（不带 provider_id）
    resp = tc.post("/api/chat", json={"message": "缺省服务商"})
    assert resp.status_code == 200

    assert marker.calls == 0, "真实客户端不应被任何请求选中"
