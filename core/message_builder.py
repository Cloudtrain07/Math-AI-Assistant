"""构造发送给大模型 API 的消息列表"""

from core.prompts import build_system_prompt


def to_api_messages(
    history: list[dict],
    level: str,
    mode: str,
    answer_style: str | None = None,
    stage: str | None = None,
    mode_changed: bool = False,
    summary: str | None = None,
    image_data_url: str | None = None,
    max_history_messages: int | None = None,
) -> list[dict]:
    """把界面历史转换为 API 消息列表，并控制上下文长度。

    image_data_url 非空时，最后一条 user 消息以多模态 content 数组发送
    （原生视觉直传）；历史本身始终保持纯文本。
    """
    messages: list[dict] = [
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
    if max_history_messages is not None:
        history = history[-max_history_messages:]
    for item in history:
        if isinstance(item.get("content"), str) and item.get("role") in (
            "user",
            "assistant",
        ):
            messages.append({"role": item["role"], "content": item["content"]})
    if image_data_url and messages[-1]["role"] == "user":
        text_content = messages[-1]["content"]
        messages[-1] = {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": image_data_url}},
                {"type": "text", "text": text_content},
            ],
        }
    return messages