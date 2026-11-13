# -*- coding: utf-8 -*-
"""界面设置接口与继续生成测试：双服务商路由、串台隔离、非法设置拒绝、
服务端历史继续生成、清空交错与 busy 共享。

全部使用假客户端与内存存储，不调用真实模型，不读取真实配置。
"""

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api import ChatRegistry, create_api_app
from conftest import API_TEST_ACCOUNTS, MemoryHistoryStore, MemoryDailyUsageStore
from core.config import Settings
from core.runtime import build_runtime


class RecordingClient:
    """记录核心调用参数的假客户端：messages、model、thinking、effort、网页覆盖值。"""

    supports_vision = False

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.calls: list[dict] = []

    def chat_stream_with_usage(
        self,
        messages,
        thinking=None,
        reasoning_effort=None,
        model=None,
        max_tokens=None,
        max_wait_seconds=None,
    ):
        self.calls.append(
            {
                "messages": [dict(m) for m in messages],
                "thinking": thinking,
                "reasoning_effort": reasoning_effort,
                "model": model,
                "max_tokens": max_tokens,
                "max_wait_seconds": max_wait_seconds,
            }
        )
        answer = f"{self.tag} 的回答：$x=1$"
        yield None, "思考过程", None, None
        for i in range(0, len(answer), 3):
            yield answer[i : i + 3], None, None, None
        yield None, None, SimpleNamespace(
            total_tokens=10, prompt_tokens=4, completion_tokens=6
        ), "stop"


class GateClient:
    """可控慢速假客户端：started 后阻塞，release 后产出带用量的回答。"""

    supports_vision = False

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def chat_stream_with_usage(self, messages, **kwargs):
        self.calls += 1
        try:
            self.started.set()
            assert self.release.wait(timeout=10)
            yield "继续生成的回答", None, SimpleNamespace(
                total_tokens=7, prompt_tokens=3, completion_tokens=4
            ), "stop"
        finally:
            pass


TWO_PROVIDERS = [
    {
        "id": "alpha",
        "name": "Alpha",
        "base_url": "https://alpha.invalid/v1",
        "api_key_env": "ALPHA_API_KEY",
        "models": ["alpha-a", "alpha-b"],
        "default_model": "alpha-a",
        "thinking_param": "deepseek",
    },
    {
        "id": "beta",
        "name": "Beta",
        "base_url": "https://beta.invalid/v1",
        "api_key_env": "BETA_API_KEY",
        "models": ["beta-a"],
        "default_model": "beta-a",
        "thinking_param": "none",
    },
]


def _injected_settings() -> Settings:
    return Settings(
        api_key="k",
        base_url="https://example.invalid",
        model="alpha-a",
        thinking=False,
        reasoning_effort="high",
        max_history_messages=20,
        max_output_tokens=100,
        max_wait_seconds=10,
        max_message_len=100,
        daily_token_limit=123,
        global_daily_token_limit=456,
    )


def build_two_provider_runtime(tmp_path, providers=TWO_PROVIDERS):
    """构建带自定义服务商的 runtime，返回 (runtime, {id: RecordingClient})。"""
    providers_file = tmp_path / "providers.json"
    providers_file.write_text(
        json.dumps(providers, ensure_ascii=False), encoding="utf-8"
    )
    accounts_path = tmp_path / "accounts.json"
    accounts_path.write_text(
        json.dumps(API_TEST_ACCOUNTS, ensure_ascii=False), encoding="utf-8"
    )
    clients: dict[str, RecordingClient] = {}

    def factory(provider, settings):
        client = RecordingClient(provider["id"])
        clients[provider["id"]] = client
        return client

    runtime = build_runtime(
        providers_file=providers_file,
        accounts_path=accounts_path,
        data_dir=tmp_path / "data",
        settings=_injected_settings(),
        client_factory=factory,
    )
    runtime.history_store = MemoryHistoryStore()
    runtime.daily_usage_store = MemoryDailyUsageStore()
    return runtime, clients


@pytest.fixture
def two_provider_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPHA_API_KEY", "key-alpha")
    monkeypatch.setenv("BETA_API_KEY", "key-beta")
    runtime, clients = build_two_provider_runtime(tmp_path)
    api = create_api_app(runtime)
    tc = TestClient(api)
    resp = tc.post(
        "/api/login",
        json={
            "username": API_TEST_ACCOUNTS[0]["username"],
            "password": API_TEST_ACCOUNTS[0]["password"],
        },
    )
    assert resp.status_code == 200
    return tc, runtime, clients


def login(tc: TestClient, account: dict) -> None:
    resp = tc.post(
        "/api/login", json={"username": account["username"], "password": account["password"]}
    )
    assert resp.status_code == 200


