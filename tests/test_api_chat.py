# -*- coding: utf-8 -*-
"""新 API 聊天接口测试：假客户端经真实核心服务完成多轮对话。

覆盖：事件顺序与映射、两轮上下文连续、两账号状态不互串、不重复结算、
拒绝映射、busy 并发保护、SSE 线格式鲁棒性。
"""

import datetime
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api import ChatRegistry, create_api_app
from conftest import (
    API_TEST_ACCOUNTS,
    CountingDailyUsageStore,
    FailingSaveStore,
    MemoryDailyUsageStore,
    MemoryHistoryStore,
)
from core import dialogue_state
from core.runtime import build_runtime


class FakeStreamClient:
    """可编程假客户端：记录收到的 messages 与网页覆盖值，按脚本产出片段。"""

    supports_vision = False
    model = "fake-model"

    def __init__(self, script: list[tuple] | None = None) -> None:
        self.calls: list[list[dict]] = []
        self.limit_calls: list[dict] = []
        self.script = script or [
            (
                "第一轮回答：$x=1$",
                "思考A",
                SimpleNamespace(total_tokens=10, prompt_tokens=4, completion_tokens=6),
                "stop",
            ),
            (
                "第二轮回答：引用前文 $x=1$",
                "思考B",
                SimpleNamespace(total_tokens=20, prompt_tokens=8, completion_tokens=12),
                "stop",
            ),
        ]

    def chat_stream_with_usage(
        self,
        messages,
        thinking=None,
        reasoning_effort=None,
        model=None,
        max_tokens=None,
        max_wait_seconds=None,
    ):
        self.calls.append([dict(m) for m in messages])
        self.limit_calls.append(
            {"max_tokens": max_tokens, "max_wait_seconds": max_wait_seconds}
        )
        answer, reasoning, usage, finish = self.script[
            min(len(self.calls) - 1, len(self.script) - 1)
        ]
        if reasoning:
            yield None, reasoning, None, None
        for i in range(0, len(answer), 3):
            yield answer[i : i + 3], None, None, None
        yield None, None, usage, finish


def login(tc: TestClient, account: dict) -> None:
    resp = tc.post(
        "/api/login",
        json={"username": account["username"], "password": account["password"]},
    )
    assert resp.status_code == 200


def parse_sse(raw: str) -> list[tuple[str, dict | None]]:
    """按 SSE 线格式解析事件流；事件以空行分隔。"""
    events: list[tuple[str, dict | None]] = []
    for block in raw.split("\n\n"):
        lines = [ln for ln in block.split("\n") if ln]
        if not lines:
            continue
        name = "message"
        data_lines = []
        for ln in lines:
            if ln.startswith("event:"):
                name = ln[len("event:"):].strip()
            elif ln.startswith("data:"):
                data_lines.append(ln[len("data:"):].strip())
            elif ln.startswith(":"):
                events.append(("ping", None))
        if data_lines:
            events.append((name, json.loads("\n".join(data_lines))))
    return events


def _chat(tc: TestClient, message: str, **extra) -> tuple[int, list[tuple]]:
    resp = tc.post("/api/chat", json={"message": message, **extra})
    return resp.status_code, parse_sse(resp.text)


@pytest.fixture
def env(api_runtime):
    """组装：假客户端 + 内存存储 + 已登录 user1 的 API 客户端。"""
    fake = FakeStreamClient()
    api_runtime.client = fake
    history_store = MemoryHistoryStore()
    daily_store = CountingDailyUsageStore()
    api_runtime.history_store = history_store
    api_runtime.daily_usage_store = daily_store
    api = create_api_app(api_runtime)
    tc = TestClient(api)
    login(tc, api_runtime.accounts[0])
    return tc, api_runtime, fake, history_store, daily_store


def test_event_order_and_mapping(env):
    tc, _, fake, _, _ = env
    status, events = _chat(tc, "解方程 x+1=2")
    assert status == 200
    names = [name for name, _ in events]
    assert names[0] == "start"
    assert names[-1] == "done"
    assert names.count("start") == 1
    assert names.count("done") == 1
    deltas = [data for name, data in events if name == "delta"]
    joined = "".join(d["answer"] for d in deltas)
    assert joined == "第一轮回答：$x=1$"
    reasoning = "".join(d["reasoning"] for d in deltas if d.get("reasoning"))
    assert reasoning == "思考A"

    done = events[-1][1]
    assert done["answer"] == "第一轮回答：$x=1$"
    assert done["turn_total"] == 10
    assert done["error"] == ""
    assert done["empty_reply"] is False
    assert done["save_status"] == "saved"
    assert done["save_note"] == ""
    # 速度字段：假客户端给出 completion_tokens=6，走真实用量路径
    assert done["completion_tokens"] == 6
    assert done["tok_s_estimated"] is False
    assert done["gen_seconds"] is not None
    assert done["first_token_seconds"] is not None
    assert done["tok_s"] is None or done["tok_s"] > 0


