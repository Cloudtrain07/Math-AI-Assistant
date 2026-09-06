"""统一大模型客户端：基于 OpenAI SDK 支持 OpenAI 兼容服务商。

当前支持两类思考参数：
- deepseek：extra_body={"thinking": {"type": "enabled"/"disabled", "reasoning_effort": ...}}
- openai：extra_body={"reasoning_effort": ...}
- none：不传思考参数
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import httpx
from openai import OpenAI

from core.config import Settings
from core.providers import get_provider_api_key


class MaxWaitTimeout(Exception):
    """等待超过配置上限时抛出，用于友好提示并停止请求。"""


class LLMClient:
    """负责与任意 OpenAI 兼容 Chat Completions 接口通信。"""

    def __init__(
        self,
        provider: dict,
        settings: Settings | None = None,
    ) -> None:
        self._provider = provider
        self._settings = settings
        self._api_key = get_provider_api_key(provider)
        self._base_url = provider.get("base_url") or ""
        self._model = provider.get("default_model") or ""
        self._thinking_param = str(provider.get("thinking_param") or "none").lower()
        max_wait = float(settings.max_wait_seconds) if settings else 600.0
        self._client = OpenAI(
            api_key=self._api_key,
            base_url=self._base_url,
            timeout=httpx.Timeout(
                connect=30.0,
                read=max_wait,
                write=60.0,
                pool=30.0,
            ),
            max_retries=2,
        )

    @property
    def model(self) -> str:
        return self._model

    @property
    def supports_vision(self) -> bool:
        """该服务商主模型是否原生支持图片输入。"""
        return bool(self._provider.get("supports_vision", False))

    def _build_extra_params(
        self, thinking: bool | None, reasoning_effort: str | None
    ) -> dict:
        use_thinking = (
            self._settings.thinking if self._settings is not None else False
        ) if thinking is None else thinking
        effort = reasoning_effort or (
            self._settings.reasoning_effort if self._settings is not None else "high"
        )
        # 统一按 DeepSeek 的思考参数格式发送，保证所有服务商在思考行为上对齐
        if self._thinking_param == "none":
            return {}
        if use_thinking:
            return {
                "thinking": {
                    "type": "enabled",
                    "reasoning_effort": effort,
                }
            }
        return {"thinking": {"type": "disabled"}}

    def _chat_stream_with_usage(
        self,
        messages: list[dict],
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
        model: str | None = None,
    ) -> Iterator[tuple[str | None, str | None, object | None, str | None]]:
        """流式生成；(内容, 思考链, usage, finish_reason) 四元组。"""
        extra_body = self._build_extra_params(thinking, reasoning_effort)
        max_tokens = (
            self._settings.max_output_tokens if self._settings is not None else 30000
        )
        max_wait = float(self._settings.max_wait_seconds) if self._settings else 600.0
        stream = self._client.chat.completions.create(
            model=model or self._model,
            messages=messages,
            stream=True,
            max_tokens=max_tokens,
            extra_body=extra_body,
            stream_options={"include_usage": True},
        )
        finish_reason: str | None = None
        start = time.monotonic()
        last_heartbeat = start
        try:
            for chunk in stream:
                now = time.monotonic()
                if now - start > max_wait:
                    raise MaxWaitTimeout(
                        f"等待超过 {int(max_wait)} 秒，已自动停止（可重新发送）"
                    )
                if chunk.choices:
                    if chunk.choices[0].finish_reason:
                        finish_reason = chunk.choices[0].finish_reason
                    delta = chunk.choices[0].delta
                    if getattr(delta, "reasoning_content", None):
                        yield None, delta.reasoning_content, None, None
                    elif delta.content:
                        yield delta.content, None, None, None
                if getattr(chunk, "usage", None) is not None:
                    yield None, None, chunk.usage, finish_reason
                if now - last_heartbeat >= 1.0:
                    last_heartbeat = now
                    yield None, None, None, None
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def chat_stream_with_usage(
        self,
        messages: list[dict],
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
        model: str | None = None,
    ) -> Iterator[tuple[str | None, str | None, object | None, str | None]]:
        """流式回答并携带用量与结束原因。"""
        yield from self._chat_stream_with_usage(
            messages, thinking, reasoning_effort, model
        )

    def complete(
        self,
        messages: list[dict],
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
        max_tokens: int = 500,
    ) -> str:
        """非流式补全：返回首个回答文本（用于摘要、标签建议等轻量任务）。"""
        extra_body = self._build_extra_params(thinking, reasoning_effort)
        response = self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            stream=False,
            max_tokens=max_tokens,
            extra_body=extra_body,
        )
        return (response.choices[0].message.content or "").strip()


# 兼容旧代码：DeepSeekClient 现在等价于一个 thinking_param=deepseek 的 LLMClient
class DeepSeekClient(LLMClient):
    """保留的 DeepSeek 兼容客户端。"""

    def __init__(self, settings: Settings) -> None:
        provider = {
            "id": "deepseek",
            "name": "DeepSeek",
            "base_url": settings.base_url,
            "api_key_env": "DEEPSEEK_API_KEY",
            "models": [settings.model],
            "default_model": settings.model,
            "thinking_param": "deepseek",
        }
        super().__init__(provider, settings)
