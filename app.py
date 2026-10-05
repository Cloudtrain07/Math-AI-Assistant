"""数学学习 AI 助手：Gradio 界面 + DeepSeek 大模型 + 拍照识题 + 学习功能。"""

from __future__ import annotations

import datetime
import functools
import inspect
import os
import threading
import time

import gradio as gr

from core.access import (
    get_lan_ip,
    is_owner_host,
    save_access_info,
)
from core.accounts import (
    get_account,
    load_accounts,
    reset_password,
)
from core.backup import daily_backup_loop, ensure_today_backup
from core.login import verify_login
# 新旧界面共用的文案与展示格式化：统一从 core.ui_text 引用（别名保留旧名，
# 现有回调与测试的引用不受影响）
from core.ui_text import (
    CLEAR_FAILED_MSG as _CLEAR_FAILED_MSG,
    CLEAR_OK_MSG,
    DAILY_FAILED_MSG as _DAILY_FAILED_MSG,
    DAILY_STATS_FAILED_MSG as _DAILY_STATS_FAILED_MSG,
    IMAGE_NOTE,
    IMAGE_ONLY_PROMPT as _IMAGE_ONLY_PROMPT,
    NO_CONTINUE_MSG,
    QUOTA_CHECK_FAILED_MSG as _QUOTA_CHECK_FAILED_MSG,
    RESTORE_FAILED_MSG as _RESTORE_FAILED_MSG,
    SAVE_FAILED_MSG as _SAVE_FAILED_MSG,
    SAVE_STALE_MSG as _SAVE_STALE_MSG,
    format_usage,
    friendly_error as _friendly_error,
)
from core.providers import (
    get_provider,
    get_provider_models,
    get_provider_supports_thinking,
)
from core.math_format import normalize_math_text
from core.prompts import (
    MODE_ANSWER,
    MODE_INTERACTIVE,
)
from core.runtime import AppRuntime, build_runtime
from core.storage import HistoryReadError
from core.tunnel import TunnelWatcher, cleanup_stale_tunnel, find_cloudflared
from core.usage import (
    estimate_tokens,
    format_cumulative,
    new_usage_state,
)
from core.usage_store import DailyUsageReadError
from core import dialogue_state
from core import chat_service

APP_VERSION = "1.3.1"

# 移动端适配与整体排版（蓝白黑冷色调主题之外的少量微调）
PAGE_CSS = """
.gradio-container { max-width: 1280px !important; margin: 0 auto !important; }
@media (max-width: 768px) {
  .gradio-container { max-width: 100% !important; padding: 8px !important; }
  .chatbot { height: 55vh !important; }
  .katex-display, .katex { overflow-x: auto; overflow-y: hidden; }
}
"""

def _new_image_state() -> dict:
    """待发图片状态：path 为上传的临时文件路径，fresh 表示本回合刚上传。

    - 上传后首次发送携带图片并把 fresh 置 False；
    - 同一题目继续讨论时每轮都重新附带原图（模型无跨轮图片记忆）；
    - 新题开始 / 用户移除图片 / 服务商不支持视觉时清空。
    """
    return {"path": None, "fresh": False}


def _check_login(runtime, username: str, password: str) -> bool:
    """校验登录并记录到访问日志（Gradio auth 回调）。

    兼容包装：校验与登录计数副作用已下沉 core.login.verify_login，
    与新 API 共用同一条登录路径，避免副作用重复。
    """
    return verify_login(runtime, username, password)


def on_image_upload(runtime, image: str | None, provider_id: str | None):
    """上传/粘贴题目图片：统一进入待发状态，由支持原生视觉的服务商直接处理。

    outputs：[textbox, result_status, image_input, image_state]
    """
    if not image:
        yield gr.skip(), "", gr.skip(), _new_image_state()    #输出重置UI的指令
        return  #退出函数
    runtime.event_logger.log_chat_event("图片就绪", str(image))
    yield (
        gr.skip(),
        "🖼️ 图片已就绪：输入你的问题后点击发送。"
        "支持原生视觉的服务商可直接看图；纯文本服务商不支持图片输入。"
        "也可以不打字直接发送。同一题继续讨论会自动携带，换新题或点 × 移除后停止。",
        image,
        {"path": image, "fresh": True},
    )


