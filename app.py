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
from core.compressor import HistoryCompressor
from core.config import get_base_settings
from core.llm_client import LLMClient, MaxWaitTimeout
from core.providers import (
    get_available_providers,
    get_default_provider,
    get_provider,
    get_provider_models,
    get_provider_supports_thinking,
    load_providers,
)
from core.math_format import normalize_math_text
from core.prompts import (
    MODE_ANSWER,
    MODE_INTERACTIVE,
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
    get_usage_total,
    get_usage_part,
)
from core import dialogue_state
from core import chat_service 

APP_VERSION = "1.3.1"
# 多账号：首次启动按 ACCOUNT_COUNT（默认 5）生成账号，之后以 data/accounts.json 为准
ACCOUNT_COUNT = int(os.getenv("ACCOUNT_COUNT", "5") or "5")
accounts = ensure_accounts(ACCOUNT_COUNT)
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


def _new_image_state() -> dict:
    """待发图片状态：path 为上传的临时文件路径，fresh 表示本回合刚上传。

    - 上传后首次发送携带图片并把 fresh 置 False；
    - 同一题目继续讨论时每轮都重新附带原图（模型无跨轮图片记忆）；
    - 新题开始 / 用户移除图片 / 服务商不支持视觉时清空。
    """
    return {"path": None, "fresh": False}


# 图片随消息附带的标记：写入历史供界面显示与上下文压缩参考（图片本身不持久化）
IMAGE_NOTE = "📷（已附题目图片）"
# 只传图不打字时自动使用的提问语
_IMAGE_ONLY_PROMPT = "请解答图中的题目。"


def format_usage(usage) -> str:
    """把 API 返回的 token 用量格式化为一行提示（含输入/输出拆分）。"""
    if usage is None:
        return ""
    total = int(getattr(usage, "total_tokens", 0) or 0)
    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    reasoning = 0
    details = getattr(usage, "completion_tokens_details", None)
    if details is not None:
        reasoning = int(getattr(details, "reasoning_tokens", 0) or 0)
    reasoning_part = f"，其中思考链约 {reasoning}" if reasoning else ""
    if prompt is not None and completion is not None:
        return (
            f"⚡ 本次 输入 {int(prompt):,} + 输出 {int(completion):,}"
            f" ≈ 共 {total:,} tokens{reasoning_part}"
        )
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


