


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