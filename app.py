"""数学学习 AI 助手：Gradio 界面 + DeepSeek 大模型 + 拍照识题 + 学习功能。"""

from __future__ import annotations

import datetime
import json
import os
import re
import threading
import time
from pathlib import Path

import gradio as gr

from core.access import (
    get_lan_ip,
    is_owner_host,
    log_access_event,
    save_access_info,
    trim_log_file,
)
from core.accounts import (
    check_login,
    ensure_accounts,
    get_account,
    load_accounts,
    reset_password,
)
from core.backup import daily_backup_loop, ensure_today_backup
from core.calculator import CALCULATOR_AVAILABLE, verify_math
from core.config import get_settings
from core.deepseek_client import DeepSeekClient, MaxWaitTimeout
from core.exercise_bank import (
    ERROR_TYPES,
    add_entry,
    delete_entry,
    entry_label,
    filter_entries,
    find_duplicate,
    get_entry,
    load_entries,
    update_entry,
)
from core import image_edit
from core.math_format import normalize_math_text
from core.prompts import (
    MODE_ANSWER,
    MODE_INTERACTIVE,
    VISION_COMPLETE_PROMPT,
    build_system_prompt,
)
from core.storage import load_history, save_history
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

APP_VERSION = "1.1.0"
# 多账号：首次启动按 ACCOUNT_COUNT（默认 5）生成账号，之后以 data/accounts.json 为准
ACCOUNT_COUNT = int(os.getenv("ACCOUNT_COUNT", "5") or "5")
accounts = ensure_accounts(ACCOUNT_COUNT)
# 与聊天框一致的 LaTeX 渲染定界符（习题库详情/状态复用）
LATEX_DELIMITERS = [
    {"left": "$$", "right": "$$", "display": True},
    {"left": "$", "right": "$", "display": False},
]

# 日志文件保留的最大行数（超过后自动裁剪）
LOG_MAX_LINES = 2000

# 移动端适配与整体排版（蓝白黑冷色调主题之外的少量微调）
PAGE_CSS = """
.gradio-container { max-width: 1280px !important; margin: 0 auto !important; }
@media (max-width: 768px) {
  .gradio-container { max-width: 100% !important; padding: 8px !important; }
  .chatbot { height: 55vh !important; }
  .katex-display, .katex { overflow-x: auto; overflow-y: hidden; }
}
"""

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
    ok = check_login(username, password)
    log_access_event("登录", f"账号={username}", f"结果={'成功' if ok else '失败'}")
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
        trim_log_file(DATA_DIR / "chat.log", max_lines=LOG_MAX_LINES)
    except Exception:
        pass


