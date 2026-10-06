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

# 内置默认服务商；环境相关值在调用 default_providers() 时读取，
# 导入本模块不读取环境变量（.env 由 core.config.load_env_file 显式加载）
def default_providers() -> list[dict]:
    return [
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


def _read_custom_providers(providers_file: Path | None = None) -> list[dict]:
    file = Path(providers_file) if providers_file is not None else PROVIDERS_FILE
    try:
        # 使用 utf-8-sig 自动去掉 Windows 记事本可能保存的 BOM
        data = json.loads(file.read_text(encoding="utf-8-sig"))
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


def load_providers(providers_file: Path | None = None) -> list[dict]:
    """返回当前可配置的全部服务商；优先读取 providers_file（默认 config/providers.json）。"""
    custom = _read_custom_providers(providers_file)
    if custom:
        return custom
    return [
        _sanitize_provider(p) for p in default_providers() if _sanitize_provider(p)
    ]


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


# ---- 按模型的思考能力判断（纯函数，客户端 / API / 设置元数据共用同一份规则） ----
#
# 必须按本轮实际选中的模型匹配，而不是服务商标记：同一服务商可能同时提供
# 遵循不同参数风格的模型（如百炼下的 kimi-k3 不能套用 Qwen 规则）。
# 只匹配下方已核实的目标模型名单，不做前缀猜测：名单外的模型一律按原有
# 逻辑处理，不根据近似名字推断参数、强度或输出上限字段。
# 档位与参数名以官方文档为准（2026-10 核对）：
# - DeepSeek：thinking.type 开关 + 顶层 reasoning_effort，档位 low/high/max
# - Qwen3.7：enable_thinking 开关，本轮不提供强度档位
# - Qwen3.8：reasoning_effort 控制思考（none 即官方关闭方式），
#   原生档位 low/medium/xhigh；high/max 是官方确认的映射别名（→xhigh），
#   仅用于兼容旧默认档位，不作为界面选项
# - MiMo：thinking.type 开关，官方未提供强度档位；
#   Qwen3.7/3.8 与 MiMo 的输出上限用 max_completion_tokens
_THINKING_STYLE_NONE = "none"
_THINKING_STYLE_DEEPSEEK = "deepseek"
_THINKING_STYLE_QWEN37 = "qwen3.7"
_THINKING_STYLE_QWEN38 = "qwen3.8"
_THINKING_STYLE_MIMO = "mimo"
_THINKING_STYLE_LEGACY = "legacy"

_LEGACY_EFFORT_LEVELS = ("low", "high", "max")
_QWEN38_EFFORT_ALIASES = frozenset({"high", "max"})

# 本轮已核实的目标模型名单（明确枚举，不做前缀匹配）
_TARGET_MODELS: dict[str, str] = {
    "deepseek-flash": _THINKING_STYLE_DEEPSEEK,
    "deepseek-v4-pro": _THINKING_STYLE_DEEPSEEK,
    "deepseek-v4-flash": _THINKING_STYLE_DEEPSEEK,
    "qwen3.7-flash": _THINKING_STYLE_QWEN37,
    "qwen3.8-flash": _THINKING_STYLE_QWEN38,
    "qwen3.8-max": _THINKING_STYLE_QWEN38,
    "mimo-v2.6-flash": _THINKING_STYLE_MIMO,
    "mimo-v2.6-pro": _THINKING_STYLE_MIMO,
}

# 风格 → (强度档位, 显式提交可接受的值, 切换模型时的默认档位, 输出上限参数名)。
# accepted_efforts 为 None 表示非目标模型 / thinking_param=none：
# 显式强度不在此套用模型级规则，由调用方保持原有全局档位校验
_THINKING_STYLES: dict[str, tuple[tuple[str, ...], frozenset | None, str | None, str]] = {
    _THINKING_STYLE_NONE: ((), None, None, "max_tokens"),
    _THINKING_STYLE_DEEPSEEK: (
        _LEGACY_EFFORT_LEVELS,
        frozenset(_LEGACY_EFFORT_LEVELS),
        "high",
        "max_tokens",
    ),
    _THINKING_STYLE_QWEN37: ((), frozenset(), None, "max_completion_tokens"),
    _THINKING_STYLE_QWEN38: (
        ("low", "medium", "xhigh"),
        frozenset(("low", "medium", "xhigh")) | _QWEN38_EFFORT_ALIASES,
        "xhigh",
        "max_completion_tokens",
    ),
    _THINKING_STYLE_MIMO: ((), frozenset(), None, "max_completion_tokens"),
    _THINKING_STYLE_LEGACY: (
        _LEGACY_EFFORT_LEVELS,
        None,
        "high",
        "max_tokens",
    ),
}


def _match_thinking_style(model: str | None) -> str:
    name = str(model or "").strip().lower()
    return _TARGET_MODELS.get(name, _THINKING_STYLE_LEGACY)


def get_model_thinking_capability(provider: dict | None, model: str | None) -> dict:
    """返回指定模型的思考参数风格与档位（纯函数，无 IO）。

    provider 为 None（组装回退拿不到服务商配置）时仅按模型名单匹配；
    thinking_param 为 none 的服务商一律不发送思考参数，与既有行为一致。
    其余字段：effort_levels 供界面展示，accepted_efforts 供显式参数校验
    （含官方映射别名；None 表示非目标模型，由调用方按原有全局档位校验），
    default_effort 供切换模型时同步合法选中值，
    output_limit_param 为该模型的输出上限参数名。
    """
    if provider is not None and str(
        provider.get("thinking_param") or "none"
    ).lower() == "none":
        style = _THINKING_STYLE_NONE
    else:
        style = _match_thinking_style(model)
    levels, accepted, default_effort, output_param = _THINKING_STYLES[style]
    return {
        "style": style,
        "supports_thinking": style != _THINKING_STYLE_NONE,
        "effort_levels": levels,
        "accepted_efforts": accepted,
        "default_effort": default_effort,
        "output_limit_param": output_param,
    }


def get_provider_supports_vision(provider: dict) -> bool:
    """该服务商的主模型是否原生支持图片输入。"""
    return bool(provider.get("supports_vision", False))