# ===================== /api/settings =====================


def test_settings_lists_display_fields_only(two_provider_env):
    """设置清单只含展示字段：无 base_url / api_key / 内部地址等泄露。"""
    tc, runtime, _ = two_provider_env
    resp = tc.get("/api/settings")
    assert resp.status_code == 200
    body = resp.json()

    assert body["default_provider"] == "alpha"
    assert body["providers"] == [
        {
            "id": "alpha",
            "name": "Alpha",
            "models": ["alpha-a", "alpha-b"],
            "default_model": "alpha-a",
            "supports_thinking": True,
            "model_thinking": {
                "alpha-a": {"effort_levels": ["low", "high", "max"], "default_effort": "high"},
                "alpha-b": {"effort_levels": ["low", "high", "max"], "default_effort": "high"},
            },
        },
        {
            "id": "beta",
            "name": "Beta",
            "models": ["beta-a"],
            "default_model": "beta-a",
            "supports_thinking": False,
            "model_thinking": {
                "beta-a": {"effort_levels": [], "default_effort": None},
            },
        },
    ]
    assert body["levels"] == ["小学", "初中", "高中", "大学"]
    assert body["modes"] == ["互动模式", "解答模式"]
    assert body["answer_styles"] == ["完整步骤+答案", "简略过程+答案", "只给答案和结论"]
    assert body["default_level"] == "高中"  # 账号配置的学段
    assert body["default_mode"] == "互动模式"
    assert body["default_answer_style"] == "完整步骤+答案"
    assert body["default_thinking"] is False
    assert body["default_reasoning_effort"] == "high"

    raw = resp.text
    assert "base_url" not in raw
    assert "api_key" not in raw
    assert "alpha.invalid" not in raw
    assert "key-alpha" not in raw


def test_settings_requires_login(two_provider_env):
    tc, _, _ = two_provider_env
    fresh = TestClient(tc.app)
    assert fresh.get("/api/settings").status_code == 401


# ===================== 双 runtime 串台隔离 =====================


def test_two_runtimes_providers_do_not_cross(monkeypatch, tmp_path):
    """两份 runtime 各自只看到自己的服务商；跨 runtime 指定服务商被拒绝。"""
    monkeypatch.setenv("ALPHA_API_KEY", "key-alpha")
    monkeypatch.setenv("BETA_API_KEY", "key-beta")

    providers_a = [TWO_PROVIDERS[0]]
    providers_b = [TWO_PROVIDERS[1]]
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    runtime_a, clients_a = build_two_provider_runtime(
        tmp_path / "a", providers=providers_a
    )
    runtime_b, clients_b = build_two_provider_runtime(
        tmp_path / "b", providers=providers_b
    )
    app_a = create_api_app(runtime_a)
    app_b = create_api_app(runtime_b)
    tc_a = TestClient(app_a)
    tc_b = TestClient(app_b)
    login(tc_a, API_TEST_ACCOUNTS[0])
    login(tc_b, API_TEST_ACCOUNTS[0])

    assert [p["id"] for p in tc_a.get("/api/settings").json()["providers"]] == ["alpha"]
    assert [p["id"] for p in tc_b.get("/api/settings").json()["providers"]] == ["beta"]

    # A 应用指定 B 的服务商：400 且不调用任何模型
    resp = tc_a.post("/api/chat", json={"message": "hi", "provider_id": "beta"})
    assert resp.status_code == 400
    assert "服务商" in resp.json()["detail"]
    resp = tc_b.post("/api/chat", json={"message": "hi", "provider_id": "alpha"})
    assert resp.status_code == 400
    assert all(len(c.calls) == 0 for c in clients_a.values())
    assert all(len(c.calls) == 0 for c in clients_b.values())


# ===================== 服务商切换与设置路由 =====================


def test_provider_switch_routes_client_model_and_settings(two_provider_env):
    """指定服务商后：对应客户端被调用，模型与思考设置原样传给核心。

    beta（thinking_param=none）是非目标服务商：显式 low 保持原有兼容，
    客户端必须原样收到 low，不得被模型级校验拒绝。
    """
    tc, _, clients = two_provider_env
    resp = tc.post(
        "/api/chat",
        json={
            "message": "求导数",
            "mode": "解答模式",
            "level": "大学",
            "provider_id": "beta",
            "model": "beta-a",
            "thinking": True,
            "reasoning_effort": "low",
            "answer_style": "只给答案和结论",
        },
    )
    assert resp.status_code == 200
    assert clients["beta"].calls
    assert not clients["alpha"].calls
    call = clients["beta"].calls[0]
    assert call["model"] == "beta-a"
    assert call["thinking"] is True
    assert call["reasoning_effort"] == "low"
    system = call["messages"][0]["content"]
    assert "用户学段：大学" in system
    assert "教学模式：解答模式" in system
    assert "解答风格：只给答案和结论" in system