def _write_error_log(record: dict) -> None:
    """把一条诊断记录追加写入 data/error.log（JSON 一行一条）。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with _log_lock:
            with open(DATA_DIR / "error.log", "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        trim_log_file(DATA_DIR / "error.log", max_lines=LOG_MAX_LINES)
    except Exception:
        pass


def _base_error_record(account: str, turn_total: int | None) -> dict:
    """公共字段：当前配置与账号信息。"""
    return {
        "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "account": account,
        "tokens": turn_total,
        "model": settings.model if settings is not None else None,
        "thinking": settings.thinking if settings is not None else None,
        "reasoning_effort": (
            settings.reasoning_effort if settings is not None else None
        ),
    }


def _log_error(
    exc: Exception, *, account: str, turn_total: int | None, partial: bool
) -> None:
    """把一次 API 异常的完整信息写入 data/error.log，便于定位根因。"""
    record = _base_error_record(account, turn_total)
    record.update(
        {
            "type": exc.__class__.__name__,
            "message": str(exc)[:2000],
            "partial_answer": partial,
        }
    )
    _write_error_log(record)


def _log_empty_reply(
    *, account: str, turn_total: int | None, truncated: bool
) -> None:
    """记录"请求完成但没收到正式回答"的情况（常见于思考链耗尽输出额度）。"""
    record = _base_error_record(account, turn_total)
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
            "",
            usage_state,
            stage_state,
            "",
        )
        return

    username = request.username if request and request.username else "unknown"
    ip = (request.client.host if request and request.client else None) or "unknown"
    if len(message) > MAX_MESSAGE_LEN:
        log_access_event(
            "过长拒绝", f"账号={username}", f"IP={ip}", f"长度={len(message)}"
        )
        yield (
            history,
            "",
            f"⚠️ 消息过长（最多 {MAX_MESSAGE_LEN:,} 字符），请精简后重试。",
            "",
            usage_state,
            stage_state,
            "",
        )
        return

    today = datetime.date.today().isoformat()
    daily_used = get_daily_used(today, username)
    global_used = get_daily_total(today)
    quota_msg = check_daily_quota(
        daily_used, global_used, DAILY_LIMIT, GLOBAL_DAILY_LIMIT
    )
    if quota_msg is not None:
        log_access_event(
            "超额拒绝",
            f"账号={username}",
            f"本访客={daily_used}",
            f"全局={global_used}",
        )
        yield history, "", quota_msg, "", usage_state, stage_state, ""
        return

    # 根据用户新消息推进互动模式阶段（解答模式无阶段）
    new_stage = advance_stage(mode, message, stage_state, has_history=bool(history))

    user_content = normalize_math_text(message, auto_close=True)
    _log_event("发送", user_content)
    log_access_event(
        "提问", f"账号={username}", f"IP={ip}", f"长度={len(message)}"
    )
    add_daily_question(today)
    if _recognition_incomplete(message):
        _log_event("发送-原始疑似不完整", message)
    history.append({"role": "user", "content": user_content})
    if client is None:
        yield (
            history + [{"role": "assistant", "content": f"⚠️ {startup_hint}"}],
            "",
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
                    "",
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
        _log_error(
            exc, account=username, turn_total=turn_total, partial=bool(answer)
        )
        usage_state = record_usage(usage_state, turn_total)
        if turn_total is not None:
            daily_used = add_daily_usage(today, username, turn_total)
            log_access_event(
                "回复失败",
                f"账号={username}",
                f"错误={exc.__class__.__name__}",
                f"估算tokens={turn_total}",
                f"本访客={daily_used}",
            )
        else:
            log_access_event(
                "回复失败", f"账号={username}", f"错误={exc.__class__.__name__}"
            )
        if answer:
            display = normalize_math_text(answer, auto_close=True)
            partial_history = history + [{"role": "assistant", "content": display}]
            save_history(partial_history, username=username)
            status_text = _friendly_error(exc) + "\n\n（已保留生成的部分内容）"
        else:
            partial_history = history
            status_text = _friendly_error(exc)
        yield (
            partial_history,
            "",
            status_text,
            format_cumulative(usage_state),
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
        _log_empty_reply(
            account=username, turn_total=turn_total, truncated=truncated
        )
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
            format_cumulative(usage_state),
            usage_state,
            new_stage,
            normalize_math_text(reasoning_text, auto_close=True),
        )
        return

    # 最终展示启用自动闭合：模型偶发漏写尾部 $$ 时补上，保证公式能渲染
    display = normalize_math_text(answer, auto_close=True)
    final_history = history + [{"role": "assistant", "content": display}]
    save_history(final_history, username=username)
    turn_total = (
        int(getattr(usage, "total_tokens", 0) or 0) if usage is not None else None
    )
    usage_state = record_usage(usage_state, turn_total)
    if turn_total is not None:
        daily_used = add_daily_usage(today, username, turn_total)
        global_used = get_daily_total(today)
        log_access_event(
            "回复",
            f"账号={username}",
            f"tokens={turn_total}",
            f"本访客={daily_used}",
            f"全局={global_used}",
        )
    result_text = format_usage(usage)
    if truncated:
        result_text += "\n\n⚠️ 回答因长度限制被截断，可以回复「继续」让我接着写。"
    usage_text = format_cumulative(usage_state)
    usage_text += (
        f"\n\n🛡️ 今日已用 {daily_used:,} / {DAILY_LIMIT:,}（本访客），"
        f"全局 {global_used:,} / {GLOBAL_DAILY_LIMIT:,}"
    )
    reasoning_display = normalize_math_text(reasoning_text, auto_close=True)
    # 第一段：先立即输出答案，验算随后刷新，避免界面等待
    yield (
        final_history,
        "",
        result_text,
        usage_text,
        usage_state,
        new_stage,
        reasoning_display,
    )
    # 第二段：本地验算（发现错误时模型复核），结果追加到「本次结果」
    verify_note = _auto_verify_loop(answer, final_history, username)
    if verify_note:
        result_text += "\n\n" + verify_note
    yield (
        final_history,
        "",
        result_text,
        usage_text,
        usage_state,
        new_stage,
        reasoning_display,
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
            "",
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


def clear_conversation(request: gr.Request):
    """开启新对话：清空当前账号的聊天记录、用量与阶段状态并持久化。"""
    username = request.username if request and request.username else "unknown"
    save_history([], username=username)
    return (
        gr.update(value=[]),
        "",
        "🗑️ 已开启新对话。",
        "",
        new_usage_state(),
        _new_stage_state(),
        "（本轮无思考过程）",
    )


def render_usage_panel(request: gr.Request) -> str:
    """主人用量面板：仅本机（localhost）可见，展示今日全局/每账号用量与次数。"""
    ip = (request.client.host if request and request.client else None) or "unknown"
    if not is_owner_host(ip):
        return "🔒 （仅主人可见）"
    today = datetime.date.today().isoformat()
    global_used = get_daily_total(today)
    per_account = get_daily_ip_usage(today)
    logins = get_daily_logins(today)
    questions = get_daily_questions(today)
    lines = [
        f"**主人用量面板（{today}）**",
        f"- 全局已用：{global_used:,} / {GLOBAL_DAILY_LIMIT:,} tokens",
        f"- 登录次数：{logins}，提问次数：{questions}",
        f"- 账号数量：{len(accounts)}（清单见 data/accounts.json，可自行增删）",
        "**各账号用量：**",
    ]
    for account in accounts:
        name = account["username"]
        display = account.get("display_name") or name
        enabled = "✅" if account.get("enabled", True) else "⛔"
        used = per_account.get(name, 0)
        lines.append(
            f"- {name}（{display}）{enabled}：{used:,} tokens，"
            f"密码 {account['password']}"
        )
    return "\n".join(lines)


def reset_account_password(
    request: gr.Request, account_name: str, new_password: str
) -> str:
    """主人重置指定账号的密码（仅本机可见）。"""
    ip = (request.client.host if request and request.client else None) or "unknown"
    if not is_owner_host(ip):
        return "🔒 （仅主人可见）"
    if not account_name:
        return "ℹ️ 请选择要重置的账号。"
    if not (new_password or "").strip():
        return "⚠️ 新密码不能为空。"
    global accounts
    if reset_password(account_name, new_password):
        accounts = load_accounts()
        log_access_event("重置密码", f"账号={account_name}")
        return f"✅ 已重置账号 {account_name} 的密码。"
    return "⚠️ 账号不存在，请刷新页面后重试。"


def restore_history(request: gr.Request) -> list[dict]:
    """登录后加载当前账号上次的聊天记录（页面加载时触发）。"""
    username = request.username if request and request.username else "unknown"
    return load_history(username)


def restore_level(request: gr.Request) -> str:
    """登录后把学段默认值设为账号配置的学段（页面加载时触发）。"""
    username = request.username if request and request.username else "unknown"
    account = get_account(username)
    level = account["level"] if account else "高中"
    if level not in ("小学", "初中", "高中", "大学"):
        level = "高中"
    return level


def _recheck_wrong_answer(answer: str) -> str:
    """验算发现不成立时，请模型快速复核并给出修正；失败返回空串。"""
    complete_fn = getattr(client, "complete", None)
    if complete_fn is None:
        return ""
    prompt = (
        "下面是 AI 对一道数学题的解答，本地验算发现其中有公式不成立。"
        "请重新检查，找出错误并给出修正后的关键步骤与最终答案。\n\n"
        f"解答：\n{answer[:2000]}"
    )
    try:
        text = complete_fn(
            [{"role": "user", "content": prompt}],
            thinking=False,
            max_tokens=800,
        )
    except Exception:
        return ""
    return (text or "").strip()


def _auto_verify_loop(answer: str, history: list[dict], username: str) -> str:
    """自动验算回答中的公式；发现不成立时请模型复核，返回状态摘要。"""
    if not CALCULATOR_AVAILABLE or not answer:
        return ""
    results = verify_math(answer)
    if not results:
        return ""
    ok = sum(1 for r in results if r["status"] == "成立")
    bad = sum(1 for r in results if r["status"] == "不成立")
    other = len(results) - ok - bad
    parts = [f"🧮 自动验算 {len(results)} 条公式"]
    if bad:
        parts.append(f"发现 {bad} 条不成立")
    if ok:
        parts.append(f"{ok} 条成立")
    if other:
        parts.append(f"{other} 条无法自动判定")
    note = "，".join(parts) + "。"
    if bad:
        correction = _recheck_wrong_answer(answer)
        if correction:
            history.append(
                {"role": "assistant", "content": f"（🔍 自动复核）{correction}"}
            )
            save_history(history, username=username)
            note += "\n\n🔍 已自动复核并给出修正，见上方补充消息。"
        else:
            note += "\n\n⚠️ 验算发现疑点，复核未成功，可回复「重新检查」。"
    return note


def toggle_answer_style(mode: str):
    """解答风格选项只在解答模式下显示。"""
    return gr.update(visible=(mode == MODE_ANSWER))


# ===================== 习题库 =====================


def _username_of(request: gr.Request) -> str:
    return request.username if request and request.username else "unknown"


def _now_text() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _render_detail(entry: dict, show_solution: bool = False) -> str:
    """把一条习题渲染成详情 Markdown；正确解默认隐藏。"""
    lines: list[str] = []
    title = (entry.get("title") or "").strip()
    if title:
        lines.append(f"**{title}**")
    lines.append(f"**题目**\n\n{normalize_math_text(entry['question'], auto_close=True)}")
    if entry.get("mistake"):
        lines.append(f"\n**我的卡点 / 错误做法**\n\n{entry['mistake']}")
    if entry.get("error_type"):
        lines.append(f"\n**错误类型**：{entry['error_type']}")
    tags = entry.get("tags") or []
    if tags:
        lines.append(f"\n**标签**：{'、'.join(tags)}")
    review = (
        f"，上次复习：{entry['last_reviewed']}" if entry.get("last_reviewed") else ""
    )
    lines.append(f"\n**收录时间**：{entry.get('created_at') or '-'}{review}")
    if show_solution:
        solution = entry.get("solution") or "（还没有收录正确解）"
        lines.append(
            f"\n**正确解**\n\n{normalize_math_text(solution, auto_close=True)}"
        )
    else:
        lines.append("\n**正确解**：勾选「显示正确解」查看（建议先自己重做一遍）")
    return "\n".join(lines)


def _all_tag_choices(entries: list[dict]) -> list[str]:
    return sorted({tag for e in entries for tag in (e.get("tags") or [])})


def _bank_view(entries: list[dict], selected_id: str | None = None):
    """习题库统一视图：统计、标签筛选项、题目下拉与详情。"""
    stats = f"📚 当前共 **{len(entries)}** 道题"
    tag_update = gr.update(choices=_all_tag_choices(entries))
    choices = [(entry_label(e), e["id"]) for e in reversed(entries)]
    selector_update = gr.update(choices=choices, value=None)
    detail = "（先在上方选择一道题）"
    if selected_id:
        entry = next((e for e in entries if e["id"] == selected_id), None)
        if entry is not None:
            selector_update = gr.update(choices=choices, value=selected_id)
            detail = _render_detail(entry, show_solution=False)
    return stats, tag_update, selector_update, detail


def _suggest_meta(question: str, answer: str) -> dict:
    """让模型生成建议标签与错误类型；未配置或失败时返回空。"""
    if client is None or not question:
        return {"tags": [], "error_type": ""}
    prompt = (
        "你是数学学习助手。请为下面这道题给出：\n"
        "1. 建议标签：1~3 个中文知识点标签，用顿号分隔；\n"
        "2. 建议错误类型：从「概念不清 / 计算失误 / 方法不会 / 审题不清」中选一个；\n"
        "只输出两行：\n标签：...\n错误类型：...\n\n"
        f"题目：{question[:800]}\n\n参考解答：{answer[:800]}"
    )
    try:
        text = client.complete(
            [{"role": "user", "content": prompt}],
            thinking=False,
            max_tokens=200,
        )
    except Exception:
        return {"tags": [], "error_type": ""}
    tags: list[str] = []
    error_type = ""
    for line in (text or "").splitlines():
        if line.startswith("标签"):
            tags = [
                tag.strip()
                for tag in line.split("：", 1)[-1].replace("，", "、").split("、")
                if tag.strip()
            ]
        elif line.startswith("错误类型"):
            error_type = line.split("：", 1)[-1].strip()
    if error_type not in ERROR_TYPES[1:]:
        error_type = ""
    return {"tags": tags[:3], "error_type": error_type}


def _open_form_outputs(
    title: str,
    question: str,
    mistake: str,
    solution: str,
    error_type: str,
    tags_text: str,
    edit_id: str,
    bank_msg: str,
    form_msg: str,
):
    """习题库表单与页面统一输出元组（收藏 / 手动新建共用）。"""
    return (
        gr.update(selected="习题库"),
        gr.update(visible=True, open=True),
        title,
        question,
        mistake,
        solution,
        gr.update(choices=ERROR_TYPES, value=error_type),
        tags_text,
        edit_id,
        bank_msg,
        form_msg,
    )


def collect_current_problem(history: list[dict], request: gr.Request):
    """收藏当前题目：把最近一问一带入表单并切换到习题库 Tab。"""
    history = list(history or [])
    question = ""
    answer = ""
    for item in reversed(history):
        content = item.get("content")
        if (
            not question
            and item.get("role") == "user"
            and isinstance(content, str)
            and content.strip()
        ):
            question = content
        elif (
            not answer
            and item.get("role") == "assistant"
            and isinstance(content, str)
            and content.strip()
        ):
            answer = content
        if question and answer:
            break
    if not question:
        yield _open_form_outputs(
            "", "", "", "", "", "", "", "💡 聊天里还没有题目，请先提问，或使用「手动新建」。", ""
        )
        return
    # 先立即打开表单给反馈，标签建议稍后生成，避免点击后长时间无反应
    yield _open_form_outputs(
        "",
        question,
        "",
        "",
        "",
        "",
        "",
        "✅ 已带入题目，正在生成标签建议…",
        "⏳ 正在生成建议标签…",
    )
    meta = _suggest_meta(question, answer)
    tag_text = "、".join(meta["tags"])
    hint = (
        "（模型生成建议，可增删改）"
        if meta["tags"]
        else "（模型未能生成，可手动填写）"
    )
    yield _open_form_outputs(
        "",
        question,
        "",
        "",
        meta["error_type"],
        tag_text,
        "",
        "✅ 已带入题目，请在表单确认后保存。",
        f"ℹ️ 建议标签：{tag_text or '无'}{hint}",
    )


def new_blank_entry(request: gr.Request):
    """手动新建：打开空白表单并切换到习题库 Tab。"""
    yield _open_form_outputs(
        "", "", "", "", "", "", "", "📝 手动新建：填写索引描述与题目内容后保存。", ""
    )


def refresh_bank(request: gr.Request, tag: str | None, keyword: str | None):
    """刷新习题库列表（应用标签筛选与关键词搜索）。"""
    username = _username_of(request)
    entries = filter_entries(load_entries(username), tag=tag or None, keyword=keyword)
    stats = f"📚 当前共 **{len(entries)}** 道题" + (
        "（筛选后）" if (tag or keyword) else ""
    )
    tag_update = gr.update(choices=_all_tag_choices(entries))
    choices = [(entry_label(e), e["id"]) for e in reversed(entries)]
    return (
        stats,
        tag_update,
        gr.update(choices=choices, value=None),
        "（先在上方选择一道题）",
        "",
        gr.update(value=False),
        "🔄 已刷新。",
    )


def select_entry(entry_id: str, show_solution: bool, request: gr.Request):
    """选择题目后展示详情（正确解默认隐藏）。"""
    username = _username_of(request)
    if not entry_id:
        return "（先在上方选择一道题）", "", gr.update(value=False), ""
    entry = get_entry(entry_id, username)
    if entry is None:
        return "（未找到该题，请刷新）", "", gr.update(value=False), ""
    return _render_detail(entry, show_solution=False), entry_id, gr.update(value=False), ""


def toggle_solution(
    show_solution: bool, selected_id: str, request: gr.Request
):
    """切换「显示正确解」；展开时顺带记录本次复习时间。"""
    username = _username_of(request)
    if not selected_id:
        return "（请先选择一道题）", ""
    entry = get_entry(selected_id, username)
    if entry is None:
        return "（未找到该题，请刷新）", ""
    if show_solution:
        entry = (
            update_entry(selected_id, {"last_reviewed": _now_text()}, username)
            or entry
        )
        return _render_detail(entry, show_solution=True), "📅 已记录本次复习时间。"
    return _render_detail(entry, show_solution=False), ""


def edit_entry(selected_id: str, request: gr.Request):
    """把选中题目载入表单进入编辑模式。"""
    username = _username_of(request)
    entry = get_entry(selected_id, username) if selected_id else None
    if entry is None:
        return (
            gr.update(),
            "⚠️ 请先选择一道题。",
            "",
            "",
            "",
            "",
            gr.update(choices=ERROR_TYPES, value=""),
            "",
            "",
            "",
        )
    return (
        gr.update(visible=True, open=True),
        "✏️ 正在编辑，改完后点保存。",
        entry["title"],
        entry["question"],
        entry["mistake"],
        entry["solution"],
        gr.update(choices=ERROR_TYPES, value=entry.get("error_type") or ""),
        "、".join(entry.get("tags") or []),
        entry["id"],
        "",
    )


def delete_selected_entry(selected_id: str, request: gr.Request):
    """删除选中的题目并刷新列表。"""
    username = _username_of(request)
    if not selected_id:
        return (
            "⚠️ 请先选择一道题。",
            gr.update(),
            gr.update(),
            gr.update(),
            "（先在上方选择一道题）",
            "",
            gr.update(value=False),
        )
    ok = delete_entry(selected_id, username)
    entries = load_entries(username)
    stats, tag_update, selector_update, _ = _bank_view(entries)
    return (
        "🗑️ 已删除。" if ok else "⚠️ 未找到该题。",
        stats,
        tag_update,
        selector_update,
        "（先在上方选择一道题）",
        "",
        gr.update(value=False),
    )


def save_entry(
    request: gr.Request,
    edit_id: str,
    title: str,
    question: str,
    mistake: str,
    solution: str,
    error_type: str,
    tags_text: str,
    force_state: str,
):
    """保存/更新习题；命中查重时需再次点击确认。"""
    username = _username_of(request)
    title = (title or "").strip()
    question = (question or "").strip()
    if not title:
        yield (
            "⚠️ 索引描述不能为空，请填写用于查找的简短中文。",
            "",
            gr.update(),
            gr.update(),
            gr.update(),
            "（先在上方选择一道题）",
            "",
            gr.update(value=False),
        )
        return
    if not question:
        yield (
            "⚠️ 题目不能为空，请填写后再保存。",
            "",
            gr.update(),
            gr.update(),
            gr.update(),
            "（先在上方选择一道题）",
            "",
            gr.update(value=False),
        )
        return
    tags = [
        tag.strip()
        for tag in re.split(r"[、,，\s]+", tags_text or "")
        if tag.strip()
    ]
    fields = {
        "title": title,
        "question": question,
        "mistake": (mistake or "").strip(),
        "solution": (solution or "").strip(),
        "error_type": error_type if error_type in ERROR_TYPES else "",
        "tags": tags,
    }
    entries = load_entries(username)
    duplicate = find_duplicate(question, entries, exclude_id=edit_id or None)
    force_id = force_state or ""
    if duplicate is not None and force_id != duplicate["id"]:
        yield (
            f"⚠️ 这题已在库中（{entry_label(duplicate)}）。再次点击保存将仍然入库。",
            duplicate["id"],
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
        )
        return
    if edit_id:
        saved = update_entry(edit_id, fields, username) or add_entry(fields, username)
        message = "✅ 已更新。"
    else:
        saved = add_entry(fields, username)
        message = "✅ 已保存到习题库。"
    stats, tag_update, selector_update, detail = _bank_view(
        load_entries(username), selected_id=saved["id"]
    )
    yield (
        message,
        "",
        stats,
        tag_update,
        selector_update,
        detail,
        saved["id"],
        gr.update(value=False),
    )


def generate_similar(
    entry_id: str,
    difficulty: str,
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
    """选中题目生成同类型变式，发进聊天区走正常问答流程。"""
    username = _username_of(request)
    entry = get_entry(entry_id, username) if entry_id else None
    if difficulty not in ("基础", "进阶", "挑战"):
        difficulty = "进阶"
    if entry is None:
        yield (
            gr.update(),
            "",
            "⚠️ 请先在习题库选择一道题。",
            "",
            usage_state,
            stage_state,
            gr.update(),
        )
        return
    message = (
        f"【习题库变式】请根据下面这道题，出一道同知识点的「{difficulty}」难度变式题"
        "（改数字或条件，题型方法保持一致），只出题、不要解答：\n\n"
        f"{entry['question']}"
    )
    last = None
    for item in respond(
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
    ):
        last = item
        yield item
    if last is not None:
        h, t, s, usage_text, u, st, r = last
        yield (
            h,
            t,
            s + "\n\n📌 如需收藏这道变式，点击「收藏当前题目」。",
            usage_text,
            u,
            st,
            r,
        )


def confirm_clear(
    request: gr.Request,
    history: list[dict],
    confirm_state: str,
):
    """新对话二次确认：第一次点击进入待确认，第二次才真正清空。"""
    if not confirm_state:
        return (
            gr.update(),
            gr.update(),
            "⚠️ 再点一次确认清空当前账号的对话（5 秒后自动取消）。",
            "",
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(value="⚠️ 再点一次确认清空"),
            "armed",
        )
    (
        new_history,
        _text,
        result_msg,
        usage_msg,
        new_usage,
        new_stage,
        _reasoning,
    ) = clear_conversation(request)
    return (
        new_history,
        _text,
        result_msg,
        usage_msg,
        new_usage,
        new_stage,
        _reasoning,
        gr.update(value="🗑️ 新对话"),
        "",
    )


def confirm_delete(
    request: gr.Request,
    selected_id: str,
    confirm_state: str,
):
    """删除习题二次确认：第一次点击进入待确认，第二次才真正删除。"""
    if not selected_id:
        return (
            "",
            gr.update(value="🗑️ 删除"),
            "⚠️ 请先选择一道题。",
            gr.update(),
            gr.update(),
            gr.update(),
            "（先在上方选择一道题）",
            "",
            gr.update(value=False),
        )
    if not confirm_state:
        return (
            "armed",
            gr.update(value="⚠️ 再点一次确认删除"),
            "⚠️ 再点一次确认删除这道题（5 秒后自动取消）。",
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
        )
    (
        bank_msg,
        stats,
        tag_update,
        selector_update,
        detail,
        selected,
        solution_update,
    ) = delete_selected_entry(selected_id, request)
    return (
        "",
        gr.update(value="🗑️ 删除"),
        bank_msg,
        stats,
        tag_update,
        selector_update,
        detail,
        selected,
        solution_update,
    )


def reset_confirm_buttons(clear_state: str, delete_state: str):
    """Timer 到点后复位二次确认状态与按钮文案。"""
    return (
        gr.update(value="") if clear_state else gr.update(),
        gr.update(value="") if delete_state else gr.update(),
        gr.update(value="🗑️ 新对话") if clear_state else gr.update(),
        gr.update(value="🗑️ 删除") if delete_state else gr.update(),
    )


def _edit_current_image(
    image: str | None,
    original_state: str,
    operation,
    success_text: str,
):
    """通用图片编辑：加载当前图 → 处理 → 更新图片组件与状态提示。"""
    if not image:
        return gr.update(), "⚠️ 请先上传图片。"
    try:
        edited = operation(image_edit.load(image))
        return (
            gr.update(value=edited),
            f"{success_text}，可继续编辑或点击「识别题目」。",
        )
    except Exception as exc:
        return gr.update(), f"❌ 图片处理失败：{exc}"


def on_image_upload(image: str | None, original_state: str):
    """上传/拍照后保存原图，不再自动识别，由用户编辑后手动识别。"""
    if not image:
        return gr.update(), "💡 请先上传或拍摄题目图片。", ""
    return (
        gr.update(),
        "📷 已上传图片：可编辑（旋转/裁剪/缩放）后点击「识别题目」。",
        image,
    )


def edit_rotate(image: str | None, original_state: str):
    return _edit_current_image(image, original_state, image_edit.rotate90, "↻ 已旋转 90°")


def edit_crop_preset(image: str | None, original_state: str, percent: float):
    return _edit_current_image(
        image,
        original_state,
        lambda img: image_edit.crop_preset(img, percent),
        f"✂️ 已裁剪 {percent:.0f}%",
    )


def edit_zoom(image: str | None, original_state: str, percent: float):
    label = "＋ 已放大 10%" if percent > 0 else "－ 已缩小 10%"
    return _edit_current_image(
        image, original_state, lambda img: image_edit.zoom(img, percent), label
    )


def edit_crop_custom(image: str | None, crop_text: str, original_state: str):
    """按输入百分比（左,上,右,下）裁剪。"""
    if not image:
        return gr.update(), "⚠️ 请先上传图片。"
    try:
        parts = [float(x) for x in (crop_text or "").replace("，", ",").split(",")]
    except Exception:
        return gr.update(), "⚠️ 裁剪格式应为：左,上,右,下（百分比，如 10,5,90,95）。"
    if len(parts) != 4:
        return gr.update(), "⚠️ 裁剪需要 4 个数字：左,上,右,下（百分比）。"
    try:
        edited = image_edit.crop_percent(image_edit.load(image), *parts)
        return gr.update(value=edited), "✅ 已按自定义比例裁剪，可点击「识别题目」。"
    except Exception as exc:
        return gr.update(), f"❌ 图片处理失败：{exc}"


def edit_reset(original_state: str):
    if not original_state:
        return gr.update(), "⚠️ 还没有可恢复的原图。"
    return gr.update(value=original_state), "🔄 已恢复原图。"


with gr.Blocks(
    title="数学学习 AI 助手",
    theme=gr.themes.Base(
        primary_hue="blue", secondary_hue="blue", neutral_hue="slate"
    ),
    css=PAGE_CSS,
) as demo:
    gr.Markdown(
        "# 🧮 数学学习 AI 助手\n\n"
        "基于 DeepSeek 大模型，采用苏格拉底式引导，一步步带你学会数学。"
        f"支持 LaTeX 公式、拍照识题、习题库与本地验算。版本 v{APP_VERSION}"
    )
    with gr.Tabs() as tabs:
        with gr.Tab("学习"):
            with gr.Row():
                mode_radio = gr.Radio(
                    [MODE_INTERACTIVE, MODE_ANSWER],
                    value=MODE_INTERACTIVE,
                    label="模式",
                )
                thinking_checkbox = gr.Checkbox(
                    value=settings.thinking if settings else False,
                    label="深度思考模式（更严谨，稍慢）",
                )
            with gr.Accordion("⚙️ 高级设置", open=False):
                level_radio = gr.Radio(
                    ["小学", "初中", "高中", "大学"], value="高中", label="学段"
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
                answer_style_radio = gr.Radio(
                    ["完整步骤+答案", "简略过程+答案", "只给答案和结论"],
                    value="完整步骤+答案",
                    label="解答风格（仅解答模式）",
                    visible=False,
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
                textbox = gr.Textbox(
                    placeholder="输入你的数学问题，例如：怎么解一元二次方程？",
                    show_label=False,
                    container=False,
                    scale=6,
                )
                send_btn = gr.Button("发送", variant="primary", scale=1)
            result_status = gr.Markdown("")
            usage_status = gr.Markdown("")
            usage_state = gr.State(value=new_usage_state())
            stage_state = gr.State(value=_new_stage_state())
            original_image_state = gr.State(value="")
            with gr.Accordion("📷 题目图片（拍照 / 上传 / 编辑）", open=True):
                image_input = gr.Image(
                    label="题目图片（拍照/上传）",
                    sources=["upload", "clipboard"],
                    type="filepath",
                )
                with gr.Row():
                    rotate_btn = gr.Button("↻ 旋转90°")
                    crop10_btn = gr.Button("✂️ 裁剪10%")
                    crop25_btn = gr.Button("✂️ 裁剪25%")
                with gr.Row():
                    zoom_in_btn = gr.Button("＋ 放大")
                    zoom_out_btn = gr.Button("－ 缩小")
                    reset_img_btn = gr.Button("🔄 重置")
                with gr.Row():
                    crop_textbox = gr.Textbox(
                        label="自定义裁剪（左,上,右,下 百分比）",
                        placeholder="如：10,5,90,95",
                        scale=2,
                    )
                    crop_btn = gr.Button("✂️ 应用裁剪")
                    recognize_btn = gr.Button("🔍 识别题目", variant="primary")
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
                collect_btn = gr.Button("📌 收藏当前题目")
            with gr.Accordion("🔐 主人设置（仅本机可见）", open=False):
                with gr.Row():
                    refresh_btn = gr.Button("🔄 刷新用量")
                    usage_panel = gr.Markdown("（点击“刷新用量”查看今日用量）")
                with gr.Row():
                    reset_account_dropdown = gr.Dropdown(
                        choices=[a["username"] for a in accounts],
                        label="重置密码账号",
                    )
                    reset_pwd_textbox = gr.Textbox(
                        label="新密码",
                        placeholder="输入新密码后点击重置",
                    )
                    reset_pwd_btn = gr.Button("重置密码")
                    reset_pwd_status = gr.Markdown("")
            gr.Examples(
                examples=[
                    "解方程 x² - 5x + 6 = 0，请引导我自己算出来",
                    "什么是导数？用生活中的例子讲讲",
                    "我不会做几何证明题，怎么入手？",
                ],
                inputs=textbox,
            )

        with gr.Tab("习题库"):
            with gr.Row():
                with gr.Column(scale=1):
                    bank_stats = gr.Markdown("（习题库为空）")
                    with gr.Row():
                        bank_tag_filter = gr.Dropdown(
                            choices=[], label="按标签筛选", value=None
                        )
                        bank_search = gr.Textbox(
                            label="关键词搜索",
                            placeholder="搜索题目或标签",
                            scale=2,
                        )
                    with gr.Row():
                        bank_refresh_btn = gr.Button("🔄 刷新")
                        new_btn = gr.Button("📝 手动新建")
                    entry_selector = gr.Dropdown(
                        choices=[], label="选择题目", value=None
                    )
                with gr.Column(scale=2):
                    with gr.Row():
                        solution_checkbox = gr.Checkbox(
                            label="显示正确解", value=False
                        )
                        edit_btn = gr.Button("✏️ 编辑")
                        delete_btn = gr.Button("🗑️ 删除")
                        similar_btn = gr.Button("🎯 生成同类型题")
                    difficulty_radio = gr.Radio(
                        ["基础", "进阶", "挑战"],
                        value="进阶",
                        label="变式难度（生成同类型题时使用）",
                    )
                    detail_md = gr.Markdown(
                        "（先在上方选择一道题）",
                        latex_delimiters=LATEX_DELIMITERS,
                    )
                    bank_status = gr.Markdown(
                        "", latex_delimiters=LATEX_DELIMITERS
                    )
                    with gr.Accordion(
                        "题目表单（收藏 / 手动新建 / 编辑）", open=False
                    ) as entry_form:
                        edit_id_state = gr.State(value="")
                        form_title = gr.Textbox(
                            label="索引描述（必填，纯中文，用于查找）",
                            lines=1,
                            placeholder="如：讨论反常积分的绝对收敛条件",
                        )
                        form_question = gr.Textbox(
                            label="题目内容",
                            lines=4,
                            placeholder="输入完整题目，用中文描述即可；公式建议用 $...$ 包裹以便排版",
                        )
                        form_mistake = gr.Textbox(
                            label="我的卡点 / 错误做法（可选）", lines=2
                        )
                        form_solution = gr.Textbox(
                            label="正确解（可选，建议自己写一遍）", lines=4
                        )
                        form_error_type = gr.Dropdown(
                            choices=ERROR_TYPES, label="错误类型（可选）"
                        )
                        form_tags = gr.Textbox(
                            label="标签（多个用顿号或逗号分隔）", lines=1
                        )
                        with gr.Row():
                            form_save_btn = gr.Button(
                                "💾 保存", variant="primary"
                            )
                            form_cancel_btn = gr.Button("取消")
                        form_status = gr.Markdown(
                            "", latex_delimiters=LATEX_DELIMITERS
                        )
                    force_save_state = gr.State(value="")
                    selected_id_state = gr.State(value="")

    clear_confirm_state = gr.State(value="")
    delete_confirm_state = gr.State(value="")
    confirm_timer = gr.Timer(value=5, active=True)

    # ===== 学习 Tab 事件 =====
    image_input.upload(
        on_image_upload,
        inputs=[image_input, original_image_state],
        outputs=[image_input, result_status, original_image_state],
    )
    rotate_btn.click(
        edit_rotate,
        inputs=[image_input, original_image_state],
        outputs=[image_input, result_status],
    )
    crop10_btn.click(
        lambda image, state: edit_crop_preset(image, state, 10),
        inputs=[image_input, original_image_state],
        outputs=[image_input, result_status],
    )
    crop25_btn.click(
        lambda image, state: edit_crop_preset(image, state, 25),
        inputs=[image_input, original_image_state],
        outputs=[image_input, result_status],
    )
    zoom_in_btn.click(
        lambda image, state: edit_zoom(image, state, 10),
        inputs=[image_input, original_image_state],
        outputs=[image_input, result_status],
    )
    zoom_out_btn.click(
        lambda image, state: edit_zoom(image, state, -10),
        inputs=[image_input, original_image_state],
        outputs=[image_input, result_status],
    )
    reset_img_btn.click(
        edit_reset,
        inputs=[original_image_state],
        outputs=[image_input, result_status],
    )
    crop_btn.click(
        edit_crop_custom,
        inputs=[image_input, crop_textbox, original_image_state],
        outputs=[image_input, result_status],
    )
    recognize_btn.click(
        recognize_image,
        inputs=[image_input],
        outputs=[textbox, result_status, image_input],
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
        outputs=[
            chatbot,
            textbox,
            result_status,
            usage_status,
            usage_state,
            stage_state,
            reasoning_md,
        ],
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
        outputs=[
            chatbot,
            textbox,
            result_status,
            usage_status,
            usage_state,
            stage_state,
            reasoning_md,
        ],
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
        outputs=[
            chatbot,
            textbox,
            result_status,
            usage_status,
            usage_state,
            stage_state,
            reasoning_md,
        ],
    )
    collect_event = collect_btn.click(
        collect_current_problem,
        inputs=[chatbot],
        outputs=[
            tabs,
            entry_form,
            form_title,
            form_question,
            form_mistake,
            form_solution,
            form_error_type,
            form_tags,
            edit_id_state,
            bank_status,
            form_status,
        ],
    )
    stop_btn.click(
        None, None, None, cancels=[send_event, submit_event, continue_event]
    )
    clear_btn.click(
        confirm_clear,
        inputs=[chatbot, clear_confirm_state],
        outputs=[
            chatbot,
            textbox,
            result_status,
            usage_status,
            usage_state,
            stage_state,
            reasoning_md,
            clear_btn,
            clear_confirm_state,
        ],
    )
    refresh_btn.click(render_usage_panel, inputs=[], outputs=[usage_panel])
    reset_pwd_btn.click(
        reset_account_password,
        inputs=[reset_account_dropdown, reset_pwd_textbox],
        outputs=[reset_pwd_status],
    )
    mode_radio.change(
        toggle_answer_style,
        inputs=[mode_radio],
        outputs=[answer_style_radio],
    )
    confirm_timer.tick(
        reset_confirm_buttons,
        inputs=[clear_confirm_state, delete_confirm_state],
        outputs=[clear_confirm_state, delete_confirm_state, clear_btn, delete_btn],
    )

    # ===== 习题库 Tab 事件 =====
    bank_refresh_btn.click(
        refresh_bank,
        inputs=[bank_tag_filter, bank_search],
        outputs=[
            bank_stats,
            bank_tag_filter,
            entry_selector,
            detail_md,
            selected_id_state,
            solution_checkbox,
            bank_status,
        ],
    )
    new_btn.click(
        new_blank_entry,
        outputs=[
            tabs,
            entry_form,
            form_title,
            form_question,
            form_mistake,
            form_solution,
            form_error_type,
            form_tags,
            edit_id_state,
            bank_status,
            form_status,
        ],
    )
    entry_selector.change(
        select_entry,
        inputs=[entry_selector, solution_checkbox],
        outputs=[detail_md, selected_id_state, solution_checkbox, bank_status],
    )
    solution_checkbox.change(
        toggle_solution,
        inputs=[solution_checkbox, selected_id_state],
        outputs=[detail_md, bank_status],
    )
    edit_btn.click(
        edit_entry,
        inputs=[selected_id_state],
        outputs=[
            entry_form,
            bank_status,
            form_title,
            form_question,
            form_mistake,
            form_solution,
            form_error_type,
            form_tags,
            edit_id_state,
            form_status,
        ],
    )
    delete_btn.click(
        confirm_delete,
        inputs=[selected_id_state, delete_confirm_state],
        outputs=[
            delete_confirm_state,
            delete_btn,
            bank_status,
            bank_stats,
            bank_tag_filter,
            entry_selector,
            detail_md,
            selected_id_state,
            solution_checkbox,
        ],
    )
    similar_btn.click(
        generate_similar,
        inputs=[
            selected_id_state,
            difficulty_radio,
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
        outputs=[
            chatbot,
            textbox,
            result_status,
            usage_status,
            usage_state,
            stage_state,
            reasoning_md,
        ],
    )
    form_save_btn.click(
        save_entry,
        inputs=[
            edit_id_state,
            form_title,
            form_question,
            form_mistake,
            form_solution,
            form_error_type,
            form_tags,
            force_save_state,
        ],
        outputs=[
            form_status,
            force_save_state,
            bank_stats,
            bank_tag_filter,
            entry_selector,
            detail_md,
            selected_id_state,
            solution_checkbox,
        ],
    )
    form_cancel_btn.click(
        lambda: (gr.update(visible=False), "", ""),
        outputs=[entry_form, form_status, bank_status],
    )
    demo.load(restore_history, outputs=[chatbot], show_progress="hidden")
    demo.load(restore_level, outputs=[level_radio], show_progress="hidden")


if __name__ == "__main__":
    print(f"数学学习 AI 助手 v{APP_VERSION} 启动中…")
    auto_open = os.getenv("AUTO_OPEN_BROWSER", "true").strip().lower() != "false"
    server_name = os.getenv("GRADIO_SERVER_NAME", "0.0.0.0").strip()
    print("访问账号（固定密码，完整清单见 data/accounts.json）：")
    for account in accounts:
        print(f"  {account['username']} / {account['password']}")
    backup_path = ensure_today_backup()
    if backup_path:
        print(f"数据备份：已生成 {backup_path}")
    threading.Thread(target=daily_backup_loop, daemon=True).start()
    demo.queue(default_concurrency_limit=4).launch(
        server_name=server_name,
        server_port=7860,
        inbrowser=auto_open,
        auth=_check_login,
        auth_message="请输入访问用户名与密码",
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
                save_access_info(url, accounts)
                print(f"公网链接已更新：{url}（已写入访问信息文件）")

            watcher = TunnelWatcher(cloudflared_path, on_url=_on_url)
            watcher.start()
            if watcher.url:
                print(f"公网链接：{watcher.url}")
            else:
                print("公网隧道建立失败（连接 Cloudflare 超时），仍可通过局域网访问。")
        access_file = save_access_info(watcher.url if watcher else None, accounts)
        print(f"局域网地址：http://{get_lan_ip()}:7860")
        print(f"访问信息已保存到：{access_file}")
        demo.block_thread()
    finally:
        if watcher is not None:
            watcher.stop()
