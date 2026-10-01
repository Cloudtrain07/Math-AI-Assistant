# -*- coding: utf-8 -*-
"""日志格式与轮转测试（chat.log / access.log / error.log）。

格式与裁剪均使用临时目录；裁剪行为用较小的保留行数验证，
默认保留行数另行断言（chat/error 2000 行，access 1000 行）。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.logger import (
    ACCESS_LOG_MAX_LINES,
    CHAT_LOG_MAX_LINES,
    ERROR_LOG_MAX_LINES,
    FileEventLogger,
    trim_log_file,
)


def test_default_max_lines(tmp_path):
    assert CHAT_LOG_MAX_LINES == 2000
    assert ERROR_LOG_MAX_LINES == 2000
    assert ACCESS_LOG_MAX_LINES == 1000
    logger = FileEventLogger(tmp_path)
    assert logger.chat_max_lines == 2000
    assert logger.access_max_lines == 1000
    assert logger.error_max_lines == 2000


def test_trim_log_file_keeps_last_n(tmp_path):
    target = tmp_path / "access.log"
    target.write_text(
        "\n".join(f"line{i}" for i in range(1, 9)) + "\n", encoding="utf-8"
    )
    trim_log_file(target, max_lines=5)
    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines == ["line4", "line5", "line6", "line7", "line8"]


def test_trim_log_file_missing_ok(tmp_path):
    trim_log_file(tmp_path / "nope.log", max_lines=5)


# ---------- chat.log 格式 ----------


def test_chat_log_format_replaces_newlines(tmp_path):
    logger = FileEventLogger(tmp_path)
    logger.log_chat_event("发送", "第一行\n第二行")
    text = (tmp_path / "chat.log").read_text(encoding="utf-8")
    # 一条记录一行，换行被替换为 ⏎，含事件名与文本长度
    assert text.count("\n") == 1
    line = text.rstrip("\n")
    assert "发送 长度=7 开头='第一行⏎第二行' 结尾='第一行⏎第二行'" in line
    assert line.startswith("[")


def test_chat_log_keeps_head_and_tail_40_chars(tmp_path):
    logger = FileEventLogger(tmp_path)
    text = "x" * 10 + "y" * 100
    logger.log_chat_event("回复", text)
    line = (tmp_path / "chat.log").read_text(encoding="utf-8").rstrip("\n")
    assert f"长度={len(text)}" in line
    assert "开头='" + "x" * 10 + "y" * 30 in line
    assert "结尾='" + "y" * 40 in line


# ---------- access.log 格式 ----------


def test_access_log_format_joins_parts(tmp_path):
    logger = FileEventLogger(tmp_path)
    logger.log_access_event("提问", "账号=user1", "IP=127.0.0.1", "长度=6")
    line = (tmp_path / "access.log").read_text(encoding="utf-8").rstrip("\n")
    assert line.startswith("[")
    assert " 提问 账号=user1 IP=127.0.0.1 长度=6" in line


# ---------- error.log 格式与字段 ----------


def test_error_log_exception_fields(tmp_path):
    logger = FileEventLogger(
        tmp_path, model="测试模型", thinking=True, reasoning_effort="high"
    )
    logger.log_error(
        RuntimeError("模拟异常"), account="user1", turn_total=123, partial=True
    )
    line = (tmp_path / "error.log").read_text(encoding="utf-8").splitlines()[0]
    record = json.loads(line)
    assert record["type"] == "RuntimeError"
    assert record["message"] == "模拟异常"
    assert record["partial_answer"] is True
    assert record["account"] == "user1"
    assert record["tokens"] == 123
    assert record["model"] == "测试模型"
    assert record["thinking"] is True
    assert record["reasoning_effort"] == "high"
    assert record["time"]


def test_error_log_empty_reply_fields(tmp_path):
    logger = FileEventLogger(tmp_path)  # 未传配置字段时应写 null
    logger.log_empty_reply(account="user2", turn_total=None, truncated=True)
    line = (tmp_path / "error.log").read_text(encoding="utf-8").splitlines()[0]
    record = json.loads(line)
    assert record["type"] == "empty_reply"
    assert record["truncated"] is True
    assert "truncated=True" in record["message"]
    assert record["account"] == "user2"
    assert record["tokens"] is None
    assert record["model"] is None
    assert record["thinking"] is None
    assert record["reasoning_effort"] is None


# ---------- 裁剪：保留最新记录 ----------


def test_chat_log_trim_keeps_latest(tmp_path):
    logger = FileEventLogger(tmp_path, chat_max_lines=5)
    for i in range(7):
        logger.log_chat_event("发送", f"n={i}")
    lines = (tmp_path / "chat.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5
    assert "n=6" in lines[-1]
    assert "n=0" not in "".join(lines)


def test_access_log_trim_keeps_latest(tmp_path):
    logger = FileEventLogger(tmp_path, access_max_lines=5)
    for i in range(7):
        logger.log_access_event("事件", f"n={i}")
    lines = (tmp_path / "access.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5
    assert "n=6" in lines[-1]
    assert "n=0" not in "".join(lines)


def test_error_log_trim_keeps_latest(tmp_path):
    logger = FileEventLogger(tmp_path, error_max_lines=5)
    for i in range(7):
        logger.log_error(
            RuntimeError(f"n={i}"), account="user1", turn_total=i, partial=False
        )
    lines = (tmp_path / "error.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5
    assert json.loads(lines[-1])["message"] == "n=6"
    assert all(json.loads(line)["message"] != "n=0" for line in lines)
