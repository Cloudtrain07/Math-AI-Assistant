
from core import message_builder
from core import dialogue_state
from core.vision_client import image_to_data_url

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
):
    """调用模型，并在每次收到片段后交出当前的累计结果。"""
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
