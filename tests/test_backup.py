# -*- coding: utf-8 -*-
"""每日自动备份模块测试。"""

import sys
import zipfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import backup as backup_mod


def _make_data(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "history").mkdir(parents=True)
    (data_dir / "exercise_bank").mkdir()
    (data_dir / "accounts.json").write_text("{}", encoding="utf-8")
    (data_dir / "history" / "user1.json").write_text("[]", encoding="utf-8")
    (data_dir / "exercise_bank" / "user1.json").write_text("[]", encoding="utf-8")
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
    assert "exercise_bank/user1.json" in names
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
