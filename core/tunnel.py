"""Cloudflare 快速隧道封装：查找 cloudflared、解析并启动/停止快速隧道。"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
PID_FILE = DATA_DIR / "cloudflared.pid"
LOCAL_URL = "http://127.0.0.1:7860"
TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
DEFAULT_TIMEOUT = 90.0


def find_cloudflared(search_dirs: list[Path] | None = None) -> Path | None:
    """在指定目录（默认项目根目录）与 PATH 中查找 cloudflared / cloudflared.exe。"""
    dirs = [Path(d) for d in (search_dirs if search_dirs is not None else [PROJECT_DIR])]
    for directory in dirs:
        for name in ("cloudflared.exe", "cloudflared"):
            candidate = directory / name
            if candidate.exists():
                return candidate
    found = shutil.which("cloudflared")
    if found:
        return Path(found)
    return None


def parse_tunnel_url(text: str) -> str | None:
    """从 cloudflared 输出中提取快速隧道 URL；没有则返回 None。"""
    match = TUNNEL_URL_RE.search(text or "")
    return match.group(0) if match else None


def start_quick_tunnel(
    cloudflared_path: Path | str,
    local_url: str = LOCAL_URL,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[str | None, subprocess.Popen | None]:
    """启动快速隧道并等待公网 URL；超时或失败返回 (None, None)。

    启动后后台线程持续排空子进程输出，避免管道缓冲区写满阻塞隧道。
    """
    proc = subprocess.Popen(
        [str(cloudflared_path), "tunnel", "--url", local_url, "--no-autoupdate"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    _write_pid(proc)
    result: dict[str, str | None] = {"url": None}

    def _reader() -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if result["url"] is None:
                    url = parse_tunnel_url(line)
                    if url:
                        result["url"] = url
        except Exception:
            pass

    threading.Thread(target=_reader, daemon=True).start()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result["url"]:
            return result["url"], proc
        if proc.poll() is not None:
            break
        time.sleep(0.3)
    stop_tunnel(proc)
    return None, None


def stop_tunnel(proc: subprocess.Popen | None) -> None:
    """终止并回收 cloudflared 子进程（尽力而为）。"""
    if proc is None:
        return
    if proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    _remove_pid()


def _write_pid(proc: subprocess.Popen) -> None:
    """把 cloudflared 进程 PID 写入 data/cloudflared.pid，便于下次启动清理残留。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    except Exception:
        pass


def _remove_pid() -> None:
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def cleanup_stale_tunnel() -> None:
    """启动前清理上次运行残留的 cloudflared 进程（依据 pid 文件，只动自己启动的）。"""
    if not PID_FILE.exists():
        return
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except ValueError:
        _remove_pid()
        return
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout or ""
    except Exception:
        return
    if "cloudflared" in out.lower():
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    _remove_pid()


class TunnelWatcher:
    """管理 cloudflared 快速隧道，进程退出后自动重启并回调新公网 URL。"""

    def __init__(
        self,
        cloudflared_path: Path | str,
        local_url: str = LOCAL_URL,
        on_url=None,
        check_interval: float = 5.0,
        restart_delay: float = 15.0,
    ) -> None:
        self._path = Path(cloudflared_path)
        self._local_url = local_url
        self._on_url = on_url
        self._check_interval = check_interval
        self._restart_delay = restart_delay
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.url: str | None = None

    def start(self) -> None:
        self.url, self.proc = start_quick_tunnel(self._path, self._local_url)
        if self.url:
            self._notify_url()
        threading.Thread(target=self._watch, daemon=True).start()

    def _notify_url(self) -> None:
        if self._on_url is not None and self.url:
            try:
                self._on_url(self.url)
            except Exception:
                pass

    def _watch(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self._check_interval)
            if self._stop.is_set():
                break
            with self._lock:
                proc = self.proc
            if proc is not None and proc.poll() is not None:
                print("公网隧道已断开，正在自动重连…")
                self._stop.wait(self._restart_delay)
                if self._stop.is_set():
                    break
                with self._lock:
                    url, new_proc = start_quick_tunnel(self._path, self._local_url)
                    if new_proc is not None:
                        self.proc = new_proc
                    if url:
                        self.url = url
                        self._notify_url()
                        print(f"公网链接已更新：{url}")

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            stop_tunnel(self.proc)
            self.proc = None
