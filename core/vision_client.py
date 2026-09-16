"""图片校验、预处理与 data URL 编码，供原生视觉模型输入。"""

from __future__ import annotations

import base64
import io
from pathlib import Path


SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_EDGE = 2048
JPEG_QUALITY = 88


def image_to_data_url(image_path: str) -> str:
    """校验图片、用 Pillow 预处理（转 RGB / 缩放 / 压缩）并编码为 data URL。

    供原生视觉模型直接接收图片
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