def test_streaming_usage_is_forwarded_before_done(env):
    tc, _, fake, _, _ = env
    def stream(*args, **kwargs):
        yield None, "思考", None, None
        yield None, None, SimpleNamespace(completion_tokens=2), None
        yield "答案", None, None, None
        yield None, None, SimpleNamespace(completion_tokens=5, total_tokens=9, prompt_tokens=4), "stop"
    fake.chat_stream_with_usage = stream
    status, events = _chat(tc, "测试")
    assert status == 200
    assert [data["completion_tokens"] for name, data in events if name == "usage"] == [2, 5]
    assert events[-1][0] == "done"
    assert events[-1][1]["completion_tokens"] == 5


def test_done_speed_estimated_without_usage(env):
    """引擎不回 usage 时按字符估算：回答4字+思考2字 → 3 tokens，标记估算。"""
    tc, _, fake, _, _ = env
    fake.script = [("回答文字", "思考", None, "stop")]
    status, events = _chat(tc, "你好")
    assert status == 200
    done = events[-1][1]
    assert done["tok_s_estimated"] is True
    assert done["completion_tokens"] == 3
    assert done["gen_seconds"] is not None
    assert done["tok_s"] is None or done["tok_s"] > 0


def test_done_speed_empty_reply_has_no_timing(env):
    """没有任何输出：估算 0 tokens，首包/生成时长与速度均为 None。"""
    tc, _, fake, _, _ = env
    fake.script = [("", "", None, "stop")]
    status, events = _chat(tc, "你好")
    assert status == 200
    done = events[-1][1]
    assert done["empty_reply"] is True
    assert done["completion_tokens"] == 0
    assert done["tok_s_estimated"] is True
    assert done["gen_seconds"] is None
    assert done["first_token_seconds"] is None
    assert done["tok_s"] is None


def test_explicit_guidance_is_injected_into_model_messages(env):
    tc, _, fake, _, _ = env
    status, events = _chat(tc, "先不要给答案，只提示我下一步。")
    assert status == 200 and events[-1][0] == "done"
    system = fake.calls[-1][0]["content"]
    assert "当前阶段：单步提示" in system
    assert "只给一个可执行的下一步，给完即停" in system
    assert "不要给出后续步骤、完整解法或最终答案" in system

    status, events = _chat(tc, "现在可以讲完整解法了")
    assert status == 200 and events[-1][0] == "done"
    assert "当前阶段：完整解答" in fake.calls[-1][0]["content"]


@pytest.mark.parametrize(("message", "expected_stage"), [
    pytest.param("我只要提示，不要答案。", "单步提示", id="hint-only"),
    pytest.param("直接给我答案。算了，还是只要提示。", "单步提示", id="changes-mind"),
    pytest.param("不用提示了，直接给我答案。", "完整解答", id="answer-only"),
    pytest.param(
        "直接给我答案。算了，别把答案告诉我，我想自己做。",
        "单步提示", id="declines-disclosing-answer",
    ),
    pytest.param(
        "先只要提示。算了，把答案告诉我吧。",
        "完整解答", id="requests-disclosing-answer",
    ),
    pytest.param(
        "先告诉我答案。还是不要把最终答案直接告诉我。",
        "单步提示", id="declines-direct-disclosure",
    ),
])
def test_final_guidance_request_controls_model_prompt(env, message, expected_stage):
    tc, _, fake, _, _ = env
    for turn in ("解方程 2x+3=11", "我还没想出来", "这个方法我还是不会"):
        status, events = _chat(tc, turn)
        assert status == 200 and events[-1][0] == "done"
    assert "当前阶段：针对卡点提示" in fake.calls[-1][0]["content"]

    status, events = _chat(tc, message)
    assert status == 200 and events[-1][0] == "done"
    system = fake.calls[-1][0]
    assert system["role"] == "system"
    stage_instruction = next(
        line for line in system["content"].splitlines() if line.startswith("当前阶段：")
    )
    assert stage_instruction.partition("——")[0] == f"当前阶段：{expected_stage}"
    if expected_stage == "单步提示":
        assert "只给一个可执行的下一步，给完即停" in stage_instruction


