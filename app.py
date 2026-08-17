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
from core.compressor import HistoryCompressor
from core.config import get_base_settings, get_settings
from core.llm_client import LLMClient, MaxWaitTimeout
from core.providers import (
    get_available_providers,
    get_default_provider,
    get_provider,
    get_provider_models,
    get_provider_supports_thinking,
    load_providers,
    provider_has_key,
)
from core.exercise_bank import (
    DEFAULT_CATEGORIES,
    ERROR_TYPES,
    MASTERY_LEVELS,
    add_category,
    add_entry,
    delete_category,
    delete_entry,
    entry_label,
    filter_entries,
    find_duplicate,
    get_entry,
    load_categories,
    load_entries,
    record_practice,
    rename_category,
    update_entry,
)
from core.math_format import normalize_math_text
from core.prompts import (
    MODE_ANSWER,
    MODE_INTERACTIVE,
    VISION_COMPLETE_PROMPT,
    VISION_HANDWRITTEN_COMPLETE_PROMPT,
    VISION_HANDWRITTEN_PROMPT,
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

APP_VERSION = "1.3.0"
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
.bank-toolbar { flex-wrap: wrap !important; row-gap: 6px !important; }
.bank-toolbar .gr-box, .bank-toolbar .gr-button { min-width: 96px; }
.bank-actions { flex-wrap: wrap !important; row-gap: 6px !important; }
.mastery-row { justify-content: flex-end !important; gap: 6px !important; }
.mastery-row .gr-button { padding: 2px 12px !important; }
.bank-layout { display: grid !important; gap: 8px !important; align-items: start !important; }
.bank-filters { grid-area: filters; }
.bank-stats { grid-area: stats; }
.bank-selector { grid-area: selector; }
.bank-detail { grid-area: detail; }
.bank-status { grid-area: status; }
.bank-actions-area { grid-area: actions; }
.bank-mastery { grid-area: mastery; }
.bank-practice { grid-area: practice; }
.bank-manage { grid-area: manage; }
.bank-form { grid-area: form; }
@media (min-width: 769px) {
  .bank-layout { grid-template-columns: 1fr 2fr; grid-template-areas:
    "filters filters"
    "stats actions"
    "selector practice"
    "manage mastery"
    ". detail"
    ". status"
    ". form"; }
}
@media (max-width: 768px) {
  .gradio-container { max-width: 100% !important; padding: 8px !important; }
  .chatbot { height: 55vh !important; }
  .katex-display, .katex { overflow-x: auto; overflow-y: hidden; }
  .bank-toolbar > * { flex: 1 1 30% !important; }
  .bank-actions > * { flex: 1 1 30% !important; }
  .bank-actions .gr-checkbox { flex: 1 1 30% !important; }
  .mastery-row { justify-content: flex-start !important; }
  .mastery-row > * { flex: 0 1 auto !important; }
  .bank-layout { grid-template-columns: 1fr; grid-template-areas:
    "filters"
    "stats"
    "selector"
    "detail"
    "status"
    "actions"
    "mastery"
    "practice"
    "manage"
    "form"; }
}
"""

# app.py 位于项目根目录，data 目录就在同级
DATA_DIR = Path(__file__).resolve().parent / "data"

try:
    settings = get_base_settings()
    all_providers = load_providers()
    available_providers = get_available_providers(all_providers)
    clients = {p["id"]: LLMClient(p, settings) for p in available_providers}
    default_provider = get_default_provider(all_providers)
    client = clients.get(default_provider["id"]) if default_provider else None
    ui_providers = available_providers or all_providers
    startup_hint = ("未找到可用的 API Key（DeepSeek / OpenAI），请检查 .env 后重启。" if not client else "")
    # 客户端已在上方按服务商构建
except ValueError as exc:
    # 未配置 API Key 时应用仍可启动，界面给出提示，方便先看效果
    settings = None
    client = None
    ui_providers = []
    startup_hint = str(exc)
else:
    pass
# 上下文压缩器（按账号增量维护长会话摘要）
history_compressor = HistoryCompressor()
# 每日每账号 token 上限（成本护栏）
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
    summary: str | None = None,
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
    if summary:
        messages.append(
            {
                "role": "system",
                "content": f"【更早对话摘要】\n{summary}",
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


def recognize_image(image: str | None, handwritten: bool = False):
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
        recognized, truncated = vision_client.transcribe_image(
            image, prompt=VISION_HANDWRITTEN_PROMPT if handwritten else None
        )
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
                image,
                prompt=(
                    VISION_HANDWRITTEN_COMPLETE_PROMPT.format(partial=recognized)
                    if handwritten
                    else VISION_COMPLETE_PROMPT.format(partial=recognized)
                ),
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
    provider_id: str | None,
    request: gr.Request,
    verify: bool = True,
    force_continue: bool = False,
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
            f"本账号={daily_used}",
            f"全局={global_used}",
        )
        yield history, "", quota_msg, "", usage_state, stage_state, ""
        return

    # 根据用户新消息推进互动模式阶段（解答模式无阶段）
    if force_continue:
        # 继续生成时保留当前阶段，避免把“题目原文+继续”误判为新题或切到批改
        new_stage = dict(stage_state)
        new_stage["mode_changed"] = False
        if mode != MODE_INTERACTIVE:
            new_stage["stage"] = None
            new_stage["last_mode"] = mode
    else:
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
    # 根据界面选择的服务商使用对应客户端
    active_client = clients.get(provider_id) or client
    if active_client is None:
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
    # 长会话自动压缩：把掉出窗口的早期消息合并成摘要，作为系统消息注入
    summary = ""
    if settings is not None and len(history) > settings.max_history_messages:
        summary = history_compressor.get_summary(
            username, history, settings.max_history_messages, active_client
        )

    answer = ""
    reasoning_text = ""
    usage = None
    truncated = False
    thinking_started = time.monotonic()
    try:
        for piece, reasoning_piece, chunk_usage, finish_reason in (
            active_client.chat_stream_with_usage(
                to_api_messages(
                    history,
                    level,
                    mode,
                    answer_style,
                    stage=new_stage["stage"],
                    mode_changed=new_stage["mode_changed"],
                    summary=summary,
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
                f"本账号={daily_used}",
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
            f"本账号={daily_used}",
            f"全局={global_used}",
        )
    _log_event("回复", answer)
    result_text = format_usage(usage)
    if truncated:
        result_text += "\n\n⚠️ 回答因长度限制被截断，可以回复「继续」让我接着写。"
    usage_text = format_cumulative(usage_state)
    usage_text += (
        f"\n\n🛡️ 今日已用 {daily_used:,} / {DAILY_LIMIT:,}（本账号），"
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
    verify_note = ""
    if verify:
        verify_note = _auto_verify_loop(
            answer, final_history, username, active_client
        )
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
    provider_id: str | None,
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
        provider_id,
        request,
        force_continue=True,
    )


def clear_conversation(request: gr.Request):
    """开启新对话：清空当前账号的聊天记录、用量与阶段状态并持久化。"""
    username = request.username if request and request.username else "unknown"
    save_history([], username=username)
    history_compressor.reset(username)
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


def _recheck_wrong_answer(answer: str, active_client=None) -> str:
    """验算发现不成立时，请模型快速复核并给出修正；失败返回空串。"""
    complete_fn = getattr(active_client or client, "complete", None)
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


def _auto_verify_loop(
    answer: str, history: list[dict], username: str, active_client=None
) -> str:
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
    note = "，".join(parts) + "。（验算仅针对公式本身，不代表解题过程正确）"
    if bad:
        correction = _recheck_wrong_answer(answer, active_client)
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
    category = (entry.get("category") or "").strip()
    lines.append(f"\n**大类**：{category if category else '未分类'}")
    mastery = entry.get("mastery") or "未掌握"
    right = int(entry.get("practice_right") or 0)
    wrong = int(entry.get("practice_wrong") or 0)
    consecutive = int(entry.get("consecutive_right") or 0)
    practiced = entry.get("last_practiced") or ""
    mastery_part = f"**掌握度**：{mastery}"
    if right or wrong:
        mastery_part += f"（答对 {right} · 答错 {wrong}"
        if consecutive:
            mastery_part += f" · 连续答对 {consecutive}"
        if practiced:
            mastery_part += f" · 最近 {practiced}"
        mastery_part += "）"
    lines.append(f"\n{mastery_part}")
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


def _resolve_category_filter(category: str | None) -> str | None:
    """大类筛选值映射：全部/空不过滤，未分类映射为空串。"""
    category = (category or "").strip()
    if category in ("", "全部"):
        return None
    if category == "未分类":
        return ""
    return category


def _category_choices(categories: list[str]) -> list[str]:
    return ["全部", "未分类"] + list(categories)


def _bank_view(
    entries: list[dict], selected_id: str | None = None, username: str = "default"
):
    """习题库统一视图：统计（按大类计数）、标签与大类筛选项、题目下拉与详情。"""
    categories = load_categories(username)
    counts = {
        c: sum(1 for e in entries if (e.get("category") or "") == c)
        for c in categories
    }
    uncat = sum(1 for e in entries if not (e.get("category") or ""))
    parts = [f"{c} {n}" for c, n in counts.items() if n]
    if uncat:
        parts.append(f"未分类 {uncat}")
    stats = f"📚 当前共 **{len(entries)}** 道题"
    if parts:
        stats += "（" + " · ".join(parts) + "）"
    mastery_counts = {
        level: sum(
            1 for e in entries if (e.get("mastery") or "未掌握") == level
        )
        for level in MASTERY_LEVELS
    }
    if entries:
        stats += "\n\n掌握度：" + " · ".join(
            f"{level} {mastery_counts[level]}"
            for level in MASTERY_LEVELS
            if mastery_counts[level]
        )
    tag_update = gr.update(choices=_all_tag_choices(entries))
    category_update = gr.update(choices=_category_choices(categories))
    choices = [(entry_label(e), e["id"]) for e in reversed(entries)]
    selector_update = gr.update(choices=choices, value=None)
    detail = "（先在上方选择一道题）"
    if selected_id:
        entry = next((e for e in entries if e["id"] == selected_id), None)
        if entry is not None:
            selector_update = gr.update(choices=choices, value=selected_id)
            detail = _render_detail(entry, show_solution=False)
    return stats, tag_update, category_update, selector_update, detail


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
    category: str,
    mastery: str,
    tags_text: str,
    edit_id: str,
    bank_msg: str,
    form_msg: str,
    username: str = "default",
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
        gr.update(
            choices=["未分类"] + load_categories(username),
            value=category or "未分类",
        ),
        gr.update(
            choices=MASTERY_LEVELS,
            value=mastery if mastery in MASTERY_LEVELS else "未掌握",
        ),
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
            "", "", "", "", "", "", "", "", "", "💡 聊天里还没有题目，请先提问，或使用「手动新建」。", "",
            _username_of(request),
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
        "",
        "",
        "✅ 已带入题目，正在生成标签建议…",
        "⏳ 正在生成建议标签…",
        _username_of(request),
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
        "",
        "",
        tag_text,
        "",
        "✅ 已带入题目，请在表单确认后保存。",
        f"ℹ️ 建议标签：{tag_text or '无'}{hint}",
        _username_of(request),
    )


def new_blank_entry(request: gr.Request):
    """手动新建：打开空白表单并切换到习题库 Tab。"""
    yield _open_form_outputs(
        "", "", "", "", "", "", "", "", "", "📝 手动新建：填写索引描述与题目内容后保存。", "",
        _username_of(request),
    )


def _resolve_mastery_filter(mastery: str | None) -> str | None:
    """掌握度筛选值映射：全部/空不过滤，其余精确匹配。"""
    mastery = (mastery or "").strip()
    if mastery in ("", "全部"):
        return None
    return mastery if mastery in MASTERY_LEVELS else None


def refresh_bank(
    request: gr.Request,
    tag: str | None,
    keyword: str | None,
    category: str | None,
    mastery: str | None,
):
    """刷新习题库列表（应用大类、掌握度、标签筛选与关键词搜索）。"""
    username = _username_of(request)
    resolved_category = _resolve_category_filter(category)
    resolved_mastery = _resolve_mastery_filter(mastery)
    entries = filter_entries(
        load_entries(username),
        tag=tag or None,
        keyword=keyword,
        category=resolved_category,
        mastery=resolved_mastery,
    )
    stats = f"📚 当前共 **{len(entries)}** 道题" + (
        "（筛选后）"
        if (
            tag
            or keyword
            or resolved_category is not None
            or resolved_mastery is not None
        )
        else ""
    )
    tag_update = gr.update(choices=_all_tag_choices(entries))
    category_update = gr.update(choices=_category_choices(load_categories(username)))
    mastery_update = gr.update(choices=["全部"] + MASTERY_LEVELS)
    choices = [(entry_label(e), e["id"]) for e in reversed(entries)]
    return (
        stats,
        tag_update,
        category_update,
        mastery_update,
        gr.update(choices=choices, value=None),
        "（先在上方选择一道题）",
        "",
        gr.update(value=False),
        "🔄 已刷新。",
        gr.update(visible=False),
    )


def select_entry(entry_id: str, show_solution: bool, request: gr.Request):
    """选择题目后展示详情（正确解默认隐藏）。"""
    username = _username_of(request)
    if not entry_id:
        return (
            "（先在上方选择一道题）",
            "",
            gr.update(value=False),
            "",
            gr.update(visible=False),
        )
    entry = get_entry(entry_id, username)
    if entry is None:
        return (
            "（未找到该题，请刷新）",
            "",
            gr.update(value=False),
            "",
            gr.update(visible=False),
        )
    return (
        _render_detail(entry, show_solution=False),
        entry_id,
        gr.update(value=False),
        "",
        gr.update(visible=True),
    )


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
    category_choices = ["未分类"] + load_categories(username)
    if entry is None:
        return (
            gr.update(),
            "⚠️ 请先选择一道题。",
            "",
            "",
            "",
            "",
            gr.update(choices=ERROR_TYPES, value=""),
            gr.update(choices=category_choices, value="未分类"),
            gr.update(choices=MASTERY_LEVELS, value="未掌握"),
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
        gr.update(choices=category_choices, value=entry.get("category") or "未分类"),
        gr.update(
            choices=MASTERY_LEVELS,
            value=entry.get("mastery") or "未掌握",
        ),
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
            gr.update(),
            gr.update(),
            "（先在上方选择一道题）",
            "",
            gr.update(value=False),
            gr.update(visible=False),
        )
    ok = delete_entry(selected_id, username)
    entries = load_entries(username)
    stats, tag_update, category_update, selector_update, _ = _bank_view(
        entries, username=username
    )
    return (
        "🗑️ 已删除。" if ok else "⚠️ 未找到该题。",
        stats,
        tag_update,
        category_update,
        gr.update(choices=["全部"] + MASTERY_LEVELS),
        selector_update,
        "（先在上方选择一道题）",
        "",
        gr.update(value=False),
        gr.update(visible=False),
    )


def save_entry(
    request: gr.Request,
    edit_id: str,
    title: str,
    question: str,
    mistake: str,
    solution: str,
    error_type: str,
    category: str,
    mastery: str,
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
            gr.update(),
            gr.update(),
            "（先在上方选择一道题）",
            "",
            gr.update(value=False),
            gr.update(visible=False),
        )
        return
    if not question:
        yield (
            "⚠️ 题目不能为空，请填写后再保存。",
            "",
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
            "（先在上方选择一道题）",
            "",
            gr.update(value=False),
            gr.update(visible=False),
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
        "category": (
            ""
            if (category or "") in ("", "未分类")
            else (category or "").strip()
        ),
        "mastery": (
            mastery if mastery in MASTERY_LEVELS else "未掌握"
        ),
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
            gr.update(),
            gr.update(),
            gr.update(visible=False),
        )
        return
    if edit_id:
        saved = update_entry(edit_id, fields, username) or add_entry(fields, username)
        message = "✅ 已更新。"
    else:
        saved = add_entry(fields, username)
        message = "✅ 已保存到习题库。"
    stats, tag_update, category_update, selector_update, detail = _bank_view(
        load_entries(username), selected_id=saved["id"], username=username
    )
    yield (
        message,
        "",
        stats,
        tag_update,
        category_update,
        gr.update(choices=["全部"] + MASTERY_LEVELS),
        selector_update,
        detail,
        saved["id"],
        gr.update(value=False),
        gr.update(visible=True),
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
    provider_id: str | None,
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
        provider_id,
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


def generate_category_quiz(
    category_filter: str | None,
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
    provider_id: str | None,
    request: gr.Request,
):
    """按大类出题：选中单题优先，否则用大类筛选结果作素材池，进入练习闭环。"""
    username = _username_of(request)
    if difficulty not in ("基础", "进阶", "挑战"):
        difficulty = "进阶"
    entries = load_entries(username)
    pool: list[dict] = []
    source_desc = ""
    if entry_id:
        entry = get_entry(entry_id, username)
        if entry is not None:
            pool = [entry]
            title = (entry.get("title") or "").strip()[:20]
            source_desc = f"选中题目「{title or '无索引'}」"
    if not pool:
        cat = _resolve_category_filter(category_filter)
        if cat is None:
            pool = entries
            source_desc = "全部题目"
        elif cat == "":
            pool = [e for e in entries if not (e.get("category") or "")]
            source_desc = "未分类题目"
        else:
            pool = [e for e in entries if (e.get("category") or "") == cat]
            source_desc = f"「{cat}」大类题目"
    mode_switched = mode != MODE_INTERACTIVE
    mode_update = gr.update(value=MODE_INTERACTIVE) if mode_switched else gr.update()
    if not pool:
        yield (
            gr.update(),
            "",
            "",
            "",
            usage_state,
            stage_state,
            gr.update(),
            mode_update,
            f"⚠️ {source_desc or '当前范围'}下还没有题目，先收藏几道题或换个范围。",
        )
        return
    # 素材总量控制在单条消息长度上限内（MAX_MESSAGE_LEN 默认 2000 字符）
    budget = 1700
    refs_parts: list[str] = []
    for e in pool[:5]:
        q = (e.get("question") or "").strip()
        if not q:
            continue
        item = f"【参考题 {len(refs_parts) + 1}】\n{q[:300]}"
        if len("\n\n".join(refs_parts + [item])) > budget:
            break
        refs_parts.append(item)
    refs = "\n\n".join(refs_parts)
    message = (
        f"【按类出题】请根据下面这些题（同知识点、题型方法保持一致），"
        f"出一道「{difficulty}」难度变式题（改数字或条件即可，方法不变）："
        "只出题、不写答案和解析，等用户作答后再批改。\n\n"
        f"{refs}"
    )
    stage_state = dict(stage_state or {})
    stage_state["stage"] = "practice_wait"
    stage_state["hints"] = 0
    stage_state["last_mode"] = MODE_INTERACTIVE
    last = None
    for item in respond(
        message,
        history,
        level,
        MODE_INTERACTIVE,
        thinking,
        model,
        reasoning_effort,
        answer_style,
        usage_state,
        stage_state,
        provider_id,
        request,
    ):
        last = item
        yield (*item, mode_update, "")
    if last is not None:
        h, t, s, usage_text, u, st, r = last
        note = f"📚 已从 {len(refs_parts)} 道{source_desc}中选材出题，作答后我会批改。"
        if mode_switched:
            note += "\n（已自动切换到互动模式进行练习）"
        yield (h, t, s, usage_text, u, st, r, mode_update, note)


def _new_paper_state() -> dict:
    return {"active": False, "questions": "", "size": 0}


def _paper_pool(
    username: str, category_filter: str | None, entry_id: str
) -> tuple[list[dict], str]:
    """组卷素材池：选中题优先按其所属大类取素材，否则用大类筛选结果。"""
    entries = load_entries(username)
    if entry_id:
        entry = get_entry(entry_id, username)
        if entry is not None:
            cat = (entry.get("category") or "").strip()
            if cat:
                pool = [e for e in entries if (e.get("category") or "") == cat]
                return pool, f"「{cat}」大类题目"
            pool = [e for e in entries if not (e.get("category") or "")]
            return pool, "未分类题目"
    cat = _resolve_category_filter(category_filter)
    if cat is None:
        return entries, "全部题目"
    if cat == "":
        return [e for e in entries if not (e.get("category") or "")], "未分类题目"
    return (
        [e for e in entries if (e.get("category") or "") == cat],
        f"「{cat}」大类题目",
    )


def _pick_paper_categories(pool: list[dict], max_cats: int = 3) -> list[str]:
    """按题数从多到少选出组卷素材涉及的大类（未分类视为一个伪大类）。

    返回空列表表示池为空；返回 1 个大类表示应出同类卷；
    返回 ≥2 个大类表示素材横跨多类，应出混合卷。
    """
    counts: dict[str, int] = {}
    for entry in pool:
        cat = (entry.get("category") or "").strip() or "未分类"
        counts[cat] = counts.get(cat, 0) + 1
    return sorted(counts, key=lambda c: counts[c], reverse=True)[:max(1, max_cats)]


def _clamp_paper_size(size: str | int | None) -> int:
    try:
        n = int(str(size or "").strip())
    except (TypeError, ValueError):
        return 5
    return max(1, min(10, n))


def generate_paper(
    category_filter: str | None,
    entry_id: str,
    paper_size: str | int | None,
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
    # UNIQUE_PAPER_SIG
    paper_state: dict,
    provider_id: str | None,
    request: gr.Request,
):
    """组卷自测：按大类素材一次生成 N 道变式题，存入试卷状态等待作答。"""
    username = _username_of(request)
    size = _clamp_paper_size(paper_size)
    if difficulty not in ("基础", "进阶", "挑战"):
        difficulty = "进阶"
    pool, source_desc = _paper_pool(username, category_filter, entry_id)
    mode_switched = mode != MODE_INTERACTIVE
    mode_update = gr.update(value=MODE_INTERACTIVE) if mode_switched else gr.update()
    if not pool:
        yield (
            gr.update(),
            "",
            "",
            "",
            usage_state,
            stage_state,
            gr.update(),
            _new_paper_state(),
            mode_update,
            f"⚠️ {source_desc}下还没有题目，先收藏几道题或换个范围。",
        )
        return
    # 素材池横跨多个大类时自动出混合卷（交错练习），否则维持同类卷
    categories = _pick_paper_categories(pool)
    mixed = len(categories) >= 2
    # 素材总量控制在单条消息长度上限内（MAX_MESSAGE_LEN 默认 2000 字符）
    budget = 1700
    refs_parts: list[str] = []
    if mixed:
        # 混合卷：每个大类最多取 2 道参考题，总数上限 6，参考题标注来源大类
        for cat in categories:
            cat_entries = [
                e
                for e in pool
                if ((e.get("category") or "").strip() or "未分类") == cat
            ]
            for e in cat_entries[:2]:
                q = (e.get("question") or "").strip()
                if not q:
                    continue
                item = f"【参考题 {len(refs_parts) + 1}·{cat}】\n{q[:300]}"
                if len("\n\n".join(refs_parts + [item])) > budget:
                    break
                refs_parts.append(item)
            if len(refs_parts) >= 6:
                break
    else:
        for e in pool[:5]:
            q = (e.get("question") or "").strip()
            if not q:
                continue
            item = f"【参考题 {len(refs_parts) + 1}】\n{q[:300]}"
            if len("\n\n".join(refs_parts + [item])) > budget:
                break
            refs_parts.append(item)
    refs = "\n\n".join(refs_parts)
    if mixed:
        message = (
            f"【组卷自测·混合卷】请根据下面这些参考题"
            f"（覆盖 {len(categories)} 个不同知识点：{'、'.join(categories)}），"
            f"生成一套共 {size} 道「{difficulty}」难度变式题："
            "交叉覆盖多个知识点，把不同知识点的题交错排布、不要按知识点分组，"
            "卷面上不要标注每道题的知识点名称（让学生自己判断该用什么方法）；"
            "每道题改数字或条件即可、题型方法保持一致，"
            f"按 1 到 {size} 编号输出：只出题、不写答案和解析，等用户作答后统一批改。\n\n"
            f"{refs}"
        )
    else:
        message = (
            f"【组卷自测】请根据下面这些题（同知识点、题型方法保持一致），"
            f"生成一套共 {size} 道「{difficulty}」难度变式题"
            "（每道改数字或条件即可，方法不变），按 1 到 "
            f"{size} 编号输出：只出题、不写答案和解析，等用户作答后统一批改。\n\n"
            f"{refs}"
        )
    stage_state = dict(stage_state or {})
    stage_state["stage"] = "practice_wait"
    stage_state["hints"] = 0
    stage_state["last_mode"] = MODE_INTERACTIVE
    last = None
    for item in respond(
        message,
        history,
        level,
        MODE_INTERACTIVE,
        thinking,
        model,
        reasoning_effort,
        answer_style,
        usage_state,
        stage_state,
        provider_id,
        request,
        verify=False,
    ):
        last = item
        yield (*item, gr.update(), mode_update, "")
    if last is not None:
        h, t, s, usage_text, u, st, r = last
        questions_text = ""
        for m in reversed(h or []):
            if m.get("role") == "assistant" and m.get("content"):
                questions_text = m["content"]
                break
        new_paper = {
            "active": True,
            "questions": questions_text,
            "size": size,
            "mixed": mixed,
        }
        if mixed:
            note = (
                f"🌪️ 已根据 {len(categories)} 个大类（{'、'.join(categories)}）"
                f"的 {len(refs_parts)} 道素材题生成 {size} 道混合卷，"
                "题序已交错、不标注知识点，请把全部作答写进输入框，然后点「批改本卷」。"
            )
        else:
            note = (
                f"📄 已根据 {len(refs_parts)} 道{source_desc}生成 {size} 道题，"
                "请把全部作答写进输入框，然后点「批改本卷」。"
            )
        if mode_switched:
            note += "\n（已自动切换到互动模式）"
        yield (h, t, s, usage_text, u, st, r, new_paper, mode_update, note)


def grade_paper(
    answers: str,
    history: list[dict],
    level: str,
    mode: str,
    thinking: bool,
    model: str,
    reasoning_effort: str,
    answer_style: str | None,
    usage_state: dict,
    stage_state: dict,
    paper_state: dict,
    provider_id: str | None,
    request: gr.Request,
):
    """批改本卷：把输入框里的整套作答交给 AI 统一批改并小结。"""
    paper_state = dict(paper_state or {})
    if not paper_state.get("active"):
        yield (
            gr.update(),
            "",
            "💡 当前没有待批改的试卷，先点「组卷自测」出一套题。",
            "",
            usage_state,
            stage_state,
            gr.update(),
            gr.update(),
            gr.update(),
            "",
        )
        return
    answers = (answers or "").strip()
    if not answers:
        yield (
            gr.update(),
            "",
            "✍️ 请先把全部作答写进输入框，再点「批改本卷」。",
            "",
            usage_state,
            stage_state,
            gr.update(),
            gr.update(),
            gr.update(),
            "",
        )
        return
    size = int(paper_state.get("size") or 0)
    if paper_state.get("mixed"):
        mixed_hint = (
            "本卷为混合卷：批改时请顺带指出每道题所属知识点，"
            "并在小结里点明哪类知识点掌握得最弱、建议优先复习哪一类。"
        )
    else:
        mixed_hint = ""
    message = (
        f"【批改本卷】请帮我批改这份共 {size} 道题的作答：逐题判定对错"
        "（哪道对、哪道错、错在哪），最后给一句小结：共对几道。"
        "只批改、不重出题。"
        f"{mixed_hint}\n\n作答：\n"
        f"{answers[:1500]}"
    )
    stage_state = dict(stage_state or {})
    stage_state["stage"] = "practice_grade"
    stage_state["hints"] = 0
    stage_state["last_mode"] = MODE_INTERACTIVE
    last = None
    for item in respond(
        message,
        history,
        level,
        MODE_INTERACTIVE,
        thinking,
        model,
        reasoning_effort,
        answer_style,
        usage_state,
        stage_state,
        provider_id,
        request,
        verify=False,
    ):
        last = item
        yield (*item, gr.update(), gr.update(), "")
    if last is not None:
        h, t, s, usage_text, u, st, r = last
        yield (
            h,
            t,
            s + "\n\n✅ 本卷已批改完成。",
            usage_text,
            u,
            st,
            r,
            _new_paper_state(),
            gr.update(),
            "",
        )


def mark_mastery(
    selected_id: str, correct: bool, request: gr.Request
):
    """记录选中题目的练习对/错并自动更新掌握度。"""
    username = _username_of(request)
    if not selected_id:
        return (
            gr.update(),
            gr.update(),
            "⚠️ 请先在习题库选择一道题，再标记对错。",
        )
    entry = record_practice(selected_id, bool(correct), username)
    if entry is None:
        return (
            gr.update(),
            gr.update(),
            "⚠️ 未找到该题，请刷新后重试。",
        )
    detail = _render_detail(entry, show_solution=False)
    stats = _bank_view(load_entries(username), username=username)[0]
    if correct:
        msg = (
            f"✅ 已记录答对（连续答对 {entry['consecutive_right']} 次，"
            f"当前掌握度：{entry['mastery']}）。"
        )
    else:
        msg = f"❌ 已记录答错（当前掌握度：{entry['mastery']}）。"
    return detail, stats, msg


def _category_widget_updates(categories: list[str]):
    """大类相关下拉统一更新（筛选 / 管理 / 表单）。"""
    return (
        gr.update(choices=_category_choices(categories)),
        gr.update(choices=categories),
        gr.update(choices=["未分类"] + categories),
    )


def manage_add_category(name: str, request: gr.Request):
    """新增大类。"""
    username = _username_of(request)
    ok, categories = add_category(name, username)
    msg = (
        "✅ 已添加大类。"
        if ok
        else "⚠️ 名称不能为空、已存在或超过 50 个大类上限。"
    )
    return (*_category_widget_updates(categories), msg)


def manage_rename_category(old: str, new: str, request: gr.Request):
    """重命名大类（同步更新该类下所有题目的分类）。"""
    username = _username_of(request)
    old = (old or "").strip()
    new = (new or "").strip()
    categories = rename_category(old, new, username)
    if old and new and old not in categories and new in categories:
        msg = "✅ 已重命名，该类下的题目已同步更新。"
    else:
        msg = "⚠️ 重命名失败：新名称不能为空或与已有大类重复。"
    return (*_category_widget_updates(categories), msg)


def manage_delete_category(
    name: str,
    confirm_state: str,
    category_filter: str,
    tag_filter: str,
    keyword: str,
    mastery_filter: str,
    request: gr.Request,
):
    """删除大类（二次确认）；该类下的题目全部变为未分类。"""
    username = _username_of(request)
    name = (name or "").strip()
    if not name:
        return (
            "⚠️ 请先选择要删除的大类。",
            "",
            gr.update(value="🗑️ 删除大类"),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(value=False),
            gr.update(),
            gr.update(visible=False),
        )
    if not confirm_state:
        return (
            "⚠️ 再点一次确认删除（该大类下的题会变成未分类，5 秒后自动取消）。",
            "armed",
            gr.update(value="⚠️ 再点一次确认删除"),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(value=False),
            gr.update(),
            gr.update(visible=False),
        )
    categories = delete_category(name, username)
    if (category_filter or "") == name:
        category_filter = "全部"
    entries = filter_entries(
        load_entries(username),
        tag=tag_filter or None,
        keyword=keyword,
        category=_resolve_category_filter(category_filter),
        mastery=_resolve_mastery_filter(mastery_filter),
    )
    stats, tag_update, _, selector_update, detail = _bank_view(
        entries, username=username
    )
    return (
        f"🗑️ 已删除大类「{name}」，其中题目已变为未分类。",
        "",
        gr.update(value="🗑️ 删除大类"),
        gr.update(choices=_category_choices(categories), value=category_filter),
        gr.update(choices=["全部"] + MASTERY_LEVELS),
        gr.update(choices=categories),
        gr.update(choices=["未分类"] + categories),
        stats,
        tag_update,
        selector_update,
        detail,
        "",
        gr.update(value=False),
        "🔄 已刷新。",
        gr.update(visible=False),
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
            _new_paper_state(),
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
        _new_paper_state(),
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
            gr.update(),
            gr.update(),
            "（先在上方选择一道题）",
            "",
            gr.update(value=False),
            gr.update(visible=False),
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
            gr.update(),
            gr.update(),
            gr.update(visible=False),
        )
    (
        bank_msg,
        stats,
        tag_update,
        category_update,
        mastery_update,
        selector_update,
        detail,
        selected,
        solution_update,
        mastery_vis,
    ) = delete_selected_entry(selected_id, request)
    return (
        "",
        gr.update(value="🗑️ 删除"),
        bank_msg,
        stats,
        tag_update,
        category_update,
        gr.update(choices=["全部"] + MASTERY_LEVELS),
        selector_update,
        detail,
        selected,
        solution_update,
        mastery_vis,
    )


def reset_confirm_buttons(
    clear_state: str, delete_state: str, category_state: str
):
    """Timer 到点后复位二次确认状态与按钮文案。"""
    return (
        gr.update(value="") if clear_state else gr.update(),
        gr.update(value="") if delete_state else gr.update(),
        gr.update(value="") if category_state else gr.update(),
        gr.update(value="🗑️ 新对话") if clear_state else gr.update(),
        gr.update(value="🗑️ 删除") if delete_state else gr.update(),
        gr.update(value="🗑️ 删除大类") if category_state else gr.update(),
    )


def on_provider_change(provider_id: str):
    """服务商切换时，动态更新模型列表与思考相关控件。"""
    provider = get_provider(provider_id)
    if provider is None:
        return (
            gr.update(choices=[]),
            gr.update(visible=False),
            gr.update(visible=False),
        )
    models = get_provider_models(provider)
    supports_thinking = get_provider_supports_thinking(provider)
    return (
        gr.update(choices=models, value=provider.get("default_model")),
        gr.update(visible=supports_thinking),
        gr.update(visible=supports_thinking),
    )



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
                provider_radio = gr.Radio(
                      choices=[(p["name"], p["id"]) for p in ui_providers],
                      value=(default_provider["id"] if default_provider else None),
                      label="服务商",
                  )
                model_radio = gr.Radio(
                    (default_provider.get("models") if default_provider else ["deepseek-v4-flash", "deepseek-v4-pro"]),
                    value=(
                        default_provider.get("default_model")
                        if default_provider
                        else (settings.model if settings else "deepseek-v4-flash")
                    ),
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
            provider_state = gr.State(
                  value=(default_provider["id"] if default_provider else None)
              )
            with gr.Accordion("📷 题目图片（上传 / 粘贴后自动识别）", open=True):
                image_input = gr.Image(
                    label="题目图片（上传/粘贴后自动识别）",
                    sources=["upload", "clipboard"],
                    type="filepath",
                )
                handwritten_checkbox = gr.Checkbox(
                    label="✍️ 手写公式模式（只识别手写公式/题目，输出 LaTeX）",
                    value=False,
                  )
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
                grade_paper_btn = gr.Button("📝 批改本卷")
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
            with gr.Row(elem_classes=["bank-layout"]):
                with gr.Accordion(
                    "🔍 筛选与搜索", open=False, elem_classes=["bank-filters"]
                ):
                    with gr.Row(elem_classes=["bank-toolbar"]):
                        bank_category_filter = gr.Dropdown(
                            choices=["全部", "未分类"] + list(DEFAULT_CATEGORIES),
                            label="大类筛选",
                            value="全部",
                        )
                        bank_mastery_filter = gr.Dropdown(
                            choices=["全部"] + MASTERY_LEVELS,
                            label="掌握度筛选",
                            value="全部",
                        )
                        bank_tag_filter = gr.Dropdown(
                            choices=[], label="按标签筛选", value=None
                        )
                        bank_search = gr.Textbox(
                            label="关键词搜索",
                            placeholder="搜索题目或标签",
                            scale=2,
                        )
                        bank_refresh_btn = gr.Button("🔄 刷新")
                        new_btn = gr.Button("📝 手动新建")
                bank_stats = gr.Markdown(
                    "（习题库为空）", elem_classes=["bank-stats"]
                )
                entry_selector = gr.Dropdown(
                    choices=[],
                    label="选择题目",
                    value=None,
                    elem_classes=["bank-selector"],
                )
                detail_md = gr.Markdown(
                    "（先在上方选择一道题）",
                    latex_delimiters=LATEX_DELIMITERS,
                    elem_classes=["bank-detail"],
                )
                bank_status = gr.Markdown(
                    "",
                    latex_delimiters=LATEX_DELIMITERS,
                    elem_classes=["bank-status"],
                )
                with gr.Row(
                    elem_classes=["bank-actions", "bank-actions-area"]
                ):
                    solution_checkbox = gr.Checkbox(
                        label="显示正确解", value=False
                    )
                    edit_btn = gr.Button("✏️ 编辑")
                    delete_btn = gr.Button("🗑️ 删除")
                with gr.Row(
                    elem_classes=["mastery-row", "bank-mastery"],
                    visible=False,
                ) as mastery_row:
                    mastery_correct_btn = gr.Button(
                        "✅ 答对了", size="sm"
                    )
                    mastery_wrong_btn = gr.Button(
                        "❌ 答错了", size="sm"
                    )
                with gr.Accordion(
                    "🎯 练习与组卷",
                    open=False,
                    elem_classes=["bank-practice"],
                ):
                    difficulty_radio = gr.Radio(
                        ["基础", "进阶", "挑战"],
                        value="进阶",
                        label="变式难度",
                    )
                    with gr.Row():
                        similar_btn = gr.Button("🎯 生成同类型题")
                        quiz_btn = gr.Button("📝 按此类出题")
                    with gr.Row():
                        paper_size_textbox = gr.Textbox(
                            value="5", label="题数(1-10)", scale=1
                        )
                        paper_btn = gr.Button("📄 组卷自测")
                with gr.Accordion(
                    "🗂️ 管理大类",
                    open=False,
                    elem_classes=["bank-manage"],
                ):
                    category_add_input = gr.Textbox(
                        label="新大类名称", placeholder="如：三角函数"
                    )
                    category_add_btn = gr.Button("➕ 添加大类")
                    category_manage_dropdown = gr.Dropdown(
                        choices=[], label="选择要管理的大类"
                    )
                    category_rename_input = gr.Textbox(
                        label="重命名为", placeholder="输入新名称"
                    )
                    category_rename_btn = gr.Button("✏️ 重命名")
                    category_delete_btn = gr.Button("🗑️ 删除大类")
                    category_mgmt_status = gr.Markdown("")
                    category_confirm_state = gr.State(value="")
                with gr.Accordion(
                    "题目表单（收藏 / 手动新建 / 编辑）",
                    open=False,
                    elem_classes=["bank-form"],
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
                    form_category = gr.Dropdown(
                        choices=["未分类"] + list(DEFAULT_CATEGORIES),
                        label="大类（可选）",
                        value="未分类",
                    )
                    form_mastery = gr.Dropdown(
                        choices=MASTERY_LEVELS,
                        label="掌握度（手动纠正用）",
                        value="未掌握",
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
                    mastery_correct_state = gr.State(value=True)
                    mastery_wrong_state = gr.State(value=False)

    clear_confirm_state = gr.State(value="")
    delete_confirm_state = gr.State(value="")
    paper_state = gr.State(value=_new_paper_state())
    confirm_timer = gr.Timer(value=5, active=True)

    # ===== 学习 Tab 事件 =====
    image_input.upload(
        recognize_image,
        inputs=[image_input, handwritten_checkbox],
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
              provider_radio,
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
              provider_radio,
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
              provider_radio,
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
            form_category,
            form_mastery,
            form_tags,
            edit_id_state,
            bank_status,
            form_status,
        ],
    )
    quiz_event = quiz_btn.click(
        generate_category_quiz,
        inputs=[
            bank_category_filter,
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
              provider_radio,
        ],
        outputs=[
            chatbot,
            textbox,
            result_status,
            usage_status,
            usage_state,
            stage_state,
            reasoning_md,
            mode_radio,
            bank_status,
        ],
    )
    paper_event = paper_btn.click(
        generate_paper,
        inputs=[
            bank_category_filter,
            selected_id_state,
            paper_size_textbox,
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
              # UNIQUE_PAPER_EVENT
              provider_radio,
                # UNIQUE_PAPER_AFTER
            paper_state,
        ],
        outputs=[
            chatbot,
            textbox,
            result_status,
            usage_status,
            usage_state,
            stage_state,
            reasoning_md,
            paper_state,
            mode_radio,
            bank_status,
        ],
    )
    grade_paper_event = grade_paper_btn.click(
        grade_paper,
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
              provider_radio,
            paper_state,
        ],
        outputs=[
            chatbot,
            textbox,
            result_status,
            usage_status,
            usage_state,
            stage_state,
            reasoning_md,
            paper_state,
            mode_radio,
            bank_status,
        ],
    )
    mastery_correct_btn.click(
        mark_mastery,
        inputs=[selected_id_state, mastery_correct_state],
        outputs=[detail_md, bank_stats, bank_status],
    )
    mastery_wrong_btn.click(
        mark_mastery,
        inputs=[selected_id_state, mastery_wrong_state],
        outputs=[detail_md, bank_stats, bank_status],
    )
    stop_btn.click(
        None,
        None,
        None,
        cancels=[
            send_event,
            submit_event,
            continue_event,
            quiz_event,
            paper_event,
            grade_paper_event,
        ],
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
            paper_state,
        ],
    )
    refresh_btn.click(render_usage_panel, inputs=[], outputs=[usage_panel])
    reset_pwd_btn.click(
        reset_account_password,
        inputs=[reset_account_dropdown, reset_pwd_textbox],
        outputs=[reset_pwd_status],
    )
    provider_radio.change(
          on_provider_change,
          inputs=[provider_radio],
          outputs=[model_radio, thinking_checkbox, effort_radio],
      )

    mode_radio.change(
        toggle_answer_style,
        inputs=[mode_radio],
        outputs=[answer_style_radio],
    )
    confirm_timer.tick(
        reset_confirm_buttons,
        inputs=[clear_confirm_state, delete_confirm_state, category_confirm_state],
        outputs=[
            clear_confirm_state,
            delete_confirm_state,
            category_confirm_state,
            clear_btn,
            delete_btn,
            category_delete_btn,
        ],
    )

    # ===== 习题库 Tab 事件 =====
    bank_refresh_btn.click(
        refresh_bank,
        inputs=[
            bank_tag_filter,
            bank_search,
            bank_category_filter,
            bank_mastery_filter,
        ],
        outputs=[
            bank_stats,
            bank_tag_filter,
            bank_category_filter,
            bank_mastery_filter,
            entry_selector,
            detail_md,
            selected_id_state,
            solution_checkbox,
            bank_status,
            mastery_row,
        ],
    )
    bank_category_filter.change(
        refresh_bank,
        inputs=[
            bank_tag_filter,
            bank_search,
            bank_category_filter,
            bank_mastery_filter,
        ],
        outputs=[
            bank_stats,
            bank_tag_filter,
            bank_category_filter,
            bank_mastery_filter,
            entry_selector,
            detail_md,
            selected_id_state,
            solution_checkbox,
            bank_status,
            mastery_row,
        ],
    )
    bank_mastery_filter.change(
        refresh_bank,
        inputs=[
            bank_tag_filter,
            bank_search,
            bank_category_filter,
            bank_mastery_filter,
        ],
        outputs=[
            bank_stats,
            bank_tag_filter,
            bank_category_filter,
            bank_mastery_filter,
            entry_selector,
            detail_md,
            selected_id_state,
            solution_checkbox,
            bank_status,
            mastery_row,
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
            form_category,
            form_mastery,
            form_tags,
            edit_id_state,
            bank_status,
            form_status,
        ],
    )
    entry_selector.change(
        select_entry,
        inputs=[entry_selector, solution_checkbox],
        outputs=[
            detail_md,
            selected_id_state,
            solution_checkbox,
            bank_status,
            mastery_row,
        ],
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
            form_category,
            form_mastery,
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
            bank_category_filter,
            bank_mastery_filter,
            entry_selector,
            detail_md,
            selected_id_state,
            solution_checkbox,
            mastery_row,
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
              provider_radio,
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
    category_add_btn.click(
        manage_add_category,
        inputs=[category_add_input],
        outputs=[
            bank_category_filter,
            category_manage_dropdown,
            form_category,
            category_mgmt_status,
        ],
    )
    category_rename_btn.click(
        manage_rename_category,
        inputs=[category_manage_dropdown, category_rename_input],
        outputs=[
            bank_category_filter,
            category_manage_dropdown,
            form_category,
            category_mgmt_status,
        ],
    )
    category_delete_btn.click(
        manage_delete_category,
        inputs=[
            category_manage_dropdown,
            category_confirm_state,
            bank_category_filter,
            bank_tag_filter,
            bank_search,
            bank_mastery_filter,
        ],
        outputs=[
            category_mgmt_status,
            category_confirm_state,
            category_delete_btn,
            bank_category_filter,
            bank_mastery_filter,
            category_manage_dropdown,
            form_category,
            bank_stats,
            bank_tag_filter,
            entry_selector,
            detail_md,
            selected_id_state,
            solution_checkbox,
            bank_status,
            mastery_row,
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
            form_category,
            form_mastery,
            form_tags,
            force_save_state,
        ],
        outputs=[
            form_status,
            force_save_state,
            bank_stats,
            bank_tag_filter,
            bank_category_filter,
            bank_mastery_filter,
            entry_selector,
            detail_md,
            selected_id_state,
            solution_checkbox,
            mastery_row,
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
