
from core import message_builder
from core import dialogue_state
from core.math_format import normalize_math_text
from core.vision_client import image_to_data_url
from core.usage import (
    estimate_tokens,
    get_usage_part,
    get_usage_total,
    record_usage,
    check_daily_quota,
)
from core.usage_store import DailyUsageReadError

def build_continue_message(history: list[dict], image_note: str) -> str | None:
    """根据最近一条用户消息，生成继续回答的指令。"""
    for item in reversed(history):
        content = item.get("content")
        if item.get("role") != "user" or not isinstance(content, str) or not content.strip():
            continue

        last_user = content.replace(f"\n{image_note}", "").replace(image_note, "")
        if not last_user:
            return None
        return f"{last_user}\n（请继续完成上一条回答）"

    return None


def prepare_api_messages(
    username: str,
    history: list[dict],
    level: str,
    mode: str,
    answer_style: str | None,
    active_client,
    history_compressor,
    image_data_url: str | None,
    stage: str | None,
    mode_changed: bool,
    max_history_messages: int | None,
) -> list[dict]:
    """准备发送给 API 的消息列表。"""
    summary = ""
    if max_history_messages is not None and len(history) > max_history_messages:
        summary = history_compressor.get_summary(username, history, max_history_messages, active_client)
    return message_builder.to_api_messages(
        history,
        level,
        mode,
        answer_style,
        stage=stage,
        mode_changed=mode_changed,
        summary=summary,
        image_data_url=image_data_url,
        max_history_messages=max_history_messages,
    )


