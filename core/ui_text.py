"""界面共用的用户文案与展示格式化（API 层负责界面形态）。

本模块只放界面直接消费的纯文本与纯函数：随消息附带的标记、保存/清空/
每日用量等结果提示、API 异常到中文提示的映射、token 用量的一行格式化。
不导入 api.py 或任何界面模块。
"""

from __future__ import annotations

from core.llm_client import MaxWaitTimeout

# 图片随消息附带的标记：写入历史供界面显示与上下文压缩参考（图片本身不持久化）
IMAGE_NOTE = "📷（已附题目图片）"
# 只传图不打字时自动使用的提问语
IMAGE_ONLY_PROMPT = "请解答图中的题目。"

# 历史保存/清空结果提示：只追加说明，不掩盖原有的模型异常或截断提示
SAVE_FAILED_MSG = "⚠️ 历史保存失败，刷新后可能丢失。"
SAVE_STALE_MSG = "ℹ️ 另一个操作已更新历史，本次回答未保存。"
CLEAR_FAILED_MSG = "⚠️ 清空未成功，请稍后重试。"
CLEAR_OK_MSG = "🗑️ 已开启新对话。"
RESTORE_FAILED_MSG = (
    "⚠️ 上次聊天记录读取失败（{detail}），本次以空历史开始；原文件未修改。"
)
# 每日用量失败提示：只追加说明，不掩盖原有的模型异常、截断或历史保存提示
DAILY_FAILED_MSG = "⚠️ 每日用量未记录成功。"
DAILY_STATS_FAILED_MSG = "⚠️ 每日用量已记录，但全局统计暂时无法刷新。"
QUOTA_CHECK_FAILED_MSG = "⚠️ 每日额度暂时无法检查，请稍后重试。"

# 继续生成没有可继续题目时的拒绝提示
NO_CONTINUE_MSG = "💡 还没有可继续的题目，请先提问。"


def format_usage(usage) -> str:
    """把 API 返回的 token 用量格式化为一行提示（含输入/输出拆分）。"""
    if usage is None:
        return ""
    total = int(getattr(usage, "total_tokens", 0) or 0)
    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    reasoning = 0
    details = getattr(usage, "completion_tokens_details", None)
    if details is not None:
        reasoning = int(getattr(details, "reasoning_tokens", 0) or 0)
    reasoning_part = f"，其中思考链约 {reasoning}" if reasoning else ""
    if prompt is not None and completion is not None:
        return (
            f"⚡ 本次 输入 {int(prompt):,} + 输出 {int(completion):,}"
            f" ≈ 共 {total:,} tokens{reasoning_part}"
        )
    if reasoning:
        return f"⚡ 本次约 {total} tokens（其中思考链约 {reasoning}）"
    return f"⚡ 本次约 {total} tokens"


def friendly_error(exc: Exception) -> str:
    """把 API 异常映射为简洁的中文提示，避免把堆栈抛给用户。"""
    name = exc.__class__.__name__
    text = str(exc)[:150]
    if isinstance(exc, MaxWaitTimeout):
        hint = "已达到最长等待时间，已自动停止（可重新发送）"
    elif "Authentication" in name or "401" in text:
        hint = "API Key 无效或未配置，请检查 .env 中的 DEEPSEEK_API_KEY"
    elif "RateLimit" in name or "429" in text:
        hint = "请求过于频繁或额度不足（触发限流）"
    elif "Connection" in name or "APIConnection" in name:
        hint = "网络无法连接到 API 服务，请检查网络或代理"
    elif "Timeout" in name:
        hint = "请求超时，请稍后重试"
    else:
        hint = "请求失败"
    return f"⚠️ {hint}（{name}: {text}）"
