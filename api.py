"""FastAPI 适配层：身份验证 → 输入校验 → 调用现有核心服务 → 事件映射。

本层只做协议适配：额度判断、阶段推进、上下文压缩、模型调用、历史保存、
用量结算与日志全部留在 core.chat_service，不在 API 层复制。

剩余耦合（已知且暂不重构）：本层从 app.py 导入登录副作用与展示文案
（_check_login、_friendly_error、提示常量），而 app.py 依赖 gradio，
因此新 API 间接依赖 Gradio；"core/app 零改动"不是硬性指标，
后续如需解耦可把这些纯展示常量与登录日志下沉到 core，再由两界面共用。

身份与会话：
- 复用 accounts.check_login（经 app._check_login，含访问日志与登录计数）；
- 会话为服务端内存 token（secrets.token_urlsafe），经 HttpOnly + SameSite=Lax
  Cookie 下发，进程重启即失效需重新登录；不引入数据库；
- 写请求做同源校验（浏览器 POST 带 Origin 时必须与 Host 一致），
  配合 SameSite=Lax 覆盖跨站风险；不开放 CORS，不信任转发头。

聊天状态归属：
- 历史、阶段、会话累计用量按账号保存在服务端 ChatRegistry，浏览器不可伪造；
- 历史初值来自 history_store.load（磁盘为真相），每轮以核心 final 结果更新；
- 同一账号同时只允许一轮生成（busy 拒绝），避免并发轮次互踩保存标记；
- 清空一致性：begin_turn 在 registry 锁内**原子地**捕获 epoch 并预取本轮
  保存标记（turn_token 经核心的可选参数复用，核心缺省行为不变）；
  clear_account 在同一锁内按固定顺序执行——先 history_store.clear 推进
  保存计数，再重置内存状态并递增 epoch。两组更新各自原子，旧轮次
  不可能从窗口中取得混合状态：清空前开始的轮次拿的是清空前的标记，
  清空后保存必然 stale（磁盘由核心保存标记拦截，内存由 epoch 拦截）；
- 每日已产生用量记录在 daily_usage_store，清空不影响，也不重复记录。

流式契约（SSE，text/event-stream）：
- event: rejected  data:{status, message}          准备阶段被拒（含额度/长度/无客户端）
- event: start     data:{}                          通过校验，开始生成
- event: delta     data:{answer?, reasoning?}       增量片段（核心累计快照的差分）
- ": ping" 注释行                                   核心心跳（每秒）转成 SSE 注释保活
- event: done      data:{...}                       收尾：最终文本、用量、保存/每日状态
                                                     （含 save_status: saved/failed/stale）

生命周期保证：响应在正常完成或客户端断连取消时，都会在请求处理返回处
确定性 close 同步生成器（ClosingStreamingResponse），busy 随之释放，
不依赖垃圾回收。断连只表示服务端停止消费并关闭上游流，
不代表模型侧已停止计费，界面文案不做该承诺。
"""

from __future__ import annotations

import argparse
import datetime
import json
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import anyio
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import iterate_in_threadpool

# 复用 app.py 的登录副作用与用户文案映射，保持新旧界面提示一致
from app import (
    _DAILY_FAILED_MSG,
    _DAILY_STATS_FAILED_MSG,
    _IMAGE_ONLY_PROMPT,
    _QUOTA_CHECK_FAILED_MSG,
    _RESTORE_FAILED_MSG,
    _SAVE_FAILED_MSG,
    _SAVE_STALE_MSG,
    _check_login,
    _friendly_error,
    IMAGE_NOTE,
    format_usage,
)
from core import chat_service
from core import dialogue_state
from core.prompts import MODE_ANSWER, MODE_INTERACTIVE
from core.runtime import AppRuntime, build_runtime
from core.storage import HistoryReadError
from core.usage import format_cumulative, new_usage_state

APP_ROOT = Path(__file__).resolve().parent
FRONTEND_DIST = APP_ROOT / "frontend" / "dist"

SESSION_COOKIE = "maa_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600

LEVELS = ("小学", "初中", "高中", "大学")
MODES = (MODE_INTERACTIVE, MODE_ANSWER)
DEFAULT_ANSWER_STYLE = "完整步骤+答案"


