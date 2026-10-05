# -*- coding: utf-8 -*-
"""每日自动备份模块测试。

除全局目录的原有行为外，重点覆盖显式 data_dir：新版 API 的备份必须落在
自己 runtime 的数据目录，且"生成/已存在/失败"三种结局分得清。
"""

import sys
import threading
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import backup as backup_mod


def _make_data(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "history").mkdir(parents=True)
    (data_dir / "extra_data").mkdir()
    (data_dir / "accounts.json").write_text("{}", encoding="utf-8")
    (data_dir / "history" / "user1.json").write_text("[]", encoding="utf-8")
    (data_dir / "extra_data" / "example.json").write_text("[]", encoding="utf-8")
    (data_dir / "chat.log").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(backup_mod, "DATA_DIR", data_dir)
    return data_dir


def test_backup_zip_contents(monkeypatch, tmp_path):
    _make_data(monkeypatch, tmp_path)
    path = backup_mod.backup_data()
    assert path is not None and path.exists()
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
    assert "accounts.json" in names
    assert "history/user1.json" in names
    assert "extra_data/example.json" in names
    assert "chat.log" in names
    assert not any(name.startswith("backups/") for name in names)


def test_backup_same_day_idempotent(monkeypatch, tmp_path):
    _make_data(monkeypatch, tmp_path)
    first = backup_mod.backup_data()
    second = backup_mod.backup_data()
    assert first is not None
    assert second is None
    assert first.exists()


def test_ensure_today_backup_once(monkeypatch, tmp_path):
    _make_data(monkeypatch, tmp_path)
    assert backup_mod.ensure_today_backup() is not None
    assert backup_mod.ensure_today_backup() is None


def test_prune_keeps_latest(monkeypatch, tmp_path):
    _make_data(monkeypatch, tmp_path)
    target = backup_mod.backup_dir()
    target.mkdir(parents=True, exist_ok=True)
    for offset in range(15):
        day = date(2026, 8, 1) + timedelta(days=offset)
        (target / f"math-backup-{day.isoformat()}.zip").write_text(
            "x", encoding="utf-8"
        )
    removed = backup_mod.prune_backups(keep=14, target_dir=target)
    assert removed == 1
    remaining = sorted(p.name for p in target.iterdir())
    assert len(remaining) == 14
    assert "math-backup-2026-08-01.zip" not in remaining
    assert "math-backup-2026-08-15.zip" in remaining


def test_backup_empty_data_dir_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(backup_mod, "DATA_DIR", tmp_path / "data")
    path = backup_mod.backup_data()
    assert path is not None and path.exists()


def test_daily_backup_loop_stops_on_signal(tmp_path, monkeypatch):
    """停止信号置位后，等待中的备份循环不必等满间隔即可退出。

    join 设 5 秒上限：若停止机制失效（等满 60 秒间隔），线程仍存活、测试失败。
    """
    monkeypatch.setattr(backup_mod, "DATA_DIR", tmp_path / "data")
    stop_event = threading.Event()
    thread = threading.Thread(
        target=backup_mod.daily_backup_loop,
        kwargs={"interval_seconds": 60, "stop_event": stop_event},
        daemon=True,
    )
    thread.start()
    stop_event.set()
    thread.join(timeout=5)
    assert not thread.is_alive()


def _make_tree(root: Path, marker: str) -> Path:
    """最小数据目录：两个文件内容都带 marker，便于确认 zip 装的是哪一份。"""
    (root / "history").mkdir(parents=True)
    (root / "accounts.json").write_text(marker, encoding="utf-8")
    (root / "history" / "user1.json").write_text(marker, encoding="utf-8")
    return root


