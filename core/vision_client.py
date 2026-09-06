"""百炼（DashScope）视觉模型封装：把题目图片转写成文字。"""

from __future__ import annotations

import base64
import io
from pathlib import Path

from openai import OpenAI

from core.config import Settings
from core.prompts import VISION_TRANSCRIBE_PROMPT

SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_EDGE = 2048
JPEG_QUALITY = 88


def image_to_data_url(image_path: str) -> str:
    """校验图片、用 Pillow 预处理（转 RGB / 缩放 / 压缩）并编码为 data URL。

    纯本地处理，供视觉转写与原生视觉直传共用。
    """
    path = Path(image_path)
    if not path.exists():
        raise FileNotFoundError(f"图片文件不存在：{image_path}")
    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTS:
        raise ValueError(f"不支持的图片格式：{ext or '(无扩展名)'}，仅支持 jpg/png/webp/bmp/gif")
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError("图片超过 10MB，请压缩后再试")

    from PIL import Image

    with Image.open(path) as img:
        img = img.convert("RGB")
        if max(img.size) > MAX_EDGE:
            ratio = MAX_EDGE / max(img.size)
            new_size = (
                max(1, round(img.width * ratio)),
                max(1, round(img.height * ratio)),
            )
            img = img.resize(new_size, Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY)
        raw = buf.getvalue()

    return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")


class VisionClient:
    """调用百炼 OpenAI 兼容接口，把图片转写为可编辑的文字。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = OpenAI(
            api_key=settings.vision_api_key,
            base_url=settings.vision_base_url,
            timeout=60.0,
            max_retries=2,
        )

    def transcribe_image(self, image_path: str, prompt: str | None = None) -> tuple[str, bool]:
        """读取图片 -> 预处理 -> base64 -> 调用视觉模型，返回 (转写文字, 是否被截断)。"""
        data_url = image_to_data_url(image_path)
        text_prompt = prompt or VISION_TRANSCRIBE_PROMPT
        response = self._client.chat.completions.create(
            model=self._settings.vision_model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": text_prompt},
                    ],
                }
            ],
            stream=False,
            max_tokens=4096,
        )
        content = (response.choices[0].message.content or "").strip()
        truncated = response.choices[0].finish_reason == "length"
        return content, truncated
