"""每日自动备份：把数据目录全量打包到 <数据目录>/backups/，保留最近 14 份。

备份包含数据目录中的账号、聊天历史、用量、日志及其他文件（zip 内含明文密码文件，
仅用于本地保存）。当天同名备份只生成一次；超出保留份数的旧备份自动删除。

所有函数都可显式传入 data_dir（启动方用它绑定 runtime 的数据目录）；
缺省时使用模块级 DATA_DIR。
"""

from __future__ import annotations

import datetime
import re
import threading
import time
import zipfile
from pathlib import Path
from typing import Callable, NamedTuple

# 缺省数据目录；显式传 data_dir 时不读取此常量
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
BACKUP_KEEP = 14
BACKUP_PREFIX = "math-backup-"
BACKUP_SUFFIX = ".zip"
_BACKUP_NAME = re.compile(
    rf"^{BACKUP_PREFIX}(\d{{4}}-\d{{2}}-\d{{2}}){BACKUP_SUFFIX}$"
)

# 一次备份的结局：created 新写入 / exists 当天已存在 / failed 写盘失败
BACKUP_CREATED = "created"
BACKUP_EXISTS = "exists"
BACKUP_FAILED = "failed"


class BackupResult(NamedTuple):
    status: str
    path: Path | None


def resolve_data_dir(data_dir: Path | str | None = None) -> Path:
    """本次备份的数据目录：显式传入优先，否则沿用全局 DATA_DIR。"""
    return Path(data_dir) if data_dir is not None else DATA_DIR


def backup_dir(data_dir: Path | str | None = None) -> Path:
    return resolve_data_dir(data_dir) / "backups"


def backup_path_for(
    date: datetime.date | None = None,
    data_dir: Path | str | None = None,
) -> Path:
    date = date or datetime.date.today()
    return backup_dir(data_dir) / f"{BACKUP_PREFIX}{date.isoformat()}{BACKUP_SUFFIX}"


def _write_backup_zip(source_dir: Path, path: Path) -> None:
    """把 source_dir 全量打包到 path（不含 backups 自身）；异常原样抛出。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(source_dir.rglob("*")):
            if file.is_dir():
                continue
            relative = file.relative_to(source_dir)
            if relative.parts[0] == "backups":
                continue
            archive.write(file, arcname=str(relative))


def run_backup(
    path: Path | None = None,
    *,
    data_dir: Path | str | None = None,
) -> BackupResult:
    """打包一次并给出明确结局；失败时删除半成品，不留下看起来成功的空 zip。"""
    source = resolve_data_dir(data_dir)
    target = Path(path) if path is not None else backup_path_for(data_dir=source)
    if target.exists():
        return BackupResult(BACKUP_EXISTS, target)
    try:
        _write_backup_zip(source, target)
    except Exception:
        try:
            target.unlink(missing_ok=True)
        except Exception:
            pass
        return BackupResult(BACKUP_FAILED, None)
    return BackupResult(BACKUP_CREATED, target)


def prune_backups(
    keep: int = BACKUP_KEEP,
    target_dir: Path | None = None,
    *,
    data_dir: Path | str | None = None,
) -> int:
    """按备份日期保留最近 keep 份，删除更旧的；返回删除数量。"""
    target_dir = (
        Path(target_dir)
        if target_dir is not None
        else backup_dir(data_dir)
    )
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


def daily_backup_loop(
    interval_seconds: int = 3600,
    stop_event: threading.Event | None = None,
    *,
    data_dir: Path | str | None = None,
    keep: int = BACKUP_KEEP,
    on_result: Callable[[BackupResult], None] | None = None,
    today: Callable[[], datetime.date] | None = None,
) -> None:
    """后台守护线程：每小时检查一次日期，跨天自动备份并清理旧备份。

    stop_event 仅用于收到退出信号时及时结束等待并返回；
    正在进行的备份不会被信号打断，等待中的循环则立即退出。
    on_result 只用于将跨天备份的结局告知启动方（回调异常不影响循环）。
    """
    source = resolve_data_dir(data_dir)
    clock = today or datetime.date.today
    last_date = clock()
    while True:
        if stop_event is not None:
            if stop_event.wait(interval_seconds):
                return
        else:
            time.sleep(interval_seconds)
        current_date = clock()
        if current_date != last_date:
            last_date = current_date
            result = run_backup(backup_path_for(current_date, source), data_dir=source)
            prune_backups(keep=keep, data_dir=source)
            if on_result is not None:
                try:
                    on_result(result)
                except Exception:
                    pass