def test_explicit_data_dir_keeps_two_runtimes_apart(monkeypatch, tmp_path):
    """显式 data_dir 时全局 DATA_DIR 完全不参与：两份数据各备各的。

    防住的独立风险：新版 API 按 runtime.data_dir 备份，若实现仍从全局常量取目录，
    备份会写进另一份（甚至真实）data 目录，两个运行状态互相覆盖。
    """
    global_dir = _make_tree(tmp_path / "global", "GLOBAL")
    monkeypatch.setattr(backup_mod, "DATA_DIR", global_dir)
    first = _make_tree(tmp_path / "first", "FIRST")
    second = _make_tree(tmp_path / "second", "SECOND")

    path_first = backup_mod.backup_data(data_dir=first)
    path_second = backup_mod.backup_data(data_dir=second)
    today = date.today().isoformat()
    assert path_first == first / "backups" / f"math-backup-{today}.zip"
    assert path_second == second / "backups" / f"math-backup-{today}.zip"
    with zipfile.ZipFile(path_first) as archive:
        assert archive.read("accounts.json").decode() == "FIRST"
        assert archive.read("history/user1.json").decode() == "FIRST"
    with zipfile.ZipFile(path_second) as archive:
        assert archive.read("accounts.json").decode() == "SECOND"
    # 全局目录既没被打包，也没被写入备份
    assert not (global_dir / "backups").exists()


def test_run_backup_separates_created_exists_failed(monkeypatch, tmp_path):
    """三种结局分得清：写盘失败是 failed 且不留下看起来成功的半成品 zip。

    防住的独立风险：失败与"今日已备份"都返回 None，启动方把失败报成成功；
    旧接口 backup_data 的缺省语义（两种情况都返回 None）必须保持不变。
    """
    data = _make_tree(tmp_path / "data", "x")
    created = backup_mod.run_backup(data_dir=data)
    assert created.status == backup_mod.BACKUP_CREATED and created.path.exists()

    again = backup_mod.run_backup(data_dir=data)
    assert again.status == backup_mod.BACKUP_EXISTS
    assert backup_mod.backup_data(data_dir=data) is None  # 旧语义不变

    def boom(source_dir, path):
        raise OSError("模拟写盘失败")

    failing_dir = _make_tree(tmp_path / "failing", "y")
    monkeypatch.setattr(backup_mod, "_write_backup_zip", boom)
    failed = backup_mod.run_backup(data_dir=failing_dir)
    assert failed.status == backup_mod.BACKUP_FAILED and failed.path is None
    assert not (failing_dir / "backups" / f"math-backup-{date.today().isoformat()}.zip").exists()


def test_daily_backup_loop_cross_day_uses_given_dir_and_reports(tmp_path):
    """跨天备份落在本次的 data_dir、按保留份数清理，并把结局回报给启动方。

    防住的独立风险：循环体内仍从全局 DATA_DIR 取目录（跨天后备错地方），
    或跨天备份悄悄失败而无人提示。
    """
    data = _make_tree(tmp_path / "data", "x")
    target_dir = data / "backups"
    target_dir.mkdir(parents=True)
    for offset in range(14):
        day = date(2026, 8, 1) + timedelta(days=offset)
        (target_dir / f"math-backup-{day.isoformat()}.zip").write_text("x", encoding="utf-8")

    days = iter([date(2026, 8, 1), date(2026, 9, 1)])
    results: list[backup_mod.BackupResult] = []
    stop_event = threading.Event()
    thread = threading.Thread(
        target=backup_mod.daily_backup_loop,
        kwargs={
            "interval_seconds": 0.01,
            "stop_event": stop_event,
            "data_dir": data,
            "on_result": results.append,
            "today": lambda: next(days, date(2026, 9, 1)),
        },
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not results and time.monotonic() < deadline:
        time.sleep(0.05)
    stop_event.set()
    thread.join(timeout=5)

    assert results, "跨天备份没有回报结局"
    assert results[0].status == backup_mod.BACKUP_CREATED
    assert results[0].path == target_dir / "math-backup-2026-09-01.zip"
    assert results[0].path.exists()
    remaining = sorted(p.name for p in target_dir.iterdir())
    assert len(remaining) == 14  # 保留份数语义不变
    assert "math-backup-2026-08-01.zip" not in remaining
    assert not thread.is_alive()
