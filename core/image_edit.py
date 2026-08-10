"""拍照识图前的图片编辑：旋转、裁剪、缩放（纯 PIL，服务端处理）。"""

from __future__ import annotations

from PIL import Image


def load(path: str) -> Image.Image:
    """从文件路径加载图片（自动转 RGB，避免 PNG 透明通道问题）。"""
    with Image.open(path) as image:
        return image.convert("RGB")


def rotate90(image: Image.Image) -> Image.Image:
    """顺时针旋转 90°。"""
    return image.rotate(-90, expand=True)


def crop_percent(
    image: Image.Image, left: float, top: float, right: float, bottom: float
) -> Image.Image:
    """按百分比裁剪：left/top/right/bottom 取值范围 0~100。"""
    width, height = image.size
    left = max(0.0, min(100.0, float(left)))
    top = max(0.0, min(100.0, float(top)))
    right = max(0.0, min(100.0, float(right)))
    bottom = max(0.0, min(100.0, float(bottom)))
    if right <= left:
        right = left + 1.0
    if bottom <= top:
        bottom = top + 1.0
    box = (
        int(width * left / 100.0),
        int(height * top / 100.0),
        int(width * right / 100.0),
        int(height * bottom / 100.0),
    )
    return image.crop(box)


def crop_preset(image: Image.Image, percent: float) -> Image.Image:
    """四周各裁剪 percent%（0~49），保留中间区域。"""
    percent = max(0.0, min(49.0, float(percent)))
    return crop_percent(image, percent, percent, 100.0 - percent, 100.0 - percent)


def zoom(image: Image.Image, percent: float) -> Image.Image:
    """以中心缩放：percent>0 放大（裁掉四周后放大回原尺寸）；
    percent<0 缩小（先放大视野再裁回原尺寸）。输出尺寸与原图一致。"""
    width, height = image.size
    step = max(-45.0, min(45.0, float(percent))) / 100.0
    if step > 0:
        keep = 1.0 - step
        box = (
            int(width * (1.0 - keep) / 2.0),
            int(height * (1.0 - keep) / 2.0),
            int(width * (1.0 + keep) / 2.0),
            int(height * (1.0 + keep) / 2.0),
        )
        cropped = image.crop(box)
        return cropped.resize((width, height), Image.LANCZOS)
    if step < 0:
        scale = 1.0 / (1.0 + step)
        enlarged = image.resize(
            (max(1, int(width * scale)), max(1, int(height * scale))),
            Image.LANCZOS,
        )
        ew, eh = enlarged.size
        box = (
            (ew - width) // 2,
            (eh - height) // 2,
            (ew - width) // 2 + width,
            (eh - height) // 2 + height,
        )
        return enlarged.crop(box)
    return image.copy()
