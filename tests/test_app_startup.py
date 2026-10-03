# -*- coding: utf-8 -*-
"""__main__ 启动边界测试：启动参数、关键先后关系、退出清理与异常传播。

launch/queue/block_thread/close 一律打桩，不起真实服务器；
备份、隧道、访问信息写入与局域网探测在调用 main 前于 app 模块名空间
替换为记录型假实现，core 层真实实现不执行。
真实服务器的启动与关闭由仓库外隔离实跑验证；真实 Ctrl+C 退出留人工验收。
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gradio as gr
import pytest

import app as app_module

_ORIG_QUEUE = gr.Blocks.queue
_ORIG_CLOSE = gr.Blocks.close


class _FakeWatcher:
    """记录型假隧道 watcher：start 时同步回调 URL，模拟现有 _notify_url 时机。"""

    def __init__(self, fakes, cloudflared_path, on_url):
        self._fakes = fakes
        self.cloudflared_path = cloudflared_path
        self.on_url = on_url
        self.url = None
        self.stop_calls = 0

    def start(self):
        self._fakes.calls.append(("watcher_start",))
        self.url = self._fakes.watcher_url
        if self.url and self.on_url is not None:
            self.on_url(self.url)

    def stop(self):
        self.stop_calls += 1
        self._fakes.calls.append(("watcher_stop",))


class StartupFakes:
    """main() 的一套共享假实现：记录关键调用，按用例配置失败点。"""

    def __init__(self, tmp_path):
        self.calls = []
        self.access_path = tmp_path / "访问信息.txt"
        self.watcher_url = "https://fake.trycloudflare.com"
        self.cloudflared = None  # None=未找到；Path=找到并启动假 watcher
        self.launch_error = None  # 非 None 时 launch 抛出该异常
        self.save_fail_from = 0  # 第 N 次 save_access_info 起抛错；0=永不
        self.backup_stop_events = []
        self.watcher_instances = []
        self._save_count = 0

    # ---- 备份 ----
    def ensure_today_backup(self):
        self.calls.append(("backup_ensure",))
        return None

    def daily_backup_loop(self, interval_seconds=3600, stop_event=None):
        self.calls.append(("backup_loop_start",))
        self.backup_stop_events.append(stop_event)
        if stop_event is not None and stop_event.wait(5):
            self.calls.append(("backup_loop_stopped",))

    # ---- 隧道 ----
    def cleanup_stale_tunnel(self):
        self.calls.append(("cleanup_stale",))

    def find_cloudflared(self, search_dirs=None):
        self.calls.append(("find_cloudflared",))
        return self.cloudflared

    def tunnel_watcher(self, cloudflared_path, on_url=None, **kwargs):
        watcher = _FakeWatcher(self, cloudflared_path, on_url)
        self.watcher_instances.append(watcher)
        return watcher

    # ---- 访问信息 / 局域网探测 ----
    def save_access_info(self, url, accounts, lan_url=None, target=None):
        self.calls.append(("save", url, list(accounts)))
        self._save_count += 1
        if self.save_fail_from and self._save_count >= self.save_fail_from:
            raise RuntimeError("模拟访问信息写盘失败")
        return self.access_path

    def get_lan_ip(self):
        return "192.0.2.7"


def _call_names(fakes):
    return [c[0] for c in fakes.calls]


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    """替换 main() 触达的全部真实操作（调用 main 前完成）并打桩 gr.Blocks。"""
    f = StartupFakes(tmp_path)
    monkeypatch.setattr(app_module, "ensure_today_backup", f.ensure_today_backup)
    monkeypatch.setattr(app_module, "daily_backup_loop", f.daily_backup_loop)
    monkeypatch.setattr(app_module, "cleanup_stale_tunnel", f.cleanup_stale_tunnel)
    monkeypatch.setattr(app_module, "find_cloudflared", f.find_cloudflared)
    monkeypatch.setattr(app_module, "TunnelWatcher", f.tunnel_watcher)
    monkeypatch.setattr(app_module, "save_access_info", f.save_access_info)
    monkeypatch.setattr(app_module, "get_lan_ip", f.get_lan_ip)

    def queue(demo_self, **kwargs):
        f.calls.append(("queue", kwargs))
        return _ORIG_QUEUE(demo_self, **kwargs)

    def launch(demo_self, **kwargs):
        f.calls.append(("launch", dict(kwargs)))
        if f.launch_error is not None:
            raise f.launch_error

    def block_thread(demo_self):
        f.calls.append(("block_thread",))

    def close(demo_self, *args, **kwargs):
        f.calls.append(("close",))
        return _ORIG_CLOSE(demo_self, *args, **kwargs)

    monkeypatch.setattr(gr.Blocks, "queue", queue)
    monkeypatch.setattr(gr.Blocks, "launch", launch)
    monkeypatch.setattr(gr.Blocks, "block_thread", block_thread)
    monkeypatch.setattr(gr.Blocks, "close", close)
    monkeypatch.delenv("GRADIO_SERVER_NAME", raising=False)
    monkeypatch.setenv("AUTO_OPEN_BROWSER", "false")
    return f


def test_main_normal_startup_runs_cleanup(fakes):
    """正常启动：参数与原 __main__ 一致，退出后备份/watcher/界面均已收尾。"""
    fakes.cloudflared = Path("cloudflared.exe")
    app_module.main()

    # 必要参数（SERVER_PORT 保持默认 7860，不打真实端口）
    launch_kwargs = next(c[1] for c in fakes.calls if c[0] == "launch")
    assert launch_kwargs["server_port"] == app_module.SERVER_PORT == 7860
    assert launch_kwargs["server_name"] == "0.0.0.0"
    assert launch_kwargs["inbrowser"] is False
    assert launch_kwargs["prevent_thread_lock"] is True
    assert launch_kwargs["auth_message"] == "请输入访问用户名与密码"
    assert list(inspect.signature(launch_kwargs["auth"]).parameters) == [
        "username",
        "password",
    ]  # runtime 首参被 _bind 隐去
    # main 的显式 queue 调用（create_app 内部事件绑定也会触发无参 queue()，按参数定位）
    explicit_queue = [
        c[1]
        for c in fakes.calls
        if c[0] == "queue" and c[1].get("default_concurrency_limit") == 4
    ]
    assert len(explicit_queue) == 1

    names = _call_names(fakes)
    # 关键先后关系：备份 → 启动服务器 → 清残留/隧道 → 等待退出 → 收尾
    assert names.index("backup_ensure") < names.index("launch") < names.index(
        "cleanup_stale"
    )
    assert names.index("watcher_start") < names.index("block_thread")
    assert names.index("block_thread") < names.index("watcher_stop") < names.index(
        "close"
    )
    # 备份停止信号发生在退出等待之后
    assert names.index("backup_loop_stopped") > names.index("block_thread")

    # 现有 URL 回调写入行为：watcher start 内回调一次 + 主路径一次，均带隧道 URL
    save_calls = [c for c in fakes.calls if c[0] == "save"]
    assert [c[1] for c in save_calls] == [fakes.watcher_url, fakes.watcher_url]
    assert all(c[2] for c in save_calls)  # 传入账号清单且非空

    # 已启动资源收尾：备份信号、watcher 停止一次、界面关闭一次
    assert fakes.backup_stop_events and all(
        ev.is_set() for ev in fakes.backup_stop_events
    )
    assert fakes.watcher_instances[0].stop_calls == 1
    assert names.count("close") == 1


def test_main_launch_failure_propagates_and_signals_backup(fakes):
    """launch 抛 OSError：异常原样传播，备份收到停止信号，后续步骤未触达。"""
    fakes.launch_error = OSError("模拟端口占用")
    with pytest.raises(OSError, match="模拟端口占用"):
        app_module.main()

    assert fakes.backup_stop_events and all(
        ev.is_set() for ev in fakes.backup_stop_events
    )
    names = _call_names(fakes)
    assert not {"cleanup_stale", "find_cloudflared", "watcher_start", "save"} & set(
        names
    )
    assert "close" in names  # finally 收尾仍执行（未启动时 close 幂等返回）


def test_main_access_info_failure_still_cleans_up(fakes):
    """访问信息写盘失败：异常保留，且隧道、界面服务器与备份线程均已收尾。"""
    fakes.cloudflared = Path("cloudflared.exe")
    fakes.save_fail_from = 2  # 第 1 次（URL 回调）成功，第 2 次（主路径）失败
    with pytest.raises(RuntimeError, match="模拟访问信息写盘失败"):
        app_module.main()

    names = _call_names(fakes)
    assert names.count("watcher_start") == 1  # 失败前服务器与隧道均已启动
    assert fakes.watcher_instances[0].stop_calls == 1
    assert "close" in names
    assert fakes.backup_stop_events and all(
        ev.is_set() for ev in fakes.backup_stop_events
    )