# ===================== 会话与账号聊天状态 =====================


class SessionStore:
    """内存会话：token → username，绝对过期；进程重启全部失效。"""

    def __init__(self, ttl_seconds: int = SESSION_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._sessions: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def create(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = (username, time.monotonic() + self._ttl)
        return token

    def get(self, token: str | None) -> str | None:
        if not token:
            return None
        with self._lock:
            entry = self._sessions.get(token)
            if entry is None:
                return None
            username, expires = entry
            if time.monotonic() > expires:
                del self._sessions[token]
                return None
            return username

    def drop(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)


@dataclass
class ChatState:
    """单个账号的服务端聊天状态；history 初值来自磁盘。

    epoch 为清空计数：清空时递增，旧轮次完成后凭它判断状态是否仍有效。
    """

    history: list[dict]
    usage_state: dict
    stage_state: dict
    load_error: str | None = None
    epoch: int = 0


class ChatRegistry:
    """按账号保存聊天状态，并保证同一账号同时只有一轮生成。"""

    def __init__(self) -> None:
        self._states: dict[str, ChatState] = {}
        self._busy: set[str] = set()
        self._lock = threading.Lock()

    def _build_state(self, runtime: AppRuntime, username: str) -> ChatState:
        load_error = None
        try:
            history = runtime.history_store.load(username)
        except HistoryReadError as exc:
            history = []
            load_error = _RESTORE_FAILED_MSG.format(detail=exc)
        return ChatState(
            history=history,
            usage_state=new_usage_state(),
            stage_state=dialogue_state.new_stage_state(),
            load_error=load_error,
        )

    def get_state(self, runtime: AppRuntime, username: str) -> ChatState:
        with self._lock:
            state = self._states.get(username)
            if state is not None:
                return state
            state = self._build_state(runtime, username)
            self._states[username] = state
            return state

    def begin_turn(self, runtime: AppRuntime, username: str):
        """占住账号，并在同一锁内原子捕获（epoch, 保存标记）；busy 返回 None。

        turn_token 必须在此处与 epoch 一起取得：清空（clear_account）持同一把
        锁，不可能落在两次捕获之间，旧轮次拿不到清空后的新保存资格。
        锁序固定为 registry → history 守卫，clear_account 相同，无死锁。
        """
        with self._lock:
            if username in self._busy:
                return None
            self._busy.add(username)
            state = self._states.get(username)
            if state is None:
                state = self._build_state(runtime, username)
                self._states[username] = state
            epoch = state.epoch
            turn_token = runtime.history_store.start_turn(username)
            return state, epoch, turn_token

    def clear_account(self, runtime: AppRuntime, username: str) -> bool:
        """一致顺序的清空：先磁盘（推进保存计数），后内存（递增 epoch）。

        全程持有 registry 锁，与 begin_turn 串行化。磁盘清空失败时不动
        内存状态（与既有行为一致：等待用户重试）。返回磁盘清空是否成功。
        """
        with self._lock:
            try:
                runtime.history_store.clear(username)
            except Exception:
                return False
            runtime.history_compressor.reset(username)
            state = self._states.get(username)
            if state is not None:
                state.history = []
                state.usage_state = new_usage_state()
                state.stage_state = dialogue_state.new_stage_state()
                state.load_error = None
                state.epoch += 1
            return True

    def clear_state(self, username: str) -> None:
        """清空内存状态并递增清空计数；进行中的旧轮次完成时据此放弃写回。"""
        with self._lock:
            state = self._states.get(username)
            if state is not None:
                state.history = []
                state.usage_state = new_usage_state()
                state.stage_state = dialogue_state.new_stage_state()
                state.load_error = None
                state.epoch += 1

    def finish_turn(
        self,
        username: str,
        state: ChatState,
        epoch: int,
        *,
        history: list[dict],
        usage_state: dict,
        stage_state: dict,
    ) -> bool:
        """旧轮次完成时写回状态；期间发生过清空（epoch 变化）则放弃。"""
        with self._lock:
            if self._states.get(username) is not state or state.epoch != epoch:
                return False
            state.history = history
            state.usage_state = usage_state
            state.stage_state = stage_state
            return True

    def try_acquire(self, username: str) -> bool:
        with self._lock:
            if username in self._busy:
                return False
            self._busy.add(username)
            return True

    def release(self, username: str) -> None:
        with self._lock:
            self._busy.discard(username)


# ===================== 请求依赖 =====================


def _same_origin(request: Request) -> None:
    """写请求同源校验：浏览器会带 Origin；缺失（脚本/测试）时放行。"""
    origin = request.headers.get("origin")
    if not origin:
        return
    host = request.headers.get("host", "")
    if urllib.parse.urlsplit(origin).netloc != host:
        raise HTTPException(status_code=403, detail="跨站请求被拒绝")


def _session_cookie(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE)


def _current_user(request: Request) -> str:
    sessions: SessionStore = request.app.state.sessions
    username = sessions.get(_session_cookie(request))
    if username is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return username


class LoginBody(BaseModel):
    username: str
    password: str


class ChatBody(BaseModel):
    message: str
    mode: str = MODE_INTERACTIVE
    level: str | None = None
    thinking: bool | None = None


# ===================== SSE 事件映射 =====================


def _sse(name: str, payload: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _reject_message(status: str, turn: dict, runtime: AppRuntime) -> str:
    """把核心拒绝状态映射为界面文案（与 Gradio respond 保持一致语义）。"""
    if status == "empty":
        return "💡 请先输入问题。"
    if status == "too_long":
        return (
            f"⚠️ 消息过长（最多 {runtime.max_message_len:,} 字符），请精简后重试。"
        )
    if status == "quota_read_failed":
        return _QUOTA_CHECK_FAILED_MSG
    if status == "no_client":
        return f"⚠️ {runtime.startup_hint}" if runtime.startup_hint else "⚠️ 模型客户端不可用。"
    return turn.get("reject_message") or "⚠️ 请求被拒绝。"


def _default_model(runtime: AppRuntime) -> str:
    if runtime.default_provider:
        return str(runtime.default_provider.get("default_model") or "")
    if runtime.settings is not None:
        return runtime.settings.model
    return "deepseek-v4-flash"


def _done_payload(
    runtime: AppRuntime, reply: dict, result: dict, turn: dict
) -> dict:
    """核心 final 事件 → done 载荷：只含可序列化的展示数据。"""
    daily_result = result["daily_usage"]
    save_status = result["save_status"]
    return {
        "answer": _normalize_final(reply["answer"]),
        "reasoning_text": _normalize_final(reply["reasoning_text"]),
        "truncated": reply["truncated"],
        "error": _friendly_error(result["error"]) if result["error"] else "",
        "empty_reply": not reply["answer"] and result["error"] is None,
        "turn_total": result["turn_total"],
        "usage_note": format_usage(reply["usage"]),
        "cumulative_usage": format_cumulative(result["usage_state"]),
        "save_status": save_status,
        "save_note": {
            None: "",
            "saved": "",
            "failed": _SAVE_FAILED_MSG,
            "stale": _SAVE_STALE_MSG,
        }[save_status],
        "daily_note": {
            "recorded": "",
            "record_failed": _DAILY_FAILED_MSG,
            "stats_failed": _DAILY_STATS_FAILED_MSG,
        }.get(daily_result["status"], "") if daily_result is not None else "",
        "daily_used": daily_result["daily_used"] if daily_result else None,
        "global_used": daily_result["global_used"] if daily_result else None,
        "daily_limit": runtime.daily_limit,
        "global_daily_limit": runtime.global_daily_limit,
        "image_status": turn.get("image_status", ""),
    }


def _normalize_final(text: str) -> str:
    from core.math_format import normalize_math_text

    return normalize_math_text(text, auto_close=True)


def chat_event_stream(
    runtime: AppRuntime,
    registry: ChatRegistry,
    username: str,
    body: ChatBody,
    ip: str,
):
    """核心 run_chat_turn 事件 → SSE 文本流（同步生成器，Starlette 线程池执行）。

    生成期间若账号被清空（epoch 变化），完成时不写回内存状态，
    done 的会话累计改按清空后的状态显示；磁盘侧由核心保存标记判 stale。
    """
    turn = registry.begin_turn(runtime, username)
    if turn is None:
        yield _sse(
            "rejected",
            {
                "status": "busy",
                "message": "⏳ 上一条回答还在生成中，请等它结束后再发送。",
            },
        )
        return
    state, start_epoch, turn_token = turn

    try:
        account_level = None
        for account in runtime.accounts:
            if account["username"] == username:
                account_level = account.get("level")
                break
        level = body.level if body.level in LEVELS else (
            account_level if account_level in LEVELS else "高中"
        )
        thinking = (
            body.thinking
            if body.thinking is not None
            else (runtime.settings.thinking if runtime.settings is not None else False)
        )

        prev_answer = ""
        prev_reasoning = ""
        for event in chat_service.run_chat_turn(
            message=body.message,
            history=state.history,
            usage_state=state.usage_state,
            username=username,
            ip=ip,
            date=datetime.date.today().isoformat(),
            level=level,
            mode=body.mode,
            stage_state=state.stage_state,
            image_state=None,
            thinking=thinking,
            model=_default_model(runtime),
            reasoning_effort=(
                runtime.settings.reasoning_effort
                if runtime.settings is not None
                else "high"
            ),
            answer_style=DEFAULT_ANSWER_STYLE,
            active_client=runtime.client,
            history_compressor=runtime.history_compressor,
            history_store=runtime.history_store,
            daily_usage_store=runtime.daily_usage_store,
            event_logger=runtime.event_logger,
            max_message_len=runtime.max_message_len,
            max_history_messages=(
                runtime.settings.max_history_messages
                if runtime.settings is not None
                else None
            ),
            daily_limit=runtime.daily_limit,
            global_daily_limit=runtime.global_daily_limit,
            image_only_prompt=_IMAGE_ONLY_PROMPT,
            image_note=IMAGE_NOTE,
            turn_token=turn_token,
        ):
            event_type = event["type"]
            turn = event["turn"]

            if event_type == "rejected":
                yield _sse(
                    "rejected",
                    {
                        "status": turn["status"],
                        "message": _reject_message(turn["status"], turn, runtime),
                    },
                )
                return

            if event_type == "ready":
                yield _sse("start", {})
                continue

            reply = event["reply"]
            answer = reply["answer"]
            reasoning = reply["reasoning_text"]
            delta_answer = (
                answer[len(prev_answer):] if answer.startswith(prev_answer) else answer
            )
            delta_reasoning = (
                reasoning[len(prev_reasoning):]
                if reasoning.startswith(prev_reasoning)
                else reasoning
            )
            prev_answer, prev_reasoning = answer, reasoning

            if event_type == "stream":
                if delta_answer or delta_reasoning:
                    yield _sse(
                        "delta",
                        {"answer": delta_answer, "reasoning": delta_reasoning},
                    )
                else:
                    # 核心心跳片段（无增量）：SSE 注释行保活
                    yield ": ping\n\n"
                continue

            # final：先补发剩余增量，再交出收尾载荷
            if delta_answer or delta_reasoning:
                yield _sse(
                    "delta",
                    {"answer": delta_answer, "reasoning": delta_reasoning},
                )
            result = event["result"]
            persisted = registry.finish_turn(
                username,
                state,
                start_epoch,
                history=result["history"],
                usage_state=result["usage_state"],
                stage_state=turn["stage_state"],
            )
            payload = _done_payload(runtime, reply, result, turn)
            if not persisted:
                # 期间被清空：会话累计改按清空后的状态显示，不回填旧上下文
                payload["cumulative_usage"] = format_cumulative(state.usage_state)
            yield _sse("done", payload)
            return
    finally:
        registry.release(username)


class ClosingStreamingResponse(StreamingResponse):
    """SSE 流式响应：请求生命周期结束时确定性关闭同步生成器。

    Starlette 在正常完成或客户端断连取消时都会退出内部任务组并返回 __call__；
    在此 close 同步生成器，触发核心生成器链的 finally（关上游流、释放 busy），
    不依赖垃圾回收。close 可能在工作线程执行，异常不外泄。
    """

    def __init__(self, sync_generator, **kwargs):
        super().__init__(iterate_in_threadpool(sync_generator), **kwargs)
        self._sync_generator = sync_generator

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                await anyio.to_thread.run_sync(self._sync_generator.close)
            except Exception:
                pass


# ===================== 应用工厂 =====================


def create_api_app(
    runtime: AppRuntime,
    *,
    sessions: SessionStore | None = None,
    registry: ChatRegistry | None = None,
    frontend_dir: Path | None = "default",
) -> FastAPI:
    """组装 FastAPI 应用；会话与状态对象可注入，便于测试隔离。"""
    app = FastAPI(title="数学学习 AI 助手 API", docs_url=None, redoc_url=None)
    app.state.runtime = runtime
    app.state.sessions = sessions if sessions is not None else SessionStore()
    app.state.registry = registry if registry is not None else ChatRegistry()

    @app.post("/api/login", dependencies=[Depends(_same_origin)])
    def login(body: LoginBody, response: Response):
        username = body.username.strip()
        ok = _check_login(runtime, username, body.password)
        if not ok:
            raise HTTPException(status_code=401, detail="用户名或密码不正确")
        token = app.state.sessions.create(username)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            samesite="lax",
            max_age=SESSION_TTL_SECONDS,
            path="/",
        )
        account = next(
            (a for a in runtime.accounts if a["username"] == username), None
        )
        return {
            "username": username,
            "display_name": account.get("display_name") if account else username,
            "level": account.get("level") if account else "高中",
        }

    @app.post("/api/logout", dependencies=[Depends(_same_origin)])
    def logout(request: Request, response: Response):
        app.state.sessions.drop(_session_cookie(request))
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/me")
    def me(username: str = Depends(_current_user)):
        account = next(
            (a for a in runtime.accounts if a["username"] == username), None
        )
        return {
            "username": username,
            "display_name": account.get("display_name") if account else username,
            "level": account.get("level") if account else "高中",
        }

    @app.get("/api/history")
    def history(username: str = Depends(_current_user)):
        state = app.state.registry.get_state(runtime, username)
        return {"history": state.history, "notice": state.load_error or ""}

    @app.post("/api/clear", dependencies=[Depends(_same_origin)])
    def clear(username: str = Depends(_current_user)):
        # clear_account：同一锁内先磁盘后内存，旧轮次无法取混合状态
        ok = app.state.registry.clear_account(runtime, username)
        if not ok:
            return {"ok": False, "message": "⚠️ 清空未成功，请稍后重试。"}
        return {"ok": True, "message": "🗑️ 已开启新对话。"}

    @app.post("/api/chat", dependencies=[Depends(_same_origin)])
    def chat(
        body: ChatBody,
        request: Request,
        username: str = Depends(_current_user),
    ):
        if body.mode not in MODES:
            raise HTTPException(status_code=400, detail="不支持的模式")
        ip = (request.client.host if request.client else None) or "unknown"
        return ClosingStreamingResponse(
            chat_event_stream(app.state.runtime, app.state.registry, username, body, ip),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    if frontend_dir == "default":
        frontend_dir = FRONTEND_DIST
    if frontend_dir is not None and Path(frontend_dir).is_dir():
        app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="数学学习 AI 助手（FastAPI 版）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--fake",
        action="store_true",
        help="开发联调：用内置假模型客户端替换默认服务商客户端（不调用任何真实 API）",
    )
    parser.add_argument(
        "--isolated",
        action="store_true",
        help="自动验证用：配置/账号/数据全部落在临时目录，不读写真实 data 与 .env",
    )
    args = parser.parse_args()

    if args.isolated:
        import tempfile

        temp_root = Path(tempfile.mkdtemp(prefix="maa-api-isolated-"))
        runtime = build_runtime(
            env_file=temp_root / "missing.env",
            providers_file=temp_root / "missing.providers.json",
            accounts_path=temp_root / "accounts.json",
            data_dir=temp_root / "data",
        )
        print(f"[isolated] 临时数据目录：{temp_root}")
    else:
        runtime = build_runtime()

    if args.fake:
        from dev_fake import FakeStreamClient

        runtime.client = FakeStreamClient()
        print("[dev] 已启用假模型客户端（不产生真实调用）")

    # 只允许输出隔离测试账号的凭据；--fake 不代表账号是假的，严禁打印真实密码
    if args.isolated:
        for account in runtime.accounts:
            print(f"[隔离账号] {account['username']} / {account['password']}")

    import uvicorn

    uvicorn.run(create_api_app(runtime), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
