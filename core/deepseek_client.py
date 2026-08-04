"""DeepSeek API 封装：基于 OpenAI SDK 的 OpenAI 兼容接口。"""

from __future__ import annotations

from collections.abc import Iterator

from openai import OpenAI

from core.config import Settings


class DeepSeekClient:
    """负责与 DeepSeek Chat Completions 接口通信。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = OpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=60.0,
            max_retries=2,
        )

    def _build_extra_params(
        self, thinking: bool | None, reasoning_effort: str | None
    ) -> dict:
        use_thinking = self._settings.thinking if thinking is None else thinking
        if use_thinking:
            effort = reasoning_effort or self._settings.reasoning_effort
            return {"thinking": {"type": "enabled", "reasoning_effort": effort}}
        # V4 系列思考模式默认开启，必须显式 disabled 才能真正关闭
        return {"thinking": {"type": "disabled"}}

    def _chat_stream_with_usage(
        self,
        messages: list[dict[str, str]],
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
    ) -> Iterator[tuple[str | None, object | None, str | None]]:
        """流式生成；(文本, usage, finish_reason) 三元组。"""
        extra_body = self._build_extra_params(thinking, reasoning_effort)
        stream = self._client.chat.completions.create(
            model=self._settings.model,
            messages=messages,
            stream=True,
            max_tokens=self._settings.max_output_tokens,
            extra_body=extra_body,
            stream_options={"include_usage": True},
        )
        finish_reason: str | None = None
        for chunk in stream:
            if chunk.choices:
                if chunk.choices[0].finish_reason:
                    finish_reason = chunk.choices[0].finish_reason
                delta = chunk.choices[0].delta
                if getattr(delta, "reasoning_content", None):
                    pass  # 思维链内容不直接展示给用户
                elif delta.content:
                    yield delta.content, None, None
            if getattr(chunk, "usage", None) is not None:
                yield None, chunk.usage, finish_reason

    def chat_stream(
        self,
        messages: list[dict[str, str]],
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
    ) -> Iterator[str]:
        """流式回答文本（兼容旧接口）。"""
        for piece, _, _ in self._chat_stream_with_usage(
            messages, thinking, reasoning_effort
        ):
            if piece:
                yield piece

    def chat_stream_with_usage(
        self,
        messages: list[dict[str, str]],
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
    ) -> Iterator[tuple[str | None, object | None, str | None]]:
        """流式回答并携带用量与结束原因，供界面展示 token 消耗与截断提示。"""
        yield from self._chat_stream_with_usage(messages, thinking, reasoning_effort)

    def complete(
        self,
        messages: list[dict[str, str]],
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
        max_tokens: int = 500,
    ) -> str:
        """非流式补全：返回首个回答文本（用于表达式转换等轻量任务）。"""
        extra_body = self._build_extra_params(thinking, reasoning_effort)
        response = self._client.chat.completions.create(
            model=self._settings.model,
            messages=messages,
            stream=False,
            max_tokens=max_tokens,
            extra_body=extra_body,
        )
        return (response.choices[0].message.content or "").strip()
