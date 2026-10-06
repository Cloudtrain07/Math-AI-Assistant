import json
from types import SimpleNamespace

import httpx
import pytest
from openai import OpenAI

from core import llm_client as llm_module
from core.chat_service import stream_reply


# ===================== SDK 序列化捕获 =====================
#
# 用真实 OpenAI SDK + httpx.MockTransport 捕获最终发出的请求 JSON，
# 证明字段位置正确（extra_body 合并到顶层、不产生名为 extra_body 的对象、
# 没有跨厂商参数），而不是只测试私有组装函数的返回值。

_STREAM_SSE = (
    'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"m",'
    '"choices":[{"index":0,"delta":{"role":"assistant","content":"答案"},'
    '"finish_reason":null}]}\n\n'
    'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"m",'
    '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}\n\n'
    "data: [DONE]\n\n"
)

_COMPLETION_JSON = {
    "id": "c2",
    "object": "chat.completion",
    "created": 1,
    "model": "m",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": " 非流式答案 "},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
}


def _install_capture_sdk(monkeypatch, captured: list[dict], *, stream: bool) -> None:
    """把真实 SDK 的 HTTP 层换成捕获传输：记录序列化后的请求 JSON 再返回假响应。"""

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode("utf-8")))
        if stream:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=_STREAM_SSE,
            )
        return httpx.Response(200, json=_COMPLETION_JSON)

    real_sdk = OpenAI(
        api_key="fake-key",
        base_url="https://example.invalid/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(llm_module, "OpenAI", lambda **kwargs: real_sdk)


def _make_client(
    monkeypatch,
    *,
    thinking_param: str,
    models: list[str],
    default_model: str,
    stream: bool = True,
) -> tuple[llm_module.LLMClient, list[dict]]:
    monkeypatch.setenv("TEST_LLM_API_KEY", "fake-key")
    captured: list[dict] = []
    _install_capture_sdk(monkeypatch, captured, stream=stream)
    client = llm_module.LLMClient(
        provider={
            "id": "p",
            "name": "P",
            "base_url": "https://example.invalid/v1",
            "api_key_env": "TEST_LLM_API_KEY",
            "models": models,
            "default_model": default_model,
            "thinking_param": thinking_param,
        }
    )
    return client, captured


def _run_stream(client, *, thinking, effort="high", model=None):
    tuples = list(
        client.chat_stream_with_usage(
            [{"role": "user", "content": "问题"}],
            thinking=thinking,
            reasoning_effort=effort,
            model=model,
        )
    )
    assert "".join(t[0] for t in tuples if t[0]) == "答案"
    assert tuples[-1][3] == "stop"


# ---- DeepSeek：thinking.type 开关 + 顶层 reasoning_effort ----


@pytest.mark.parametrize("model", ["deepseek-flash", "deepseek-v4-flash", "deepseek-v4-pro"])
def test_deepseek_stream_on_thinking_top_level_effort(monkeypatch, model):
    client, captured = _make_client(
        monkeypatch,
        thinking_param="deepseek",
        models=[model],
        default_model=model,
    )
    _run_stream(client, thinking=True, effort="max")
    body = captured[-1]
    assert body["model"] == model
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "max"      # 顶层字段，不在 thinking 内
    assert body["max_tokens"] == 30000            # 输出额度不变，仍用 max_tokens
    assert "max_completion_tokens" not in body
    assert "extra_body" not in body               # 自定义字段已合并到顶层


def test_deepseek_off_thinking_sends_no_effort(monkeypatch):
    client, captured = _make_client(
        monkeypatch,
        thinking_param="deepseek",
        models=["deepseek-flash"],
        default_model="deepseek-flash",
    )
    _run_stream(client, thinking=False)
    body = captured[-1]
    assert body["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in body         # 关闭思考不残留强度参数


def test_deepseek_complete_same_assembly(monkeypatch):
    client, captured = _make_client(
        monkeypatch,
        thinking_param="deepseek",
        models=["deepseek-v4-pro"],
        default_model="deepseek-v4-pro",
        stream=False,
    )
    assert client.complete([{"role": "user", "content": "问题"}], thinking=True) == "非流式答案"
    body = captured[-1]
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "high"
    assert body["max_tokens"] == 500


# ---- Qwen3.7：enable_thinking 开关，不带强度 ----


def test_qwen37_switch_without_effort(monkeypatch):
    client, captured = _make_client(
        monkeypatch,
        thinking_param="qwen",
        models=["qwen3.7-flash", "qwen3.8-flash"],
        default_model="qwen3.7-flash",
    )
    _run_stream(client, thinking=True, effort="high")
    body = captured[-1]
    assert body["enable_thinking"] is True
    assert "reasoning_effort" not in body         # Qwen3.7 本轮不发送强度
    assert "thinking" not in body                 # 不串用 DeepSeek/MiMo 字段
    assert body["max_completion_tokens"] == 30000
    assert "max_tokens" not in body


def test_qwen37_off_switch(monkeypatch):
    client, captured = _make_client(
        monkeypatch,
        thinking_param="qwen",
        models=["qwen3.7-flash"],
        default_model="qwen3.7-flash",
    )
    _run_stream(client, thinking=False)
    assert captured[-1]["enable_thinking"] is False


# ---- Qwen3.8：reasoning_effort 控制（none 即官方关闭方式） ----


def test_qwen38_native_effort_and_off(monkeypatch):
    for model in ("qwen3.8-flash", "qwen3.8-max"):
        client, captured = _make_client(
            monkeypatch,
            thinking_param="qwen",
            models=[model],
            default_model=model,
        )
        _run_stream(client, thinking=True, effort="xhigh")
        body = captured[-1]
        assert body["model"] == model
        assert body["reasoning_effort"] == "xhigh"    # 官方原生档位
        assert "thinking" not in body and "enable_thinking" not in body
        assert body["max_completion_tokens"] == 30000

        _run_stream(client, thinking=False)
        assert captured[-1]["reasoning_effort"] == "none"  # 官方关闭方式


def test_selected_model_differs_from_default(monkeypatch):
    """本轮选中模型决定参数：默认 qwen3.7-flash，显式选择 qwen3.8-flash。"""
    client, captured = _make_client(
        monkeypatch,
        thinking_param="qwen",
        models=["qwen3.7-flash", "qwen3.8-flash"],
        default_model="qwen3.7-flash",
    )
    _run_stream(client, thinking=True, effort="high", model="qwen3.8-flash")
    body = captured[-1]
    assert body["model"] == "qwen3.8-flash"
    assert body["reasoning_effort"] == "high"     # 官方映射别名（→xhigh）原样发送
    assert "enable_thinking" not in body

    # 反向：默认 qwen3.8，显式选择 qwen3.7 → 只发开关
    client2, captured2 = _make_client(
        monkeypatch,
        thinking_param="qwen",
        models=["qwen3.7-flash", "qwen3.8-flash"],
        default_model="qwen3.8-flash",
    )
    _run_stream(client2, thinking=True, effort="xhigh", model="qwen3.7-flash")
    body2 = captured2[-1]
    assert body2["model"] == "qwen3.7-flash"
    assert body2["enable_thinking"] is True
    assert "reasoning_effort" not in body2


# ---- MiMo：thinking.type 开关 + max_completion_tokens，无强度 ----


def test_mimo_switch_without_effort(monkeypatch):
    for model in ("mimo-v2.6-flash", "mimo-v2.6-pro"):
        client, captured = _make_client(
            monkeypatch,
            thinking_param="mimo",
            models=[model],
            default_model=model,
        )
        _run_stream(client, thinking=True, effort="high")
        body = captured[-1]
        assert body["model"] == model
        assert body["thinking"] == {"type": "enabled"}
        assert "reasoning_effort" not in body         # 官方未提供强度档位，不发送
        assert body["max_completion_tokens"] == 30000
        assert "max_tokens" not in body

        _run_stream(client, thinking=False)
        assert captured[-1]["thinking"] == {"type": "disabled"}


# ---- 非目标模型保持原有行为 ----


@pytest.mark.parametrize(
    "thinking_param,model",
    [
        ("glm", "glm-5.3-flash"),
        ("qwen", "kimi-k3"),  # 百炼下的非 Qwen 规则模型：不套用 Qwen 规则
        ("openai", "gpt-4o"),
    ],
)
def test_non_target_models_keep_legacy_format(monkeypatch, thinking_param, model):
    client, captured = _make_client(
        monkeypatch,
        thinking_param=thinking_param,
        models=[model],
        default_model=model,
    )
    _run_stream(client, thinking=True, effort="max")
    body = captured[-1]
    assert body["thinking"] == {"type": "enabled", "reasoning_effort": "max"}
    assert body["max_tokens"] == 30000


def test_thinking_param_none_sends_no_thinking_fields(monkeypatch):
    client, captured = _make_client(
        monkeypatch,
        thinking_param="none",
        models=["beta-a"],
        default_model="beta-a",
    )
    _run_stream(client, thinking=True)
    body = captured[-1]
    assert "thinking" not in body
    assert "reasoning_effort" not in body
    assert "enable_thinking" not in body


# ---- 近似前缀 / 未核实模型不误匹配：回到原有 legacy 格式 ----


@pytest.mark.parametrize(
    "thinking_param,model",
    [
        ("qwen", "qwen3.80-unverified"),
        ("deepseek", "deepseek-unverified"),
        ("mimo", "mimo-unverified"),
    ],
)
def test_unverified_near_prefix_models_keep_legacy_format(
    monkeypatch, thinking_param, model
):
    """名单按明确枚举匹配：近似前缀不套用目标规则，不猜参数与输出上限字段。"""
    client, captured = _make_client(
        monkeypatch,
        thinking_param=thinking_param,
        models=[model],
        default_model=model,
    )
    _run_stream(client, thinking=True, effort="max")
    body = captured[-1]
    assert body["model"] == model
    assert body["thinking"] == {"type": "enabled", "reasoning_effort": "max"}
    assert body["max_tokens"] == 30000
    assert "max_completion_tokens" not in body
    assert "enable_thinking" not in body


def test_length_finish_without_usage_marks_reply_truncated(monkeypatch):
    class FakeStream:
        def __init__(self):
            self.closed = False

        def __iter__(self):
            # 第一个片段：输出了一部分答案
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason=None,
                        delta=SimpleNamespace(
                            content="部分答案",
                            reasoning_content=None,
                        ),
                    )
                ],
                usage=None,
            )

            # 最后一个片段：因长度限制结束，但没有返回用量
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="length",
                        delta=SimpleNamespace(
                            content=None,
                            reasoning_content=None,
                        ),
                    )
                ],
                usage=None,
            )

        def close(self):
            self.closed = True

    fake_stream = FakeStream()
    fake_sdk = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kwargs: fake_stream,
            )
        )
    )

    # 用假 SDK 替换真实连接，不发送网络请求
    monkeypatch.setattr(llm_module, "OpenAI", lambda **kwargs: fake_sdk)
    monkeypatch.setenv("TEST_LLM_API_KEY", "fake-key")

    client = llm_module.LLMClient(
        provider={
            "id": "test",
            "base_url": "https://example.invalid/v1",
            "api_key_env": "TEST_LLM_API_KEY",
            "default_model": "test-model",
            "thinking_param": "none",
        }
    )

    states = list(
        stream_reply(
            active_client=client,
            messages=[{"role": "user", "content": "测试问题"}],
            thinking=False,
            reasoning_effort="high",
            model="test-model",
        )
    )

    assert states[-1]["answer"] == "部分答案"
    assert states[-1]["usage"] is None
    assert states[-1]["truncated"] is True
    assert fake_stream.closed is True