def test_default_settings_use_default_provider(two_provider_env):
    """缺省请求兼容旧行为：默认服务商、默认模型；风格默认值经解答模式注入。"""
    tc, _, clients = two_provider_env
    resp = tc.post("/api/chat", json={"message": "你好", "mode": "解答模式"})
    assert resp.status_code == 200
    assert clients["alpha"].calls
    assert not clients["beta"].calls
    call = clients["alpha"].calls[0]
    assert call["model"] == "alpha-a"
    assert call["thinking"] is False  # 来自注入 settings 的默认值
    assert call["reasoning_effort"] == "high"
    assert "解答风格：完整步骤+答案" in call["messages"][0]["content"]


@pytest.mark.parametrize(
    "extra",
    [
        {"provider_id": "nope"},
        {"model": "alpha-c"},
        {"provider_id": "alpha", "model": "beta-a"},  # 跨服务商的模型
        {"reasoning_effort": "turbo"},
        {"provider_id": "beta", "reasoning_effort": "turbo"},  # 非目标：非法值仍拒绝
        {"answer_style": "随便写"},
        {"mode": "闲聊模式"},
    ],
)
def test_invalid_settings_rejected_without_model_call(two_provider_env, extra):
    """非法服务商/模型/枚举值：400 明确报错，绝不静默替换，也不调用模型。"""
    tc, _, clients = two_provider_env
    resp = tc.post("/api/chat", json={"message": "你好", **extra})
    assert resp.status_code == 400
    assert resp.json()["detail"]
    assert all(len(c.calls) == 0 for c in clients.values())


# ===================== 目标模型（DeepSeek / Qwen / MiMo）的强度校验 =====================

TARGET_PROVIDERS = [
    {
        "id": "dashscope",
        "name": "百炼",
        "base_url": "https://dashscope.invalid/compatible-mode/v1",
        "api_key_env": "DASH_API_KEY",
        "models": ["qwen3.7-flash", "qwen3.8-flash", "qwen3.8-max", "kimi-k3",
                   "qwen3.80-unverified"],
        "default_model": "qwen3.7-flash",
        "thinking_param": "qwen",
    },
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "base_url": "https://deepseek.invalid",
        "api_key_env": "DS_API_KEY",
        "models": ["deepseek-flash", "deepseek-v4-pro"],
        "default_model": "deepseek-flash",
        "thinking_param": "deepseek",
    },
    {
        "id": "xiaomi-mimo",
        "name": "小米mimo",
        "base_url": "https://mimo.invalid/v1",
        "api_key_env": "MIMO_API_KEY",
        "models": ["mimo-v2.6-flash", "mimo-v2.6-pro"],
        "default_model": "mimo-v2.6-flash",
        "thinking_param": "mimo",
    },
    {
        "id": "bonsai",
        "name": "Bonsai-2-27B",
        "base_url": "https://bonsai.invalid/v1",
        "api_key_env": "BONSAI_API_KEY",
        "models": ["Bonsai-2-27B-Vision"],
        "default_model": "Bonsai-2-27B-Vision",
        "thinking_param": "ninfer",
    },
]


@pytest.fixture
def target_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DASH_API_KEY", "key-dash")
    monkeypatch.setenv("DS_API_KEY", "key-ds")
    monkeypatch.setenv("MIMO_API_KEY", "key-mimo")
    monkeypatch.setenv("BONSAI_API_KEY", "key-bonsai")
    runtime, clients = build_two_provider_runtime(tmp_path, providers=TARGET_PROVIDERS)
    api = create_api_app(runtime)
    tc = TestClient(api)
    login(tc, API_TEST_ACCOUNTS[0])
    return tc, clients


