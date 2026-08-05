"""全局配置：从 .env 或系统环境变量读取。"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    model: str
    thinking: bool
    reasoning_effort: str
    max_history_messages: int
    max_output_tokens: int
    max_message_len: int
    daily_token_limit: int
    global_daily_token_limit: int
    vision_api_key: str
    vision_base_url: str
    vision_model: str


def _env_int(name: str, default: int) -> int:
    """读取整数环境变量；缺失或非法时回退默认值并打印警告。"""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[警告] 环境变量 {name}={raw!r} 不是合法整数，已使用默认值 {default}")
        return default


def get_settings() -> Settings:
    """读取并校验配置；缺少 API Key 时抛出带指引的异常。"""
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise ValueError(
            "未找到 DEEPSEEK_API_KEY。请复制 .env.example 为 .env，"
            "填入你的 DeepSeek API Key 后重启应用。"
        )

    reasoning_effort = os.getenv("DEEPSEEK_REASONING_EFFORT", "high").strip().lower()
    if reasoning_effort not in ("low", "high", "max"):
        print(
            f"[警告] DEEPSEEK_REASONING_EFFORT={reasoning_effort!r} 无效，"
            "已使用默认值 high"
        )
        reasoning_effort = "high"

    return Settings(
        api_key=api_key,
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip(),
        model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash").strip(),
        thinking=os.getenv("DEEPSEEK_THINKING", "false").strip().lower() == "true",
        reasoning_effort=reasoning_effort,
        max_history_messages=_env_int("MAX_HISTORY_MESSAGES", 20),
        max_output_tokens=_env_int("MAX_OUTPUT_TOKENS", 30000),
        max_message_len=_env_int("MAX_MESSAGE_LEN", 2000),
        daily_token_limit=_env_int("DAILY_TOKEN_LIMIT", 1_000_000),
        global_daily_token_limit=_env_int("GLOBAL_DAILY_TOKEN_LIMIT", 3_000_000),
        # 视觉识图配置（可选）：未配置时仅影响「拍照识题」功能
        vision_api_key=os.getenv("DASHSCOPE_API_KEY", "").strip(),
        vision_base_url=os.getenv(
            "DASHSCOPE_BASE_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        ).strip(),
        vision_model=os.getenv("VISION_MODEL", "qwen3.7-flash-2026-07-15").strip(),
    )
