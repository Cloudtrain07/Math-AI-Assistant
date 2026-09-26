from types import SimpleNamespace

from core import llm_client as llm_module
from core.chat_service import stream_reply


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