def test_settings_metadata_reports_per_model_levels(target_env):
    """设置元数据按模型给出档位：Qwen3.7/MiMo 无强度，Qwen3.8 原生档位，
    DeepSeek 与 kimi-k3（非 Qwen 规则）保持既有档位；
    近似前缀的未核实模型不进目标规则，保持 legacy 档位。"""
    tc, _ = target_env
    providers = {p["id"]: p for p in tc.get("/api/settings").json()["providers"]}
    thinking = providers["dashscope"]["model_thinking"]
    assert thinking["qwen3.7-flash"] == {"effort_levels": [], "default_effort": None}
    assert thinking["qwen3.8-flash"] == {
        "effort_levels": ["low", "medium", "xhigh"],
        "default_effort": "xhigh",
    }
    assert thinking["qwen3.8-max"] == thinking["qwen3.8-flash"]
    # kimi-k3 与百炼共用平台标记，但不套用 Qwen 规则
    assert thinking["kimi-k3"] == {
        "effort_levels": ["low", "high", "max"],
        "default_effort": "high",
    }
    # 近似前缀（qwen3.80-…）不在已核实名单内，不误匹配 Qwen3.8 规则
    assert thinking["qwen3.80-unverified"] == {
        "effort_levels": ["low", "high", "max"],
        "default_effort": "high",
    }
    assert providers["deepseek"]["model_thinking"]["deepseek-flash"] == {
        "effort_levels": ["low", "high", "max"],
        "default_effort": "high",
    }
    assert providers["xiaomi-mimo"]["model_thinking"]["mimo-v2.6-flash"] == {
        "effort_levels": [],
        "default_effort": None,
    }
    assert providers["bonsai"]["model_thinking"]["Bonsai-2-27B-Vision"] == {
        "effort_levels": ["minimal", "low", "medium", "high", "xhigh", "max"],
        "default_effort": "xhigh",
    }
    for p in providers.values():
        assert p["supports_thinking"] is True


def test_unverified_model_keeps_legacy_effort_validation(target_env):
    """近似前缀的未核实模型显式 low 走原有全局校验（接受），非法值仍拒绝。"""
    tc, clients = target_env
    resp = tc.post(
        "/api/chat",
        json={
            "message": "你好",
            "provider_id": "dashscope",
            "model": "qwen3.80-unverified",
            "reasoning_effort": "low",
        },
    )
    assert resp.status_code == 200
    assert clients["dashscope"].calls[-1]["reasoning_effort"] == "low"

    resp = tc.post(
        "/api/chat",
        json={
            "message": "你好",
            "provider_id": "dashscope",
            "model": "qwen3.80-unverified",
            "reasoning_effort": "turbo",
        },
    )
    assert resp.status_code == 400
    assert "思考强度" in resp.json()["detail"]


def test_qwen38_accepts_native_and_official_alias(target_env):
    """Qwen3.8 接受原生档位与官方映射别名（high/max→xhigh），并路由到所选模型。"""
    tc, clients = target_env
    for effort in ("xhigh", "medium", "low", "high", "max"):
        resp = tc.post(
            "/api/chat",
            json={
                "message": "你好",
                "provider_id": "dashscope",
                "model": "qwen3.8-flash",
                "thinking": True,
                "reasoning_effort": effort,
            },
        )
        assert resp.status_code == 200, (effort, resp.text)
    call = clients["dashscope"].calls[-1]
    assert call["model"] == "qwen3.8-flash"       # 本轮选中模型 ≠ 客户端默认模型
    assert call["reasoning_effort"] == "max"


def test_qwen38_rejects_unknown_effort(target_env):
    tc, clients = target_env
    resp = tc.post(
        "/api/chat",
        json={
            "message": "你好",
            "provider_id": "dashscope",
            "model": "qwen3.8-flash",
            "reasoning_effort": "turbo",
        },
    )
    assert resp.status_code == 400
    assert "思考强度" in resp.json()["detail"]
    assert all(len(c.calls) == 0 for c in clients.values())


def test_qwen37_rejects_explicit_effort_but_default_works(target_env):
    """Qwen3.7 不提供强度：显式提交明确拒绝；缺省请求可用。"""
    tc, clients = target_env
    resp = tc.post(
        "/api/chat",
        json={
            "message": "你好",
            "provider_id": "dashscope",
            "model": "qwen3.7-flash",
            "reasoning_effort": "high",
        },
    )
    assert resp.status_code == 400
    assert "思考强度" in resp.json()["detail"]
    assert all(len(c.calls) == 0 for c in clients.values())

    resp = tc.post(
        "/api/chat",
        json={"message": "你好", "provider_id": "dashscope", "model": "qwen3.7-flash"},
    )
    assert resp.status_code == 200
    assert clients["dashscope"].calls[-1]["model"] == "qwen3.7-flash"


def test_mimo_rejects_explicit_effort_but_default_works(target_env):
    tc, clients = target_env
    resp = tc.post(
        "/api/chat",
        json={
            "message": "你好",
            "provider_id": "xiaomi-mimo",
            "model": "mimo-v2.6-flash",
            "reasoning_effort": "low",
        },
    )
    assert resp.status_code == 400
    assert "思考强度" in resp.json()["detail"]
    assert all(len(c.calls) == 0 for c in clients.values())

    resp = tc.post(
        "/api/chat",
        json={"message": "你好", "provider_id": "xiaomi-mimo"},
    )
    assert resp.status_code == 200
    assert clients["xiaomi-mimo"].calls[-1]["model"] == "mimo-v2.6-flash"


