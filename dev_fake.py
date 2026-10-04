"""开发专用假模型客户端：仅 `python api.py --fake` 时替换 runtime.client。

供浏览器联调走完整链路（新前端 → 新 API → 真实核心服务 → 假客户端），
不调用任何真实模型 API。正常启动不导入本模块，聊天页面不含任何 mock 播放逻辑。
"""

from __future__ import annotations

import time
from types import SimpleNamespace


class FakeStreamClient:
    """按现有 LLMClient 接口输出固定的流式公式回答。"""

    supports_vision = False
    model = "fake-math-model"

    def __init__(self, piece_delay: float = 0.02) -> None:
        self.piece_delay = piece_delay
        self.calls = 0
        self.last_messages: list[dict] = []

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
        last_user = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"), ""
        )
        answer = (
            f"（假模型 · 第 {user_turns} 轮）\n\n"
            f"你问的是：{last_user}\n\n"
            "分步计算：\n\n"
            "1. 先列出条件\n"
            "2. 代入公式 $S = \\pi r^2$\n\n"
            "$$\nS = 3.14 \\times 2^2 = 12.56\n$$\n\n"
            "所以半径为 2 的圆面积约为 $12.56$。"
        )
        reasoning = "先判断题型，再选择公式，最后代入数值验算。" if thinking else ""
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