def stream_reply(
    active_client,
    messages: list[dict],
    thinking: bool,
    reasoning_effort: str,
    model: str,
    max_tokens: int | None = None,
    max_wait_seconds: int | float | None = None,
):
    """调用模型，并在每次收到片段后交出当前的累计结果。

    max_tokens / max_wait_seconds 为网页覆盖值（None 表示沿用配置）。
    """
    answer = ""
    reasoning_text = ""
    usage = None
    truncated = False

    for piece, reasoning_piece, chunk_usage, finish_reason in (
        active_client.chat_stream_with_usage(
            messages,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
            model=model,
            max_tokens=max_tokens,
            max_wait_seconds=max_wait_seconds,
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

        yield {
            "answer": answer,
            "reasoning_text": reasoning_text,
            "usage": usage,
            "truncated": truncated,
        }


def route_image_for_turn(
    message: str,
    history: list[dict],
    image_state: dict,
    previous_stage: str | None,
    supports_vision: bool,
    force_continue: bool,
    attach_image: bool,
    image_note: str,
) -> dict:
    """决定本轮是否附图，并返回更新后的历史、图片状态和提示。"""
    new_history = list(history)
    new_image_state = dict(image_state)
    image_data_url = None
    image_status = ""
    reject_message = None

    image_path = image_state.get("path")
    fresh_image = bool(image_state.get("fresh"))

    if image_path and not force_continue and attach_image:
        looks_like_new = dialogue_state.looks_like_new_problem(message)
        stale_image = (
            previous_stage in ("summary", "full_solution", "grading")
            or previous_stage is None
        ) and looks_like_new

        if stale_image and not fresh_image:
            # 上一题留下的旧图，不带到新题中
            new_image_state = {"path": None, "fresh": False}

        elif supports_vision:
            try:
                image_data_url = image_to_data_url(image_path)
            except Exception as exc:
                # 读图失败仍继续发送文字
                image_status = f"⚠️ 图片读取失败（{exc}），已按纯文本发送。"
                new_image_state = {"path": None, "fresh": False}
            else:
                new_image_state = {"path": image_path, "fresh": False}
                new_history[-1] = {
                    "role": "user",
                    "content": f"{new_history[-1]['content']}\n{image_note}",
                }

        else:
            # 模型不支持看图：拒绝本轮请求，保留待发图片
            reject_message = (
                "⚠️ 当前服务商不支持图片输入，"
                "请切换到支持原生视觉的服务商后重试。"
            )

    return {
        "history": new_history,
        "image_data_url": image_data_url,
        "image_state": new_image_state,
        "image_status": image_status,
        "reject_message": reject_message,
    }


def prepare_user_message(
    message: str,
    has_pending_image: bool,
    max_message_len: int,
    image_only_prompt: str,
) -> dict:
    """准备本轮提问文字，返回文字和拒绝原因。"""
    if not message.strip():
        if not has_pending_image:
            return {"message": message, "error": "empty"}
        message = image_only_prompt

    if len(message) > max_message_len:
        return {"message": message, "error": "too_long"}

    return {"message": message, "error": None}


def settle_reply_usage(
    usage_state: dict,
    usage,
    answer: str,
    *,
    interrupted: bool = False,
) -> tuple[int | None, dict]:
    """结算本轮用量，不负责文件保存或界面输出。"""
    turn_total = get_usage_total(usage)

    # 只有异常中断、没有模型用量但已有回答时，才估算。
    if interrupted and usage is None and answer:
        turn_total = estimate_tokens(answer)

    new_usage_state = record_usage(
        usage_state,
        turn_total,
        prompt_tokens=get_usage_part(usage, "prompt_tokens"),
        completion_tokens=get_usage_part(usage, "completion_tokens"),
    )

    return turn_total, new_usage_state


def build_answer_history(history: list[dict], answer: str) -> list[dict] | None:
    """为已生成的回答构建可保存的历史；空回答不构建历史。"""
    if not answer:
        return None

    display = normalize_math_text(answer, auto_close=True)
    return history + [{"role": "assistant", "content": display}]


def record_daily_usage(
    date: str,
    username: str,
    turn_total: int,
    *,
    daily_usage_store,
    event_logger,
) -> dict:
    """记录每日用量，返回累计数字和处理状态；失败不重试。"""
    try:
        daily_used = daily_usage_store.add_usage(date, username, turn_total)
    except Exception as exc:
        event_logger.log_access_event(
            "每日用量记录失败",
            f"账号={username}",
            f"错误={exc.__class__.__name__}",
        )
        return {
            "daily_used": None,
            "global_used": None,
            "status": "record_failed",
        }

    try:
        global_used = daily_usage_store.get_total(date)
    except Exception as exc:
        event_logger.log_access_event(
            "全局统计刷新失败",
            f"账号={username}",
            f"错误={exc.__class__.__name__}",
        )
        return {
            "daily_used": daily_used,
            "global_used": None,
            "status": "stats_failed",
        }

    return {
        "daily_used": daily_used,
        "global_used": global_used,
        "status": "recorded",
    }


def _save_answer_history(
    history: list[dict],
    answer: str,
    *,
    username: str,
    turn_token: int,
    history_store,
) -> tuple[list[dict], str | None]:
    """构建并尝试保存回答；空回答不保存，失败不重试。"""
    final_history = build_answer_history(history, answer)
    if final_history is None:
        return history, None

    try:
        saved = history_store.save(
            final_history,
            username=username,
            turn_token=turn_token,
        )
    except Exception:
        return final_history, "failed"

    return final_history, "saved" if saved else "stale"


def finish_chat_turn(
    history: list[dict],
    answer: str,
    usage,
    usage_state: dict,
    *,
    username: str,
    date: str,
    turn_token: int,
    history_store,
    daily_usage_store,
    event_logger,
    truncated: bool = False,
    error: Exception | None = None,
) -> dict:
    """处理聊天收尾，保留正常与异常情况下的保存顺序。"""
    final_history = history
    save_status = None

    # 正常结束：先尝试保存，再结算。
    if error is None:
        final_history, save_status = _save_answer_history(
            history,
            answer,
            username=username,
            turn_token=turn_token,
            history_store=history_store,
        )

    turn_total, new_usage_state = settle_reply_usage(
        usage_state,
        usage,
        answer,
        interrupted=error is not None,
    )

    if error is not None:
        event_logger.log_error(
            error,
            account=username,
            turn_total=turn_total,
            partial=bool(answer),
        )

    daily_result = None
    if turn_total is not None:
        daily_result = record_daily_usage(
            date=date,
            username=username,
            turn_total=turn_total,
            daily_usage_store=daily_usage_store,
            event_logger=event_logger,
        )
        daily_used = daily_result["daily_used"]
        global_used = daily_result["global_used"]

        if error is None and answer and daily_used is not None:
            if global_used is not None:
                event_logger.log_access_event(
                    "回复",
                    f"账号={username}",
                    f"tokens={turn_total}",
                    f"本账号={daily_used}",
                    f"全局={global_used}",
                )
            else:
                event_logger.log_access_event(
                    "回复",
                    f"账号={username}",
                    f"tokens={turn_total}",
                    f"本账号={daily_used}",
                )

    if error is not None:
        daily_used = (
            daily_result["daily_used"]
            if daily_result is not None
            else None
        )

        if daily_used is not None:
            event_logger.log_access_event(
                "回复失败",
                f"账号={username}",
                f"错误={error.__class__.__name__}",
                f"估算tokens={turn_total}",
                f"本账号={daily_used}",
            )
        else:
            event_logger.log_access_event(
                "回复失败",
                f"账号={username}",
                f"错误={error.__class__.__name__}",
            )

        # 异常中断：结算和日志完成后，再尝试保存部分回答。
        final_history, save_status = _save_answer_history(
            history,
            answer,
            username=username,
            turn_token=turn_token,
            history_store=history_store,
        )
    elif answer:
        event_logger.log_chat_event("回复", answer)
    else:
        event_logger.log_empty_reply(
            account=username,
            turn_total=turn_total,
            truncated=truncated,
        )

    return {
        "history": final_history,
        "usage_state": new_usage_state,
        "turn_total": turn_total,
        "save_status": save_status,
        "daily_usage": daily_result,
        "error": error,
    }


def generate_reply(
    history: list[dict],
    usage_state: dict,
    *,
    username: str,
    date: str,
    turn_token: int,
    level: str,
    mode: str,
    answer_style: str | None,
    stage: str | None,
    mode_changed: bool,
    thinking: bool,
    reasoning_effort: str,
    model: str,
    image_data_url: str | None,
    max_history_messages: int | None,
    max_output_tokens: int | None = None,
    max_wait_seconds: int | None = None,
    active_client,
    history_compressor,
    history_store,
    daily_usage_store,
    event_logger,
):
    """准备上下文、流式生成并收尾，交出进度事件和最终事件。"""
    reply = {
        "answer": "",
        "reasoning_text": "",
        "usage": None,
        "truncated": False,
    }
    error = None

    try:
        messages = prepare_api_messages(
            username=username,
            history=history,
            level=level,
            mode=mode,
            answer_style=answer_style,
            active_client=active_client,
            history_compressor=history_compressor,
            image_data_url=image_data_url,
            stage=stage,
            mode_changed=mode_changed,
            max_history_messages=max_history_messages,
        )

        for reply in stream_reply(
            active_client,
            messages,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
            model=model,
            max_tokens=max_output_tokens,
            max_wait_seconds=max_wait_seconds,
        ):
            yield {"type": "stream", "reply": reply}

    except Exception as exc:
        error = exc

    result = finish_chat_turn(
        history=history,
        answer=reply["answer"],
        usage=reply["usage"],
        usage_state=usage_state,
        username=username,
        date=date,
        turn_token=turn_token,
        history_store=history_store,
        daily_usage_store=daily_usage_store,
        event_logger=event_logger,
        truncated=reply["truncated"],
        error=error,
    )

    yield {"type": "final", "reply": reply, "result": result}


def _recognition_incomplete(text: str) -> bool:
    """沿用现有规则，判断原始文字是否疑似在公式中途截断。"""
    if not text:
        return False
    stripped = text.rstrip()
    if stripped.endswith(("\\", "{", "}")):
        return True
    return stripped.count("$") % 2 == 1


def prepare_chat_turn(
    message: str,
    history: list[dict],
    *,
    mode: str,
    stage_state: dict | None,
    image_state: dict | None,
    username: str,
    ip: str,
    date: str,
    active_client,
    history_store,
    daily_usage_store,
    event_logger,
    max_message_len: int,
    daily_limit: int,
    global_daily_limit: int,
    image_only_prompt: str,
    image_note: str,
    force_continue: bool = False,
    attach_image: bool = True,
    turn_token: int | None = None,
) -> dict:
    """检查输入和额度，推进阶段、处理图片，并取得本轮保存标记。

    turn_token 缺省时在本准备流程末尾调用 history_store.start_turn 取标记
    （原行为）；显式传入时直接复用调用方预先取得的标记——API 层需要把
    "取得标记"与清空守卫做成原子操作，保证清空前开始的轮次在清空后
    保存必然 stale，不能在清空后重新取得新的有效保存资格。
    """
    new_history = list(history or [])
    prepared_stage = dialogue_state.prepare_stage_state(mode, stage_state)
    new_image_state = dict(
        image_state or {"path": None, "fresh": False}
    )
    new_image_state.setdefault("path", None)
    new_image_state.setdefault("fresh", False)

    input_result = prepare_user_message(
        message=message,
        has_pending_image=(
            bool(new_image_state.get("path")) and not force_continue
        ),
        max_message_len=max_message_len,
        image_only_prompt=image_only_prompt,
    )
    message = input_result["message"]

    result = {
        "status": "ready",
        "message": message,
        "history": new_history,
        "stage_state": prepared_stage,
        "image_state": new_image_state,
        "image_data_url": None,
        "image_status": "",
        "reject_message": None,
        "turn_token": None,
    }

    if input_result["error"] is not None:
        result["status"] = input_result["error"]
        if input_result["error"] == "too_long":
            event_logger.log_access_event(
                "过长拒绝",
                f"账号={username}",
                f"IP={ip}",
                f"长度={len(message)}",
            )
        return result

    try:
        daily_used = daily_usage_store.get_used(date, username)
        global_used = daily_usage_store.get_total(date)
    except DailyUsageReadError:
        event_logger.log_access_event(
            "额度检查失败", f"账号={username}"
        )
        result["status"] = "quota_read_failed"
        return result

    quota_message = check_daily_quota(
        daily_used, global_used, daily_limit, global_daily_limit
    )
    if quota_message is not None:
        event_logger.log_access_event(
            "超额拒绝",
            f"账号={username}",
            f"本账号={daily_used}",
            f"全局={global_used}",
        )
        result["status"] = "quota"
        result["reject_message"] = quota_message
        return result

    new_stage = dialogue_state.advance_stage(
        mode,
        message,
        prepared_stage,
        has_history=bool(new_history),
        force_continue=force_continue,
    )
    result["stage_state"] = new_stage

    user_content = normalize_math_text(message, auto_close=True)
    event_logger.log_chat_event("发送", user_content)
    event_logger.log_access_event(
        "提问",
        f"账号={username}",
        f"IP={ip}",
        f"长度={len(message)}",
    )
    try:
        daily_usage_store.add_question(date)
    except Exception:
        event_logger.log_access_event(
            "提问次数记录失败", f"账号={username}"
        )
    if _recognition_incomplete(message):
        event_logger.log_chat_event("发送-原始疑似不完整", message)

    new_history.append({"role": "user", "content": user_content})

    if active_client is None:
        result["status"] = "no_client"
        return result

    image_result = route_image_for_turn(
        message=message,
        history=new_history,
        image_state=new_image_state,
        previous_stage=prepared_stage.get("stage"),
        supports_vision=bool(
            getattr(active_client, "supports_vision", False)
        ),
        force_continue=force_continue,
        attach_image=attach_image,
        image_note=image_note,
    )
    result["history"] = image_result["history"]
    result["image_state"] = image_result["image_state"]
    result["image_data_url"] = image_result["image_data_url"]
    result["image_status"] = image_result["image_status"]

    if image_result["reject_message"] is not None:
        result["status"] = "image_rejected"
        result["reject_message"] = image_result["reject_message"]
        return result

    if turn_token is None:
        result["turn_token"] = history_store.start_turn(username)
    else:
        result["turn_token"] = turn_token
    return result


def run_chat_turn(
    message: str,
    history: list[dict],
    usage_state: dict,
    *,
    username: str,
    ip: str,
    date: str,
    level: str,
    mode: str,
    stage_state: dict | None,
    image_state: dict | None,
    thinking: bool,
    model: str,
    reasoning_effort: str,
    answer_style: str | None,
    active_client,
    history_compressor,
    history_store,
    daily_usage_store,
    event_logger,
    max_message_len: int,
    max_history_messages: int | None,
    daily_limit: int,
    global_daily_limit: int,
    image_only_prompt: str,
    image_note: str,
    force_continue: bool = False,
    attach_image: bool = True,
    turn_token: int | None = None,
    max_output_tokens: int | None = None,
    max_wait_seconds: int | None = None,
):
    """从原始提问开始，交出拒绝、准备完成、流式和最终事件。

    turn_token 语义同 prepare_chat_turn：缺省由核心自取，传入则复用。
    """
    prepared = prepare_chat_turn(
        message=message,
        history=history,
        mode=mode,
        stage_state=stage_state,
        image_state=image_state,
        username=username,
        ip=ip,
        date=date,
        active_client=active_client,
        history_store=history_store,
        daily_usage_store=daily_usage_store,
        event_logger=event_logger,
        max_message_len=max_message_len,
        daily_limit=daily_limit,
        global_daily_limit=global_daily_limit,
        image_only_prompt=image_only_prompt,
        image_note=image_note,
        force_continue=force_continue,
        attach_image=attach_image,
        turn_token=turn_token,
    )

    if prepared["status"] != "ready":
        yield {"type": "rejected", "turn": prepared}
        return

    yield {"type": "ready", "turn": prepared}

    for event in generate_reply(
        history=prepared["history"],
        usage_state=usage_state,
        username=username,
        date=date,
        turn_token=prepared["turn_token"],
        level=level,
        mode=mode,
        answer_style=answer_style,
        stage=prepared["stage_state"]["stage"],
        mode_changed=prepared["stage_state"]["mode_changed"],
        thinking=thinking,
        reasoning_effort=reasoning_effort,
        model=model,
        image_data_url=prepared["image_data_url"],
        max_history_messages=max_history_messages,
        active_client=active_client,
        history_compressor=history_compressor,
        history_store=history_store,
        daily_usage_store=daily_usage_store,
        event_logger=event_logger,
        max_output_tokens=max_output_tokens,
        max_wait_seconds=max_wait_seconds,
    ):
        event["turn"] = prepared
        yield event