def test_kimi_keeps_legacy_efforts(target_env):
    """kimi-k3 不套用 Qwen 规则：既有档位照常接受。"""
    tc, clients = target_env
    resp = tc.post(
        "/api/chat",
        json={
            "message": "你好",
            "provider_id": "dashscope",
            "model": "kimi-k3",
            "reasoning_effort": "max",
        },
    )
    assert resp.status_code == 200
    assert clients["dashscope"].calls[-1]["model"] == "kimi-k3"


def test_bonsai_ninfer_accepts_engine_levels(target_env):
    """Bonsai（NInfer）接受引擎控制台六档，并路由到所选模型。"""
    tc, clients = target_env
    for effort in ("minimal", "low", "medium", "high", "xhigh", "max"):
        resp = tc.post(
            "/api/chat",
            json={
                "message": "你好",
                "provider_id": "bonsai",
                "model": "Bonsai-2-27B-Vision",
                "thinking": True,
                "reasoning_effort": effort,
            },
        )
        assert resp.status_code == 200, (effort, resp.text)
    call = clients["bonsai"].calls[-1]
    assert call["model"] == "Bonsai-2-27B-Vision"
    assert call["reasoning_effort"] == "max"


def test_bonsai_ninfer_rejects_unknown_effort(target_env):
    tc, clients = target_env
    resp = tc.post(
        "/api/chat",
        json={
            "message": "你好",
            "provider_id": "bonsai",
            "model": "Bonsai-2-27B-Vision",
            "reasoning_effort": "turbo",
        },
    )
    assert resp.status_code == 400
    assert "思考强度" in resp.json()["detail"]
    assert all(len(c.calls) == 0 for c in clients.values())


def test_deepseek_default_request_usable(target_env):
    """缺省请求（缺省服务商/模型/强度）仍然可用。"""
    tc, clients = target_env
    resp = tc.post("/api/chat", json={"message": "你好"})
    assert resp.status_code == 200
    call = clients["dashscope"].calls[-1]  # 默认服务商为清单第一项
    assert call["model"] == "qwen3.7-flash"
    resp = tc.post(
        "/api/chat",
        json={
            "message": "你好",
            "provider_id": "deepseek",
            "reasoning_effort": "max",
        },
    )
    assert resp.status_code == 200
    assert clients["deepseek"].calls[-1]["reasoning_effort"] == "max"


# ===================== 校验边界（第二轮补齐） =====================


def _write_accounts(tmp_path) -> Path:
    accounts_path = tmp_path / "accounts.json"
    accounts_path.write_text(
        json.dumps(API_TEST_ACCOUNTS, ensure_ascii=False), encoding="utf-8"
    )
    return accounts_path


def test_no_clients_default_request_keeps_no_client_hint(monkeypatch, tmp_path):
    """服务商清单非空但全部无客户端：缺省请求保留 no_client 启动提示回退。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    runtime = build_runtime(
        accounts_path=_write_accounts(tmp_path),
        data_dir=tmp_path / "data",
        settings=_injected_settings(),
    )
    runtime.history_store = MemoryHistoryStore()
    runtime.daily_usage_store = MemoryDailyUsageStore()
    tc = TestClient(create_api_app(runtime))
    login(tc, API_TEST_ACCOUNTS[0])

    # 缺省请求：不做服务商/模型校验拒绝，由核心 no_client 给出启动提示
    events = parse_sse(tc.post("/api/chat", json={"message": "你好"}).text)
    assert events[0][0] == "rejected"
    assert events[0][1]["status"] == "no_client"
    assert "未找到可用的 API Key" in events[0][1]["message"]

    # busy 已释放：继续生成可正常走到 no_continue（而非 busy）
    events = parse_sse(tc.post("/api/continue", json={}).text)
    assert events[0][1]["status"] == "no_continue"

    # 显式选择清单内但没有客户端的服务商：明确拒绝，不静默回退
    resp = tc.post("/api/chat", json={"message": "你好", "provider_id": "deepseek"})
    assert resp.status_code == 400
    assert "不可用" in resp.json()["detail"]


def test_fallback_runtime_rejects_explicit_provider_and_model(monkeypatch, tmp_path):
    """ui_providers 为空（组装回退）：不接受显式 provider_id/model；缺省保留提示。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    def failing_factory(provider, settings):
        raise ValueError("客户端配置非法")

    runtime = build_runtime(
        accounts_path=_write_accounts(tmp_path),
        data_dir=tmp_path / "data",
        settings=_injected_settings(),
        client_factory=failing_factory,
    )
    assert runtime.ui_providers == []
    runtime.history_store = MemoryHistoryStore()
    runtime.daily_usage_store = MemoryDailyUsageStore()
    tc = TestClient(create_api_app(runtime))
    login(tc, API_TEST_ACCOUNTS[0])

    assert tc.post(
        "/api/chat", json={"message": "你好", "provider_id": "deepseek"}
    ).status_code == 400
    assert tc.post(
        "/api/chat", json={"message": "你好", "model": "deepseek-v4-flash"}
    ).status_code == 400

    # 缺省请求仍走无客户端回退，核心以 no_client 展示启动提示
    events = parse_sse(tc.post("/api/chat", json={"message": "你好"}).text)
    assert events[0][1]["status"] == "no_client"
    assert "客户端配置非法" in events[0][1]["message"]


