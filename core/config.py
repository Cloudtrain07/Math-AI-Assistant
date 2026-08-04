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
    vision_api_key: str
    vision_base_url: str
    vision_model: str


def get_settings() -> Settings:
    """读取并校验配置；缺少 API Key 时抛出带指引的异常。"""
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise ValueError(
            "未找到 DEEPSEEK_API_KEY。请复制 .env.example 为 .env，"
            "填入你的 DeepSeek API Key 后重启应用。"
        )

    return Settings(
        api_key=api_key,
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip(),
        model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash").strip(),
        thinking=os.getenv("DEEPSEEK_THINKING", "false").strip().lower() == "true",
        reasoning_effort=os.getenv("DEEPSEEK_REASONING_EFFORT", "high").strip().lower(),
        max_history_messages=int(os.getenv("MAX_HISTORY_MESSAGES", "20")),
        max_output_tokens=int(os.getenv("MAX_OUTPUT_TOKENS", "16384")),
        # 视觉识图配置（可选）：未配置时仅影响「拍照识题」功能
        vision_api_key=os.getenv("DASHSCOPE_API_KEY", "").strip(),
        vision_base_url=os.getenv(
            "DASHSCOPE_BASE_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        ).strip(),
        vision_model=os.getenv("VISION_MODEL", "qwen3.7-flash-2026-07-15").strip(),
    )