def on_image_upload(image: str | None, provider_id: str | None):
    """上传/粘贴题目图片：统一进入待发状态，由支持原生视觉的服务商直接处理。

    outputs：[textbox, result_status, image_input, image_state]
    """
    if not image:
        yield gr.skip(), "", gr.skip(), _new_image_state()    #输出重置UI的指令
        return  #退出函数
    _log_event("图片就绪", str(image))
    yield (
        gr.skip(),
        "🖼️ 图片已就绪：输入你的问题后点击发送。"
        "支持原生视觉的服务商可直接看图；纯文本服务商不支持图片输入。"
        "也可以不打字直接发送。同一题继续讨论会自动携带，换新题或点 × 移除后停止。",
        image,
        {"path": image, "fresh": True},
    )


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
    image_state: dict | None,
    provider_id: str | None,
    request: gr.Request,
    force_continue: bool = False,
    attach_image: bool = True,
):
    """聊天主流程：流式回复，累计会话用量，并按互动模式阶段引导。

    发送时按所选服务商能力路由图片：支持原生视觉则直接附图；
    纯文本服务商收到图片时返回提示。attach_image=False 表示本次只发送文字，
    不携带待发图片。思考链仅当轮展示。
    """

    #承接以前的会话历史记录，并且排除只输入空格的情况
    history = list(history or [])       #这是为了承接以前的历史记录，[]是为了防止history为None时报错
    stage_state = dialogue_state.prepare_stage_state(mode, stage_state)
    #图片状态
    image_state = dict(image_state or _new_image_state())
    image_state.setdefault("path", None)
    image_state.setdefault("fresh", False)
    has_pending_image = bool(image_state.get("path")) and not force_continue
    input_result = chat_service.prepare_user_message(
        message=message,
        has_pending_image=has_pending_image,
        max_message_len=MAX_MESSAGE_LEN,
        image_only_prompt=_IMAGE_ONLY_PROMPT,
    )
    message = input_result["message"]

    if input_result["error"] == "empty":
        yield (
            history,
            "",
            "💡 请先输入问题，或上传题目图片后直接发送。",
            "",
            usage_state,
            stage_state,
            "",
            image_state,
        )
        return

    username = request.username if request and request.username else "unknown"
    ip = (request.client.host if request and request.client else None) or "unknown"

    if input_result["error"] == "too_long":
        log_access_event(
            "过长拒绝",
            f"账号={username}",
            f"IP={ip}",
            f"长度={len(message)}",
        )
        yield (
            history,
            "",
            f"⚠️ 消息过长（最多 {MAX_MESSAGE_LEN:,} 字符），请精简后重试。",
            "",
            usage_state,
            stage_state,
            "",
            image_state,
        )
        return

    #日配额检查
    today = datetime.date.today().isoformat()    #获取当前日期
    daily_used = get_daily_used(today, username)       #获取当前账号今日使用量
    global_used = get_daily_total(today)        #获取当天全局用量
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
        yield (
            history,
            "",
            quota_msg,
            "",
            usage_state,
            stage_state,
            "",
            image_state,
        )
        return

    # 根据用户新消息推进互动模式阶段（解答模式无阶段）
    new_stage = dialogue_state.advance_stage(
        mode,
        message,
        stage_state,
        has_history=bool(history),
        force_continue=force_continue,
    )

    user_content = normalize_math_text(message, auto_close=True)     #补全用户输入公式，并且改成新的变量
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
            image_state,
        )
        return

    # 决定本轮是否附图；Gradio 的输出仍由 respond 负责
    image_result = chat_service.route_image_for_turn(
        message=message,
        history=history,
        image_state=image_state,
        previous_stage=stage_state.get("stage"),
        supports_vision=bool(getattr(active_client, "supports_vision", False)),
        force_continue=force_continue,
        attach_image=attach_image,
        image_note=IMAGE_NOTE,
    )
    history = image_result["history"]
    image_data_url = image_result["image_data_url"]
    new_image_state = image_result["image_state"]
    image_status = image_result["image_status"]

    if image_result["reject_message"] is not None:
        yield (
            history,
            "",
            image_result["reject_message"],
            "",
            usage_state,
            new_stage,
            "",
            new_image_state,
        )
        return
            

    #正式开始流式调用大模型回答
    answer = ""
    reasoning_text = ""
    usage = None
    truncated = False    #是否被截断
    thinking_started = time.monotonic()
    try:
        for state in chat_service.stream_reply(
            active_client,
            chat_service.prepare_api_messages(
                username=username,
                history=history,
                level=level,
                mode=mode,
                answer_style=answer_style,
                active_client=active_client,
                history_compressor=history_compressor,
                image_data_url=image_data_url,
                stage=new_stage["stage"],
                mode_changed=new_stage["mode_changed"],
                max_history_messages=(
                    settings.max_history_messages if settings is not None else None
                ),
            ),
            thinking=thinking,
            reasoning_effort=reasoning_effort,
            model=model,
        ):
            answer = state["answer"]
            reasoning_text = state["reasoning_text"]
            usage = state["usage"]
            truncated = state["truncated"]
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
                    new_image_state,
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
                    new_image_state,
                )
    # 流式输出结束，由下面的逻辑继续处理最终结果
    except Exception as exc:
        turn_total = get_usage_total(usage)
        if usage is None and answer:
            turn_total = estimate_tokens(answer)
        _log_error(
            exc, account=username, turn_total=turn_total, partial=bool(answer)
        )
        usage_state = record_usage(
            usage_state,
            turn_total,
            prompt_tokens=get_usage_part(usage, "prompt_tokens"),
            completion_tokens=get_usage_part(usage, "completion_tokens"),
        )
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
            new_image_state,
        )
        return

    if not answer:
        turn_total = get_usage_total(usage)
        usage_state = record_usage(
            usage_state,
            turn_total,
            prompt_tokens=get_usage_part(usage, "prompt_tokens"),
            completion_tokens=get_usage_part(usage, "completion_tokens"),
        )
        if turn_total is not None:
            daily_used = add_daily_usage(today, username, turn_total)
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
            new_image_state,
        )
        return

    # 最终展示启用自动闭合：模型偶发漏写尾部 $$ 时补上，保证公式能渲染
    display = normalize_math_text(answer, auto_close=True)
    final_history = history + [{"role": "assistant", "content": display}]
    save_history(final_history, username=username)
    turn_total = (
        int(getattr(usage, "total_tokens", 0) or 0) if usage is not None else None
    )
    usage_state = record_usage(
        usage_state,
        turn_total,
        prompt_tokens=get_usage_part(usage, "prompt_tokens"),
        completion_tokens=get_usage_part(usage, "completion_tokens"),
    )
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
    if image_status:
        result_text = f"{image_status}\n\n{result_text}" if result_text else image_status
    if truncated:
        result_text += "\n\n⚠️ 回答因长度限制被截断，可以回复「继续」让我接着写。"
    usage_text = format_cumulative(usage_state)
    usage_text += (
        f"\n\n🛡️ 今日已用 {daily_used:,} / {DAILY_LIMIT:,}（本账号），"
        f"全局 {global_used:,} / {GLOBAL_DAILY_LIMIT:,}"
    )
    reasoning_display = normalize_math_text(reasoning_text, auto_close=True)
    # 输出最终答案
    yield (
        final_history,
        "",
        result_text,
        usage_text,
        usage_state,
        new_stage,
        reasoning_display,
        new_image_state,
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
    image_state: dict | None,
    provider_id: str | None,
    request: gr.Request,
):
    """「继续生成」按钮：取最近一条题目原文，拼上继续指令后复用完整回复流程。"""
    history = list(history or [])
    image_state = dict(image_state or _new_image_state())
    message = chat_service.build_continue_message(history, IMAGE_NOTE)
    if message is None:
        yield (
            history,
            "",
            "💡 还没有可继续的题目，请先提问。",
            "",
            usage_state,
            stage_state,
            gr.skip(),
            image_state,
        )
        return
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
        image_state,
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
        dialogue_state.new_stage_state(),
        "（本轮无思考过程）",
        _new_image_state(),
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


