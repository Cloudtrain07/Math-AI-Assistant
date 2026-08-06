"""数学学习 AI 助手：Gradio 界面 + DeepSeek 大模型 + 拍照识题 + 学习功能。"""

from __future__ import annotations

import datetime
import json
import os
import threading
import time
from pathlib import Path

import gradio as gr

from core.access import (
    generate_password,
    get_lan_ip,
    is_owner_host,
    log_access_event,
    save_access_info,
)
from core.calculator import CALCULATOR_AVAILABLE, verify_math
from core.config import get_settings
from core.deepseek_client import DeepSeekClient, MaxWaitTimeout
from core.math_format import normalize_math_text
from core.prompts import (
    MODE_ANSWER,
    MODE_INTERACTIVE,
    VISION_COMPLETE_PROMPT,
    build_system_prompt,
)
from core.storage import save_history
from core.tunnel import TunnelWatcher, cleanup_stale_tunnel, find_cloudflared
from core.usage import (
    add_daily_login,
    add_daily_question,
    add_daily_usage,
    check_daily_quota,
    estimate_tokens,
    format_cumulative,
    get_daily_used,
    get_daily_ip_usage,
    get_daily_logins,
    get_daily_questions,
    get_daily_total,
    new_usage_state,
    record_usage,
)
from core.vision_client import VisionClient

APP_VERSION = "2026-08-05"
# 公网访问：固定用户名 + 每次启动随机生成的 8 位密码
ACCESS_USERNAME = "math"
access_password = generate_password()
# app.py 位于项目根目录，data 目录就在同级
DATA_DIR = Path(__file__).resolve().parent / "data"

try:
    settings = get_settings()
    client = DeepSeekClient(settings)
except ValueError as exc:
    # 未配置 API Key 时应用仍可启动，界面给出提示，方便先看效果
    settings = None
    client = None
    startup_hint = str(exc)
else:
    startup_hint = ""
# 每日每访客 token 上限（成本护栏）
DAILY_LIMIT = settings.daily_token_limit if settings is not None else 1_000_000
# 全局每日 token 总上限（成本护栏第二道保险）
GLOBAL_DAILY_LIMIT = (
    settings.global_daily_token_limit if settings is not None else 3_000_000
)
# 单条消息最大长度（字符），超长直接拒绝
MAX_MESSAGE_LEN = settings.max_message_len if settings else 2000

_log_lock = threading.Lock()

# 互动模式阶段关键词（启发式，用户可用「给答案」等口令纠正）
ESCAPE_KEYWORDS = (
    "给答案",
    "直接讲",
    "没时间",
    "别问了",
    "直接解",
    "说答案",
    "给我答案",
    "直接给答案",
    "讲一下",
    "讲讲",
)
PRACTICE_KEYWORDS = (
    "出题",
    "练练",
    "再来一道",
    "巩固",
    "出道题",
    "练习题",
    "再来一题",
    "换个题",
)
GRADING_KEYWORDS = (
    "我的做法",
    "我的过程",
    "我的解答",
    "我的思路",
    "我这样写",
    "我这样算",
    "我算得",
    "我解出来",
    "我算出来",
    "帮我看看",
    "帮我看",
    "检查一下",
    "请批改",
    "帮我批改",
    "改一下",
    "对吗",
    "对不对",
)


def _new_stage_state() -> dict:
    """互动模式的初始阶段状态。"""
    return {"stage": "initial_hint", "hints": 0, "last_mode": MODE_INTERACTIVE}


# 视觉客户端（可选）：未配置 DASHSCOPE_API_KEY 时，上传图片会给出提示
if settings is not None and settings.vision_api_key:
    vision_client = VisionClient(settings)
else:
    vision_client = None


def to_api_messages(
    history: list[dict],
    level: str,
    mode: str,
    answer_style: str | None = None,
    stage: str | None = None,
    mode_changed: bool = False,
) -> list[dict[str, str]]:
    """把界面历史转换为 DeepSeek 需要的消息列表，并控制上下文长度。"""
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": build_system_prompt(level, mode, answer_style, stage),
        }
    ]
    if mode_changed:
        messages.append(
            {
                "role": "system",
                "content": "【模式切换】现在起按新模式作答，忽略此前对话中的旧模式行为。",
            }
        )
    if settings is not None:
        history = history[-settings.max_history_messages :]
    for item in history:
        if isinstance(item.get("content"), str) and item.get("role") in (
            "user",
            "assistant",
        ):
            messages.append({"role": item["role"], "content": item["content"]})
    return messages


