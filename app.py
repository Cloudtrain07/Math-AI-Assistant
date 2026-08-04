"""数学学习 AI 助手：Gradio 界面 + DeepSeek 大模型 + 拍照识题 + 学习功能。"""

from __future__ import annotations

import datetime
import os
from pathlib import Path

import gradio as gr

from core.calculator import CALCULATOR_AVAILABLE, verify_math
from core.config import get_settings
from core.deepseek_client import DeepSeekClient
from core.math_format import normalize_math_text
from core.prompts import VISION_COMPLETE_PROMPT, build_system_prompt
from core.storage import save_history
from core.vision_client import VisionClient

APP_VERSION = "2026-08-05"
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

# 视觉客户端（可选）：未配置 DASHSCOPE_API_KEY 时，上传图片会给出提示
if settings is not None and settings.vision_api_key:
    vision_client = VisionClient(settings)
else:
    vision_client = None


def to_api_messages(
    history: list[dict], level: str, mode: str, answer_style: str | None = None
) -> list[dict[str, str]]:
    """把界面历史转换为 DeepSeek 需要的消息列表，并控制上下文长度。"""
    messages: list[dict[str, str]] = [
        {"role": "system", "content": build_system_prompt(level, mode, answer_style)}
    ]
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


def _log_event(kind: str, text: str) -> None:
    """把事件与文本追加写入 data/chat.log，用于排查发送链路完整性。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        head = text[:40].replace("\n", "⏎")
        tail = text[-40:].replace("\n", "⏎")
        line = f"[{ts}] {kind} 长度={len(text)} 开头={head!r} 结尾={tail!r}\n"
        with open(DATA_DIR / "chat.log", "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def _recognition_incomplete(text: str) -> bool:
    """粗略判断识别文本是否在公式中途被截断（奇数个 $ 或结尾是公式残片）。"""
    if not text:
        return False
    stripped = text.rstrip()
    if stripped.endswith(("\\", "{", "}")):
        return True
    return stripped.count("$") % 2 == 1


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
    answer_style: str | None,
):
    """纯文本聊天：按学段/模式组装提示词，流式回复并记录 token 用量。"""
    history = list(history or [])
    if not message.strip():
        yield history, "", "💡 请先输入问题，或上传题目图片等待自动识别。"
        return

    user_content = normalize_math_text(message, auto_close=True)
    _log_event("发送", user_content)
    if _recognition_incomplete(message):
        _log_event("发送-原始疑似不完整", message)
    history.append({"role": "user", "content": user_content})
    if client is None:
        yield history + [{"role": "assistant", "content": f"⚠️ {startup_hint}"}], "", ""
        return

    answer = ""
    usage = None
    truncated = False
    for piece, chunk_usage, finish_reason in client.chat_stream_with_usage(
        to_api_messages(history, level, mode, answer_style), thinking=thinking
    ):
        if piece:
            answer += piece
        if chunk_usage is not None:
            usage = chunk_usage
        if finish_reason == "length":
            truncated = True
        display = normalize_math_text(answer)
        yield history + [{"role": "assistant", "content": display}], "", ""

    if not answer:
        yield history + [
            {"role": "assistant", "content": "（没有收到回复，请检查 API Key、余额或网络）"}
        ], "", ""
        return

    # 最终展示启用自动闭合：模型偶发漏写尾部 $$ 时补上，保证公式能渲染
    display = normalize_math_text(answer, auto_close=True)
    final_history = history + [{"role": "assistant", "content": display}]
    save_history(final_history)
    status_text = format_usage(usage)
    if truncated:
        status_text += "\n\n⚠️ 回答因长度限制被截断，可以回复「继续」让我接着写。"
    yield final_history, "", status_text


def clear_conversation():
    """开启新对话：清空聊天记录并持久化。"""
    save_history([])
    return gr.update(value=[]), "", "🗑️ 已开启新对话。"


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
    """解答风格选项只在直接解答模式下显示。"""
    return gr.update(visible=(mode == "直接解答模式"))


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
            ["讲解模式", "重点互动模式", "直接解答模式", "练习模式"],
            value="讲解模式",
            label="模式",
        )
        thinking_checkbox = gr.Checkbox(
            value=settings.thinking if settings else False,
            label="深度思考模式（更严谨，稍慢）",
        )
    with gr.Row():
        answer_style_radio = gr.Radio(
            ["完整步骤+答案", "简略过程+答案", "只给答案和结论"],
            value="完整步骤+答案",
            label="解答风格（仅直接解答模式）",
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
            send_btn = gr.Button("发送", variant="primary")
    with gr.Row():
        clear_btn = gr.Button("🗑️ 新对话")
        stop_btn = gr.Button("⏹️ 停止生成")
        verify_btn = gr.Button("🧮 验算上一条回答")
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
            answer_style_radio,
        ],
        outputs=[chatbot, textbox, status],
    )
    submit_event = textbox.submit(
        respond,
        inputs=[
            textbox,
            chatbot,
            level_radio,
            mode_radio,
            thinking_checkbox,
            answer_style_radio,
        ],
        outputs=[chatbot, textbox, status],
    )
    stop_btn.click(None, None, None, cancels=[send_event, submit_event])
    clear_btn.click(clear_conversation, outputs=[chatbot, textbox, status])
    verify_btn.click(verify_last_answer, inputs=[chatbot], outputs=[status])
    mode_radio.change(
        toggle_answer_style,
        inputs=[mode_radio],
        outputs=[answer_style_radio],
    )


if __name__ == "__main__":
    print(f"数学学习 AI 助手 v{APP_VERSION} 启动中…")
    auto_open = os.getenv("AUTO_OPEN_BROWSER", "true").strip().lower() != "false"
    server_name = os.getenv("GRADIO_SERVER_NAME", "0.0.0.0").strip()
    demo.queue().launch(
        server_name=server_name,
        server_port=7860,
        inbrowser=auto_open,
    )
