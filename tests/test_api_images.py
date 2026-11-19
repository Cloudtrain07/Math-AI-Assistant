# -*- coding: utf-8 -*-
"""新 API 图片发送链路测试：上传校验、跨账号隔离、附图核心语义与临时文件生命周期。

覆盖：上传登录/大小/格式/解码/像素校验；服务端生成文件名与账号隔离目录；
跨账号 image_id 拒绝；纯图片与图文发送（假视觉客户端收到编辑后图片的
模型消息）；继续生成不重新附图；不支持视觉拒绝后保留图片可重试；新题
丢图并清理文件；替换图片删除旧文件；清空删除临时文件且旧轮次无法恢复。
"""

import base64
import io
import struct
import zlib

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from api import (
    ChatBody,
    ChatRegistry,
    TempImageStore,
    _resolve_turn_settings,
    chat_event_stream,
    create_api_app,
)
from conftest import MemoryDailyUsageStore, MemoryHistoryStore
from test_api_chat import FakeStreamClient, login, parse_sse


class VisionFakeClient(FakeStreamClient):
    """与 FakeStreamClient 行为一致，但声明支持原生视觉。"""

    supports_vision = True


def make_png(width: int = 64, height: int = 48, color=(255, 0, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def decode_data_url(data_url: str) -> Image.Image:
    assert data_url.startswith("data:image/jpeg;base64,")
    raw = base64.b64decode(data_url.split(",", 1)[1])
    return Image.open(io.BytesIO(raw))


def last_user_content(messages: list[dict]) -> str | list[dict]:
    return next(m["content"] for m in reversed(messages) if m["role"] == "user")


def last_image_part(messages: list[dict]) -> dict | None:
    """检查当前轮的 user 消息。"""
    content = last_user_content(messages)
    if not isinstance(content, list):
        return None
    return next((part for part in content if part.get("type") == "image_url"), None)


def upload_image(tc: TestClient, data: bytes, name: str = "edited.jpg"):
    return tc.post(
        "/api/images",
        files={"file": (name, data, "image/jpeg")},
    )


@pytest.fixture
def env(api_runtime, tmp_path):
    """双账号 + 视觉假客户端 + 独立临时图片目录的 API 测试环境。"""
    vision = VisionFakeClient()
    api_runtime.client = vision
    api_runtime.history_store = MemoryHistoryStore()
    api_runtime.daily_usage_store = MemoryDailyUsageStore()
    store = TempImageStore(tmp_path / "images")
    api = create_api_app(api_runtime, image_store=store)
    tc = TestClient(api)
    login(tc, api_runtime.accounts[0])
    return tc, api_runtime, vision, store


def chat(tc: TestClient, message: str, **extra):
    resp = tc.post("/api/chat", json={"message": message, **extra})
    return resp.status_code, parse_sse(resp.text)


def test_upload_requires_login(api_runtime, tmp_path):
    api = create_api_app(api_runtime, image_store=TempImageStore(tmp_path / "i"))
    tc = TestClient(api)
    resp = upload_image(tc, make_png())
    assert resp.status_code == 401


def test_upload_validates_size_format_and_decode(env):
    tc, api_runtime, _, store = env
    # 超限：按读取字节数拒绝，不做解码
    oversize = b"\x00" * (10 * 1024 * 1024 + 1)
    assert upload_image(tc, oversize, "big.png").status_code == 413
    # 扩展名不在支持列表
    assert upload_image(tc, make_png(), "a.tiff").status_code == 400
    # 扩展名合法但内容不是图片（真实解码失败）
    resp = upload_image(tc, b"this is not an image", "broken.png")
    assert resp.status_code == 400
    assert "损坏或无法解码" in resp.json()["detail"]
    # 正常上传：服务端生成文件名，落在账号隔离目录
    resp = upload_image(tc, make_png())
    assert resp.status_code == 200
    body = resp.json()
    assert body["width"] == 64 and body["height"] == 48
    files = list((store._root / "alice").iterdir())
    assert len(files) == 1
    # 文件名是服务端随机 id + 扩展名，客户端不能指定
    assert files[0].name.startswith(body["image_id"])


def test_upload_rejects_huge_pixels_before_decode(env, monkeypatch):
    tc, _, _, _ = env
    monkeypatch.setattr("api.MAX_IMAGE_PIXELS", 100)
    resp = upload_image(tc, make_png(64, 48))
    assert resp.status_code == 400
    assert "像素过大" in resp.json()["detail"]


def test_image_only_send_model_receives_uploaded_image(env):
    tc, api_runtime, vision, store = env
    resp = upload_image(tc, make_png(64, 48, (255, 0, 0)))
    image_id = resp.json()["image_id"]
    status, events = chat(tc, "", image_id=image_id)
    assert status == 200
    names = [name for name, _ in events]
    assert names[0] == "start" and names[-1] == "done"
    # 假视觉客户端实际收到包含图片的多模态消息，而非仅页面缩略图
    assert len(vision.calls) == 1
    part = last_image_part(vision.calls[0])
    assert part is not None
    img = decode_data_url(part["image_url"]["url"])
    assert img.size == (64, 48)
    r, g, b = img.convert("RGB").getpixel((2, 2))
    assert abs(r - 255) < 20 and g < 30 and b < 30
    # 文本部分为纯图片发送的默认题目提示
    texts = [
        p["text"]
        for p in vision.calls[0][-1]["content"]
        if p.get("type") == "text"
    ]
    assert texts and "图中" in texts[0]
    # 轮次结束后核心保留图片状态（fresh 复位），文件继续存在供同题追问
    state = tc.app.state.registry._states["alice"]
    assert state.image_state["path"] and state.image_state["fresh"] is False
    assert list((store._root / "alice").iterdir())


def test_foreign_and_unknown_image_id_rejected(env):
    tc, api_runtime, vision, _ = env
    image_id = upload_image(tc, make_png()).json()["image_id"]
    tc2 = TestClient(tc.app)
    login(tc2, api_runtime.accounts[1])
    resp = tc2.post("/api/chat", json={"message": "看图", "image_id": image_id})
    assert resp.status_code == 400
    # 未知 id 同样拒绝
    resp = tc.post("/api/chat", json={"message": "看图", "image_id": "no-such-id"})
    assert resp.status_code == 400
    # 两次被拒都不调用模型
    assert vision.calls == []


def test_continue_does_not_reattach_image(env):
    tc, _, vision, _ = env
    image_id = upload_image(tc, make_png()).json()["image_id"]
    chat(tc, "请看图解题", image_id=image_id)
    resp = tc.post("/api/continue", json={})
    assert resp.status_code == 200
    events = parse_sse(resp.text)
    assert events[-1][0] == "done"
    # 继续生成不重新附图
    assert last_image_part(vision.calls[1]) is None


def test_no_vision_rejects_and_keeps_image_for_retry(api_runtime, tmp_path):
    plain = FakeStreamClient()
    api_runtime.client = plain
    store = TempImageStore(tmp_path / "img")
    api_runtime.history_store = MemoryHistoryStore()
    api_runtime.daily_usage_store = MemoryDailyUsageStore()
    api = create_api_app(api_runtime, image_store=store)
    tc = TestClient(api)
    login(tc, api_runtime.accounts[0])

    image_id = upload_image(tc, make_png()).json()["image_id"]
    status, events = chat(tc, "看图", image_id=image_id)
    names = [name for name, _ in events]
    assert names == ["rejected"]
    assert "不支持图片输入" in events[0][1]["message"]
    # 拒绝路径保留待发图片：文件在、图片状态在，可换服务商后重试
    assert list((store._root / "alice").iterdir())
    state = tc.app.state.registry._states["alice"]
    assert state.image_state["path"]

    # 切换为支持视觉的客户端后重试成功
    api_runtime.client = VisionFakeClient()
    status, events = chat(tc, "看图", image_id=image_id)
    assert events[-1][0] == "done"
    assert last_image_part(api_runtime.client.calls[0]) is not None


def test_new_problem_drops_image_and_deletes_file(api_runtime, tmp_path):
    vision = VisionFakeClient()
    api_runtime.client = vision
    store = TempImageStore(tmp_path / "img")
    api_runtime.history_store = MemoryHistoryStore()
    api_runtime.daily_usage_store = MemoryDailyUsageStore()
    api = create_api_app(api_runtime, image_store=store)
    tc = TestClient(api)
    login(tc, api_runtime.accounts[0])

    # 解答模式：首题附图
    image_id = upload_image(tc, make_png()).json()["image_id"]
    _, events = chat(tc, "解方程 2x+1=5", mode="解答模式", image_id=image_id)
    assert events[-1][0] == "done"
    assert last_image_part(vision.calls[0]) is not None
    files = list((store._root / "alice").iterdir())
    assert len(files) == 1

    # 新题（纯文本）：核心判定旧图过期并丢弃，文件被清理
    _, events = chat(tc, "再解一道新题：x+3=5", mode="解答模式")
    assert events[-1][0] == "done"
    assert last_image_part(vision.calls[1]) is None
    assert not (store._root / "alice").exists() or not list(
        (store._root / "alice").iterdir()
    )
    state = tc.app.state.registry._states["alice"]
    assert state.image_state["path"] is None


def test_replace_image_deletes_old_file(env):
    tc, _, vision, store = env
    first = upload_image(tc, make_png(64, 48, (255, 0, 0))).json()["image_id"]
    _, events = chat(tc, "第一题", image_id=first)
    assert events[-1][0] == "done"
    old_files = list((store._root / "alice").iterdir())
    assert len(old_files) == 1

    # 上传新图并发送：旧文件被替换删除，模型收到新图
    second = upload_image(tc, make_png(40, 30, (0, 0, 255))).json()["image_id"]
    _, events = chat(tc, "第二题", image_id=second)
    assert events[-1][0] == "done"
    assert len(list((store._root / "alice").iterdir())) == 1
    part = last_image_part(vision.calls[1])
    img = decode_data_url(part["image_url"]["url"])
    assert img.size == (40, 30)
    r, g, b = img.convert("RGB").getpixel((2, 2))
    assert b > 200 and r < 30


@pytest.mark.parametrize("question", [
    "刚才那张图里，为什么角A等于角B？",
    "图中这一步为什么能写成 x=3？",
    r"这一步为什么能写成 \(x=3\)？",
])
def test_followup_after_full_solution_sends_same_image_to_model(env, question):
    tc, _, vision, _ = env
    image_id = upload_image(tc, make_png()).json()["image_id"]
    _, events = chat(tc, "直接给我答案", image_id=image_id)
    assert events[-1][0] == "done"
    assert "当前阶段：完整解答" in vision.calls[-1][0]["content"]
    original = last_image_part(vision.calls[-1])
    assert original is not None

    _, events = chat(tc, question)
    assert events[-1][0] == "done"
    assert last_image_part(vision.calls[-1]) == original
    assert events[-1][1]["image_kept"]


@pytest.mark.parametrize(("question", "keep_image"), [
    pytest.param("求图上标出的角是多少度？", True, id="same-image"),
    pytest.param("下一题，求方程 2x+3=11 的解。", False, id="next-problem"),
])
def test_image_routing_for_question_and_followup(env, question, keep_image):
    tc, _, vision, _ = env
    image = make_png(73, 51, (0, 0, 255))
    image_id = upload_image(tc, image, "angle.png").json()["image_id"]
    status, events = chat(
        tc, "直接给我答案", image_id=image_id, background=True,
        generation_id="original-diagram",
    )
    assert status == 200 and events[-1][0] == "done"
    assert "当前阶段：完整解答" in vision.calls[-1][0]["content"]
    preview_id = next(data["preview_id"] for name, data in events if name == "start")
    original = last_image_part(vision.calls[-1])
    assert original is not None
    with decode_data_url(original["image_url"]["url"]) as decoded:
        assert decoded.size == (73, 51)
        r, g, b = decoded.convert("RGB").getpixel((2, 2))
        assert r < 20 and g < 20 and b > 235
    assert tc.get("/api/history").json()["active_preview_id"] == preview_id

    parts = []
    pure_text = []
    kept = []
    active_previews = []
    for turn in (question, "为什么？"):
        status, events = chat(tc, turn)
        assert status == 200 and events[-1][0] == "done"
        parts.append(last_image_part(vision.calls[-1]))
        pure_text.append(isinstance(last_user_content(vision.calls[-1]), str))
        kept.append(events[-1][1]["image_kept"])
        active_previews.append(tc.get("/api/history").json().get("active_preview_id"))

    preview = tc.get("/api/history", params={"preview_id": preview_id})
    assert preview.status_code == 200 and preview.content == image
    assert [part is not None for part in parts] == [keep_image, keep_image]
    assert parts == ([original, original] if keep_image else [None, None])
    assert pure_text == [not keep_image, not keep_image]
    assert kept == [keep_image, keep_image]
    assert active_previews == ([preview_id, preview_id] if keep_image else [None, None])


def test_explicit_problem_switch_drops_active_image_during_hints(env):
    tc, _, vision, _ = env
    image_id = upload_image(tc, make_png()).json()["image_id"]
    _, events = chat(tc, "只提示一步", image_id=image_id)
    assert events[-1][0] == "done" and last_image_part(vision.calls[-1])
    _, events = chat(tc, "换一道题，求方程 2x+3=11 的解")
    assert events[-1][0] == "done" and not events[-1][1]["image_kept"]
    assert last_image_part(vision.calls[-1]) is None


def test_replace_and_remove_active_image_keep_old_message_previews(env):
    tc, _, vision, _ = env
    images = [make_png(color=(255, 0, 0)), make_png(40, 30, (0, 0, 255))]
    previews = []
    for index, image in enumerate(images):
        image_id = upload_image(tc, image, "edited.png").json()["image_id"]
        _, events = chat(
            tc, "直接给我答案" if index == 0 else "换一道题，请解答图中的问题",
            image_id=image_id, background=True, generation_id=f"replace-{index}",
        )
        assert events[-1][0] == "done"
        previews.append(next(data["preview_id"] for name, data in events if name == "start"))
        part = last_image_part(vision.calls[-1])
        assert part is not None
        received = decode_data_url(part["image_url"]["url"])
        with Image.open(io.BytesIO(image)) as expected:
            assert received.size == expected.size
            assert all(
                abs(actual - expected_channel) < 20
                for actual, expected_channel in zip(
                    received.convert("RGB").getpixel((2, 2)), expected.getpixel((2, 2))
                )
            )

    assert tc.get("/api/history").json()["active_preview_id"] == previews[-1]
    assert tc.delete("/api/images").json()["ok"]
    _, events = chat(tc, "图中这一步为什么能写成 x=3？")
    assert events[-1][0] == "done" and not events[-1][1]["image_kept"]
    assert last_image_part(vision.calls[-1]) is None
    history = tc.get("/api/history").json()
    assert "active_preview_id" not in history
    assert [item["id"] for item in history["image_previews"]] == previews
    for preview_id, image in zip(previews, images):
        assert tc.get("/api/history", params={"preview_id": preview_id}).content == image


def test_clear_removes_temp_files(env):
    tc, api_runtime, _, store = env
    image_id = upload_image(tc, make_png()).json()["image_id"]
    chat(tc, "看图", image_id=image_id)
    assert list((store._root / "alice").iterdir())
    resp = tc.post("/api/clear")
    assert resp.json()["ok"] is True
    assert not (store._root / "alice").exists() or not list(
        (store._root / "alice").iterdir()
    )
    state = tc.app.state.registry._states["alice"]
    assert state.image_state["path"] is None


def corrupt_pixel_png(data: bytes) -> bytes:
    """破坏 IDAT 压缩流并修正块 CRC：结构校验（verify）通过、像素解码失败。

    用于证明上传校验不止于 verify()：头部可读但像素数据损坏的图片
    必须在上传阶段被拒绝。
    """
    out = bytearray(data)
    i = out.find(b"IDAT")
    length = struct.unpack(">I", out[i - 4 : i])[0]
    payload_start = i + 4
    out[payload_start + 2] ^= 0xFF
    payload = bytes(out[payload_start : payload_start + length])
    crc = zlib.crc32(b"IDAT" + payload) & 0xFFFFFFFF
    out[payload_start + length : payload_start + length + 4] = struct.pack(">I", crc)
    return bytes(out)


def run_turn(api_runtime, registry, username: str, message: str, image_id: str | None = None):
    """直接驱动 chat_event_stream 同步生成器，返回 SSE 事件列表。

    用于在生成器挂起点之间确定性地插入清空等操作，避免线程竞态。
    """
    body = ChatBody(message=message)
    resolved = _resolve_turn_settings(api_runtime, username, body)
    stream = chat_event_stream(
        api_runtime, registry, username, body, "test", resolved, image_id=image_id
    )
    events = list(stream)
    return parse_sse("".join(events))


def test_registry_epoch_guard_blocks_image_restore(api_runtime, tmp_path):
    """生成中清空：状态立即失效，文件延迟到 release 删除，旧轮次无法写回。"""
    store = TempImageStore(tmp_path / "img")
    registry = ChatRegistry(store)
    state = registry.get_state(api_runtime, "alice")
    # 经 store.save 建立索引（与真实上传一致）
    image_id, path = store.save("alice", b"jpeg", ".jpg")
    assert registry.mount_pending_image("alice", state, state.epoch, image_id) is True
    assert state.image_state["path"] == str(path)

    turn = registry.begin_turn(api_runtime, "alice")
    assert turn is not None
    turn_state, epoch, _token = turn

    # 生成期间清空（busy）：epoch 递增、状态立即失效；文件延迟删除
    assert registry.clear_account(api_runtime, "alice") is True
    assert state.image_state["path"] is None
    assert path.exists()

    # 旧轮次完成时写回被拒绝，文件也不会复活
    assert (
        registry.sync_image_state("alice", turn_state, epoch, turn_state.image_state)
        is False
    )
    assert path.exists()

    # release 释放引用后，延迟删除执行
    registry.release("alice")
    assert not path.exists()
    assert state.image_state["path"] is None


def test_registry_sync_drops_replaced_file(api_runtime, tmp_path):
    """核心返回 path=None（新题丢图/读图失败）时同步删除临时文件。"""
    store = TempImageStore(tmp_path / "img")
    registry = ChatRegistry(store)
    state = registry.get_state(api_runtime, "alice")
    image_id, path = store.save("alice", b"jpeg", ".jpg")
    assert registry.mount_pending_image("alice", state, state.epoch, image_id) is True
    turn = registry.begin_turn(api_runtime, "alice")
    turn_state, epoch, _token = turn
    registry.sync_image_state(
        "alice", turn_state, epoch, {"path": None, "fresh": False}
    )
    assert not path.exists()
    assert state.image_state == {"path": None, "fresh": False}


def test_mount_rejected_when_cleared_between_begin_and_mount(api_runtime, tmp_path):
    """场景2：begin_turn 捕获旧 epoch 后、挂载前发生清空，旧轮次不得重新挂图。"""
    store = TempImageStore(tmp_path / "img")
    registry = ChatRegistry(store)
    state = registry.get_state(api_runtime, "alice")
    image_id, path = store.save("alice", b"jpeg", ".jpg")

    turn = registry.begin_turn(api_runtime, "alice")
    turn_state, epoch, _token = turn

    # 清空发生在 begin_turn 与挂载之间：busy → 状态立即失效、索引移除、文件延迟删除
    assert registry.clear_account(api_runtime, "alice") is True
    assert state.image_state == {"path": None, "fresh": False}

    # 旧 epoch 挂载被拒绝（epoch 与索引双重拦截）：清空后的状态不被写回旧图
    assert registry.mount_pending_image("alice", turn_state, epoch, image_id) is False
    assert state.image_state["path"] is None
    assert path.exists()  # release 前保留（旧轮次可能读取）
    registry.release("alice")
    assert not path.exists()


def test_mount_rejects_when_index_removed_but_file_lingers(api_runtime, tmp_path):
    """回归：release 已释放 busy、旧文件尚未物理删除时，新请求不得挂上旧图。

    复现路径：旧请求解析 → 生成中清空（移除索引、延迟删除）→ release →
    新请求插入并尝试挂载旧图。挂载以索引为准：已从账号索引移除的
    image_id 一律拒绝，即使文件因删除未完成仍在磁盘上。
    """
    store = TempImageStore(tmp_path / "img")
    registry = ChatRegistry(store)
    state = registry.get_state(api_runtime, "alice")
    image_id, path = store.save("alice", b"jpeg", ".jpg")
    assert registry.mount_pending_image("alice", state, state.epoch, image_id) is True

    turn = registry.begin_turn(api_runtime, "alice")
    turn_state, epoch, _token = turn
    assert registry.clear_account(api_runtime, "alice") is True
    assert path.exists()  # 索引已移除，文件延迟删除

    # 用打开句柄模拟"busy 已释放、物理删除尚未完成"的窗口：
    # Windows 上打开中的文件 unlink 失败（被吞掉），文件留在磁盘；
    # POSIX 上 unlink 照常成功——两种平台下断言同样成立（索引已空）。
    with open(path, "rb"):
        registry.release("alice")
        new_turn = registry.begin_turn(api_runtime, "alice")
        assert new_turn is not None
        new_state, new_epoch, _token = new_turn
        # 新请求挂载旧图被拒绝，不恢复图片状态
        assert registry.mount_pending_image("alice", new_state, new_epoch, image_id) is False
        assert new_state.image_state["path"] is None
        registry.release("alice")

    # 清理句柄保护期的残留文件（unlink 失败时由过期清理兜底），确认最终一致
    store.delete("alice", path)
    assert not path.exists()

    # 清空后新上传的合法图片仍可正常使用
    new_id, new_path = store.save("alice", b"new", ".jpg")
    turn3 = registry.begin_turn(api_runtime, "alice")
    state3, epoch3, _token = turn3
    assert registry.mount_pending_image("alice", state3, epoch3, new_id) is True
    assert state3.image_state["path"] == str(new_path)
    registry.sync_image_state(
        "alice", state3, epoch3, {"path": str(new_path), "fresh": False}
    )
    registry.release("alice")
    assert new_path.exists()


def test_chat_rejects_when_resolved_path_deleted_by_clear(env):
    """场景1：图片路径已解析、清空移除索引后，旧请求开始时被拒绝且不调用模型。"""
    tc, api_runtime, vision, store = env
    image_id = upload_image(tc, make_png()).json()["image_id"]
    path = store.resolve("alice", image_id)
    assert path is not None

    # 端点解析之后、本轮开始前发生清空（非 busy → 文件与索引立即删除）
    assert tc.post("/api/clear").json()["ok"] is True
    assert not path.exists()

    events = run_turn(
        api_runtime, tc.app.state.registry, "alice", "看图", image_id=image_id
    )
    assert events[0][0] == "rejected"
    assert events[0][1]["status"] == "image_invalid"
    assert vision.calls == []

    # 旧 image_id 也不再可解析，端点直接 400
    resp = tc.post("/api/chat", json={"message": "看图", "image_id": image_id})
    assert resp.status_code == 400


def test_clear_image_then_new_mount_survives(api_runtime, tmp_path):
    """场景3：显式移除后新一轮挂图不被误删；生成中移除被拒绝且不动文件。"""
    store = TempImageStore(tmp_path / "img")
    registry = ChatRegistry(store)
    state = registry.get_state(api_runtime, "alice")
    id_a, path_a = store.save("alice", b"a", ".jpg")
    assert registry.mount_pending_image("alice", state, state.epoch, id_a) is True

    # 移除：状态变更与文件释放在同一锁边界，旧图删除
    assert registry.clear_image("alice") is True
    assert not path_a.exists()
    assert state.image_state["path"] is None

    # 新一轮挂新图：旧移除操作不可能再删掉它
    id_b, path_b = store.save("alice", b"b", ".jpg")
    turn = registry.begin_turn(api_runtime, "alice")
    turn_state, epoch, _token = turn
    assert registry.mount_pending_image("alice", turn_state, epoch, id_b) is True

    # 生成中移除被拒绝，新图不受影响
    assert registry.clear_image("alice") is False
    assert path_b.exists()

    # 轮次正常收尾后新图仍在（核心保留图片状态）
    registry.sync_image_state(
        "alice", turn_state, epoch, {"path": str(path_b), "fresh": False}
    )
    registry.release("alice")
    assert path_b.exists()
    assert state.image_state["path"] == str(path_b)


def test_clear_during_generation_defers_deletion_and_blocks_restore(env):
    """场景4：生成中清空——状态立即失效、文件延迟删除、done 不回写旧图。"""
    tc, api_runtime, vision, store = env
    registry = tc.app.state.registry
    image_id = upload_image(tc, make_png()).json()["image_id"]
    path = store.resolve("alice", image_id)

    body = ChatBody(message="看图")
    resolved = _resolve_turn_settings(api_runtime, "alice", body)
    stream = chat_event_stream(
        api_runtime,
        registry,
        "alice",
        body,
        "test",
        resolved,
        image_id=image_id,
    )
    start = next(stream)  # 已通过挂载校验并开始生成
    assert "event: start" in start

    # 生成中清空（busy 持有）：状态立即失效，文件延迟删除
    assert registry.clear_account(api_runtime, "alice") is True
    state = registry._states["alice"]
    assert state.image_state["path"] is None
    assert path.exists()  # 旧轮次仍可能读取，物理删除未执行

    rest = parse_sse("".join([start] + list(stream)))
    assert rest[-1][0] == "done"
    # 旧轮次不恢复图片状态：done 后文件已删、状态为空
    assert not path.exists()
    assert state.image_state["path"] is None


def test_upload_rejects_corrupt_pixel_data(env):
    """场景6：图片头可读（verify 通过）但像素解码失败，上传阶段被拒绝。"""
    tc, _, _, _ = env
    data = corrupt_pixel_png(make_png())
    # 前提确认：结构校验确实能通过，失败发生在像素解码
    with Image.open(io.BytesIO(data)) as img:
        img.verify()
    resp = upload_image(tc, data, "corrupt.png")
    assert resp.status_code == 400
    assert "损坏或无法解码" in resp.json()["detail"]


def test_background_previews_remain_separate_and_account_scoped(env):
    tc, runtime, vision, store = env
    images = [make_png(color=(255, 0, 0)), make_png(color=(0, 0, 255))]
    ids = []
    for number, image in enumerate(images):
        image_id = upload_image(tc, image, "edited.png").json()["image_id"]
        response = tc.post("/api/chat", json={"message": f"第{number}题", "image_id": image_id,
                                                "background": True, "generation_id": f"preview-{number}"})
        events = parse_sse(response.text)
        start = next(data for name, data in events if name == "start")
        ids.append(start["preview_id"])
    history = tc.get("/api/history").json()
    assert [x["id"] for x in history["image_previews"]] == ids
    assert history["active_preview_id"] == ids[-1]
    assert len(vision.calls) == 2
    for preview_id, image in zip(ids, images):
        response = tc.get("/api/history", params={"preview_id": preview_id})
        assert response.content == image
        assert response.headers["content-type"] == "image/png"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
    other = TestClient(tc.app)
    login(other, runtime.accounts[1])
    assert other.get("/api/history", params={"preview_id": ids[0]}).status_code == 404
    assert TestClient(tc.app).get("/api/history", params={"preview_id": ids[0]}).status_code == 401
    assert tc.post("/api/clear").json()["ok"]
    assert tc.get("/api/history", params={"preview_id": ids[0]}).status_code == 404


def test_plain_background_turn_has_no_preview_metadata(env):
    tc, _, _, _ = env
    response = tc.post("/api/chat", json={"message": "文字", "background": True, "generation_id": "plain"})
    start = next(data for name, data in parse_sse(response.text) if name == "start")
    assert "preview_id" not in start
    assert "image_previews" not in tc.get("/api/history").json()


def test_preview_tracks_committed_message_not_raw_text_or_failed_duplicate(env):
    tc, runtime, _, _ = env
    class FailedVision(VisionFakeClient):
        def chat_stream_with_usage(self, *args, **kwargs):
            raise RuntimeError("upstream failure")
            yield
    runtime.client = FailedVision()
    question = r"求 \(x+1\)"
    first = upload_image(tc, make_png(color=(255, 0, 0)), "red.png").json()["image_id"]
    failed = tc.post("/api/chat", json={"message": question, "image_id": first, "background": True, "generation_id": "bad"})
    failed_events = parse_sse(failed.text)
    failed_id = next(data["preview_id"] for name, data in failed_events if name == "start")
    assert tc.get("/api/history", params={"preview_id": failed_id}).status_code == 404
    assert "image_previews" not in tc.get("/api/history").json()
    runtime.client = VisionFakeClient()
    blue = make_png(color=(0, 0, 255))
    second = upload_image(tc, blue, "blue.png").json()["image_id"]
    response = tc.post("/api/chat", json={"message": question, "image_id": second, "background": True, "generation_id": "good"})
    good_id = next(data["preview_id"] for name, data in parse_sse(response.text) if name == "start")
    history = tc.get("/api/history").json()
    assert len(history["image_previews"]) == 1
    index = history["image_previews"][0]["index"]
    assert history["image_previews"][0]["id"] == good_id
    assert history["history"][index]["role"] == "user"
    assert question not in history["history"][index]["content"]
    assert tc.get("/api/history", params={"preview_id": good_id}).content == blue
