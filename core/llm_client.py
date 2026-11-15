"""统一大模型客户端：基于 OpenAI SDK 支持 OpenAI 兼容服务商。

思考参数按本轮实际选中的模型组装（规则见 core.providers.get_model_thinking_capability）：
- deepseek 系：extra_body={"thinking": {"type": ...}} 开关 + 顶层 reasoning_effort
- qwen3.7 系：extra_body={"enable_thinking": ...} 开关，不带强度
- qwen3.8 系：extra_body={"reasoning_effort": ...}（none 即关闭）
- ninfer 系（本地 NInfer 引擎）：extra_body={"reasoning_effort": ...}（none 即关闭）
- mimo 系：extra_body={"thinking": {"type": ...}} 开关，不带强度
- 其余模型：保持原有统一格式（thinking 内联 reasoning_effort），不套用目标规则
输出上限：qwen3.7/3.8 与 mimo 用 max_completion_tokens，其余仍用 max_tokens；
聊天轮次可按请求覆盖（网页设置），覆盖只作用于本轮，不改动共享客户端的
默认输出额度与超时。自定义字段经 SDK 的 extra_body 合并到请求 JSON 顶层，
不产生名为 extra_body 的对象。
流式读循环在工作线程执行，本生成器按固定间隔拉取：上游完全静默（等待
响应头、首个分片前、生成中任一间隙）时产出心跳四元组（SILENT_HEARTBEAT_
SECONDS），与既有"分片到达但无增量"的心跳同形，下游无需感知区别。
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Iterator

import anyio
import httpx
from openai import AsyncOpenAI, OpenAI

from core.config import Settings
from core.providers import get_model_thinking_capability, get_provider_api_key

# 静默期独立心跳间隔（秒）：上游完全静默（等待响应头、首个分片前、生成中
# 任一间隙）时由拉取侧主动产出心跳四元组，保证 SSE 链路有字节流动，避免
# Cloudflare 等代理的空闲读超时（官方默认 125 秒，超限 524）掐断连接。
# 有分片到达时的既有 1 秒节奏心跳不受影响。
SILENT_HEARTBEAT_SECONDS = 10.0

# 消费端关闭后等待读线程退出的上限（秒）。读循环被关闭的流/连接池阻塞时
# 会立刻抛错退出；建流与重试阶段由请求级连接池的关闭取消，重试因连接池
# 已关闭立即失败。join 只做收尾同步（上限计入 busy 释放延迟），超时的
# 线程为守护线程自行退出，不影响释放后的新轮次。
_PRODUCER_JOIN_TIMEOUT = 2.0

# 轻量非流式任务（历史摘要等）的总时限：单一绝对截止时间约束连接、发送、
# 读取与任何重试等待（摘要请求关闭 SDK 自动重试，到期由看门狗取消在途
# 请求）。摘要发生在 SSE 首个事件之前，该静默段没有心跳可发，总时限必须
# 显著低于代理空闲读超时（Cloudflare 官方默认 125 秒）；超时由调用方
# （compressor）捕获并回退本地保守摘要，不影响轮次。
COMPLETE_TOTAL_TIMEOUT_SECONDS = 60.0


class MaxWaitTimeout(Exception):
    """等待超过配置上限时抛出，用于友好提示并停止请求。"""


class TurnAborted(Exception):
    """轮次被消费端中止（停止/断连）：拉取侧据此立即退出，不作为模型错误。"""


class TurnAbort:
    """单轮生成的中止句柄：abort() 立即唤醒阻塞中的拉取侧。

    消费端关闭生成器时，若同步调用链正阻塞在下一次拉取上（等待心跳或
    分片），close 无法向执行中的帧注入 GeneratorExit；此时由响应层调用
    abort() 把哨兵投递进事件队列，拉取侧随即以 TurnAborted 退出，在
    finally 内取消上游并交还 busy 释放时机——不再等待下一个心跳。
    线程安全，可重复调用；轮次尚未开始拉取（未 bind）时只记状态，
    bind 时补投一次哨兵。
    """

    def __init__(self) -> None:
        self.event = threading.Event()
        self._queue: queue.Queue | None = None

    def bind(self, events: queue.Queue) -> None:
        """拉取侧登记事件队列；登记前已 abort 的补投一次哨兵。"""
        self._queue = events
        if self.event.is_set():
            self._wake()

    def abort(self) -> None:
        self.event.set()
        self._wake()

    def _wake(self) -> None:
        if self._queue is not None:
            try:
                self._queue.put_nowait(("abort", None))
            except Exception:
                pass


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

    # 能力声明：支持消费端中止句柄（chat_stream_with_usage 接受 abort 参数）。
    # 假客户端等其余实现不带该声明，核心调用时不注入此参数。
    supports_turn_abort = True

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
        if style == "ninfer":
            # 引擎只认顶层 reasoning_effort；不发 thinking 对象（文档未列该字段）
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
        max_tokens: int | None = None,
        max_wait_seconds: int | float | None = None,
        abort: TurnAbort | None = None,
    ) -> Iterator[tuple[str | None, str | None, object | None, str | None]]:
        """流式生成；(内容, 思考链, usage, finish_reason) 四元组。

        读循环在工作线程里执行，本生成器按 SILENT_HEARTBEAT_SECONDS 间隔
        拉取事件队列：上游完全静默（等待响应头、首个分片前、生成中任一
        间隙）时产出 (None, None, None, None) 心跳，与既有"分片到达但无
        增量"的心跳同形，下游（SSE ": ping"）无需感知区别；Cloudflare 等
        代理的空闲读超时（官方默认 125 秒）因此不再掐断静默中的回答。
        总时长上限口径微调：超过 effective_max_wait 抛 MaxWaitTimeout——
        现在由主生成器判定，等待响应头的静默同样计入（.env 口径"含思考
        时间"）；此前该段仅受 SDK 读超时加重试约束，无统一上限。
        消费端 close（停止/断连）或总超时时 finally 关闭上游流与请求级连接
        池：读取阶段的阻塞读随即抛错退出；建流（等待响应头）与 SDK 内部
        重试阶段的在途请求一并被取消，未发出的重试因连接池已关闭立即失败。
        工作线程先登记流再投递事件、退出时兜底关流，覆盖"停止恰好发生在
        建流完成时"的窗口。busy 释放与历史保存路径不变（出错不保存）。
        """
        model_name = model or self._model
        capability = get_model_thinking_capability(self._provider, model_name)
        extra_body = self._build_extra_params(
            thinking, reasoning_effort, capability
        )
        # 覆盖值只决定本轮的数值；参数名仍按模型分流（providers 决定）
        effective_max_tokens = (
            max_tokens
            if max_tokens is not None
            else (
                self._settings.max_output_tokens
                if self._settings is not None
                else 30000
            )
        )
        effective_max_wait = (
            float(max_wait_seconds)
            if max_wait_seconds is not None
            else (
                float(self._settings.max_wait_seconds)
                if self._settings
                else 600.0
            )
        )
        create_kwargs: dict = {
            "model": model_name,
            "messages": messages,
            "stream": True,
            "extra_body": extra_body,
            "stream_options": {"include_usage": True},
        }
        # 输出额度数值不变，仅按官方要求调整参数名（qwen/mimo 用 max_completion_tokens）
        create_kwargs[capability["output_limit_param"]] = effective_max_tokens
        # 按请求覆盖超时：读超时与下面的流式计时使用同一个有效等待值；
        # 未覆盖时数值与构造时注入的客户端超时一致，行为不变
        create_kwargs["timeout"] = httpx.Timeout(
            connect=30.0,
            read=effective_max_wait,
            write=60.0,
            pool=30.0,
        )

        # 事件契约：(kind, payload) —— "stream" 建流完成、"chunk" 一次分片、
        # "end" 正常读完、"error" 工作线程内的任何异常（含建流失败）、
        # "abort" 消费端中止哨兵（来自 TurnAbort，立即唤醒阻塞中的拉取）
        events: queue.Queue = queue.Queue()
        stop = threading.Event()
        if abort is not None:
            abort.bind(events)
        # 流登记处：工作线程在建流完成后立即登记（先于事件投递），
        # 主侧据此随时可关；两侧行为对称，覆盖建流完成瞬间停止的窗口
        holder: dict = {"stream": None}

        # 请求级 SDK 客户端与连接池：停止/超时时统一关闭，取消在建流、
        # SDK 重试与读取阶段的在途请求（共享客户端不能关，会影响其他轮次；
        # 测试注入假 SDK 时该连接池未被使用，关闭无副作用）
        stream_http = httpx.Client(
            timeout=httpx.Timeout(
                connect=30.0,
                read=effective_max_wait,
                write=60.0,
                pool=30.0,
            )
        )
        stream_sdk = OpenAI(
            api_key=self._api_key,
            base_url=self._base_url,
            http_client=stream_http,
            max_retries=2,
        )

        def _produce() -> None:
            try:
                stream = stream_sdk.chat.completions.create(**create_kwargs)
                holder["stream"] = stream  # 先登记再投递：主侧随时可能关闭
                events.put(("stream", stream))
                for chunk in stream:
                    if stop.is_set() or (abort is not None and abort.event.is_set()):
                        break
                    events.put(("chunk", chunk))
                events.put(("end", None))
            except BaseException as exc:  # 线程内异常一律转交消费端，不让其悬挂
                events.put(("error", exc))
            finally:
                # 工作线程退出时兜底关流：与主侧的关闭互为冗余（幂等），
                # 覆盖"停止恰好发生在建流完成、主侧尚未消费 stream 事件"的窗口
                stream = holder["stream"]
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass

        producer = threading.Thread(
            target=_produce, daemon=True, name="maa-llm-stream"
        )
        producer.start()
        start = time.monotonic()
        deadline = start + effective_max_wait
        last_heartbeat = start
        finish_reason: str | None = None
        aborted = False
        try:
            while True:
                try:
                    # 等待值取"心跳间隔"与"距总上限的剩余时间"的较小者：
                    # 静默期至多每 10 秒产出一次心跳，总上限到期即精准触发
                    wait_seconds = max(
                        0.0, min(SILENT_HEARTBEAT_SECONDS, deadline - time.monotonic())
                    )
                    kind, payload = events.get(timeout=wait_seconds)
                except queue.Empty:
                    now = time.monotonic()
                    if now >= deadline:
                        raise MaxWaitTimeout(
                            f"等待超过 {int(effective_max_wait)} 秒，已自动停止（可重新发送）"
                        )
                    last_heartbeat = now
                    yield None, None, None, None  # 静默期心跳
                    continue
                if kind == "abort":
                    # 消费端已中止（停止/断连）：立即取消上游并退出，
                    # 不等待下一个心跳；join 由 finally 跳过，不拖慢释放
                    aborted = True
                    raise TurnAborted()
                if kind == "stream":
                    continue  # 流已由工作线程登记，主侧经 holder 关闭
                if kind == "end":
                    return
                if kind == "error":
                    raise payload
                # chunk：与既有逐片处理完全一致
                now = time.monotonic()
                if now >= deadline:
                    raise MaxWaitTimeout(
                        f"等待超过 {int(effective_max_wait)} 秒，已自动停止（可重新发送）"
                    )
                chunk = payload
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
            # 停止/断连/总超时统一在此取消上游：置停止位 → 关流（工作线程中
            # 被阻塞的读随即抛错）→ 关请求级连接池（取消在建流与重试阶段的
            # 在途请求，未发出的重试因连接池已关闭立即失败）→ 限时 join。
            # 中止哨兵路径跳过 join：TCP 已在上方关闭，busy 释放不应被
            # SDK 重试退避拖住（守护线程自行收尾）
            stop.set()
            stream = holder["stream"]
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            try:
                stream_http.close()
            except Exception:
                pass
            producer.join(timeout=0.0 if aborted else _PRODUCER_JOIN_TIMEOUT)

    def chat_stream_with_usage(
        self,
        messages: list[dict],
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        max_wait_seconds: int | float | None = None,
        abort: TurnAbort | None = None,
    ) -> Iterator[tuple[str | None, str | None, object | None, str | None]]:
        """流式回答并携带用量与结束原因。

        max_tokens / max_wait_seconds 为网页覆盖值（None 表示沿用配置）：
        等待值同时作用于本次请求的网络读取超时与流式全程计时。
        abort 为消费端中止句柄（None 表示本调用没有中止通道）。
        """
        yield from self._chat_stream_with_usage(
            messages,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
            model=model,
            max_tokens=max_tokens,
            max_wait_seconds=max_wait_seconds,
            abort=abort,
        )

    def complete(
        self,
        messages: list[dict],
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
        max_tokens: int = 500,
        abort: TurnAbort | None = None,
    ) -> str:
        """非流式补全：返回首个回答文本（用于摘要、标签建议等轻量任务）。

        单一绝对截止时间覆盖代理 CONNECT、TLS 握手、发送与读取：摘要走
        AsyncOpenAI + anyio 取消作用域（本同步方法内经 anyio.run 起独立
        事件循环，同步调用方接口不变）。异步取消能立即打断阻塞中的
        握手与读取——线程池看门狗关闭连接池时关不掉握手期的连接
        （Codex 复现：预算 1 秒、假代理 CONNECT 延迟 0.8 秒且不完成 TLS
        握手，总耗时 2.09 秒）。SDK 自动重试保持关闭（429 的 Retry-After
        不再拖长总时长）；正式环境的代理设置经 httpx trust_env 原样生效。

        abort 为消费端中止句柄：置位后（用户停止/断连）取消作用域立即
        打断在途摘要请求并抛 TurnAborted，不再等待剩余预算。超时
        （TimeoutError）、中止（TurnAborted）与失败均由 compressor 捕获
        并回退本地保守摘要，不使轮次失败。
        """
        capability = get_model_thinking_capability(self._provider, self._model)
        extra_body = self._build_extra_params(
            thinking, reasoning_effort, capability
        )
        budget = COMPLETE_TOTAL_TIMEOUT_SECONDS
        create_kwargs = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            "extra_body": extra_body,
            # 阶段超时作兜底；总时长与中止由 anyio 取消作用域收紧
            "timeout": httpx.Timeout(
                connect=budget,
                read=budget,
                write=budget,
                pool=budget,
            ),
        }
        create_kwargs[capability["output_limit_param"]] = max_tokens

        async def _run() -> str:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=budget,
                    read=budget,
                    write=budget,
                    pool=budget,
                )
            ) as http_client:
                sdk = AsyncOpenAI(
                    api_key=self._api_key,
                    base_url=self._base_url,
                    http_client=http_client,
                    max_retries=0,
                )
                try:
                    outcome: dict = {"response": None, "aborted": False}
                    create_error = None
                    # 预算看门狗与中止监视共用一个取消作用域：任一触发
                    # 立即取消在途请求（异步取消覆盖握手/发送/读取）
                    with anyio.CancelScope() as scope:
                        async with anyio.create_task_group() as tg:
                            async def _deadline() -> None:
                                await anyio.sleep(budget)
                                scope.cancel()

                            async def _abort_watch() -> None:
                                if abort is None:
                                    return
                                # 线程安全事件轮询（0.2 秒粒度），置位即取消
                                while not abort.event.is_set():
                                    await anyio.sleep(0.2)
                                outcome["aborted"] = True
                                scope.cancel()

                            tg.start_soon(_deadline)
                            tg.start_soon(_abort_watch)
                            try:
                                outcome["response"] = await sdk.chat.completions.create(
                                    **create_kwargs
                                )
                            except Exception as exc:
                                # 业务异常先记下、在作用域外重抛：异常传播途中
                                # 取消作用域会与监视任务的取消组合成
                                # ExceptionGroup（如 429 的 RateLimitError）
                                create_error = exc
                                scope.cancel()
                            else:
                                scope.cancel()  # 完成后立即结束监视任务
                    if create_error is not None:
                        raise create_error
                    if outcome["response"] is None:
                        if outcome["aborted"]:
                            raise TurnAborted()
                        raise TimeoutError(
                            f"摘要超过 {budget:g} 秒总时限，已取消在途请求"
                        )
                    return (outcome["response"].choices[0].message.content or "").strip()
                finally:
                    # 客户端收尾不受到期/中止取消影响，确保连接真正关闭
                    with anyio.CancelScope(shield=True):
                        await sdk.close()

        return anyio.run(_run)

