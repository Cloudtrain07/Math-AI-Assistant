"""集中文件日志：聊天事件、访问事件与错误记录的写入与裁剪。

应用共用同一个日志对象，同一文件的追加与裁剪在对象内同一把锁中完成
（单进程范围）。写入或裁剪失败静默返回，不打断登录、回答显示、
历史保存、用量结算等业务流程。
"""

from __future__ import annotations

import datetime
import json
import threading
from pathlib import Path

# 各日志文件默认保留行数（超过后自动裁剪）
CHAT_LOG_MAX_LINES = 2000
ACCESS_LOG_MAX_LINES = 1000
ERROR_LOG_MAX_LINES = 2000


def trim_log_file(path: Path | str, max_lines: int) -> None:
    """日志超过 max_lines 行时只保留最后 max_lines 行（失败静默）。"""
    try:
        target = Path(path)
        if not target.exists():
            return
        lines = target.read_text(encoding="utf-8").splitlines()
        if len(lines) > max_lines:
            target.write_text(
                "\n".join(lines[-max_lines:]) + "\n", encoding="utf-8"
            )
    except Exception:
        pass


class FileEventLogger:
    """把聊天、访问与错误日志写入 data 目录的简单文件日志对象。

    错误记录所需的模型配置字段（model / thinking / reasoning_effort）
    在构造时明确传入；缺失用量以 None/null 写入。
    """

    def __init__(
        self,
        data_dir: Path | str,
        *,
        model: str | None = None,
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
        chat_max_lines: int = CHAT_LOG_MAX_LINES,
        access_max_lines: int = ACCESS_LOG_MAX_LINES,
        error_max_lines: int = ERROR_LOG_MAX_LINES,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.model = model
        self.thinking = thinking
        self.reasoning_effort = reasoning_effort
        self.chat_max_lines = chat_max_lines
        self.access_max_lines = access_max_lines
        self.error_max_lines = error_max_lines
        self._lock = threading.Lock()

    # ---- chat.log ----

    def log_chat_event(self, kind: str, text: str) -> None:
        """把事件与文本追加写入 chat.log，用于排查发送链路完整性。"""
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        head = text[:40].replace("\n", "⏎")
        tail = text[-40:].replace("\n", "⏎")
        line = f"[{ts}] {kind} 长度={len(text)} 开头={head!r} 结尾={tail!r}\n"
        self._append_line(self.data_dir / "chat.log", line, self.chat_max_lines)

    # ---- access.log ----

    def log_access_event(self, *parts: str) -> None:
        """把一条访问事件按现有拼接格式追加写入 access.log。"""
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] " + " ".join(str(p) for p in parts) + "\n"
        self._append_line(self.data_dir / "access.log", line, self.access_max_lines)

    # ---- error.log ----

    def log_error(
        self, exc: Exception, *, account: str, turn_total: int | None, partial: bool
    ) -> None:
        """把一次 API 异常的完整信息写入 error.log，便于定位根因。"""
        record = self._base_error_record(account, turn_total)
        record.update(
            {
                "type": exc.__class__.__name__,
                "message": str(exc)[:2000],
                "partial_answer": partial,
            }
        )
        self._write_error_record(record)

    def log_empty_reply(
        self, *, account: str, turn_total: int | None, truncated: bool
    ) -> None:
        """记录"请求完成但没收到正式回答"的情况（常见于思考链耗尽输出额度）。"""
        record = self._base_error_record(account, turn_total)
        record.update(
            {
                "type": "empty_reply",
                "truncated": truncated,
                "message": (
                    "请求完成但未收到正式回答（无异常抛出）；"
                    f"truncated={truncated}，可能为思考链已用尽 max_tokens 额度"
                ),
            }
        )
        self._write_error_record(record)

    def _base_error_record(self, account: str, turn_total: int | None) -> dict:
        """error.log 公共字段：时间、账号与构造时传入的模型配置。"""
        return {
            "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "account": account,
            "tokens": turn_total,
            "model": self.model,
            "thinking": self.thinking,
            "reasoning_effort": self.reasoning_effort,
        }

    def _write_error_record(self, record: dict) -> None:
        """把一条诊断记录以 JSON 一行一条追加写入 error.log。"""
        self._append_line(
            self.data_dir / "error.log",
            json.dumps(record, ensure_ascii=False) + "\n",
            self.error_max_lines,
        )

    def _append_line(self, path: Path, line: str, max_lines: int) -> None:
        """追加一行并在同一把锁内裁剪；失败静默，不抛给业务调用者。"""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line)
                trim_log_file(path, max_lines)
        except Exception:
            return
