
from core import message_builder


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