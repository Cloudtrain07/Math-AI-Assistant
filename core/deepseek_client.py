"""DeepSeek API 封装：基于 OpenAI SDK 的 OpenAI 兼容接口。"""

from __future__ import annotations

import time
from collections.abc import Iterator

import httpx
from openai import OpenAI

from core.config import Settings


class MaxWaitTimeout(Exception):
    """等待超过配置上限时抛出，用于友好提示并停止请求。"""


class DeepSeekClient:
    """负责与 DeepSeek Chat Completions 接口通信。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = OpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=httpx.Timeout(
                connect=30.0,
                read=float(settings.max_wait_seconds),
                write=60.0,
                pool=30.0,
            ),
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
        model: str | None = None,
    ) -> Iterator[tuple[str | None, str | None, object | None, str | None]]:
        """流式生成；(内容, 思考链, usage, finish_reason) 四元组。

        思考期间每秒发一个心跳 (None, None, None, None)：让界面显示进度、
        让 Gradio 的「停止」按钮生效，并支持最长等待上限。
        """
        extra_body = self._build_extra_params(thinking, reasoning_effort)
        stream = self._client.chat.completions.create(
            model=model or self._settings.model,
            messages=messages,
            stream=True,
            max_tokens=self._settings.max_output_tokens,
            extra_body=extra_body,
            stream_options={"include_usage": True},
        )
        finish_reason: str | None = None
        start = time.monotonic()
        last_heartbeat = start
        max_wait = float(self._settings.max_wait_seconds)
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

    def chat_stream(
        self,
        messages: list[dict[str, str]],
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
        model: str | None = None,
    ) -> Iterator[str]:
        """流式回答文本（兼容旧接口）。"""
        for piece, _, _, _ in self._chat_stream_with_usage(
            messages, thinking, reasoning_effort, model
        ):
            if piece:
                yield piece

    def chat_stream_with_usage(
        self,
        messages: list[dict[str, str]],
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
        model: str | None = None,
    ) -> Iterator[tuple[str | None, str | None, object | None, str | None]]:
        """流式回答并携带用量与结束原因，供界面展示 token 消耗与截断提示。"""
        yield from self._chat_stream_with_usage(
            messages, thinking, reasoning_effort, model
        )

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
