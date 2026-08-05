# -*- coding: utf-8 -*-
"""Cloudflare 隧道工具函数测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import time

from core.tunnel import (
    TunnelWatcher,
    cleanup_stale_tunnel,
    find_cloudflared,
    parse_tunnel_url,
)


def test_parse_tunnel_url_extracts_url():
    line = (
        "Your quick Tunnel has been created! Visit it at "
        "(it may take some time to be reachable):\n"
        "https://random-words-1234.trycloudflare.com\n"
        "2026-08-05T00:00:00Z INF Registered tunnel connection connIndex=0"
    )
    assert parse_tunnel_url(line) == "https://random-words-1234.trycloudflare.com"


def test_parse_tunnel_url_returns_none_without_url():
    assert parse_tunnel_url("INF Registered tunnel connection connIndex=0") is None
    assert parse_tunnel_url("") is None
    assert parse_tunnel_url("http://127.0.0.1:7860 局域网地址") is None


def test_find_cloudflared_found(tmp_path, monkeypatch):
    monkeypatch.setattr("core.tunnel.shutil.which", lambda *a, **k: None)
    (tmp_path / "cloudflared.exe").write_bytes(b"MZ")
    assert find_cloudflared(search_dirs=[tmp_path]) == tmp_path / "cloudflared.exe"


def test_find_cloudflared_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("core.tunnel.shutil.which", lambda *a, **k: None)
    assert find_cloudflared(search_dirs=[tmp_path]) is None


def test_cleanup_stale_tunnel_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr("core.tunnel.PID_FILE", tmp_path / "cloudflared.pid")
    cleanup_stale_tunnel()  # 无文件时应无异常、不执行任何操作


def test_cleanup_stale_tunnel_non_numeric_pid(tmp_path, monkeypatch):
    pid_file = tmp_path / "cloudflared.pid"
    monkeypatch.setattr("core.tunnel.PID_FILE", pid_file)
    pid_file.write_text("abc", encoding="utf-8")
    cleanup_stale_tunnel()
    assert not pid_file.exists()


def test_cleanup_stale_tunnel_process_gone(tmp_path, monkeypatch):
    pid_file = tmp_path / "cloudflared.pid"
    monkeypatch.setattr("core.tunnel.PID_FILE", pid_file)
    pid_file.write_text("999999", encoding="utf-8")
    monkeypatch.setattr(
        "core.tunnel.subprocess.run",
        lambda *a, **k: type("R", (), {"stdout": "INFO: No tasks are running."})(),
    )
    cleanup_stale_tunnel()
    assert not pid_file.exists()


def test_cleanup_stale_tunnel_kills_cloudflared(tmp_path, monkeypatch):
    pid_file = tmp_path / "cloudflared.pid"
    monkeypatch.setattr("core.tunnel.PID_FILE", pid_file)
    pid_file.write_text("1234", encoding="utf-8")
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if "tasklist" in args:
            return type("R", (), {"stdout": "cloudflared.exe ... PID 1234"})()
        return type("R", (), {"stdout": ""})()

    monkeypatch.setattr("core.tunnel.subprocess.run", fake_run)
    cleanup_stale_tunnel()
    assert any("taskkill" in call for call in calls)
    assert not pid_file.exists()


class _FakeProc:
    def __init__(self, dead_after: int):
        self._calls = 0
        self._dead_after = dead_after

    def poll(self):
        self._calls += 1
        return 0 if self._calls <= self._dead_after else 1


def test_tunnel_watcher_reconnects(monkeypatch):
    urls = iter(
        ["https://a.trycloudflare.com", "https://b.trycloudflare.com"]
    )

    def fake_start(path, local_url="", timeout=90):
        url = next(urls, None)
        proc = _FakeProc(dead_after=0 if url == "https://a.trycloudflare.com" else 10)
        return url, proc

    monkeypatch.setattr("core.tunnel.start_quick_tunnel", fake_start)
    notified = []
    watcher = TunnelWatcher(
        "cloudflared.exe",
        check_interval=0.05,
        restart_delay=0.05,
        on_url=notified.append,
    )
    watcher.start()
    time.sleep(0.5)
    watcher.stop()
    assert "https://b.trycloudflare.com" in notified