def test_explicit_provider_without_client_rejected_no_fallback(two_provider_env):
    """显式选择服务商后只用该服务商客户端：缺失即拒绝，不回退 runtime.client。"""
    tc, runtime, clients = two_provider_env
    del runtime.clients["beta"]

    resp = tc.post("/api/chat", json={"message": "你好", "provider_id": "beta"})
    assert resp.status_code == 400
    assert "不可用" in resp.json()["detail"]
    assert not clients["alpha"].calls  # 没有静默切到默认服务商的客户端

    # 缺省路径保持兼容：默认服务商客户端正常可用
    events = parse_sse(tc.post("/api/chat", json={"message": "你好"}).text)
    assert events[-1][0] == "done"
    assert clients["alpha"].calls


@pytest.mark.parametrize(
    "extra",
    [
        {"provider_id": ""},
        {"model": ""},
        {"reasoning_effort": ""},
        {"answer_style": ""},
        {"level": ""},
    ],
)
def test_empty_string_settings_are_invalid_not_default(two_provider_env, extra):
    """显式空字符串是非法值而非缺省：400 拒绝，不静默换成默认项。"""
    tc, _, clients = two_provider_env
    resp = tc.post("/api/chat", json={"message": "你好", **extra})
    assert resp.status_code == 400
    assert all(len(c.calls) == 0 for c in clients.values())


def test_explicit_invalid_level_rejected_default_uses_account(two_provider_env):
    """显式非法学段 400；缺省学段按账号配置；校验同样覆盖继续生成。"""
    tc, runtime, clients = two_provider_env
    username = API_TEST_ACCOUNTS[0]["username"]
    _save_history(
        runtime,
        username,
        [
            {"role": "user", "content": "旧题目"},
            {"role": "assistant", "content": "旧回答"},
        ],
    )

    resp = tc.post("/api/chat", json={"message": "你好", "level": "博士"})
    assert resp.status_code == 400
    assert "学段" in resp.json()["detail"]

    resp = tc.post("/api/continue", json={"level": "博士"})
    assert resp.status_code == 400
    assert not clients["alpha"].calls

    # 缺省学段：取账号配置的 高中
    events = parse_sse(tc.post("/api/chat", json={"message": "你好"}).text)
    assert events[-1][0] == "done"
    assert "用户学段：高中" in clients["alpha"].calls[0]["messages"][0]["content"]


def test_continue_rejects_invalid_provider(two_provider_env):
    """继续生成与普通发送共用同一设置校验。"""
    tc, _, clients = two_provider_env
    resp = tc.post("/api/continue", json={"provider_id": "nope"})
    assert resp.status_code == 400
    assert all(len(c.calls) == 0 for c in clients.values())


# ===================== 继续生成 =====================


def parse_sse(raw: str) -> list[tuple[str, dict | None]]:
    events: list[tuple[str, dict | None]] = []
    for block in raw.split("\n\n"):
        lines = [ln for ln in block.split("\n") if ln]
        if not lines:
            continue
        name = "message"
        data_lines = []
        for ln in lines:
            if ln.startswith("event:"):
                name = ln[len("event:"):].strip()
            elif ln.startswith("data:"):
                data_lines.append(ln[len("data:"):].strip())
        if data_lines:
            events.append((name, json.loads("\n".join(data_lines))))
    return events


def _save_history(runtime, username: str, messages: list[dict]) -> None:
    store = runtime.history_store
    store.save(messages, username=username, turn_token=store.start_turn(username))


