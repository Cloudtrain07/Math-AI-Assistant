"""开发专用假模型客户端：仅 `python api.py --fake` 时替换全部可选中客户端。

供浏览器联调走完整链路（新前端 → 新 API → 真实核心服务 → 假客户端），
不调用任何真实模型 API。正常启动不导入本模块，聊天页面不含任何 mock 播放逻辑。

能力对齐：每个假客户端的 supports_vision 取自对应服务商配置的声明，
不把所有服务商都伪装成视觉模型；因此「支持图片的服务商」在假链路下
照样能收到编辑后的图片消息，而其余服务商仍按真实语义拒绝图片。
"""

from __future__ import annotations

import time
from types import SimpleNamespace


def _image_parts(content: object) -> int:
    """统计一条多模态 user 消息里的图片部分数量（纯文本返回 0）。"""
    if not isinstance(content, list):
        return 0
    return sum(
        1
        for part in content
        if isinstance(part, dict) and part.get("type") == "image_url"
    )


class FakeStreamClient:
    """按现有 LLMClient 接口输出固定的流式公式回答。

    - supports_vision 由服务商声明决定（见 build_fake_clients）；
    - 记录每次收到的 messages 与累计收到的图片部分数量，供验收确认
      「假客户端实际收到编辑后的图片消息」；
    - 回答在早期即完成一个块级公式，随后继续追加多段文字与后续公式，
      推理文本同样含公式，便于对回答与思考区分别做公式稳定性观测。
    """

    model = "fake-math-model"

    def __init__(
        self,
        *,
        supports_vision: bool = False,
        provider_id: str = "fake",
        piece_delay: float = 0.02,
    ) -> None:
        self.supports_vision = supports_vision
        self.provider_id = provider_id
        self.piece_delay = piece_delay
        self.calls = 0
        self.last_messages: list[dict] = []
        self.received_image_parts = 0

    def chat_stream_with_usage(
        self,
        messages: list[dict],
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
        model: str | None = None,
    ):
        self.calls += 1
        self.last_messages = [dict(m) for m in messages]

        user_turns = sum(1 for m in messages if m.get("role") == "user")
        last_user_message = next(
            (m for m in reversed(messages) if m.get("role") == "user"), None
        )
        content = last_user_message.get("content") if last_user_message else ""
        image_parts = _image_parts(content)
        self.received_image_parts += image_parts
        if isinstance(content, list):
            # 多模态消息只回显文本部分，不回显图片 data URL
            last_user = " ".join(
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        else:
            last_user = content or ""

        image_note = f"（本轮收到 {image_parts} 张图片）" if image_parts else ""
        answer = (
            f"（假模型 · 第 {user_turns} 轮）{image_note}\n\n"
            f"你问的是：{last_user}\n\n"
            "分步计算：\n\n"
            "1. 先列出条件\n"
            "2. 代入公式 $S = \\pi r^2$\n\n"
            "$$\nS = 3.14 \\times 2^2 = 12.56\n$$\n\n"
            "所以半径为 2 的圆面积约为 $12.56$。\n\n"
            "补充一：若半径变为 3，则 $S = \\pi \\times 3^2 = 28.26$。\n\n"
            "补充二：一般地，面积随半径平方增长：\n\n"
            "$$\nS(r) = \\pi r^2 \\quad (r > 0)\n$$\n\n"
            "结论：记住公式 $S = \\pi r^2$ 即可。"
        )
        reasoning = (
            "先判断题型的分类，再用公式 $S = \\pi r^2$ 估算。\n\n"
            "先在草稿上验证 $\\frac{d}{dx}x^2 = 2x$，"
            "再确认 $\\int_0^1 x^2\\,dx = \\frac{1}{3}$ 这一步。"
            if thinking
            else ""
        )
        usage = SimpleNamespace(
            total_tokens=100 + 20 * user_turns,
            prompt_tokens=60 + 10 * user_turns,
            completion_tokens=40 + 10 * user_turns,
        )

        if reasoning:
            for i in range(0, len(reasoning), 6):
                yield None, reasoning[i : i + 6], None, None
                time.sleep(self.piece_delay)
        for i in range(0, len(answer), 6):
            yield answer[i : i + 6], None, None, None
            time.sleep(self.piece_delay)
        yield None, None, usage, "stop"


def build_fake_clients(
    providers: list[dict], *, piece_delay: float = 0.02
) -> dict[str, FakeStreamClient]:
    """按服务商清单构造假客户端字典：能力（supports_vision）与服务商声明一致。"""
    return {
        provider["id"]: FakeStreamClient(
            supports_vision=bool(provider.get("supports_vision", False)),
            provider_id=provider["id"],
            piece_delay=piece_delay,
        )
        for provider in providers
        if provider.get("id")
    }
