# -*- coding: utf-8 -*-
"""Cloudflare 隧道工具函数测试。

PID 记录与隧道目标的归属是本模块的重点风险：新版 API 与旧 Gradio 可能同时在跑，
两者的进程记录不能互相覆盖，清理也不能波及其他正在运行的隧道。
"""

import io
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import time

import pytest

from core.tunnel import (
    TunnelWatcher,
    cleanup_stale_tunnel,
    find_cloudflared,
    parse_tunnel_url,
    start_quick_tunnel,
    stop_tunnel,
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


def test_tunnel_watcher_reconnects(monkeypatch, tmp_path):
    urls = iter(
        ["https://a.trycloudflare.com", "https://b.trycloudflare.com"]
    )
    seen: list[tuple[str, object, object]] = []

    def fake_start(path, local_url="", timeout=90, pid_file=None, cancel_event=None):
        # 记录每次建立用的目标地址、PID 归属与取消信号（含重连）
        seen.append((local_url, pid_file, cancel_event))
        url = next(urls, None)
        proc = _FakeProc(dead_after=0 if url == "https://a.trycloudflare.com" else 10)
        return url, proc

    monkeypatch.setattr("core.tunnel.start_quick_tunnel", fake_start)
    notified = []
    own_pid = tmp_path / "cloudflared-8123.pid"
    watcher = TunnelWatcher(
        "cloudflared.exe",
        "http://127.0.0.1:8123",
        check_interval=0.05,
        restart_delay=0.05,
        on_url=notified.append,
        pid_file=own_pid,
    )
    watcher.start()
    time.sleep(0.5)
    watcher.stop()
    assert "https://b.trycloudflare.com" in notified
    # 重连不得回落到写死的 7860 或全局 PID 文件：每次建立都用本次的实际归属
    assert len(seen) >= 2
    assert all(local == "http://127.0.0.1:8123" and pid == own_pid for local, pid, _ in seen)
    # 每次建立（含重连）都拿到 watcher 的停止信号，等待才能被停止立即打断
    assert all(cancel is watcher._stop for _local, _pid, cancel in seen)


class _RecordedProc:
    """假 cloudflared 进程：输出里带公网 URL，可观察 terminate/kill 是否被调用。"""

    pid = 777

    def __init__(self, lines: list[str]) -> None:
        self.stdout = io.StringIO("\n".join(lines) + "\n")
        self.terminated = 0
        self.killed = 0

    def poll(self):
        return None

    def terminate(self):
        self.terminated += 1

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed += 1


def test_start_quick_tunnel_owns_only_its_pid_file(tmp_path, monkeypatch):
    """按实例归属 PID：只写/只删调用方给的文件，旧启动的记录原样保留。

    防住的独立风险：新版 API 覆盖 data/cloudflared.pid（或退出时删掉它），
    旧 Gradio 的隧道就失去了归属，退出时会停错进程或留下无人认领的隧道。
    """
    legacy = tmp_path / "cloudflared.pid"
    legacy.write_text("4242", encoding="utf-8")
    monkeypatch.setattr("core.tunnel.PID_FILE", legacy)
    own = tmp_path / "run" / "cloudflared-8123.pid"

    started: list[list[str]] = []

    def fake_popen(args, **kwargs):
        started.append(list(args))
        return _RecordedProc(["https://port-check.trycloudflare.com"])

    monkeypatch.setattr("core.tunnel.subprocess.Popen", fake_popen)

    url, proc = start_quick_tunnel(
        "cloudflared.exe", "http://127.0.0.1:8123", pid_file=own
    )
    assert url == "https://port-check.trycloudflare.com"
    # 隧道目标是本次启动的实际端口
    assert started[0][started[0].index("--url") + 1] == "http://127.0.0.1:8123"
    assert own.read_text(encoding="utf-8") == "777"
    assert legacy.read_text(encoding="utf-8") == "4242"

    stop_tunnel(proc, pid_file=own)
    assert proc.terminated == 1
    assert not own.exists()
    # 清理只动自己那份：旧启动的 PID 文件仍在，进程归属没有被抹掉
    assert legacy.read_text(encoding="utf-8") == "4242"


def test_start_quick_tunnel_timeout_stops_only_own_pid(tmp_path, monkeypatch):
    """建立超时：已结束假进程并删除自己的 PID 记录，不触碰旧启动的记录。"""
    legacy = tmp_path / "cloudflared.pid"
    legacy.write_text("4242", encoding="utf-8")
    monkeypatch.setattr("core.tunnel.PID_FILE", legacy)
    own = tmp_path / "cloudflared-8123.pid"
    monkeypatch.setattr(
        "core.tunnel.subprocess.Popen",
        lambda args, **kwargs: _RecordedProc(["INF 还没有公网地址"]),
    )

    # 超时被压到 0.01 秒：等待循环最多睡一轮，测试不真等 90 秒
    url, proc = start_quick_tunnel(
        "cloudflared.exe", "http://127.0.0.1:8123", timeout=0.01, pid_file=own
    )
    assert url is None and proc is None
    assert not own.exists()
    assert legacy.read_text(encoding="utf-8") == "4242"


def test_cleanup_stale_tunnel_touches_only_its_own_pid_file(tmp_path, monkeypatch):
    """残留清理按归属：只读并只删本次的 PID 文件，绝不结束别的实例记录的进程。

    防住的独立风险：新启动依据旧 Gradio 留下的 pid 把仍在使用的隧道一起 taskkill。
    """
    own = tmp_path / "cloudflared-8123.pid"
    other = tmp_path / "cloudflared.pid"
    own.write_text("1234", encoding="utf-8")
    other.write_text("5678", encoding="utf-8")
    monkeypatch.setattr("core.tunnel.PID_FILE", other)
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        # 无论查询结果如何，只有 pid=1234（本次那份）允许被结束
        return type("R", (), {"stdout": "cloudflared.exe  PID " + args[-1]})()

    monkeypatch.setattr("core.tunnel.subprocess.run", fake_run)
    cleanup_stale_tunnel(pid_file=own)

    kills = [args for args in calls if args[0] == "taskkill"]
    assert kills == [["taskkill", "/PID", "1234", "/F"]]
    assert not own.exists()
    assert other.exists() and other.read_text(encoding="utf-8") == "5678"


# ===================== 建立/重连期间的取消与中断 =====================


class _BlockStdoutProc:
    """假 cloudflared：stdout 读取阻塞（公网地址迟迟不来），可观察 terminate/kill。"""

    pid = 777

    def __init__(self, created: threading.Event | None = None) -> None:
        self._gate = threading.Event()
        self.stdout = _BlockedStream(self._gate)
        self.terminated = 0
        self.killed = 0
        self._created = created
        if created is not None:
            created.set()

    def poll(self):
        return None

    def terminate(self):
        self.terminated += 1

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed += 1

    def release(self) -> None:
        self._gate.set()


class _BlockingProc:
    """假 cloudflared：首次轮询（主线程正在等公网 URL）抛出异常，模拟 Ctrl+C。"""

    pid = 777

    def __init__(self, exc: BaseException) -> None:
        self.stdout = io.StringIO("INF 还没有公网地址\n")
        self.terminated = 0
        self.killed = 0
        self._exc = exc
        self._fired = False

    def poll(self):
        if not self._fired:
            self._fired = True
            raise self._exc
        return None

    def terminate(self):
        self.terminated += 1

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed += 1


class _BlockedStream:
    def __init__(self, gate: threading.Event) -> None:
        self._gate = gate

    def __iter__(self):
        self._gate.wait(10)
        return iter(())


def _proc_factory(procs: list, exc: BaseException | None = None):
    def fake_popen(args, **kwargs):
        proc = (
            _BlockingProc(exc) if exc is not None else _BlockStdoutProc()
        )
        procs.append(proc)
        return proc

    return fake_popen


def _wait_flag(flag: threading.Event, limit: float = 5.0) -> bool:
    deadline = time.monotonic() + limit
    while not flag.is_set() and time.monotonic() < deadline:
        time.sleep(0.01)
    return flag.is_set()


def _wait_until(predicate, limit: float = 5.0) -> bool:
    deadline = time.monotonic() + limit
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    return predicate()


def test_cancel_event_ends_url_wait_without_full_timeout(tmp_path, monkeypatch):
    """等待公网地址时可被停止信号立即打断：不等满超时，并回收进程与 PID。

    防住的独立风险：关闭时要等满 90 秒建立超时才能退出，
    期间隧道进程与 PID 记录都还挂着。
    """
    own = tmp_path / "cloudflared-8123.pid"
    created = threading.Event()
    procs: list[_BlockStdoutProc] = []

    def fake_popen(args, **kwargs):
        proc = _BlockStdoutProc(created)
        procs.append(proc)
        return proc

    monkeypatch.setattr("core.tunnel.subprocess.Popen", fake_popen)
    cancel = threading.Event()
    outcome: list = []

    def worker():
        outcome.append(
            start_quick_tunnel(
                "cloudflared.exe",
                "http://127.0.0.1:8123",
                timeout=600,
                pid_file=own,
                cancel_event=cancel,
            )
        )

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert _wait_flag(created), "假 cloudflared 进程未被创建"
    assert _wait_until(lambda: own.exists()), "PID 文件没有按归属写出"
    started_at = time.monotonic()
    cancel.set()
    thread.join(timeout=5)

    procs[0].release()
    assert not thread.is_alive(), "取消后仍在等满完整建立超时"
    assert time.monotonic() - started_at < 5
    assert outcome[0] == (None, None)
    assert procs[0].terminated == 1
    assert not own.exists()


def test_interrupt_while_waiting_for_url_reclaims_child_and_pid(tmp_path, monkeypatch):
    """等待期间异常（Ctrl+C 的等价效果）：子进程被终止、PID 被清掉、原异常继续传播。

    防住的独立风险（已用假进程复现）：子进程已创建但还没交给 watcher，
    异常一路传到 close() 也没人认它，terminate/kill 都没调用、PID 文件仍在。
    """
    own = tmp_path / "cloudflared-8123.pid"
    procs: list[_BlockingProc] = []
    monkeypatch.setattr("core.tunnel.subprocess.Popen", _proc_factory(procs, KeyboardInterrupt()))

    with pytest.raises(KeyboardInterrupt):
        start_quick_tunnel(
            "cloudflared.exe", "http://127.0.0.1:8123", timeout=600, pid_file=own
        )

    assert len(procs) == 1
    assert procs[0].terminated == 1
    assert not own.exists()


def test_watcher_start_interrupted_while_waiting_reclaims_process(tmp_path, monkeypatch):
    """watcher 首次建立期间被打断：真实 start_quick_tunnel 就地回收，stop() 不再漏。

    防住的独立风险：LaunchSupport.close() 之后仍留下正在运行的 cloudflared 子进程。
    """
    own = tmp_path / "cloudflared-8123.pid"
    procs: list[_BlockingProc] = []
    monkeypatch.setattr("core.tunnel.subprocess.Popen", _proc_factory(procs, KeyboardInterrupt()))

    watcher = TunnelWatcher(
        "cloudflared.exe", "http://127.0.0.1:8123", pid_file=own, on_url=lambda url: None
    )
    with pytest.raises(KeyboardInterrupt):
        watcher.start()

    assert procs[0].terminated == 1
    assert not own.exists()
    assert watcher.proc is None
    # close() 会调用 stop()：已经收尾过，不该再动别的进程，也不该报错
    watcher.stop()
    assert procs[0].terminated == 1
    assert not own.exists()


def test_cleanup_error_does_not_replace_original_exception(tmp_path, monkeypatch):
    """回收过程本身报错：只提示，仍然传播原来的 KeyboardInterrupt。

    防住的独立风险：finally/except 里的清理异常顶掉启动异常，退出码与诊断都不对。
    """
    own = tmp_path / "cloudflared-8123.pid"
    procs: list[_BlockingProc] = []
    monkeypatch.setattr("core.tunnel.subprocess.Popen", _proc_factory(procs, KeyboardInterrupt()))

    def broken_stop(proc, *, pid_file=None):
        raise RuntimeError("模拟回收失败")

    monkeypatch.setattr("core.tunnel.stop_tunnel", broken_stop)
    with pytest.raises(KeyboardInterrupt):
        start_quick_tunnel(
            "cloudflared.exe", "http://127.0.0.1:8123", timeout=600, pid_file=own
        )


def test_stop_during_first_establishment_is_prompt_and_leaves_no_process(tmp_path, monkeypatch):
    """停止信号落在首次建立期间：stop() 不被建立拖住，晚到的进程仍被回收。

    防住的独立风险：stop() 要等建立操作结束（最坏完整超时）才能取得锁，
    并且建立结束时把进程登记回来，留下一个没人再停的 cloudflared。
    """
    started = threading.Event()
    release = threading.Event()
    created: list[_BlockStdoutProc] = []
    cancels: list[threading.Event] = []

    def fake_start(path, local_url="", timeout=90, pid_file=None, cancel_event=None):
        proc = _BlockStdoutProc(started)
        created.append(proc)
        cancels.append(cancel_event)
        started.wait(5)
        release.wait(5)
        return "https://late.trycloudflare.com", proc

    monkeypatch.setattr("core.tunnel.start_quick_tunnel", fake_start)
    notified: list[str] = []
    watcher = TunnelWatcher(
        "cloudflared.exe",
        "http://127.0.0.1:8123",
        on_url=notified.append,
        pid_file=tmp_path / "cloudflared-8123.pid",
    )
    thread = threading.Thread(target=watcher.start, daemon=True)
    thread.start()
    assert _wait_flag(started), "首次建立未开始"

    stop_at = time.monotonic()
    watcher.stop()
    stop_elapsed = time.monotonic() - stop_at
    release.set()
    thread.join(timeout=5)
    watcher_proc_cleanup = created[0].terminated

    assert not thread.is_alive()
    assert stop_elapsed < 2, "stop() 被建立过程拖住（未响应停止信号）"
    assert cancels and cancels[0] is watcher._stop, "建立过程没拿到停止信号"
    assert watcher_proc_cleanup == 1, "停止后才建立的进程没有被回收"
    assert watcher.proc is None and watcher.url is None
    assert notified == [], "已停止却把公网地址回调给了启动方"


def test_stop_during_reconnect_leaves_no_new_process(tmp_path, monkeypatch):
    """重连期间停止：等待立即结束、不登记新进程，之后不再重建。

    防住的独立风险：断线重连正进行时退出，新起的 cloudflared 无人认领；
    或停止后仍在反复重连。
    """
    reconnect_started = threading.Event()
    release = threading.Event()
    procs: list[_BlockStdoutProc] = []
    cancels: list[threading.Event] = []
    calls = {"n": 0}

    def fake_start(path, local_url="", timeout=90, pid_file=None, cancel_event=None):
        cancels.append(cancel_event)
        calls["n"] += 1
        if calls["n"] == 1:
            # 首次建立：拿到一个"随后会退出"的进程，触发重连
            return "https://first.trycloudflare.com", _DeadProc(after=1)
        reconnect_started.set()
        release.wait(5)
        proc = _BlockStdoutProc()
        procs.append(proc)
        return "https://second.trycloudflare.com", proc

    monkeypatch.setattr("core.tunnel.start_quick_tunnel", fake_start)
    watcher = TunnelWatcher(
        "cloudflared.exe",
        "http://127.0.0.1:8123",
        check_interval=0.02,
        restart_delay=0.02,
        pid_file=tmp_path / "cloudflared-8123.pid",
    )
    watcher.start()
    assert _wait_flag(reconnect_started), "断线后未进入重连"

    stop_at = time.monotonic()
    watcher.stop()
    stop_elapsed = time.monotonic() - stop_at
    release.set()
    deadline = time.monotonic() + 3
    while not procs and time.monotonic() < deadline:
        time.sleep(0.02)

    assert stop_elapsed < 2, "stop() 要等重连结束才拿到锁"
    assert procs and procs[0].terminated == 1, "停止后才建立的隧道进程没有被回收"
    assert watcher.proc is None
    time.sleep(0.2)
    assert calls["n"] == 2, "停止后仍在继续重连"
    assert all(cancel is watcher._stop for cancel in cancels)


class _DeadProc:
    """按轮询次数转为"已退出"的假进程：用于触发重连。"""

    pid = 888

    def __init__(self, after: int = 1) -> None:
        self._after = after
        self._calls = 0
        self.terminated = 0

    def poll(self):
        self._calls += 1
        return None if self._calls <= self._after else 1

    def terminate(self):
        self.terminated += 1

    def wait(self, timeout=None):
        return 0