def format_usage(usage) -> str:
    """把 API 返回的 token 用量格式化为一行提示。"""
    if usage is None:
        return ""
    total = int(getattr(usage, "total_tokens", 0) or 0)
    reasoning = 0
    details = getattr(usage, "completion_tokens_details", None)
    if details is not None:
        reasoning = int(getattr(details, "reasoning_tokens", 0) or 0)
    if reasoning:
        return f"⚡ 本次约 {total} tokens（其中思考链约 {reasoning}）"
    return f"⚡ 本次约 {total} tokens"


def _friendly_error(exc: Exception) -> str:
    """把 API 异常映射为简洁的中文提示，避免把堆栈抛给用户。"""
    name = exc.__class__.__name__
    text = str(exc)[:150]
    if isinstance(exc, MaxWaitTimeout):
        hint = "已达到最长等待时间，已自动停止（可重新发送）"
    elif "Authentication" in name or "401" in text:
        hint = "API Key 无效或未配置，请检查 .env 中的 DEEPSEEK_API_KEY"
    elif "RateLimit" in name or "429" in text:
        hint = "请求过于频繁或额度不足（触发限流）"
    elif "Connection" in name or "APIConnection" in name:
        hint = "网络无法连接到 API 服务，请检查网络或代理"
    elif "Timeout" in name:
        hint = "请求超时，请稍后重试"
    else:
        hint = "请求失败"
    return f"⚠️ {hint}（{name}: {text}）"


def _check_login(username: str, password: str) -> bool:
    """校验登录并记录到访问日志（Gradio auth 回调）。"""
    ok = username == ACCESS_USERNAME and password == access_password
    log_access_event("登录", f"用户名={username}", f"结果={'成功' if ok else '失败'}")
    if ok:
        add_daily_login(datetime.date.today().isoformat())
    return ok


