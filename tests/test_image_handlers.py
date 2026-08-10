# -*- coding: utf-8 -*-
"""图片上传与编辑处理器测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image

import app as app_module


def _make_image(tmp_path, name="q.png"):
    path = tmp_path / name
    Image.new("RGB", (60, 40), (255, 0, 0)).save(path)
    return str(path)


def test_on_image_upload_saves_original(tmp_path):
    path = _make_image(tmp_path)
    _img, status, original = app_module.on_image_upload(path, "")
    assert "已上传" in status
    assert original == path


def test_on_image_upload_empty():
    _img, status, original = app_module.on_image_upload(None, "")
    assert "请先上传" in status
    assert original == ""


def test_edit_rotate_returns_update(tmp_path):
    path = _make_image(tmp_path)
    img_u, status = app_module.edit_rotate(path, path)
    assert "旋转 90°" in status
    assert isinstance(img_u, dict) and "value" in img_u


def test_edit_reset_restores_original(tmp_path):
    path = _make_image(tmp_path)
    img_u, status = app_module.edit_reset(path)
    assert img_u.get("value") == path
    assert "恢复原图" in status


def test_edit_reset_no_original():
    _img, status = app_module.edit_reset("")
    assert "还没有" in status


def test_edit_crop_custom_bad_format(tmp_path):
    path = _make_image(tmp_path)
    _img, status = app_module.edit_crop_custom(path, "1,2,3", "")
    assert "4 个数字" in status
    _img2, status2 = app_module.edit_crop_custom(path, "abc", "")
    assert "格式" in status2


def test_edit_crop_custom_ok(tmp_path):
    path = _make_image(tmp_path)
    img_u, status = app_module.edit_crop_custom(path, "10,10,90,90", "")
    assert "已按自定义比例裁剪" in status
    assert isinstance(img_u, dict) and "value" in img_u
