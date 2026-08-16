"""上下文压缩：把较早的对话增量压缩成摘要，降低长会话的 token 成本。

设计：
- 每个账号维护一份摘要和已摘要的消息条数；
- 当历史超过窗口大小后，每次只把“新掉出窗口”的消息合并进旧摘要；
- 摘要通过 DeepSeek 生成，失败时回退为简单截断，不让对话中断。
"""

from __future__ import annotations

import threading

SUMMARY_SYSTEM_PROMPT = """你是一个对话历史压缩助手。请把下面较早的数学学习对话压缩成一段简洁中文摘要。

要求：
1. 保留关键信息：当前正在做的题目、已讨论到哪一步、用户的卡点/错误、已经给出的重要结论或公式。
2. 不要新编内容，不要写完整解答，不要遗漏后续对话仍然需要的上下文。
3. 如果已经有旧摘要，把旧摘要和新消息合并成一份更完整的摘要，而不是重复旧摘要。
4. 总长度控制在 500 字以内。
"""


class HistoryCompressor:
    """按账号增量维护对话摘要。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # username -> {"summary": str, "count": int}
        self._states: dict[str, dict] = {}

    def reset(self, username: str) -> None:
        """清空指定账号的摘要（新对话/清空历史时调用）。"""
        with self._lock:
            self._states.pop(username, None)

    def get_summary(
        self,
        username: str,
        history: list[dict],
        window_size: int,
        client,
    ) -> str:
        """返回该账号当前应注入的压缩摘要；没有可压缩内容时返回空串。

        history 应为已经包含本轮用户消息的完整消息列表。
        """
        if not history or window_size <= 0:
            return ""
        with self._lock:
            state = self._states.get(username, {"summary": "", "count": 0})
            old_count = max(0, len(history) - window_size)
            if old_count <= state["count"]:
                return state["summary"]

        # 在锁外调用模型，避免长压缩阻塞其他账号的摘要读取
        new_messages = history[state["count"]:old_count]
        summary = self._summarize(client, state["summary"], new_messages)
        if not summary:
            summary = self._fallback_summary(new_messages)

        with self._lock:
            current = self._states.get(username, {"summary": "", "count": 0})
            # 并发时以更大的已摘要进度为准，避免旧结果覆盖新进度
            if old_count >= current["count"]:
                self._states[username] = {"summary": summary, "count": old_count}
        return summary

    def _summarize(self, client, existing_summary: str, messages: list[dict]) -> str:
        complete_fn = getattr(client, "complete", None)
        if complete_fn is None:
            return ""
        parts = []
        if existing_summary:
            parts.append(f"【旧摘要】\n{existing_summary}\n")
        if messages:
            parts.append("【新增消息】")
            for msg in messages:
                role = "用户" if msg.get("role") == "user" else "助手"
                content = str(msg.get("content") or "")[:800]
                parts.append(f"{role}：{content}")
        try:
            text = complete_fn(
                [
                    {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                    {"role": "user", "content": "\n\n".join(parts)},
                ],
                thinking=False,
                max_tokens=600,
            )
        except Exception:
            return ""
        return (text or "").strip()

    @staticmethod
    def _fallback_summary(messages: list[dict]) -> str:
        """模型不可用/失败时的保守摘要：保留最近几条消息的要点。"""
        lines = []
        for msg in messages[-6:]:
            role = "用户" if msg.get("role") == "user" else "助手"
            content = str(msg.get("content") or "").replace("\n", " ")[:120]
            if content:
                lines.append(f"{role}：{content}")
        return "；".join(lines) if lines else ""