def test_two_turns_use_previous_context(env):
    tc, _, fake, _, _ = env
    _chat(tc, "第一问")
    _chat(tc, "第二问")
    assert len(fake.calls) == 2
    contents = [m["content"] for m in fake.calls[1]]
    assert "第一问" in contents
    assert "第一轮回答：$x=1$" in contents
    assert contents[-1] == "第二问"


def test_usage_settled_once_per_turn(env):
    tc, _, _, _, daily_store = env
    _chat(tc, "第一问")
    _chat(tc, "第二问")
    assert [call[2] for call in daily_store.add_usage_calls] == [10, 20]


def test_two_accounts_isolated_same_app(api_runtime):
    """同一 API 应用实例（共享 ChatRegistry 与存储）下两账号状态不互串。"""
    fake = FakeStreamClient()
    api_runtime.client = fake
    api_runtime.history_store = MemoryHistoryStore()
    api_runtime.daily_usage_store = CountingDailyUsageStore()
    api = create_api_app(api_runtime)
    tc1 = TestClient(api)
    login(tc1, api_runtime.accounts[0])
    tc2 = TestClient(api)
    login(tc2, api_runtime.accounts[1])

    _chat(tc1, "甲的问题")
    status2, events2 = _chat(tc2, "乙的问题")
    assert status2 == 200
    # 共享假客户端按调用序取脚本：乙的首轮回答是脚本第二条
    assert events2[-1][1]["answer"] == "第二轮回答：引用前文 $x=1$"

    h1 = tc1.get("/api/history").json()["history"]
    h2 = tc2.get("/api/history").json()["history"]
    assert [m["content"] for m in h1] == ["甲的问题", "第一轮回答：$x=1$"]
    assert [m["content"] for m in h2] == ["乙的问题", "第二轮回答：引用前文 $x=1$"]

    # 甲追问：核心收到的上下文只有甲自己的历史 + 新问题，无乙的内容
    _chat(tc1, "甲的追问")
    contents = [m["content"] for m in fake.calls[-1]]
    assert "甲的问题" in contents
    assert "第一轮回答：$x=1$" in contents
    assert "甲的追问" in contents
    assert "乙的问题" not in contents


def test_rejected_empty(env):
    tc, _, _, _, _ = env
    status, events = _chat(tc, "   ")
    assert status == 200
    assert len(events) == 1
    assert events[0][0] == "rejected"
    assert events[0][1]["status"] == "empty"


def test_rejected_too_long(env):
    tc, api_runtime, _, _, _ = env
    _, events = _chat(tc, "长" * (api_runtime.max_message_len + 1))
    assert events[0][0] == "rejected"
    assert events[0][1]["status"] == "too_long"


def test_rejected_quota(env):
    tc, api_runtime, _, _, daily_store = env
    today = datetime.date.today().isoformat()
    daily_store.add_usage(
        today, api_runtime.accounts[0]["username"], api_runtime.daily_limit
    )
    _, events = _chat(tc, "你好")
    assert events[0][0] == "rejected"
    assert events[0][1]["status"] == "quota"
    assert "额度" in events[0][1]["message"]


def test_busy_rejected_while_previous_turn_running(api_runtime):
    """上一轮未结束时，同账号新请求被 busy 拒绝；结束后恢复正常。"""
    api_runtime.client = FakeStreamClient()
    api_runtime.daily_usage_store = CountingDailyUsageStore()
    registry = ChatRegistry()
    api = create_api_app(api_runtime, registry=registry)
    tc = TestClient(api)
    account = api_runtime.accounts[0]
    login(tc, account)

    # 人为占住账号：模拟上一轮仍在生成
    assert registry.try_acquire(account["username"]) is True
    _, events = _chat(tc, "并发问题")
    assert events[0][0] == "rejected"
    assert events[0][1]["status"] == "busy"

    registry.release(account["username"])
    _, events = _chat(tc, "恢复正常")
    assert events[-1][0] == "done"


def test_clear_resets_state(env):
    tc, api_runtime, fake, history_store, _ = env
    username = api_runtime.accounts[0]["username"]
    _chat(tc, "第一问")
    resp = tc.post("/api/clear")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert tc.get("/api/history").json()["history"] == []
    assert history_store.load(username) == []
    # 清空后的新一轮从空历史开始
    _chat(tc, "第二问")
    contents = [m["content"] for m in fake.calls[-1]]
    assert "第一问" not in contents