def _log_event(kind: str, text: str) -> None:
    """把事件与文本追加写入 data/chat.log，用于排查发送链路完整性。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        head = text[:40].replace("\n", "⏎")
        tail = text[-40:].replace("\n", "⏎")
        line = f"[{ts}] {kind} 长度={len(text)} 开头={head!r} 结尾={tail!r}\n"
        with _log_lock:
            with open(DATA_DIR / "chat.log", "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass


def _write_error_log(record: dict) -> None:
    """把一条诊断记录追加写入 data/error.log（JSON 一行一条）。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with _log_lock:
            with open(DATA_DIR / "error.log", "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _base_error_record(ip: str, turn_total: int | None) -> dict:
    """公共字段：当前配置与访客信息。"""
    return {
        "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ip": ip,
        "tokens": turn_total,
        "model": settings.model if settings is not None else None,
        "thinking": settings.thinking if settings is not None else None,
        "reasoning_effort": (
            settings.reasoning_effort if settings is not None else None
        ),
    }


def _log_error(
    exc: Exception, *, ip: str, turn_total: int | None, partial: bool
) -> None:
    """把一次 API 异常的完整信息写入 data/error.log，便于定位根因。"""
    record = _base_error_record(ip, turn_total)
    record.update(
        {
            "type": exc.__class__.__name__,
            "message": str(exc)[:2000],
            "partial_answer": partial,
        }
    )
    _write_error_log(record)


def _log_empty_reply(*, ip: str, turn_total: int | None, truncated: bool) -> None:
    """记录"请求完成但没收到正式回答"的情况（常见于思考链耗尽输出额度）。"""
    record = _base_error_record(ip, turn_total)
    record.update(
        {
            "type": "empty_reply",
            "truncated": truncated,
            "message": (
                "请求完成但未收到正式回答（无异常抛出）；"
                f"truncated={truncated}，可能为思考链已用尽 max_tokens 额度"
            ),
        }
    )
    _write_error_log(record)


def _recognition_incomplete(text: str) -> bool:
    """粗略判断识别文本是否在公式中途被截断（奇数个 $ 或结尾是公式残片）。"""
    if not text:
        return False
    stripped = text.rstrip()
    if stripped.endswith(("\\", "{", "}")):
        return True
    return stripped.count("$") % 2 == 1


def _looks_like_new_problem(text: str) -> bool:
    """粗略判断用户消息是否是一道新题（含数学定界符/运算符，或较长的问句）。"""
    t = (text or "").strip()
    if len(t) >= 15 and (
        t.endswith(("？", "?"))
        or any(k in t for k in ("求", "解", "证明", "已知", "设", "怎么", "如何", "为什么", "计算"))
    ):
        return True
    return any(ch in t for ch in "=<>≤≥×÷√∫∑±∞²³") or "$" in t or "\\" in t


def advance_stage(
    mode: str, message: str, current: dict | None, has_history: bool = False
) -> dict:
    """根据用户新消息计算本轮回复应遵循的阶段（纯函数，供测试）。

    优先级：解答模式 → 逃生词 → 练习词 → 批改词 → 新题判定 → 常规推进。
    """
    current = dict(current or {})
    stage = current.get("stage")
    hints = int(current.get("hints") or 0)
    last_mode = current.get("last_mode") or MODE_INTERACTIVE
    mode_changed = mode != last_mode
    if mode != MODE_INTERACTIVE:
        return {
            "stage": None,
            "hints": 0,
            "last_mode": mode,
            "mode_changed": mode_changed,
        }

    text = (message or "").strip()
    if mode_changed:
        new_stage = "targeted_hint" if has_history else "initial_hint"
        return {
            "stage": new_stage,
            "hints": 0,
            "last_mode": mode,
            "mode_changed": True,
        }
    if any(k in text for k in ESCAPE_KEYWORDS):
        return {
            "stage": "full_solution",
            "hints": hints,
            "last_mode": mode,
            "mode_changed": False,
        }
    if any(k in text for k in PRACTICE_KEYWORDS):
        return {
            "stage": "practice_wait",
            "hints": 0,
            "last_mode": mode,
            "mode_changed": False,
        }
    if any(k in text for k in GRADING_KEYWORDS):
        return {
            "stage": "grading",
            "hints": hints,
            "last_mode": mode,
            "mode_changed": False,
        }
    if stage in ("summary", "full_solution", "grading") and _looks_like_new_problem(
        text
    ):
        return {
            "stage": "initial_hint",
            "hints": 0,
            "last_mode": mode,
            "mode_changed": False,
        }
    if not has_history:
        return {
            "stage": "initial_hint",
            "hints": 0,
            "last_mode": mode,
            "mode_changed": False,
        }
    if stage in (None, "initial_hint"):
        return {
            "stage": "targeted_hint",
            "hints": hints + 1,
            "last_mode": mode,
            "mode_changed": False,
        }
    if stage == "targeted_hint":
        if hints + 1 >= 3:
            return {
                "stage": "full_solution",
                "hints": 0,
                "last_mode": mode,
                "mode_changed": False,
            }
        return {
            "stage": "targeted_hint",
            "hints": hints + 1,
            "last_mode": mode,
            "mode_changed": False,
        }
    if stage == "full_solution":
        return {
            "stage": "targeted_hint",
            "hints": 1,
            "last_mode": mode,
            "mode_changed": False,
        }
    if stage == "practice_wait":
        return {
            "stage": "practice_grade",
            "hints": 0,
            "last_mode": mode,
            "mode_changed": False,
        }
    if stage == "practice_grade":
        return {
            "stage": "practice_grade",
            "hints": 0,
            "last_mode": mode,
            "mode_changed": False,
        }
    if stage == "grading":
        return {
            "stage": "summary",
            "hints": 0,
            "last_mode": mode,
            "mode_changed": False,
        }
    if stage == "summary":
        return {
            "stage": "summary",
            "hints": 0,
            "last_mode": mode,
            "mode_changed": False,
        }
    return {
        "stage": "initial_hint",
        "hints": 0,
        "last_mode": mode,
        "mode_changed": False,
    }


def recognize_image(image: str | None):
    """上传/粘贴图片后自动识别：填入输入框并清空图片区，避免重复识别。"""
    if not image:
        yield "", "", gr.skip()
        return
    if vision_client is None:
        yield (
            "",
            "⚠️ 未配置视觉 Key：请在 .env 中填写 DASHSCOPE_API_KEY（参考 .env.example）",
            gr.skip(),
        )
        return
    yield "", "🔍 正在识别题目图片…", gr.skip()
    try:
        recognized, truncated = vision_client.transcribe_image(image)
    except Exception as exc:
        yield "", f"❌ 图片识别失败：{exc}", gr.skip()
        return
    if not recognized:
        recognized = "（未能识别出题目内容，请手动输入）"
    # 自动补全重试：识别不完整时用补全提示词再问一次
    if _recognition_incomplete(recognized):
        yield recognized, "🔍 识别可能不完整，正在尝试补全…", gr.skip()
        try:
            completed, truncated2 = vision_client.transcribe_image(
                image, prompt=VISION_COMPLETE_PROMPT.format(partial=recognized)
            )
        except Exception:
            completed = ""
            truncated2 = False
        if (
            completed
            and completed.strip().upper() != "ERROR"
            and not _recognition_incomplete(completed)
        ):
            recognized = completed
            truncated = truncated or truncated2
    if _recognition_incomplete(recognized):
        status_text = "⚠️ 识别结果可能不完整（公式未闭合），请核对题目后再发送或重拍。"
    elif truncated:
        status_text = "⚠️ 识别结果可能不完整（题目较长被截断），请核对题目后再发送。"
    else:
        status_text = (
            f"✅ 已识别题目（共 {len(recognized)} 字），请在输入框确认或修改后点击发送。"
        )
    _log_event("识别", recognized)
    yield recognized, status_text, None


def respond(
    message: str,
    history: list[dict],
    level: str,
    mode: str,
    thinking: bool,
    model: str,
    reasoning_effort: str,
    answer_style: str | None,
    usage_state: dict,
    stage_state: dict,
    request: gr.Request,
):
    """纯文本聊天：流式回复，累计会话用量，并按互动模式阶段引导。

    思考链仅当轮展示（reasoning_md 输出），不写入历史、不回传 API。
    """
    history = list(history or [])
    stage_state = dict(stage_state or _new_stage_state())
    stage_state.setdefault("stage", None)
    stage_state.setdefault("hints", 0)
    stage_state.setdefault("last_mode", MODE_INTERACTIVE)
    if mode == MODE_INTERACTIVE and stage_state.get("stage") not in (
        "initial_hint",
        "targeted_hint",
        "full_solution",
        "summary",
        "grading",
        "practice_wait",
        "practice_grade",
    ):
        stage_state["stage"] = "initial_hint"
    if not message.strip():
        yield (
            history,
            "",
            "💡 请先输入问题，或上传题目图片等待自动识别。",
            usage_state,
            stage_state,
            "",
        )
        return

    ip = (request.client.host if request and request.client else None) or "unknown"
    if len(message) > MAX_MESSAGE_LEN:
        log_access_event("过长拒绝", f"IP={ip}", f"长度={len(message)}")
        yield (
            history,
            "",
            f"⚠️ 消息过长（最多 {MAX_MESSAGE_LEN:,} 字符），请精简后重试。",
            usage_state,
            stage_state,
            "",
        )
        return

    today = datetime.date.today().isoformat()
    daily_used = get_daily_used(today, ip)
    global_used = get_daily_total(today)
    quota_msg = check_daily_quota(
        daily_used, global_used, DAILY_LIMIT, GLOBAL_DAILY_LIMIT
    )
    if quota_msg is not None:
        log_access_event(
            "超额拒绝", f"IP={ip}", f"本访客={daily_used}", f"全局={global_used}"
        )
        yield history, "", quota_msg, usage_state, stage_state, ""
        return

    # 根据用户新消息推进互动模式阶段（解答模式无阶段）
    new_stage = advance_stage(mode, message, stage_state, has_history=bool(history))

    user_content = normalize_math_text(message, auto_close=True)
    _log_event("发送", user_content)
    log_access_event("提问", f"IP={ip}", f"长度={len(message)}")
    add_daily_question(today)
    if _recognition_incomplete(message):
        _log_event("发送-原始疑似不完整", message)
    history.append({"role": "user", "content": user_content})
    if client is None:
        yield (
            history + [{"role": "assistant", "content": f"⚠️ {startup_hint}"}],
            "",
            "",
            usage_state,
            new_stage,
            "",
        )
        return

    answer = ""
    reasoning_text = ""
    usage = None
    truncated = False
    thinking_started = time.monotonic()
    try:
        for piece, reasoning_piece, chunk_usage, finish_reason in (
            client.chat_stream_with_usage(
                to_api_messages(
                    history,
                    level,
                    mode,
                    answer_style,
                    stage=new_stage["stage"],
                    mode_changed=new_stage["mode_changed"],
                ),
                thinking=thinking,
                reasoning_effort=reasoning_effort,
                model=model,
            )
        ):
            if reasoning_piece:
                reasoning_text += reasoning_piece
            if piece:
                answer += piece
            if chunk_usage is not None:
                usage = chunk_usage
            if finish_reason == "length":
                truncated = True
            if not answer:
                # 思考阶段：状态栏显示已用时与估算思考 token，避免界面一片空白
                tokens_part = (
                    f"，思考约 {estimate_tokens(reasoning_text)} tokens"
                    if reasoning_text
                    else ""
                )
                status_text = (
                    "⏳ 正在思考… "
                    f"已用时 {int(time.monotonic() - thinking_started)} 秒"
                    f"{tokens_part}"
                )
                yield (
                    history,
                    "",
                    status_text,
                    usage_state,
                    new_stage,
                    normalize_math_text(reasoning_text, auto_close=True),
                )
            else:
                display = normalize_math_text(answer)
                yield (
                    history + [{"role": "assistant", "content": display}],
                    "",
                    "",
                    usage_state,
                    new_stage,
                    normalize_math_text(reasoning_text, auto_close=True),
                )
    except Exception as exc:
        turn_total = None
        if usage is not None:
            turn_total = int(getattr(usage, "total_tokens", 0) or 0)
        elif answer:
            turn_total = estimate_tokens(answer)
        _log_error(exc, ip=ip, turn_total=turn_total, partial=bool(answer))
        usage_state = record_usage(usage_state, turn_total)
        if turn_total is not None:
            daily_used = add_daily_usage(today, ip, turn_total)
            log_access_event(
                "回复失败",
                f"IP={ip}",
                f"错误={exc.__class__.__name__}",
                f"估算tokens={turn_total}",
                f"本访客={daily_used}",
            )
        else:
            log_access_event(
                "回复失败", f"IP={ip}", f"错误={exc.__class__.__name__}"
            )
        if answer:
            display = normalize_math_text(answer, auto_close=True)
            partial_history = history + [{"role": "assistant", "content": display}]
            save_history(partial_history)
            status_text = _friendly_error(exc) + "\n\n（已保留生成的部分内容）"
        else:
            partial_history = history
            status_text = _friendly_error(exc)
        yield (
            partial_history,
            "",
            status_text,
            usage_state,
            new_stage,
            normalize_math_text(reasoning_text, auto_close=True),
        )
        return

    if not answer:
        usage_state = record_usage(usage_state, None)
        turn_total = (
            int(getattr(usage, "total_tokens", 0) or 0) if usage is not None else None
        )
        _log_empty_reply(ip=ip, turn_total=turn_total, truncated=truncated)
        if truncated:
            hint = "模型的思考过程已用完本次输出额度，没能生成正式回答"
        else:
            hint = "请求已完成，但模型没有生成正式回答"
        if turn_total is not None:
            hint += f"（本次约消耗 {turn_total} tokens）"
        status_text = (
            f"⚠️ {hint}。\n\n"
            "建议：关闭「深度思考模式」、把 .env 中 DEEPSEEK_REASONING_EFFORT 调低，"
            "或调大 MAX_OUTPUT_TOKENS 后重启。"
        )
        yield (
            history + [{"role": "assistant", "content": "（未收到正式回答）"}],
            "",
            status_text,
            usage_state,
            new_stage,
            normalize_math_text(reasoning_text, auto_close=True),
        )
        return

    # 最终展示启用自动闭合：模型偶发漏写尾部 $$ 时补上，保证公式能渲染
    display = normalize_math_text(answer, auto_close=True)
    final_history = history + [{"role": "assistant", "content": display}]
    save_history(final_history)
    turn_total = (
        int(getattr(usage, "total_tokens", 0) or 0) if usage is not None else None
    )
    usage_state = record_usage(usage_state, turn_total)
    if turn_total is not None:
        daily_used = add_daily_usage(today, ip, turn_total)
        global_used = get_daily_total(today)
        log_access_event(
            "回复",
            f"IP={ip}",
            f"tokens={turn_total}",
            f"本访客={daily_used}",
            f"全局={global_used}",
        )
    status_text = format_usage(usage)
    cum_text = format_cumulative(usage_state)
    status_text = f"{status_text}\n\n{cum_text}" if status_text else cum_text
    status_text += (
        f"\n\n🛡️ 今日已用 {daily_used:,} / {DAILY_LIMIT:,}（本访客），"
        f"全局 {global_used:,} / {GLOBAL_DAILY_LIMIT:,}"
    )
    if truncated:
        status_text += "\n\n⚠️ 回答因长度限制被截断，可以回复「继续」让我接着写。"
    yield (
        final_history,
        "",
        status_text,
        usage_state,
        new_stage,
        normalize_math_text(reasoning_text, auto_close=True),
    )


def continue_last_answer(
    history: list[dict],
    level: str,
    mode: str,
    thinking: bool,
    model: str,
    reasoning_effort: str,
    answer_style: str | None,
    usage_state: dict,
    stage_state: dict,
    request: gr.Request,
):
    """「继续生成」按钮：取最近一条题目原文，拼上继续指令后复用完整回复流程。"""
    history = list(history or [])
    last_user = ""
    for item in reversed(history):
        content = item.get("content")
        if item.get("role") == "user" and isinstance(content, str) and content.strip():
            last_user = content
            break
    if not last_user:
        yield (
            history,
            "",
            "💡 还没有可继续的题目，请先提问。",
            usage_state,
            stage_state,
            gr.skip(),
        )
        return
    message = f"{last_user}\n（请继续完成上一条回答）"
    yield from respond(
        message,
        history,
        level,
        mode,
        thinking,
        model,
        reasoning_effort,
        answer_style,
        usage_state,
        stage_state,
        request,
    )


def clear_conversation():
    """开启新对话：清空聊天记录、重置用量与阶段状态并持久化。"""
    save_history([])
    return (
        gr.update(value=[]),
        "",
        "🗑️ 已开启新对话。",
        new_usage_state(),
        _new_stage_state(),
        "（本轮无思考过程）",
    )


def render_usage_panel(request: gr.Request) -> str:
    """主人用量面板：仅本机（localhost）可见，展示今日全局/每访客用量与次数。"""
    ip = (request.client.host if request and request.client else None) or "unknown"
    if not is_owner_host(ip):
        return "🔒 （仅主人可见）"
    today = datetime.date.today().isoformat()
    global_used = get_daily_total(today)
    ip_usage = get_daily_ip_usage(today)
    logins = get_daily_logins(today)
    questions = get_daily_questions(today)
    lines = [
        f"**主人用量面板（{today}）**",
        f"- 全局已用：{global_used:,} / {GLOBAL_DAILY_LIMIT:,} tokens",
        f"- 登录次数：{logins}，提问次数：{questions}",
        "**各访客用量：**",
    ]
    if ip_usage:
        for visitor, tokens in sorted(
            ip_usage.items(), key=lambda item: item[1], reverse=True
        ):
            lines.append(f"- {visitor}：{tokens:,} tokens")
    else:
        lines.append("- （今日暂无用量）")
    return "\n".join(lines)


def verify_last_answer(history: list[dict]) -> str:
    """用 sympy 验算上一条 AI 回答里的公式。"""
    last = None
    for item in reversed(list(history or [])):
        if item.get("role") == "assistant" and isinstance(item.get("content"), str):
            last = item["content"]
            break
    if last is None:
        return "ℹ️ 还没有可验算的 AI 回答。"
    if not CALCULATOR_AVAILABLE:
        return "⚠️ 本地验算组件未安装（sympy / latex2sympy2），请先运行 pip install -r requirements.txt。"
    results = verify_math(last)
    if not results:
        return "ℹ️ 上一条回答里没有找到可验算的公式。"
    lines = ["**本地验算结果：**"]
    for item in results:
        source = item["source"][:60]
        if item["status"] == "成立":
            lines.append(f"✅ `{source}` → 等式成立")
        elif item["status"] == "不成立":
            lines.append(f"❌ `{source}` → **等式不成立！**")
        elif item["status"] == "表达式":
            lines.append(f"ℹ️ `{source}` → 已解析为表达式：`{item['detail']}`")
        elif item["status"] == "方程":
            lines.append(f"ℹ️ `{source}` → 已解析为方程 `{item['detail']}`（含未知数，无法自动判定，可代入具体值验证）")
        elif item["status"] == "解为":
            lines.append(f"ℹ️ `{source}` → 方程的解为 `{item['detail']}`（可对照 AI 给出的答案）")
        else:
            lines.append(f"⚠️ `{source}` → 解析失败：{item['detail']}")
    return "\n\n".join(lines)


def toggle_answer_style(mode: str):
    """解答风格选项只在解答模式下显示。"""
    return gr.update(visible=(mode == MODE_ANSWER))


with gr.Blocks(title="数学学习 AI 助手") as demo:
    gr.Markdown(
        "# 🧮 数学学习 AI 助手\n\n"
        "基于 DeepSeek 大模型，采用苏格拉底式引导，一步步带你学会数学。"
        f"支持 LaTeX 公式、拍照识题与本地验算。版本 v{APP_VERSION}"
    )
    chatbot = gr.Chatbot(
        type="messages",
        height=430,
        value=[],
        allow_tags=False,
        show_copy_button=True,
        latex_delimiters=[
            {"left": "$$", "right": "$$", "display": True},
            {"left": "$", "right": "$", "display": False},
        ],
    )
    with gr.Row():
        level_radio = gr.Radio(
            ["小学", "初中", "高中", "大学"], value="高中", label="学段"
        )
        mode_radio = gr.Radio(
            [MODE_INTERACTIVE, MODE_ANSWER],
            value=MODE_INTERACTIVE,
            label="模式",
        )
        thinking_checkbox = gr.Checkbox(
            value=settings.thinking if settings else False,
            label="深度思考模式（更严谨，稍慢）",
        )
        model_radio = gr.Radio(
            ["deepseek-v4-flash", "deepseek-v4-pro"],
            value=settings.model if settings else "deepseek-v4-flash",
            label="模型",
        )
        effort_radio = gr.Radio(
            ["low", "high", "max"],
            value=settings.reasoning_effort if settings else "high",
            label="思考强度",
        )
    with gr.Row():
        answer_style_radio = gr.Radio(
            ["完整步骤+答案", "简略过程+答案", "只给答案和结论"],
            value="完整步骤+答案",
            label="解答风格（仅解答模式）",
            visible=False,
        )
    with gr.Row():
        image_input = gr.Image(
            label="题目图片（上传/粘贴后自动识别）",
            sources=["upload", "clipboard"],
            type="filepath",
        )
        with gr.Column():
            textbox = gr.Textbox(
                placeholder="输入你的数学问题，例如：怎么解一元二次方程？",
                show_label=False,
                container=False,
            )
            status = gr.Markdown("")
            usage_state = gr.State(value=new_usage_state())
            stage_state = gr.State(value=_new_stage_state())
            send_btn = gr.Button("发送", variant="primary")
    with gr.Accordion("思考过程（仅当轮，不会写入历史）", open=False):
        reasoning_md = gr.Markdown(
            "（本轮暂无思考过程）",
            latex_delimiters=[
                {"left": "$$", "right": "$$", "display": True},
                {"left": "$", "right": "$", "display": False},
            ],
        )
    with gr.Row():
        clear_btn = gr.Button("🗑️ 新对话")
        stop_btn = gr.Button("⏹️ 停止生成")
        continue_btn = gr.Button("▶️ 继续生成")
        verify_btn = gr.Button("🧮 验算上一条回答")
    with gr.Row():
        refresh_btn = gr.Button("🔄 刷新用量（仅本机）")
        usage_panel = gr.Markdown("（点击“刷新用量”查看今日用量）")
    gr.Examples(
        examples=[
            "解方程 x² - 5x + 6 = 0，请引导我自己算出来",
            "什么是导数？用生活中的例子讲讲",
            "我不会做几何证明题，怎么入手？",
        ],
        inputs=textbox,
    )

    image_input.upload(
        recognize_image,
        inputs=[image_input],
        outputs=[textbox, status, image_input],
    )

    send_event = send_btn.click(
        respond,
        inputs=[
            textbox,
            chatbot,
            level_radio,
            mode_radio,
            thinking_checkbox,
            model_radio,
            effort_radio,
            answer_style_radio,
            usage_state,
            stage_state,
        ],
        outputs=[chatbot, textbox, status, usage_state, stage_state, reasoning_md],
    )
    submit_event = textbox.submit(
        respond,
        inputs=[
            textbox,
            chatbot,
            level_radio,
            mode_radio,
            thinking_checkbox,
            model_radio,
            effort_radio,
            answer_style_radio,
            usage_state,
            stage_state,
        ],
        outputs=[chatbot, textbox, status, usage_state, stage_state, reasoning_md],
    )
    continue_event = continue_btn.click(
        continue_last_answer,
        inputs=[
            chatbot,
            level_radio,
            mode_radio,
            thinking_checkbox,
            model_radio,
            effort_radio,
            answer_style_radio,
            usage_state,
            stage_state,
        ],
        outputs=[chatbot, textbox, status, usage_state, stage_state, reasoning_md],
    )
    stop_btn.click(
        None, None, None, cancels=[send_event, submit_event, continue_event]
    )
    clear_btn.click(
        clear_conversation,
        outputs=[chatbot, textbox, status, usage_state, stage_state, reasoning_md],
    )
    verify_btn.click(verify_last_answer, inputs=[chatbot], outputs=[status])
    refresh_btn.click(render_usage_panel, inputs=[], outputs=[usage_panel])
    mode_radio.change(
        toggle_answer_style,
        inputs=[mode_radio],
        outputs=[answer_style_radio],
    )


if __name__ == "__main__":
    print(f"数学学习 AI 助手 v{APP_VERSION} 启动中…")
    auto_open = os.getenv("AUTO_OPEN_BROWSER", "true").strip().lower() != "false"
    server_name = os.getenv("GRADIO_SERVER_NAME", "0.0.0.0").strip()
    print(
        f"访问账号：用户名 {ACCESS_USERNAME}，"
        f"密码 {access_password[:2]}******（完整密码见 data/当前访问密码.txt）"
    )
    demo.queue(default_concurrency_limit=4).launch(
        server_name=server_name,
        server_port=7860,
        inbrowser=auto_open,
        auth=_check_login,
        auth_message="请输入访问用户名与密码（用户名 math）",
        prevent_thread_lock=True,
    )
    watcher = None
    try:
        cleanup_stale_tunnel()
        cloudflared_path = find_cloudflared()
        if cloudflared_path is None:
            print(
                "未找到 cloudflared.exe，公网隧道未启动"
                "（请把 cloudflared.exe 放到项目根目录）。仍可通过局域网访问。"
            )
        else:
            print("正在建立 Cloudflare 公网隧道…")

            def _on_url(url: str) -> None:
                save_access_info(url, ACCESS_USERNAME, access_password)
                print(f"公网链接已更新：{url}（已写入访问信息文件）")

            watcher = TunnelWatcher(cloudflared_path, on_url=_on_url)
            watcher.start()
            if watcher.url:
                print(f"公网链接：{watcher.url}")
            else:
                print("公网隧道建立失败（连接 Cloudflare 超时），仍可通过局域网访问。")
        access_file = save_access_info(
            watcher.url if watcher else None, ACCESS_USERNAME, access_password
        )
        print(f"局域网地址：http://{get_lan_ip()}:7860")
        print(f"访问信息已保存到：{access_file}")
        demo.block_thread()
    finally:
        if watcher is not None:
            watcher.stop()