def test_empty_choices_usage_chunk_reaches_settlement(monkeypatch):
    """DeepSeek 末尾 choices=[] 的用量分片：用量透传一次、不当文本、结束原因保留。"""

    class FakeStream:
        def __iter__(self):
            # 正文片段
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason=None,
                        delta=SimpleNamespace(
                            content="第一段", reasoning_content=None
                        ),
                    )
                ],
                usage=None,
            )
            # 结束原因片段（无用量）
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
                        delta=SimpleNamespace(content=None, reasoning_content=None),
                    )
                ],
                usage=None,
            )
            # 用量分片：choices 为空但携带 usage
            yield SimpleNamespace(
                choices=[],
                usage=SimpleNamespace(
                    total_tokens=321, prompt_tokens=100, completion_tokens=221
                ),
            )

        def close(self):
            pass

    fake_stream = FakeStream()
    fake_sdk = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kwargs: fake_stream,
            )
        )
    )
    monkeypatch.setattr(llm_module, "OpenAI", lambda **kwargs: fake_sdk)
    monkeypatch.setenv("TEST_LLM_API_KEY", "fake-key")

    client = llm_module.LLMClient(
        provider={
            "id": "test",
            "base_url": "https://example.invalid/v1",
            "api_key_env": "TEST_LLM_API_KEY",
            "default_model": "test-model",
            "thinking_param": "none",
        }
    )

    tuples = list(
        client.chat_stream_with_usage([{"role": "user", "content": "测试问题"}])
    )
    usage_tuples = [t for t in tuples if t[2] is not None]
    assert len(usage_tuples) == 1          # 用量只透传一次，不重复
    assert usage_tuples[0][2].total_tokens == 321
    assert usage_tuples[0][3] == "stop"    # 之前记录的结束原因随用量一起交付
    text = "".join(t[0] for t in tuples if t[0])
    assert text == "第一段"                 # 用量分片不会被当成回答文本

    states = list(
        stream_reply(
            active_client=client,
            messages=[{"role": "user", "content": "测试问题"}],
            thinking=False,
            reasoning_effort="high",
            model="test-model",
        )
    )
    assert states[-1]["answer"] == "第一段"
    assert states[-1]["usage"].total_tokens == 321
    assert states[-1]["truncated"] is False