class GateClient:
    """可控慢速假客户端：started 后阻塞，release 后产出带用量的回答。"""

    supports_vision = False
    model = "gate-model"

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.closed = False

    def chat_stream_with_usage(self, messages, **kwargs):
        try:
            self.started.set()
            assert self.release.wait(timeout=10)
            yield "清空前回答", None, SimpleNamespace(
                total_tokens=7, prompt_tokens=3, completion_tokens=4
            ), "stop"
        finally:
            self.closed = True


def test_clear_during_generation_discards_stale_state(api_runtime):
    """定点回归：生成途中被清空，旧请求完成时不得把旧上下文写回内存状态。

    磁盘侧由核心保存标记判 stale（存储保持空），内存侧由 epoch 守卫；
    每日已产生用量不因清空被抹掉；清空后新问题不带旧上下文。
    """
    gate = GateClient()
    api_runtime.client = gate
    history_store = MemoryHistoryStore()
    daily_store = CountingDailyUsageStore()
    api_runtime.history_store = history_store
    api_runtime.daily_usage_store = daily_store
    registry = ChatRegistry()
    api = create_api_app(api_runtime, registry=registry)
    username = api_runtime.accounts[0]["username"]

    tc1 = TestClient(api)
    login(tc1, api_runtime.accounts[0])
    tc2 = TestClient(api)
    login(tc2, api_runtime.accounts[0])

    events_box: dict = {}

    def run_chat():
        resp = tc1.post("/api/chat", json={"message": "旧上下文问题"})
        events_box["events"] = parse_sse(resp.text)

    thread = threading.Thread(target=run_chat)
    thread.start()
    assert gate.started.wait(timeout=5)

    # 生成途中，另一"标签页/接口请求"清空
    clear_resp = tc2.post("/api/clear")
    assert clear_resp.json()["ok"] is True

    gate.release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()

    events = events_box["events"]
    done = events[-1][1]
    assert events[-1][0] == "done"
    # 核心保存被判 stale，契约如实下发
    assert done["save_status"] == "stale"
    assert "本次回答未保存" in done["save_note"]
    # 会话累计按清空后的状态显示，不回填旧请求用量
    assert "累计 0 tokens" in done["cumulative_usage"]
    # 每日已产生用量保留
    assert [call[2] for call in daily_store.add_usage_calls] == [7]
    assert done["daily_used"] == 7

    # 内存状态：history/会话用量/阶段均不被旧请求覆盖
    state = registry.get_state(api_runtime, username)
    assert state.history == []
    assert state.usage_state["total_tokens"] == 0
    assert state.stage_state == dialogue_state.new_stage_state()
    assert tc2.get("/api/history").json()["history"] == []

    # 清空后发送新问题：核心收到的上下文不含旧轮内容
    fake = FakeStreamClient()
    api_runtime.client = fake
    _, events = _chat(tc2, "清空后的新问题")
    assert events[-1][0] == "done"
    contents = [m["content"] for m in fake.calls[-1]]
    assert "旧上下文问题" not in contents
    assert "清空前回答" not in contents
    assert contents[-1] == "清空后的新问题"


class BlockingQuotaStore(CountingDailyUsageStore):
    """额度读取可阻塞的假存储：复现 begin_turn 之后、核心额度检查期间清空。"""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.gate = threading.Event()

    def get_used(self, date, account):
        self.entered.set()
        if not self.gate.wait(timeout=10):
            raise RuntimeError("gate timeout")
        return super().get_used(date, account)


