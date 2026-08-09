"""每日自动备份：把 data/ 全量打包到 data/backups/，保留最近 14 份。

备份包含账号、聊天历史、习题库、用量与日志（zip 内含明文密码文件，
仅用于本地保存）。当天同名备份只生成一次；超出保留份数的旧备份自动删除。
"""

from __future__ import annotations

import datetime
import re
import time
import zipfile
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
BACKUP_KEEP = 14
BACKUP_PREFIX = "math-backup-"
BACKUP_SUFFIX = ".zip"
_BACKUP_NAME = re.compile(
    rf"^{BACKUP_PREFIX}(\d{{4}}-\d{{2}}-\d{{2}}){BACKUP_SUFFIX}$"
)


def backup_dir() -> Path:
    return DATA_DIR / "backups"


def backup_path_for(date: datetime.date | None = None) -> Path:
    date = date or datetime.date.today()
    return backup_dir() / f"{BACKUP_PREFIX}{date.isoformat()}{BACKUP_SUFFIX}"


def backup_data(path: Path | None = None) -> Path | None:
    """把 data/ 全量打包（不含 backups 自身）；当天同名已存在时返回 None。"""
    path = Path(path) if path is not None else backup_path_for()
    if path.exists():
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for file in sorted(DATA_DIR.rglob("*")):
                if file.is_dir():
                    continue
                relative = file.relative_to(DATA_DIR)
                if relative.parts[0] == "backups":
                    continue
                archive.write(file, arcname=str(relative))
        return path
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
        return None


def prune_backups(keep: int = BACKUP_KEEP, target_dir: Path | None = None) -> int:
    """按备份日期保留最近 keep 份，删除更旧的；返回删除数量。"""
    target_dir = Path(target_dir) if target_dir is not None else backup_dir()
    if not target_dir.exists():
        return 0
    entries: list[tuple[str, Path]] = []
    for file in target_dir.iterdir():
        match = _BACKUP_NAME.match(file.name)
        if match and file.is_file():
            entries.append((match.group(1), file))
    entries.sort(key=lambda item: item[0])
    removed = 0
    for _, file in entries[: max(0, len(entries) - keep)]:
        try:
            file.unlink()
            removed += 1
        except Exception:
            pass
    return removed


def ensure_today_backup() -> Path | None:
    """今天尚未备份则备份一次；返回新生成的备份路径或 None。"""
    return backup_data(backup_path_for())


def daily_backup_loop(interval_seconds: int = 3600) -> None:
    """后台守护线程：每小时检查一次日期，跨天自动备份并清理旧备份。"""
    last_date = datetime.date.today()
    while True:
        time.sleep(interval_seconds)
        today = datetime.date.today()
        if today != last_date:
            last_date = today
            backup_data(backup_path_for(today))
            prune_backups()
