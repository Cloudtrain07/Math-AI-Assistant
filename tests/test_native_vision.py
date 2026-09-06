# -*- coding: utf-8 -*-
"""识图统一流程测试：上传只入待发区，发送时按服务商能力直附或转写。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module
from core.providers import _sanitize_provider, get_provider_supports_vision


class _FakeUsage:
    total_tokens = 123
    prompt_tokens = 100
    completion_tokens = 23


class _VisionClient:
    """原生视觉假客户端：记录收到的 messages。"""

    supports_vision = True

    def __init__(self):
        self.seen_messages = None
        self.calls = 0

    def chat_stream_with_usage(
        self, messages, thinking=None, reasoning_effort=None, model=None
    ):
        self.calls += 1
        self.seen_messages = messages
        yield "看图作答", None, None, None
        yield None, None, _FakeUsage(), "stop"


class _TextClient:
    """纯文本假客户端：不应收到图片消息。"""

    def __init__(self):
        self.seen_messages = None
        self.calls = 0

    def chat_stream_with_usage(
        self, messages, thinking=None, reasoning_effort=None, model=None
    ):
        self.calls += 1
        self.seen_messages = messages
        yield "纯文本作答", None, None, None
        yield None, None, _FakeUsage(), "stop"


class _FakeVisionAPI:
    """假转写 API：替代 vision_client。"""

    def __init__(self, text="转写的题目：已知 $x+1=2$，求 x", error=None):
        self.text = text
        self.error = error
        self.calls = 0

    def transcribe_image(self, image_path, prompt=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.text, False


def _fake_request():
    client = type("Client", (), {"host": "127.0.0.1"})()
    return type("Request", (), {"client": client, "username": "user1"})()


def _stage_answer_mode():
    return {"stage": None, "hints": 0, "last_mode": "解答模式"}


def _patch_common(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "save_history", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "_log_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "log_access_event", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "DAILY_LIMIT", 1_000_000)
    monkeypatch.setattr(app_module, "GLOBAL_DAILY_LIMIT", 3_000_000)
    monkeypatch.setattr(app_module, "get_daily_used", lambda d, u: 0)
    monkeypatch.setattr(app_module, "get_daily_total", lambda d: 0)
    monkeypatch.setattr(app_module, "add_daily_question", lambda d: None)
    monkeypatch.setattr(app_module, "add_daily_usage", lambda d, u, t: 0)


def _tiny_png(tmp_path):
    from PIL import Image

    path = tmp_path / "tiny.png"
    Image.new("RGB", (4, 4), (200, 30, 30)).save(path, format="PNG")
    return str(path)


# ---------- providers 能力标记 ----------


def test_sanitize_provider_keeps_supports_vision():
    provider = _sanitize_provider(
        {
            "id": "glm",
            "base_url": "https://example.com",
            "models": ["m1"],
            "supports_vision": True,
        }
    )
    assert provider["supports_vision"] is True
    assert get_provider_supports_vision(provider) is True


def test_sanitize_provider_defaults_vision_false():
    provider = _sanitize_provider(
        {"id": "ds", "base_url": "https://example.com", "models": ["m1"]}
    )
    assert provider["supports_vision"] is False
    assert get_provider_supports_vision(provider) is False


# ---------- to_api_messages 多模态消息 ----------


def test_to_api_messages_without_image_stays_text():
    messages = app_module.to_api_messages(
        [{"role": "user", "content": "1+1=?"}],
        "高中",
        "解答模式",
    )
    assert messages[-1] == {"role": "user", "content": "1+1=?"}


def test_to_api_messages_with_image_builds_content_array():
    messages = app_module.to_api_messages(
        [{"role": "user", "content": "这道题怎么做"}],
        "高中",
        "解答模式",
        image_data_url="data:image/jpeg;base64,QUJD",
    )
    last = messages[-1]
    assert last["role"] == "user"
    assert isinstance(last["content"], list)
    assert last["content"][0]["type"] == "image_url"
    assert last["content"][0]["image_url"]["url"] == "data:image/jpeg;base64,QUJD"
    assert last["content"][1]["type"] == "text"
    assert "这道题怎么做" in last["content"][1]["text"]


def test_to_api_messages_image_without_user_message_is_ignored():
    messages = app_module.to_api_messages(
        [], "高中", "解答模式", image_data_url="data:image/jpeg;base64,QUJD"
    )
    assert all(m["role"] == "system" for m in messages)


# ---------- on_image_upload：只入待发区，永不转写 ----------


def test_on_image_upload_vision_provider_only_pends(monkeypatch):
    def _fail_transcribe(*a, **k):
        raise AssertionError("上传阶段不应触发转写")

    monkeypatch.setattr(app_module, "_transcribe_pending_image", _fail_transcribe)
    results = list(app_module.on_image_upload("/tmp/x.png", "glm"))
    textbox, status, image_out, image_state = results[-1]
    assert "图片已就绪" in status
    assert image_out == "/tmp/x.png"  # 图片保留显示
    assert image_state == {"path": "/tmp/x.png", "fresh": True}


def test_on_image_upload_text_provider_only_pends(monkeypatch):
    def _fail_transcribe(*a, **k):
        raise AssertionError("上传阶段不应触发转写")

    monkeypatch.setattr(app_module, "_transcribe_pending_image", _fail_transcribe)
    results = list(app_module.on_image_upload("/tmp/x.png", "deepseek"))
    textbox, status, image_out, image_state = results[-1]
    assert image_out == "/tmp/x.png"  # 与看图服务商行为一致：保留待发
    assert image_state == {"path": "/tmp/x.png", "fresh": True}
    assert "图片已就绪" in status


# ---------- respond：看图服务商直传路径 ----------


def test_respond_attaches_image_for_vision_client(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path)
    fake = _VisionClient()
    monkeypatch.setattr(app_module, "client", fake)
    png = _tiny_png(tmp_path)

    results = list(
        app_module.respond(
            "这道题怎么做",
            [],
            "高中",
            "解答模式",
            False,
            "glm-5.3-flash",
            "high",
            None,
            {"total_tokens": 0, "turns": 0},
            _stage_answer_mode(),
            {"path": png, "fresh": True},
            None,
            _fake_request(),
        )
    )
    last_history, _, status, _, usage_state, _, _, image_state = results[-1]

    # API 收到的最后一条 user 消息是多模态数组
    api_messages = fake.seen_messages
    assert isinstance(api_messages[-1]["content"], list)
    assert api_messages[-1]["content"][0]["type"] == "image_url"
    assert api_messages[-1]["content"][0]["image_url"]["url"].startswith(
        "data:image/jpeg;base64,"
    )
    # 历史条目保持纯文本并带附图标记
    assert last_history[0]["role"] == "user"
    assert isinstance(last_history[0]["content"], str)
    assert app_module.IMAGE_NOTE in last_history[0]["content"]
    # 图片消耗后 fresh 置 False，路径保留供本题后续轮次重附
    assert image_state == {"path": png, "fresh": False}
    # 用量细分被记录
    assert usage_state["prompt_tokens"] == 100
    assert usage_state["completion_tokens"] == 23


def test_respond_clears_stale_image_on_new_problem(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path)
    fake = _VisionClient()
    monkeypatch.setattr(app_module, "client", fake)
    png = _tiny_png(tmp_path)
    history = [
        {"role": "user", "content": "上一题"},
        {"role": "assistant", "content": "上一题的完整解答"},
    ]

    results = list(
        app_module.respond(
            "求解方程 x²-5x+6=0",
            history,
            "高中",
            "解答模式",
            False,
            "glm-5.3-flash",
            "high",
            None,
            {"total_tokens": 0, "turns": 0},
            _stage_answer_mode(),
            {"path": png, "fresh": False},
            None,
            _fake_request(),
        )
    )
    _, _, _, _, _, _, _, image_state = results[-1]

    # 新题且图片非刚上传：旧图作废，不随新消息发送
    assert image_state == {"path": None, "fresh": False}
    assert isinstance(fake.seen_messages[-1]["content"], str)


def test_respond_force_continue_never_attaches_image(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path)
    fake = _VisionClient()
    monkeypatch.setattr(app_module, "client", fake)
    png = _tiny_png(tmp_path)

    results = list(
        app_module.respond(
            "原题\n（请继续完成上一条回答）",
            [],
            "高中",
            "解答模式",
            False,
            "glm-5.3-flash",
            "high",
            None,
            {"total_tokens": 0, "turns": 0},
            _stage_answer_mode(),
            {"path": png, "fresh": False},
            None,
            _fake_request(),
            force_continue=True,
        )
    )
    _, _, _, _, _, _, _, image_state = results[-1]

    # 继续生成不附图，状态原样保留
    assert isinstance(fake.seen_messages[-1]["content"], str)
    assert image_state == {"path": png, "fresh": False}


def test_respond_empty_message_with_image_auto_prompt(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path)
    fake = _VisionClient()
    monkeypatch.setattr(app_module, "client", fake)
    png = _tiny_png(tmp_path)

    results = list(
        app_module.respond(
            "",
            [],
            "高中",
            "解答模式",
            False,
            "glm-5.3-flash",
            "high",
            None,
            {"total_tokens": 0, "turns": 0},
            _stage_answer_mode(),
            {"path": png, "fresh": True},
            None,
            _fake_request(),
        )
    )
    last_history, _, _, _, _, _, _, _ = results[-1]

    # 空文字 + 有图：自动按"请解答图中的题目"发送，且带附图标记
    assert fake.calls == 1
    text_part = fake.seen_messages[-1]["content"][1]["text"]
    assert app_module._IMAGE_ONLY_PROMPT in text_part
    assert last_history[0]["content"].startswith(app_module._IMAGE_ONLY_PROMPT)


def test_respond_attach_image_false_passthrough(monkeypatch, tmp_path):
    """习题库等内部消息不携带待发图片，待发状态原样透传。"""
    _patch_common(monkeypatch, tmp_path)
    fake = _VisionClient()
    monkeypatch.setattr(app_module, "client", fake)
    png = _tiny_png(tmp_path)

    results = list(
        app_module.respond(
            "【习题库变式】请出一道同类题",
            [],
            "高中",
            "解答模式",
            False,
            "glm-5.3-flash",
            "high",
            None,
            {"total_tokens": 0, "turns": 0},
            _stage_answer_mode(),
            {"path": png, "fresh": True},
            None,
            _fake_request(),
            attach_image=False,
        )
    )
    _, _, _, _, _, _, _, image_state = results[-1]

    assert isinstance(fake.seen_messages[-1]["content"], str)  # 未附图
    assert image_state == {"path": png, "fresh": True}  # 待发状态原样保留


# ---------- respond：文本服务商发送时转写路径 ----------


def test_respond_transcribes_at_send_for_text_client(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path)
    fake = _TextClient()
    monkeypatch.setattr(app_module, "client", fake)
    monkeypatch.setattr(
        app_module, "vision_client", _FakeVisionAPI("转写的题目内容")
    )
    png = _tiny_png(tmp_path)

    thinking_yields = []
    results = list(
        app_module.respond(
            "这道题怎么做",
            [],
            "高中",
            "解答模式",
            False,
            "deepseek-v4-flash",
            "high",
            None,
            {"total_tokens": 0, "turns": 0},
            _stage_answer_mode(),
            {"path": png, "fresh": True},
            None,
            _fake_request(),
        )
    )
    last_history, _, status, _, _, _, _, image_state = results[-1]

    # 有"正在转写"的过渡状态
    thinking_yields = [r for r in results if r[2] and "转写" in r[2]]
    assert thinking_yields
    # API 收到"转写文字 + 用户问题"的组合纯文本
    api_text = fake.seen_messages[-1]["content"]
    assert isinstance(api_text, str)
    assert "转写的题目内容" in api_text
    assert "这道题怎么做" in api_text
    # 历史条目为组合文本（转写持久化，追问不依赖图）
    assert isinstance(last_history[0]["content"], str)
    assert "转写的题目内容" in last_history[0]["content"]
    # 待发图片已清空
    assert image_state == {"path": None, "fresh": False}
    assert "转写失败" not in status


def test_respond_transcribe_failure_keeps_pending(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path)
    fake = _TextClient()
    monkeypatch.setattr(app_module, "client", fake)
    monkeypatch.setattr(
        app_module, "vision_client", _FakeVisionAPI(error=RuntimeError("网络超时"))
    )
    png = _tiny_png(tmp_path)

    results = list(
        app_module.respond(
            "这道题怎么做",
            [],
            "高中",
            "解答模式",
            False,
            "deepseek-v4-flash",
            "high",
            None,
            {"total_tokens": 0, "turns": 0},
            _stage_answer_mode(),
            {"path": png, "fresh": True},
            None,
            _fake_request(),
        )
    )
    _, _, status, _, _, _, _, image_state = results[-1]

    # 失败提示明确，图片与状态保留，主模型未被调用
    assert "转写失败" in status
    assert "网络超时" in status
    assert image_state == {"path": png, "fresh": True}
    assert fake.calls == 0


def test_respond_text_client_without_vision_key(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path)
    fake = _TextClient()
    monkeypatch.setattr(app_module, "client", fake)
    monkeypatch.setattr(app_module, "vision_client", None)
    png = _tiny_png(tmp_path)

    results = list(
        app_module.respond(
            "这道题怎么做",
            [],
            "高中",
            "解答模式",
            False,
            "deepseek-v4-flash",
            "high",
            None,
            {"total_tokens": 0, "turns": 0},
            _stage_answer_mode(),
            {"path": png, "fresh": True},
            None,
            _fake_request(),
        )
    )
    _, _, status, _, _, _, _, image_state = results[-1]

    # 明确给出两条出路；不调主模型；待发保留
    assert "DASHSCOPE_API_KEY" in status
    assert "看图服务商" in status
    assert image_state == {"path": png, "fresh": True}
    assert fake.calls == 0


def test_respond_empty_message_without_image_rejected(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path)
    fake = _TextClient()
    monkeypatch.setattr(app_module, "client", fake)

    results = list(
        app_module.respond(
            "  ",
            [],
            "高中",
            "解答模式",
            False,
            "deepseek-v4-flash",
            "high",
            None,
            {"total_tokens": 0, "turns": 0},
            _stage_answer_mode(),
            None,
            None,
            _fake_request(),
        )
    )
    _, _, status, _, _, _, _, _ = results[-1]
    assert "请先输入问题" in status
    assert fake.calls == 0


# ---------- token 细分显示 ----------


def test_format_usage_shows_split():
    class _Usage:
        total_tokens = 123
        prompt_tokens = 100
        completion_tokens = 23

    text = app_module.format_usage(_Usage())
    assert "输入 100 + 输出 23" in text
    assert "123" in text


def test_format_usage_fallback_without_split():
    class _Usage:
        total_tokens = 123

    text = app_module.format_usage(_Usage())
    assert "本次约 123 tokens" in text
    assert "输入" not in text