def test_continue_without_history_rejected_no_model_call(two_provider_env):
    """无可继续题目：no_continue 拒绝、不调用模型、busy 随之释放。"""
    tc, _, clients = two_provider_env
    resp = tc.post("/api/continue", json={})
    assert resp.status_code == 200
    events = parse_sse(resp.text)
    assert len(events) == 1
    assert events[0][0] == "rejected"
    assert events[0][1]["status"] == "no_continue"
    assert "还没有可继续的题目" in events[0][1]["message"]
    assert all(len(c.calls) == 0 for c in clients.values())

    # busy 未被泄漏：后续正常发送可完成
    done_events = parse_sse(tc.post("/api/chat", json={"message": "新问题"}).text)
    assert done_events[-1][0] == "done"


def test_continue_uses_server_history_and_force_continue(two_provider_env):
    """继续生成走服务端历史 + build_continue_message + force_continue 语义。

    前端不提交任何历史；指令由服务端构造（末条用户消息 + 继续指令）；
    互动模式下阶段保持不变（普通发送会推进到 targeted_hint）。
    """
    tc, runtime, clients = two_provider_env
    username = API_TEST_ACCOUNTS[0]["username"]
    _save_history(
        runtime,
        username,
        [
            {"role": "user", "content": "解方程 x+1=2"},
            {"role": "assistant", "content": "$x=1$"},
        ],
    )

    resp = tc.post("/api/continue", json={})
    events = parse_sse(resp.text)
    assert events[0][0] == "start"
    assert events[0][1]["message"] == "解方程 x+1=2\n（请继续完成上一条回答）"
    assert events[-1][0] == "done"

    call = clients["alpha"].calls[0]
    contents = [m["content"] for m in call["messages"]]
    assert contents[-1] == "解方程 x+1=2\n（请继续完成上一条回答）"
    assert "解方程 x+1=2" in contents[:-1]  # 服务端历史进入上下文
    system = call["messages"][0]["content"]
    assert "当前阶段：初始提示" in system  # force_continue 保持当前阶段

    # 回答已按普通发送语义保存进服务端历史
    assert [m["content"] for m in runtime.history_store.load(username)] == [
        "解方程 x+1=2",
        "$x=1$",
        "解方程 x+1=2\n（请继续完成上一条回答）",
        "alpha 的回答：$x=1$",
    ]


def test_continue_rejected_when_busy(two_provider_env):
    """继续生成与普通发送共享同账号 busy 保护。"""
    tc, runtime, _ = two_provider_env
    registry = tc.app.state.registry
    username = API_TEST_ACCOUNTS[0]["username"]
    assert registry.try_acquire(username) is True
    resp = tc.post("/api/continue", json={})
    events = parse_sse(resp.text)
    assert events[0][0] == "rejected"
    assert events[0][1]["status"] == "busy"
    registry.release(username)


def test_clear_during_continue_discards_stale_state(monkeypatch, tmp_path):
    """清空与继续生成交错：旧请求完成时不得恢复磁盘或内存历史。"""
    monkeypatch.setenv("ALPHA_API_KEY", "key-alpha")
    monkeypatch.delenv("BETA_API_KEY", raising=False)
    providers = [TWO_PROVIDERS[0]]
    providers_file = tmp_path / "providers.json"
    providers_file.write_text(json.dumps(providers), encoding="utf-8")
    accounts_path = tmp_path / "accounts.json"
    accounts_path.write_text(
        json.dumps(API_TEST_ACCOUNTS, ensure_ascii=False), encoding="utf-8"
    )
    gate = GateClient()
    runtime = build_runtime(
        providers_file=providers_file,
        accounts_path=accounts_path,
        data_dir=tmp_path / "data",
        settings=_injected_settings(),
        client_factory=lambda provider, settings: gate,
    )
    history_store = MemoryHistoryStore()
    daily_store = MemoryDailyUsageStore()
    runtime.history_store = history_store
    runtime.daily_usage_store = daily_store
    registry = ChatRegistry()
    api = create_api_app(runtime, registry=registry)
    username = API_TEST_ACCOUNTS[0]["username"]

    _save_history(
        runtime,
        username,
        [
            {"role": "user", "content": "旧题目"},
            {"role": "assistant", "content": "旧回答"},
        ],
    )

    tc1 = TestClient(api)
    tc2 = TestClient(api)
    login(tc1, API_TEST_ACCOUNTS[0])
    login(tc2, API_TEST_ACCOUNTS[0])

    box: dict = {}

    def run_continue():
        resp = tc1.post("/api/continue", json={})
        box["events"] = parse_sse(resp.text)

    thread = threading.Thread(target=run_continue)
    thread.start()
    assert gate.started.wait(timeout=5)

    clear_resp = tc2.post("/api/clear")
    assert clear_resp.json()["ok"] is True

    gate.release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()

    events = box["events"]
    done = events[-1][1]
    assert events[-1][0] == "done"
    assert done["save_status"] == "stale"
    assert "本次回答未保存" in done["save_note"]
    assert "累计 0 tokens" in done["cumulative_usage"]

    # 磁盘与内存均为空：旧请求没有恢复清空前的历史
    assert history_store.load(username) == []
    state = registry.get_state(runtime, username)
    assert state.history == []
    assert tc2.get("/api/history").json()["history"] == []

    # 每日已产生用量保留，不因清空抹掉
    assert daily_store.get_used(
        __import__("datetime").date.today().isoformat(), username
    ) == 7


