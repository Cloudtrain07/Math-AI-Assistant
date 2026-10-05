"""Cloudflare 快速隧道封装：查找 cloudflared、解析并启动/停止快速隧道。

local_url 与 pid_file 都由调用方显式给出（新版 API 按本次实际监听端口与自己的
数据目录传入）；缺省值保持旧 Gradio 启动的原有行为：目标 7860、PID 写在
data/cloudflared.pid。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
# 旧启动方式的单例 PID 文件；按实例传入 pid_file 时不读写此路径
PID_FILE = DATA_DIR / "cloudflared.pid"
LOCAL_URL = "http://127.0.0.1:7860"
TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
DEFAULT_TIMEOUT = 90.0


def resolve_pid_file(pid_file: Path | str | None = None) -> Path:
    """本次隧道的 PID 文件归属：显式传入优先，否则沿用全局 PID_FILE。"""
    return Path(pid_file) if pid_file is not None else PID_FILE


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
    *,
    pid_file: Path | str | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple[str | None, subprocess.Popen | None]:
    """启动快速隧道并等待公网 URL；超时、失败或收到取消信号返回 (None, None)。

    启动后后台线程持续排空子进程输出，避免管道缓冲区写满阻塞隧道。
    pid_file 决定本次进程记录的归属（不传则沿用全局 PID_FILE）。
    cancel_event 置位时立即结束等待并回收刚创建的进程，不必等满 timeout。

    子进程已创建但还没交给调用方的任何异常（含 KeyboardInterrupt）都在此处回收：
    先终止进程并删除本次 PID 记录，再把原异常继续向外传播，不掩盖也不吞掉。
    """
    proc: subprocess.Popen | None = None
    try:
        proc = subprocess.Popen(
            [str(cloudflared_path), "tunnel", "--url", local_url, "--no-autoupdate"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        _write_pid(proc, pid_file)
        result: dict[str, str | None] = {"url": None}

        def _reader() -> None:
            try:
                assert proc is not None and proc.stdout is not None
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
            if cancel_event is not None and cancel_event.is_set():
                break
            if result["url"]:
                return result["url"], proc
            if proc.poll() is not None:
                break
            if cancel_event is not None:
                # 等待可被停止信号立即打断（而不是睡满一轮）
                cancel_event.wait(0.3)
            else:
                time.sleep(0.3)
        stop_tunnel(proc, pid_file=pid_file)
        return None, None
    except BaseException:
        if proc is not None:
            try:
                stop_tunnel(proc, pid_file=pid_file)
            except BaseException as exc:
                # 只提示：清理失败不能顶掉正在传播的原异常
                print(f"⚠️ 隧道子进程回收失败，请手动关闭本次启动的 cloudflared：{exc}")
        raise


def stop_tunnel(proc: subprocess.Popen | None, *, pid_file: Path | str | None = None) -> None:
    """终止并回收 cloudflared 子进程（尽力而为），只清理自己那份 PID 记录。"""
    if proc is None:
        return
    try:
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
    finally:
        # 终止过程出错也要清掉自己的进程记录，否则下次启动会认错归属
        _remove_pid(pid_file)


def _write_pid(proc: subprocess.Popen, pid_file: Path | str | None = None) -> None:
    """把 cloudflared 进程 PID 写入本次归属的 PID 文件，便于下次启动清理残留。"""
    target = resolve_pid_file(pid_file)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(proc.pid), encoding="utf-8")
    except Exception:
        pass


def _remove_pid(pid_file: Path | str | None = None) -> None:
    try:
        resolve_pid_file(pid_file).unlink(missing_ok=True)
    except Exception:
        pass


def cleanup_stale_tunnel(pid_file: Path | str | None = None) -> None:
    """启动前清理上次运行残留的 cloudflared 进程。

    只依据给定归属的 pid 文件操作（按 PID 精确结束，绝不按进程名批量清理），
    因此不会碰到旧 Gradio 或其他仍在运行的隧道。
    """
    own_file = resolve_pid_file(pid_file)
    if not own_file.exists():
        return
    try:
        pid = int(own_file.read_text(encoding="utf-8").strip())
    except ValueError:
        _remove_pid(own_file)
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
    _remove_pid(own_file)


class TunnelWatcher:
    """管理 cloudflared 快速隧道，进程退出后自动重启并回调新公网 URL。"""

    def __init__(
        self,
        cloudflared_path: Path | str,
        local_url: str = LOCAL_URL,
        on_url=None,
        check_interval: float = 5.0,
        restart_delay: float = 15.0,
        *,
        pid_file: Path | str | None = None,
    ) -> None:
        self._path = Path(cloudflared_path)
        self._local_url = local_url
        self._on_url = on_url
        self._check_interval = check_interval
        self._restart_delay = restart_delay
        self._pid_file = pid_file
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.url: str | None = None

    def start(self) -> None:
        url, proc = start_quick_tunnel(
            self._path,
            self._local_url,
            pid_file=self._pid_file,
            cancel_event=self._stop,
        )
        # 建立期间已收到停止信号：进程已被回收，不登记，也不把公网地址回调出去
        if not self._adopt(proc):
            url = None
        self.url = url
        if self.url:
            self._notify_url()
        threading.Thread(target=self._watch, daemon=True).start()

    def _adopt(self, proc: subprocess.Popen | None) -> bool:
        """把刚建立、尚未登记的子进程交给本 watcher。

        返回 False 表示进程未被接管（没有建立成功，或期间已收到停止信号）；
        已停止时在此就地回收，不给调用方留下新创建的进程。
        与 stop() 共用一把锁：进程要么在 stop() 之前被接管（由 stop() 回收），
        要么在此处回收，不存在两边都不认的窗口。
        """
        if proc is None:
            return False
        with self._lock:
            if self._stop.is_set():
                keep = False
            else:
                self.proc = proc
                keep = True
        if not keep:
            try:
                stop_tunnel(proc, pid_file=self._pid_file)
            except Exception as exc:
                print(f"⚠️ 隧道子进程回收失败，请手动关闭本次启动的 cloudflared：{exc}")
        return keep

    def _notify_url(self) -> None:
        if self._on_url is not None and self.url:
            try:
                self._on_url(self.url)
            except Exception:
                pass

    def _watch(self) -> None:
        while not self._stop.is_set():
            if self._stop.wait(self._check_interval):
                break
            with self._lock:
                proc = self.proc
            if proc is not None and proc.poll() is not None:
                print("公网隧道已断开，正在自动重连…")
                if self._stop.wait(self._restart_delay):
                    break
                # 建立过程不持锁：stop() 需要随时收回当前进程，
                # 不能等到建立结束（最坏是完整建立超时）才拿到锁
                url, new_proc = start_quick_tunnel(
                    self._path,
                    self._local_url,
                    pid_file=self._pid_file,
                    cancel_event=self._stop,
                )
                if new_proc is not None and self._adopt(new_proc):
                    if url:
                        self.url = url
                        self._notify_url()
                        print(f"公网链接已更新：{url}")
                # 建立失败（new_proc 为 None）时下一轮继续重试；已停止则退出
            if self._stop.is_set():
                break

    def stop(self) -> None:
        # 先置停止信号：正在等待公网地址的建立会立即结束并回收自己的进程
        self._stop.set()
        with self._lock:
            stop_tunnel(self.proc, pid_file=self._pid_file)
            self.proc = None
