"""统一大模型客户端：基于 OpenAI SDK 支持 OpenAI 兼容服务商。

思考参数按本轮实际选中的模型组装（规则见 core.providers.get_model_thinking_capability）：
- deepseek 系：extra_body={"thinking": {"type": ...}} 开关 + 顶层 reasoning_effort
- qwen3.7 系：extra_body={"enable_thinking": ...} 开关，不带强度
- qwen3.8 系：extra_body={"reasoning_effort": ...}（none 即关闭）
- mimo 系：extra_body={"thinking": {"type": ...}} 开关，不带强度
- 其余模型：保持原有统一格式（thinking 内联 reasoning_effort），不套用目标规则
输出上限：qwen3.7/3.8 与 mimo 用 max_completion_tokens，其余仍用 max_tokens。
自定义字段经 SDK 的 extra_body 合并到请求 JSON 顶层，不产生名为 extra_body 的对象。
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import httpx
from openai import OpenAI

from core.config import Settings
from core.providers import get_model_thinking_capability, get_provider_api_key


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

    def _resolve_effort(self, reasoning_effort: str | None) -> str:
        return reasoning_effort or (
            self._settings.reasoning_effort if self._settings is not None else "high"
        )

    def _build_extra_params(
        self,
        thinking: bool | None,
        reasoning_effort: str | None,
        capability: dict,
    ) -> dict:
        """按模型风格组装 extra_body：字段最终合并到请求 JSON 顶层。"""
        use_thinking = (
            self._settings.thinking if self._settings is not None else False
        ) if thinking is None else thinking
        effort = self._resolve_effort(reasoning_effort)
        style = capability["style"]
        if style == "none":
            return {}
        if style == "deepseek":
            # reasoning_effort 是请求 JSON 的顶层字段，不能放进 thinking 内
            if use_thinking:
                return {"thinking": {"type": "enabled"}, "reasoning_effort": effort}
            return {"thinking": {"type": "disabled"}}
        if style == "qwen3.7":
            return {"enable_thinking": bool(use_thinking)}
        if style == "qwen3.8":
            # qwen3.8 用 reasoning_effort 控制思考，none 是官方的关闭方式；
            # high/max 为官方映射别名（→xhigh），原样发送
            return {"reasoning_effort": effort if use_thinking else "none"}
        if style == "mimo":
            return {"thinking": {"type": "enabled" if use_thinking else "disabled"}}
        # legacy：非目标模型保持原有统一格式，行为不变
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
        model_name = model or self._model
        capability = get_model_thinking_capability(self._provider, model_name)
        extra_body = self._build_extra_params(
            thinking, reasoning_effort, capability
        )
        max_tokens = (
            self._settings.max_output_tokens if self._settings is not None else 30000
        )
        max_wait = float(self._settings.max_wait_seconds) if self._settings else 600.0
        create_kwargs: dict = {
            "model": model_name,
            "messages": messages,
            "stream": True,
            "extra_body": extra_body,
            "stream_options": {"include_usage": True},
        }
        # 输出额度数值不变，仅按官方要求调整参数名（qwen/mimo 用 max_completion_tokens）
        create_kwargs[capability["output_limit_param"]] = max_tokens
        stream = self._client.chat.completions.create(**create_kwargs)
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
                # usage 读取必须在 choices 判断之外：DeepSeek 末尾的用量分片
                # choices 为空但携带 usage，放进判断内会把用量静默丢弃
                chunk_usage = getattr(chunk, "usage", None)
                if chunk.choices:
                    if chunk.choices[0].finish_reason:
                        finish_reason = chunk.choices[0].finish_reason
                    delta = chunk.choices[0].delta
                    if getattr(delta, "reasoning_content", None):
                        yield None, delta.reasoning_content, None, None
                    elif delta.content:
                        yield delta.content, None, None, None
                chunk_finish_reason = (
                    chunk.choices[0].finish_reason if chunk.choices else None
                )
                if chunk_usage is not None or chunk_finish_reason:
                    yield None, None, chunk_usage, finish_reason
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
        capability = get_model_thinking_capability(self._provider, self._model)
        extra_body = self._build_extra_params(
            thinking, reasoning_effort, capability
        )
        create_kwargs: dict = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            "extra_body": extra_body,
        }
        create_kwargs[capability["output_limit_param"]] = max_tokens
        response = self._client.chat.completions.create(**create_kwargs)
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