def respond(
    runtime,
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
    """取得界面输入和依赖，将聊天 service 的事件映射为 Gradio 输出。"""
    username = request.username if request and request.username else "unknown"
    ip = (request.client.host if request and request.client else None) or "unknown"
    today = datetime.date.today().isoformat()
    active_client = runtime.clients.get(provider_id) or runtime.client

    thinking_started = None

    for event in chat_service.run_chat_turn(
        message=message,
        history=history,
        usage_state=usage_state,
        username=username,
        ip=ip,
        date=today,
        level=level,
        mode=mode,
        stage_state=stage_state,
        image_state=image_state,
        thinking=thinking,
        model=model,
        reasoning_effort=reasoning_effort,
        answer_style=answer_style,
        active_client=active_client,
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
        force_continue=force_continue,
        attach_image=attach_image,
    ):
        event_type = event["type"]
        turn = event["turn"]
        history = turn["history"]
        new_stage = turn["stage_state"]
        new_image_state = turn["image_state"]
        image_status = turn["image_status"]

        if event_type == "ready":
            # 准备已经完成，接下来才进入上下文准备和模型调用。
            thinking_started = time.monotonic()
            continue

        if event_type == "rejected":
            display_history = history
            reason = turn["status"]

            if reason == "empty":
                result_text = "💡 请先输入问题，或上传题目图片后直接发送。"
            elif reason == "too_long":
                result_text = (
                    f"⚠️ 消息过长（最多 {runtime.max_message_len:,} 字符），"
                    "请精简后重试。"
                )
            elif reason == "quota_read_failed":
                result_text = _QUOTA_CHECK_FAILED_MSG
            elif reason == "no_client":
                # 沿用原行为：启动提示显示在聊天区。
                display_history = history + [
                    {"role": "assistant", "content": f"⚠️ {runtime.startup_hint}"}
                ]
                result_text = ""
            else:
                # 超额和不支持图片的提示由 service 提供。
                result_text = turn["reject_message"]

            yield (
                display_history,
                "",
                result_text,
                "",
                usage_state,
                new_stage,
                "",
                new_image_state,
            )
            return

        reply = event["reply"]
        answer = reply["answer"]
        reasoning_text = reply["reasoning_text"]
        usage = reply["usage"]
        truncated = reply["truncated"]

        if event_type == "stream":
            if not answer:
                tokens_part = (
                    f"，思考约 {estimate_tokens(reasoning_text)} tokens"
                    if reasoning_text
                    else ""
                )
                result_text = (
                    "⏳ 正在思考… "
                    f"已用时 {int(time.monotonic() - thinking_started)} 秒"
                    f"{tokens_part}"
                )
                display_history = history
            else:
                result_text = ""
                display_history = history + [
                    {
                        "role": "assistant",
                        "content": normalize_math_text(answer),
                    }
                ]

            yield (
                display_history,
                "",
                result_text,
                "",
                usage_state,
                new_stage,
                normalize_math_text(reasoning_text, auto_close=True),
                new_image_state,
            )
            continue

        # 最终事件：保存、结算和日志已经由 service 完成。
        turn_result = event["result"]
        final_history = turn_result["history"]
        usage_state = turn_result["usage_state"]
        turn_total = turn_result["turn_total"]
        error = turn_result["error"]
        save_status = turn_result["save_status"]

        save_status_to_note = {
            None: "",
            "saved": "",
            "failed": _SAVE_FAILED_MSG,
            "stale": _SAVE_STALE_MSG,
        }
        save_note = save_status_to_note[save_status]

        daily_note = ""
        daily_used = None
        global_used = None
        daily_result = turn_result["daily_usage"]
        if daily_result is not None:
            daily_used = daily_result["daily_used"]
            global_used = daily_result["global_used"]
            daily_status_to_note = {
                "recorded": "",
                "record_failed": _DAILY_FAILED_MSG,
                "stats_failed": _DAILY_STATS_FAILED_MSG,
            }
            daily_note = daily_status_to_note[daily_result["status"]]

        usage_text = format_cumulative(usage_state)

        if error is not None:
            result_text = _friendly_error(error)
            if answer:
                if save_status == "saved":
                    result_text += "\n\n（已保留生成的部分内容）"
                else:
                    result_text += (
                        "\n\n（部分回答仅在当前页面显示，未写入历史）"
                    )
            if save_note:
                result_text = f"{result_text}\n\n{save_note}"
            if daily_note:
                result_text = f"{result_text}\n\n{daily_note}"

        elif not answer:
            # 界面占位文字不进入 service 保存的历史。
            final_history = final_history + [
                {"role": "assistant", "content": "（未收到正式回答）"}
            ]
            if truncated:
                hint = "模型的思考过程已用完本次输出额度，没能生成正式回答"
            else:
                hint = "请求已完成，但模型没有生成正式回答"
            if turn_total is not None:
                hint += f"（本次约消耗 {turn_total} tokens）"
            result_text = (
                f"⚠️ {hint}。\n\n"
                "建议：关闭「深度思考模式」、把 .env 中 DEEPSEEK_REASONING_EFFORT 调低，"
                "或调大 MAX_OUTPUT_TOKENS 后重启。"
            )
            if daily_note:
                result_text = f"{result_text}\n\n{daily_note}"

        else:
            result_text = format_usage(usage)
            if image_status:
                result_text = (
                    f"{image_status}\n\n{result_text}"
                    if result_text
                    else image_status
                )
            if truncated:
                result_text += (
                    "\n\n⚠️ 回答因长度限制被截断，"
                    "可以回复「继续」让我接着写。"
                )
            if save_note:
                result_text = f"{result_text}\n\n{save_note}"
            if daily_note:
                result_text = f"{result_text}\n\n{daily_note}"

            if daily_used is not None and global_used is not None:
                usage_text += (
                    f"\n\n🛡️ 今日已用 {daily_used:,} / {runtime.daily_limit:,}（本账号），"
                    f"全局 {global_used:,} / {runtime.global_daily_limit:,}"
                )
            elif daily_used is not None:
                usage_text += (
                    f"\n\n🛡️ 今日已用 {daily_used:,} / "
                    f"{runtime.daily_limit:,}（本账号）"
                )

        yield (
            final_history,
            "",
            result_text,
            usage_text,
            usage_state,
            new_stage,
            normalize_math_text(reasoning_text, auto_close=True),
            new_image_state,
        )
        return


def continue_last_answer(
    runtime,
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
            NO_CONTINUE_MSG,
            "",
            usage_state,
            stage_state,
            gr.skip(),
            image_state,
        )
        return
    yield from respond(
        runtime,
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


def clear_conversation(runtime, request: gr.Request):
    """开启新对话：清空当前账号的聊天记录并持久化；成功后才重置摘要与界面状态。"""
    username = request.username if request and request.username else "unknown"
    try:
        runtime.history_store.clear(username)
    except Exception:
        # 清空失败：保留当前界面状态与摘要，等待用户重试；每日累计用量不受影响
        return (
            gr.update(),
            gr.update(),
            _CLEAR_FAILED_MSG,
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
        )
    runtime.history_compressor.reset(username)
    return (
        gr.update(value=[]),
        "",
        CLEAR_OK_MSG,
        "",
        new_usage_state(),
        dialogue_state.new_stage_state(),
        "（本轮无思考过程）",
        _new_image_state(),
    )


def render_usage_panel(runtime, request: gr.Request) -> str:
    """主人用量面板：仅本机（localhost）可见，展示今日全局/每账号用量与次数。"""
    ip = (request.client.host if request and request.client else None) or "unknown"
    if not is_owner_host(ip):
        return "🔒 （仅主人可见）"
    today = datetime.date.today().isoformat()
    try:
        global_used = runtime.daily_usage_store.get_total(today)
        per_account = runtime.daily_usage_store.get_account_usage(today)
        logins = runtime.daily_usage_store.get_logins(today)
        questions = runtime.daily_usage_store.get_questions(today)
    except DailyUsageReadError:
        # 读取失败明确提示，不显示零用量的正常面板
        return "⚠️ 用量面板读取失败，暂时无法显示今日用量。"
    lines = [
        f"**主人用量面板（{today}）**",
        f"- 全局已用：{global_used:,} / {runtime.global_daily_limit:,} tokens",
        f"- 登录次数：{logins}，提问次数：{questions}",
        f"- 账号数量：{len(runtime.accounts)}（清单见 data/accounts.json，可自行增删）",
        "**各账号用量：**",
    ]
    for account in runtime.accounts:
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
    runtime, request: gr.Request, account_name: str, new_password: str
) -> str:
    """主人重置指定账号的密码（仅本机可见）。"""
    ip = (request.client.host if request and request.client else None) or "unknown"
    if not is_owner_host(ip):
        return "🔒 （仅主人可见）"
    if not account_name:
        return "ℹ️ 请选择要重置的账号。"
    if not (new_password or "").strip():
        return "⚠️ 新密码不能为空。"
    if reset_password(account_name, new_password, path=runtime.accounts_path):
        runtime.accounts = load_accounts(runtime.accounts_path)
        runtime.event_logger.log_access_event("重置密码", f"账号={account_name}")
        return f"✅ 已重置账号 {account_name} 的密码。"
    return "⚠️ 账号不存在，请刷新页面后重试。"


def restore_history(runtime, request: gr.Request) -> tuple[list[dict], str]:
    """登录后加载当前账号上次的聊天记录；读取失败时明确提示，不伪装成空历史。"""
    username = request.username if request and request.username else "unknown"
    try:
        return runtime.history_store.load(username), ""
    except HistoryReadError as exc:
        return [], _RESTORE_FAILED_MSG.format(detail=exc)


def restore_level(runtime, request: gr.Request) -> str:
    """登录后把学段默认值设为账号配置的学段（页面加载时触发）。"""
    username = request.username if request and request.username else "unknown"
    account = get_account(username, path=runtime.accounts_path)
    level = account["level"] if account else "高中"
    if level not in ("小学", "初中", "高中", "大学"):
        level = "高中"
    return level


def toggle_answer_style(mode: str):
    """解答风格选项只在解答模式下显示。"""
    return gr.update(visible=(mode == MODE_ANSWER))


# ===================== 对话控制 =====================


def confirm_clear(
    runtime,
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
    ) = clear_conversation(runtime, request)
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


def on_provider_change(runtime, provider_id: str):
    """服务商切换时，动态更新模型列表与思考相关控件。

    服务商清单来自当前 runtime.all_providers，不重新读取默认配置，
    保证界面展示与回调使用同一份配置。
    """
    provider = get_provider(provider_id, runtime.all_providers)
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


def _bind(fn, runtime):
    """把 runtime 绑定为回调首参，并向 Gradio 暴露去掉首参后的签名。

    Gradio 按签名参数个数匹配组件输入、按注解注入 gr.Request；
    functools.wraps 保留的 __wrapped__ 签名含 runtime 首参，会造成输入错位，
    因此在包装器上显式设置去掉首参的 __signature__。
    注解仍由 wraps 复制，Request 的类型注解保留，注入逻辑不受影响。
    """
    if inspect.isgeneratorfunction(fn):
        @functools.wraps(fn)
        def bound(*args, **kwargs):
            yield from fn(runtime, *args, **kwargs)
    else:
        @functools.wraps(fn)
        def bound(*args, **kwargs):
            return fn(runtime, *args, **kwargs)
    signature = inspect.signature(fn)
    bound.__signature__ = signature.replace(
        parameters=list(signature.parameters.values())[1:]
    )
    return bound


class _SafeBlocks(gr.Blocks):
    """忽略 all_endpoints 查询参数的 Blocks。

    Gradio 首页处理在登录检查之外直接调用内部 api_info()，后者会把请求里的
    all_endpoints=true 传给 get_api_info，而该参数按设计无视 show_api=False，
    于是未认证首页仍能取到全部事件的参数元数据（含界面里的账号取值）。
    """

    def get_api_info(self, all_endpoints: bool = False) -> dict:
        return super().get_api_info(all_endpoints=False)


def create_app(runtime: AppRuntime) -> gr.Blocks:
    """显式创建界面：组装 Blocks、按运行对象取默认值并绑定全部事件。

    只创建界面与事件绑定，不启动服务器、备份任务、隧道或写访问信息；
    组件默认值与回调依赖全部来自传入的 runtime，不保留模块级状态。
    """
    with _SafeBlocks(
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
                        value=runtime.settings.thinking if runtime.settings else False,
                        label="深度思考模式（更严谨，稍慢）",
                    )
                with gr.Accordion("⚙️ 高级设置", open=False):
                    level_radio = gr.Radio(
                        ["小学", "初中", "高中", "大学"], value="高中", label="学段"
                    )
                    provider_radio = gr.Radio(
                          choices=[(p["name"], p["id"]) for p in runtime.ui_providers],
                          value=(runtime.default_provider["id"] if runtime.default_provider else None),
                          label="服务商",
                      )
                    model_radio = gr.Radio(
                        (runtime.default_provider.get("models") if runtime.default_provider else ["deepseek-v4-flash", "deepseek-v4-pro"]),
                        value=(
                            runtime.default_provider.get("default_model")
                            if runtime.default_provider
                            else (runtime.settings.model if runtime.settings else "deepseek-v4-flash")
                        ),
                        label="模型",
                    )
                    effort_radio = gr.Radio(
                        ["low", "high", "max"],
                        value=runtime.settings.reasoning_effort if runtime.settings else "high",
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
                      value=(runtime.default_provider["id"] if runtime.default_provider else None)
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
                            choices=[a["username"] for a in runtime.accounts],
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
            _bind(on_image_upload, runtime),
            inputs=[image_input, provider_radio],
            outputs=[textbox, result_status, image_input, image_state],
        )
        image_input.clear(
            clear_image_state,
            inputs=None,
            outputs=[image_state],
        )

        send_event = send_btn.click(
            _bind(respond, runtime),
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
            _bind(respond, runtime),
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
            _bind(continue_last_answer, runtime),
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
            _bind(confirm_clear, runtime),
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
        refresh_btn.click(_bind(render_usage_panel, runtime), inputs=[], outputs=[usage_panel])
        reset_pwd_btn.click(
            _bind(reset_account_password, runtime),
            inputs=[reset_account_dropdown, reset_pwd_textbox],
            outputs=[reset_pwd_status],
        )
        provider_radio.change(
              _bind(on_provider_change, runtime),
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
        demo.load(_bind(restore_history, runtime), outputs=[chatbot, result_status], show_progress="hidden")
        demo.load(_bind(restore_level, runtime), outputs=[level_radio], show_progress="hidden")
    # 收敛端点元数据：show_api=False 后 /info、openapi 与登录页内嵌的
    # gradio_api_info 不再包含各事件的参数清单（含账号用户名等取值）；
    # all_endpoints=true 的绕过由 _SafeBlocks 忽略该参数关闭；
    # 界面事件走队列按事件索引调用，不受影响。
    for fn in demo.fns.values():
        fn.show_api = False
    return demo


SERVER_PORT = 7860  # 集中现有启动端口常量，仅作隔离测试的替换点，不作为生产配置项


def main() -> None:
    """启动边界：组装依赖 → 创建界面 → 启动服务器与配套任务 → 等待退出 → 清理。

    启动异常原样向外传播（保留失败退出码与诊断信息）；
    finally 只收尾本次启动实际拥有的资源：先置备份停止信号，再停隧道、
    关闭界面服务器，最后等待备份线程（最多 5 秒，正在写盘的备份不保证完成）。
    """
    runtime = build_runtime()
    demo = create_app(runtime)
    print(f"数学学习 AI 助手 v{APP_VERSION} 启动中…")
    auto_open = os.getenv("AUTO_OPEN_BROWSER", "true").strip().lower() != "false"
    server_name = os.getenv("GRADIO_SERVER_NAME", "0.0.0.0").strip()
    print("访问账号（固定密码，完整清单见 data/accounts.json）：")
    for account in runtime.accounts:
        print(f"  {account['username']} / {account['password']}")
    backup_path = ensure_today_backup()
    if backup_path:
        print(f"数据备份：已生成 {backup_path}")
    backup_stop = threading.Event()
    backup_thread = threading.Thread(
        target=daily_backup_loop, kwargs={"stop_event": backup_stop}, daemon=True
    )
    backup_thread.start()
    watcher = None
    try:
        demo.queue(default_concurrency_limit=4).launch(
            server_name=server_name,
            server_port=SERVER_PORT,
            inbrowser=auto_open,
            auth=_bind(_check_login, runtime),
            auth_message="请输入访问用户名与密码",
            prevent_thread_lock=True,
        )
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
                save_access_info(url, runtime.accounts)
                print(f"公网链接已更新：{url}（已写入访问信息文件）")

            watcher = TunnelWatcher(cloudflared_path, on_url=_on_url)
            watcher.start()
            if watcher.url:
                print(f"公网链接：{watcher.url}")
            else:
                print("公网隧道建立失败（连接 Cloudflare 超时），仍可通过局域网访问。")
        access_file = save_access_info(watcher.url if watcher else None, runtime.accounts)
        print(f"局域网地址：http://{get_lan_ip()}:{SERVER_PORT}")
        print(f"访问信息已保存到：{access_file}")
        demo.block_thread()
    finally:
        backup_stop.set()
        try:
            if watcher is not None:
                watcher.stop()
        finally:
            try:
                demo.close()
            finally:
                backup_thread.join(timeout=5)


if __name__ == "__main__":
    main()
