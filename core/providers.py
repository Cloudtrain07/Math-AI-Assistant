"""大模型服务商配置：支持 DeepSeek、OpenAI 等 OpenAI 兼容服务商。

配置来源优先级：
1. config/providers.json（如果存在，可自定义服务商）
2. 内置默认服务商（DeepSeek + OpenAI）
"""

from __future__ import annotations

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PROVIDERS_FILE = BASE_DIR / "config" / "providers.json"

# 内置默认服务商；config/providers.json 存在时会覆盖/合并
DEFAULT_PROVIDERS: list[dict] = [
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "base_url": os.getenv(
            "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
        ).strip(),
        "api_key_env": "DEEPSEEK_API_KEY",
        "models": ["deepseek-v4-flash", "deepseek-v4-pro"],
        "default_model": os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash").strip(),
        "thinking_param": "deepseek",
    },
    {
        "id": "openai",
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "models": ["gpt-4o", "gpt-4o-mini"],
        "default_model": "gpt-4o-mini",
        "thinking_param": "openai",
        "supports_vision": True,
    },
]

# 允许的字段，避免脏配置
_ALLOWED_FIELDS = {
    "id",
    "name",
    "base_url",
    "api_key_env",
    "models",
    "default_model",
    "thinking_param",
    "supports_vision",
}


def _sanitize_provider(item: object) -> dict | None:
    if not isinstance(item, dict):
        return None
    provider = {k: item.get(k) for k in _ALLOWED_FIELDS}
    provider_id = str(provider.get("id") or "").strip()
    name = str(provider.get("name") or provider_id or "").strip()
    base_url = str(provider.get("base_url") or "").strip()
    models = [
        str(m).strip()
        for m in (provider.get("models") or [])
        if str(m).strip()
    ]
    default_model = str(provider.get("default_model") or "").strip()
    if not provider_id or not base_url or not models:
        return None
    if default_model not in models:
        default_model = models[0]
    return {
        "id": provider_id,
        "name": name or provider_id,
        "base_url": base_url,
        "api_key_env": str(provider.get("api_key_env") or "").strip(),
        "models": models,
        "default_model": default_model,
        "thinking_param": str(provider.get("thinking_param") or "deepseek").strip(),
        "supports_vision": bool(provider.get("supports_vision", False)),
    }


def _read_custom_providers() -> list[dict]:
    try:
        # 使用 utf-8-sig 自动去掉 Windows 记事本可能保存的 BOM
        data = json.loads(PROVIDERS_FILE.read_text(encoding="utf-8-sig"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    result: list[dict] = []
    for item in data:
        provider = _sanitize_provider(item)
        if provider is not None:
            result.append(provider)
    return result


def load_providers() -> list[dict]:
    """返回当前可配置的全部服务商；优先读取 config/providers.json。"""
    custom = _read_custom_providers()
    if custom:
        return custom
    return [_sanitize_provider(p) for p in DEFAULT_PROVIDERS if _sanitize_provider(p)]


def get_provider(provider_id: str | None, providers: list[dict] | None = None) -> dict | None:
    providers = providers if providers is not None else load_providers()
    for provider in providers:
        if provider["id"] == provider_id:
            return provider
    return None


def get_provider_api_key(provider: dict) -> str:
    env_name = str(provider.get("api_key_env") or "").strip()
    if not env_name:
        return ""
    return os.getenv(env_name, "").strip()


def provider_has_key(provider: dict) -> bool:
    return bool(get_provider_api_key(provider))


def get_available_providers(providers: list[dict] | None = None) -> list[dict]:
    """返回已配置 API Key 的服务商；没有 Key 的服务商暂不进入界面。"""
    providers = providers if providers is not None else load_providers()
    return [p for p in providers if provider_has_key(p)]


def get_default_provider(
    providers: list[dict] | None = None,
) -> dict | None:
    """优先返回第一个已配置 Key 的服务商，否则返回第一个服务商。"""
    providers = providers if providers is not None else load_providers()
    available = get_available_providers(providers)
    if available:
        return available[0]
    return providers[0] if providers else None


def get_provider_models(provider: dict) -> list[str]:
    return list(provider.get("models") or [])


def get_provider_supports_thinking(provider: dict) -> bool:
    return str(provider.get("thinking_param") or "none").lower() != "none"


def get_provider_supports_vision(provider: dict) -> bool:
    """该服务商的主模型是否原生支持图片输入（可直接看图，无需先转写）。"""
    return bool(provider.get("supports_vision", False))
