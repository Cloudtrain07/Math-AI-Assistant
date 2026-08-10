# -*- coding: utf-8 -*-
"""图片编辑模块测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image

from core import image_edit


def _image(width=100, height=80):
    return Image.new("RGB", (width, height), (10, 20, 30))


def test_rotate90_swaps_dimensions():
    assert image_edit.rotate90(_image(100, 80)).size == (80, 100)


def test_crop_percent_dimensions():
    out = image_edit.crop_percent(_image(100, 80), 10, 10, 90, 90)
    assert out.size == (80, 64)


def test_crop_percent_clamps_and_orders():
    out = image_edit.crop_percent(_image(100, 80), 120, -5, 10, 200)
    assert out.size[0] > 0 and out.size[1] > 0


def test_crop_preset():
    assert image_edit.crop_preset(_image(100, 80), 25).size == (50, 40)


def test_zoom_in_keeps_size():
    assert image_edit.zoom(_image(100, 80), 10).size == (100, 80)


def test_zoom_out_keeps_size():
    assert image_edit.zoom(_image(100, 80), -10).size == (100, 80)


def test_zoom_zero_keeps_size():
    assert image_edit.zoom(_image(100, 80), 0).size == (100, 80)