# ===================== 网页覆盖：最大输出 token 与最大等待时间 =====================


def test_settings_exposes_web_limit_defaults(two_provider_env):
    """设置清单给出覆盖值的展示默认（来自注入 settings），供前端占位提示。"""
    tc, _, _ = two_provider_env
    data = tc.get("/api/settings").json()
    assert data["default_max_output_tokens"] == 100
    assert data["default_max_wait_seconds"] == 10


def test_chat_sends_resolved_web_limits_to_client(two_provider_env):
    """显式覆盖值经核心原样到达客户端调用参数（不做 token 反推）。"""
    tc, _, clients = two_provider_env
    resp = tc.post(
        "/api/chat",
        json={
            "message": "1+1=?",
            "max_output_tokens": 48000,
            "max_wait_seconds": 900,
        },
    )
    assert resp.status_code == 200
    call = clients["alpha"].calls[-1]
    assert call["max_tokens"] == 48000
    assert call["max_wait_seconds"] == 900


def test_chat_without_override_uses_injected_settings(two_provider_env):
    """留空/缺省：沿用服务器配置值（注入 settings 的 100/10），不改共享客户端。"""
    tc, _, clients = two_provider_env
    resp = tc.post("/api/chat", json={"message": "1+1=?"})
    assert resp.status_code == 200
    call = clients["alpha"].calls[-1]
    assert call["max_tokens"] == 100
    assert call["max_wait_seconds"] == 10


def test_continue_sends_resolved_web_limits(two_provider_env):
    """继续生成与普通发送共用同一校验与解析：覆盖值同样生效。"""
    tc, runtime, clients = two_provider_env
    username = API_TEST_ACCOUNTS[0]["username"]
    _save_history(runtime, username, [{"role": "user", "content": "解方程 x+1=2"}])
    resp = tc.post(
        "/api/continue",
        json={"max_output_tokens": 48000, "max_wait_seconds": 900},
    )
    assert resp.status_code == 200
    call = clients["alpha"].calls[-1]
    assert call["max_tokens"] == 48000
    assert call["max_wait_seconds"] == 900


@pytest.mark.parametrize(
    "payload",
    [
        {"max_output_tokens": True},
        {"max_output_tokens": False},
        {"max_output_tokens": 48000.5},
        {"max_output_tokens": 48000.0},  # 整值小数同样拒绝，不静默收整
        {"max_output_tokens": 50},
        {"max_output_tokens": 200001},
        {"max_wait_seconds": True},
        {"max_wait_seconds": 1.5},
        {"max_wait_seconds": 14},
        {"max_wait_seconds": 7201},
    ],
)
def test_invalid_web_limits_rejected_before_model_call(two_provider_env, payload):
    """布尔/小数/越界值：400 中文报错，且绝不发起模型调用。"""
    tc, _, clients = two_provider_env
    resp = tc.post("/api/chat", json={"message": "你好", **payload})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert isinstance(detail, str)
    assert ("整数" in detail) or ("之间" in detail)
    assert all(len(c.calls) == 0 for c in clients.values())


def test_invalid_limit_error_names_field(two_provider_env):
    """中文错误带字段名，网页可直接展示。"""
    tc, _, clients = two_provider_env
    resp = tc.post("/api/chat", json={"message": "你好", "max_output_tokens": True})
    assert "最大输出 token" in resp.json()["detail"]
    resp = tc.post("/api/chat", json={"message": "你好", "max_wait_seconds": 9999})
    assert "最大等待时间" in resp.json()["detail"]
    assert all(len(c.calls) == 0 for c in clients.values())


def test_blank_string_web_limit_rejected_not_default(two_provider_env):
    """显式空字符串不是「留空」：pydantic StrictInt 联合直接拒绝（不静默沿用默认）。"""
    tc, _, clients = two_provider_env
    resp = tc.post("/api/chat", json={"message": "你好", "max_output_tokens": ""})
    assert resp.status_code in (400, 422)
    assert all(len(c.calls) == 0 for c in clients.values())
