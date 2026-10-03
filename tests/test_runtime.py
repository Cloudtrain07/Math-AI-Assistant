# -*- coding: utf-8 -*-
"""运行依赖组装测试：配置读取时机、服务商选择与依赖注入边界。

只覆盖本次组装入口引入的边界；配置默认值见 test_config.py，
账号文件语义见 test_accounts.py，存储/日志假实现见 conftest.py。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import MemoryDailyUsageStore, MemoryEventLogger, MemoryHistoryStore
from core.config import Settings, get_base_settings, load_env_file
from core.providers import load_providers
from core.runtime import build_runtime


def _injected_settings() -> Settings:
    return Settings(
        api_key="k",
        base_url="https://example.invalid",
        model="测试模型",
        thinking=False,
        reasoning_effort="high",
        max_history_messages=20,
        max_output_tokens=100,
        max_wait_seconds=10,
        max_message_len=100,
        daily_token_limit=123,
        global_daily_token_limit=456,
    )


def test_load_env_file_reads_file_and_env_wins(monkeypatch, tmp_path):
    """.env 经显式调用加载；已有系统环境变量优先于文件值（优先级保持不变）。"""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DEEPSEEK_MODEL=model-from-file\nDEEPSEEK_REASONING_EFFORT=low\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    monkeypatch.delenv("DEEPSEEK_REASONING_EFFORT", raising=False)

    load_env_file(env_file)
    settings = get_base_settings()
    assert settings.model == "model-from-file"
    assert settings.reasoning_effort == "low"

    monkeypatch.setenv("DEEPSEEK_MODEL", "model-from-env")
    # 环境变量与 .env 文件值冲突时再次加载，已存在的环境变量仍然优先
    load_env_file(env_file)
    assert get_base_settings().model == "model-from-env"


def test_load_providers_custom_file_replaces_defaults(tmp_path):
    """providers_file 参数指向自定义文件时整体替换内置服务商列表。"""
    providers_file = tmp_path / "providers.json"
    providers_file.write_text(
        json.dumps(
            [
                {
                    "id": "custom",
                    "name": "自定义",
                    "base_url": "https://custom.invalid/v1",
                    "api_key_env": "CUSTOM_API_KEY",
                    "models": ["custom-model"],
                    "default_model": "custom-model",
                }
            ]
        ),
        encoding="utf-8",
    )
    providers = load_providers(providers_file)
    assert [p["id"] for p in providers] == ["custom"]


def test_build_runtime_selects_providers_by_key(monkeypatch, tmp_path):
    """有 Key 的服务商进 clients 并按顺序取默认；无提示语。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    built = []

    def fake_factory(provider, settings):
        built.append(provider["id"])
        return f"client-{provider['id']}"

    rt = build_runtime(
        data_dir=tmp_path,
        settings=_injected_settings(),
        client_factory=fake_factory,
    )
    assert rt.default_provider["id"] == "deepseek"
    assert list(rt.clients) == ["deepseek"]
    assert rt.client == "client-deepseek"
    assert [p["id"] for p in rt.ui_providers] == ["deepseek"]
    assert rt.startup_hint == ""
    assert built == ["deepseek"]


def test_build_runtime_without_key_keeps_hint_and_full_list(monkeypatch, tmp_path):
    """无可用 Key：不建客户端，界面仍展示全部服务商并给出提示。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    rt = build_runtime(data_dir=tmp_path, settings=_injected_settings())
    assert rt.clients == {}
    assert rt.client is None
    assert [p["id"] for p in rt.ui_providers] == [p["id"] for p in rt.all_providers]
    assert "未找到可用的 API Key" in rt.startup_hint


def test_build_runtime_client_factory_failure_falls_back(monkeypatch, tmp_path):
    """客户端构造抛 ValueError：返回回退运行对象并保留提示，不抛 UnboundLocalError。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    def failing_factory(provider, settings):
        raise ValueError("客户端配置非法")

    rt = build_runtime(
        data_dir=tmp_path,
        settings=_injected_settings(),
        client_factory=failing_factory,
    )
    assert rt.settings is None
    assert rt.all_providers == []
    assert rt.available_providers == []
    assert rt.clients == {}
    assert rt.default_provider is None
    assert rt.client is None
    assert rt.ui_providers == []
    assert "客户端配置非法" in rt.startup_hint
    assert rt.max_message_len == 2000  # settings 回退后限额取默认值


def test_build_runtime_injected_dependencies_and_none_settings(monkeypatch, tmp_path):
    """注入的假依赖原样进入 runtime；显式 settings=None 走限额回退值。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    seen = {}

    def fake_factory(provider, settings):
        seen["provider_id"] = provider["id"]
        seen["settings"] = settings
        return "fake-client"

    history_store = MemoryHistoryStore()
    daily_usage_store = MemoryDailyUsageStore()
    event_logger = MemoryEventLogger()
    rt = build_runtime(
        data_dir=tmp_path,
        settings=None,
        client_factory=fake_factory,
        history_store=history_store,
        daily_usage_store=daily_usage_store,
        event_logger=event_logger,
    )
    assert rt.client == "fake-client"
    assert seen["provider_id"] == "deepseek"
    assert seen["settings"] is None
    assert rt.history_store is history_store
    assert rt.daily_usage_store is daily_usage_store
    assert rt.event_logger is event_logger
    assert rt.daily_limit == 1_000_000
    assert rt.global_daily_limit == 3_000_000
    assert rt.max_message_len == 2000


def test_build_runtime_accounts_path_threads_through(tmp_path):
    """账号清单写入指定路径并记录在 runtime.accounts_path 上。"""
    accounts_path = tmp_path / "nested" / "accounts.json"
    data_dir = tmp_path / "data"
    rt = build_runtime(
        accounts_path=accounts_path,
        account_count=2,
        data_dir=data_dir,
        settings=_injected_settings(),
    )
    assert rt.accounts_path == accounts_path
    assert [a["username"] for a in rt.accounts] == ["user1", "user2"]
    assert accounts_path.is_file()
    assert rt.history_store.data_dir == data_dir