def test_clear_during_quota_check_denies_new_save_token(api_runtime):
    """定点回归：begin_turn 捕获后、核心额度检查期间清空。

    保存标记在本轮开始时（清空前）原子取得；清空推进保存计数后，
    旧请求保存必然 stale——磁盘与内存均为空，旧轮次没有有效保存，
    后续新问题不携带旧上下文；每日用量记录一次，不抹掉不重复。
    """
    quota = BlockingQuotaStore()
    fake = FakeStreamClient()
    api_runtime.client = fake
    history_store = MemoryHistoryStore()
    api_runtime.history_store = history_store
    api_runtime.daily_usage_store = quota
    registry = ChatRegistry()
    api = create_api_app(api_runtime, registry=registry)
    username = api_runtime.accounts[0]["username"]

    tc1 = TestClient(api)
    login(tc1, api_runtime.accounts[0])
    tc2 = TestClient(api)
    login(tc2, api_runtime.accounts[0])

    box: dict = {}

    def run_chat():
        resp = tc1.post("/api/chat", json={"message": "旧上下文问题"})
        box["events"] = parse_sse(resp.text)

    thread = threading.Thread(target=run_chat)
    thread.start()
    assert quota.entered.wait(timeout=5), "旧请求未进入额度检查"

    # 核心额度检查阻塞期间，另一会话完成清空
    clear_resp = tc2.post("/api/clear")
    assert clear_resp.json()["ok"] is True

    quota.gate.set()
    thread.join(timeout=10)
    assert not thread.is_alive()

    events = box["events"]
    assert events[-1][0] == "done"
    done = events[-1][1]
    # 旧轮次没有取得清空后的新保存资格：实际保存被拦截，而非仅改文案
    assert done["save_status"] == "stale"
    assert "本次回答未保存" in done["save_note"]
    assert "累计 0 tokens" in done["cumulative_usage"]

    # 磁盘与内存均为空
    assert history_store.load(username) == []
    state = registry.get_state(api_runtime, username)
    assert state.history == []
    assert state.usage_state["total_tokens"] == 0
    assert state.stage_state == dialogue_state.new_stage_state()
    assert tc2.get("/api/history").json()["history"] == []

    # 后续新问题不携带旧上下文
    _, events = _chat(tc2, "清空后的新问题")
    assert events[-1][0] == "done"
    contents = [m["content"] for m in fake.calls[-1]]
    assert "旧上下文问题" not in contents
    assert contents[-1] == "清空后的新问题"

    # 每日已产生用量：两轮各记录一次，未因清空抹掉或重复
    assert [call[2] for call in quota.add_usage_calls] == [10, 20]


def test_done_save_status_failed(api_runtime):
    """保存写盘失败：done 契约带 save_status=failed 与对应提示。"""
    api_runtime.client = FakeStreamClient()
    api_runtime.history_store = FailingSaveStore()
    api_runtime.daily_usage_store = CountingDailyUsageStore()
    tc = TestClient(create_api_app(api_runtime))
    login(tc, api_runtime.accounts[0])
    _, events = _chat(tc, "保存会失败")
    done = events[-1][1]
    assert done["save_status"] == "failed"
    assert "历史保存失败" in done["save_note"]


def test_error_becomes_done_event(api_runtime):
    """模型中途异常：以 done 事件收尾（含友好错误），不挂起、部分回答保留。"""
    from api import create_api_app as _  # noqa: F401 保持导入一致性

    class BoomClient:
        supports_vision = False
        model = "fake"

        def chat_stream_with_usage(self, messages, **kwargs):
            yield "部分回答", None, None, None
            raise RuntimeError("模型连接中断")

    api_runtime.client = BoomClient()
    api_runtime.history_store = MemoryHistoryStore()
    api_runtime.daily_usage_store = CountingDailyUsageStore()
    tc = TestClient(create_api_app(api_runtime))
    login(tc, api_runtime.accounts[0])
    status, events = _chat(tc, "会出错的问题")
    assert status == 200
    names = [name for name, _ in events]
    assert names[-1] == "done"
    done = events[-1][1]
    assert "请求失败" in done["error"]
    assert done["answer"] == "部分回答"


def test_sse_wire_format_survives_byte_splits(env):
    """事件以空行分隔、UTF-8 编码；按单字节重组后事件仍完整。"""
    tc, _, _, _, _ = env
    resp = tc.post("/api/chat", json={"message": "分片测试"})
    buffer = b""
    for i in range(len(resp.content)):
        buffer += resp.content[i : i + 1]
    events = parse_sse(buffer.decode("utf-8"))
    names = [name for name, _ in events]
    assert names[-1] == "done"
    deltas = [data for name, data in events if name == "delta"]
    assert "".join(d["answer"] for d in deltas) == "第一轮回答：$x=1$"


def test_history_endpoint_initial_load(api_runtime):
    api_runtime.client = FakeStreamClient()
    api_runtime.history_store = MemoryHistoryStore()
    api_runtime.daily_usage_store = CountingDailyUsageStore()
    history_store = api_runtime.history_store
    username = api_runtime.accounts[0]["username"]
    history_store.save(
        [
            {"role": "user", "content": "旧问题"},
            {"role": "assistant", "content": "旧回答"},
        ],
        username=username,
        turn_token=history_store.start_turn(username),
    )
    tc = TestClient(create_api_app(api_runtime))
    login(tc, api_runtime.accounts[0])
    data = tc.get("/api/history").json()
    assert [m["content"] for m in data["history"]] == ["旧问题", "旧回答"]