def toggle_answer_style(mode: str):
    """解答风格选项只在解答模式下显示。"""
    return gr.update(visible=(mode == MODE_ANSWER))


# ===================== 对话控制 =====================


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
            gr.update(),
        )
    (
        new_history,
        _text,
        result_msg,
        usage_msg,
        new_usage,
        new_stage,
        _reasoning,
        new_image_state,
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
        new_image_state,
    )


def reset_confirm_buttons(clear_state: str):
    """Timer 到点后复位二次确认状态与按钮文案。"""
    return (
        gr.update(value="") if clear_state else gr.update(),
        gr.update(value="🗑️ 新对话") if clear_state else gr.update(),
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


def clear_image_state():
    """用户点击图片组件的 × 移除图片：清空待发图片状态。"""
    return _new_image_state()



with gr.Blocks(
    title="数学学习 AI 助手",
    theme=gr.themes.Base(
        primary_hue="blue", secondary_hue="blue", neutral_hue="slate"
    ),
    css=PAGE_CSS,
) as demo:
    gr.Markdown(
        "# 🧮 数学学习 AI 助手\n\n"
        "支持多服务商模型，采用苏格拉底式引导，一步步带你学会数学。"
        f"支持 LaTeX 公式和图片输入（取决于所选模型）。版本 v{APP_VERSION}"
    )
    with gr.Tabs():
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
            stage_state = gr.State(value=dialogue_state.new_stage_state())
            image_state = gr.State(value=_new_image_state())
            provider_state = gr.State(
                  value=(default_provider["id"] if default_provider else None)
              )
            with gr.Accordion("📷 题目图片（上传 / 粘贴后直接发送）", open=True):
                image_input = gr.Image(
                    label="支持原生视觉的模型可直接看图；纯文本模型不支持图片输入",
                    sources=["upload", "clipboard"],
                    type="filepath",
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

    clear_confirm_state = gr.State(value="")
    confirm_timer = gr.Timer(value=5, active=True)

    # ===== 学习 Tab 事件 =====
    image_input.upload(
        on_image_upload,
        inputs=[image_input, provider_radio],
        outputs=[textbox, result_status, image_input, image_state],
    )
    image_input.clear(
        clear_image_state,
        inputs=None,
        outputs=[image_state],
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
            image_state,
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
            image_state,
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
            image_state,
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
            image_state,
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
            image_state,
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
            image_state,
        ],
    )
    stop_btn.click(
        None,
        None,
        None,
        cancels=[
            send_event,
            submit_event,
            continue_event,

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
            image_state,
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
        inputs=[clear_confirm_state],
        outputs=[
            clear_confirm_state,
            clear_btn,
        ],
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