def test_history_corrupt_file_serves_empty_with_notice(api_runtime):
    """历史文件损坏时如实下发空历史与 notice，不伪装成功、不覆盖原文件。"""
    api_runtime.client = FakeStreamClient()
    # 走文件实现（data_dir 已隔离到临时目录）
    from core.storage import get_history_store

    api_runtime.history_store = get_history_store(api_runtime.data_dir)
    username = api_runtime.accounts[0]["username"]
    bad = (
        api_runtime.data_dir
        / "history"
        / f"{username}.json"
    )
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{损坏的历史", encoding="utf-8")

    tc = TestClient(create_api_app(api_runtime))
    login(tc, api_runtime.accounts[0])
    data = tc.get("/api/history").json()

    assert data["history"] == []
    assert data["notice"] != ""
    assert "读取失败" in data["notice"]
    # 损坏文件未被覆盖：后续诊断仍能看到原文
    assert bad.read_text(encoding="utf-8") == "{损坏的历史"


# ===================== 网页覆盖：最大输出 token 与最大等待时间 =====================


def test_web_limit_override_reaches_client(env):
    """覆盖值随请求经核心原样到达客户端调用参数。"""
    tc, _, fake, _, _ = env
    status, events = _chat(
        tc, "解方程 x+1=2", max_output_tokens=48000, max_wait_seconds=900
    )
    assert status == 200
    assert events[-1][0] == "done"
    assert fake.limit_calls[-1] == {"max_tokens": 48000, "max_wait_seconds": 900}


def test_web_limit_absent_uses_runtime_settings(env):
    """留空（缺省）：沿用 runtime.settings 的配置值，不改共享客户端默认。"""
    tc, api_runtime, fake, _, _ = env
    settings = api_runtime.settings
    assert settings is not None
    status, _ = _chat(tc, "解方程 x+1=2")
    assert status == 200
    assert fake.limit_calls[-1] == {
        "max_tokens": settings.max_output_tokens,
        "max_wait_seconds": settings.max_wait_seconds,
    }


def test_web_limit_blank_follows_env_values(monkeypatch, tmp_path):
    """留空时按 .env 环境变量解析的 settings 生效（而非硬编码常量）。"""
    monkeypatch.setenv("MAX_OUTPUT_TOKENS", "12345")
    monkeypatch.setenv("MAX_WAIT_SECONDS", "77")
    accounts_path = tmp_path / "accounts.json"
    accounts_path.write_text(
        json.dumps(API_TEST_ACCOUNTS, ensure_ascii=False), encoding="utf-8"
    )
    runtime = build_runtime(accounts_path=accounts_path)
    fake = FakeStreamClient()
    runtime.client = fake
    runtime.history_store = MemoryHistoryStore()
    runtime.daily_usage_store = MemoryDailyUsageStore()
    tc = TestClient(create_api_app(runtime))
    login(tc, runtime.accounts[0])

    status, _ = _chat(tc, "解方程 x+1=2")
    assert status == 200
    assert fake.limit_calls[-1] == {"max_tokens": 12345, "max_wait_seconds": 77}


@pytest.mark.parametrize(
    "payload",
    [
        {"max_output_tokens": True},
        {"max_output_tokens": 48000.5},
        {"max_output_tokens": 50},
        {"max_output_tokens": 200001},
        {"max_wait_seconds": False},
        {"max_wait_seconds": 1.5},
        {"max_wait_seconds": 5},
        {"max_wait_seconds": 7201},
    ],
)
def test_invalid_web_limits_rejected_before_model_call(env, payload):
    """布尔/小数/越界值：请求开始前 400 拒绝，不调用模型。"""
    tc, _, fake, _, _ = env
    calls_before = len(fake.calls)
    resp = tc.post("/api/chat", json={"message": "解方程 x+1=2", **payload})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert isinstance(detail, str)
    assert ("整数" in detail) or ("之间" in detail)
    assert len(fake.calls) == calls_before


def test_continue_accepts_web_limits(env):
    """继续生成同样携带覆盖值。"""
    tc, _, fake, _, _ = env
    status, _ = _chat(tc, "第一题")
    assert status == 200
    resp = tc.post(
        "/api/continue", json={"max_output_tokens": 48000, "max_wait_seconds": 900}
    )
    assert resp.status_code == 200
    assert fake.limit_calls[-1] == {"max_tokens": 48000, "max_wait_seconds": 900}
