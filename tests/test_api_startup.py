# -*- coding: utf-8 -*-
"""新版 API 的启动配套测试：每日备份、访问地址信息与退出清理。

替身策略（不起真实服务器）：
- uvicorn.run 打桩，由替身决定"服务运行期间"发生什么、以何种方式退出；
- webbrowser.open 与端口就绪探测打桩，浏览器等待线程可确定性验证。
文件一律落在临时目录：不读写真实 data、.env、providers.json，不调用收费 API。
"""

import json
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

import api
from api import _port_is_listening  # 模块导入时取原始实现，env 替身只替换 api 上的属性
from conftest import API_TEST_ACCOUNTS
from core.backup import BACKUP_CREATED, BACKUP_FAILED, BackupResult
from core.runtime import build_runtime

REPO_ROOT = Path(__file__).resolve().parents[1]
# 非默认端口：任何把 7860/8000 写死的实现都会在这里暴露
PORT = 8123
FAKE_LAN_IP = "192.0.2.7"


def make_runtime(tmp_path: Path, *, accounts=None, data_dir: Path | None = None):
    """按临时账号清单与临时数据目录组装 runtime（不碰真实配置与 data）。"""
    accounts = accounts if accounts is not None else API_TEST_ACCOUNTS
    accounts_path = tmp_path / "accounts.json"
    accounts_path.parent.mkdir(parents=True, exist_ok=True)
    accounts_path.write_text(json.dumps(accounts, ensure_ascii=False), encoding="utf-8")
    return build_runtime(
        env_file=tmp_path / "missing.env",
        providers_file=tmp_path / "missing.providers.json",
        accounts_path=accounts_path,
        data_dir=data_dir if data_dir is not None else tmp_path / "data",
    )


class StartupEnv:
    """LaunchSupport / main() 触达的全部外部动作替身，并记录调用。"""

    def __init__(self, tmp_path, monkeypatch):
        self.browser_opened: list[str] = []
        self.browser_ready = threading.Event()
        self.readiness_probes: list[tuple[str, int]] = []
        # 非 None 时在"写地址文件"这一步向主线程投递该信号：
        # 模拟控制台 Ctrl+C / Ctrl+Break 正好落在启动配套阶段（信号总在主线程被处理）
        self.signal_at_address_write: int | None = None
        # 非 None 时在 uvicorn 收尾（恢复原处理器并重发信号）之后投递该信号
        self.signal_after_uvicorn_exit: int | None = None
        # IPv6 局域网探测结果：None 表示拿不到；probes 记录探测次数
        self.lan_ipv6: str | None = None
        self.lan_ipv6_probes = 0
        self.serving = True
        self.uvicorn_calls: list[tuple[object, dict]] = []
        self.uvicorn_error: BaseException | None = None
        # True 时替身等到浏览器打开才返回，模拟"服务正在运行的这段时间"
        self.uvicorn_blocks_until_browser = False

        real_save_address_info = api.save_address_info

        def save_address_info(*args, **kwargs):
            if self.signal_at_address_write is not None:
                # 主线程正停在启动配套阶段，此刻控制台按下 Break/Ctrl+C
                signal.raise_signal(self.signal_at_address_write)
            return real_save_address_info(*args, **kwargs)

        monkeypatch.setattr(api, "_port_is_listening", self._port_is_listening)
        monkeypatch.setattr(api, "save_address_info", save_address_info)
        monkeypatch.setattr("webbrowser.open", self._open_browser)
        # 局域网探测不打真网络：地址文件里的局域网 IP 固定可断言
        monkeypatch.setattr("core.access.get_lan_ip", lambda: FAKE_LAN_IP)
        # IPv6 局域网探测同样替身化：结果与次数都可断言
        monkeypatch.setattr(api, "get_lan_ipv6", self._probe_lan_ipv6)
        import uvicorn

        monkeypatch.setattr(uvicorn, "run", self._uvicorn_run)

    # ---- 替身 ----

    def _probe_lan_ipv6(self):
        self.lan_ipv6_probes += 1
        return self.lan_ipv6

    def _port_is_listening(self, host, port, timeout=0.2):
        self.readiness_probes.append((host, port))
        return self.serving

    def _open_browser(self, url, *args, **kwargs):
        self.browser_opened.append(url)
        self.browser_ready.set()
        return True

    def _uvicorn_run(self, app, **kwargs):
        self.uvicorn_calls.append((app, kwargs))
        if self.uvicorn_blocks_until_browser:
            assert self.browser_ready.wait(5), "服务运行期间没有打开浏览器"
        if self.signal_after_uvicorn_exit is not None:
            # 与 uvicorn capture_signals 一致：把进来时的处理器当作"原处理器"保存，
            # 运行期间换成自己的，退出时先恢复、再重发捕获到的信号
            saved = signal.signal(self.signal_after_uvicorn_exit, lambda sig, frame: None)
            signal.signal(self.signal_after_uvicorn_exit, saved)
            signal.raise_signal(self.signal_after_uvicorn_exit)
        if self.uvicorn_error is not None:
            raise self.uvicorn_error

    # ---- 驱动 ----

    def support(self, runtime, **kwargs):
        kwargs.setdefault("host", "127.0.0.1")
        kwargs.setdefault("port", PORT)
        return api.LaunchSupport(runtime, **kwargs)

    def run_main(self, argv, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["api.py", *[str(a) for a in argv]])
        api.main()


def backup_threads() -> list[threading.Thread]:
    """当前还活着的 API 备份线程（清理是否彻底的唯一外部可见证据）。"""
    return [t for t in threading.enumerate() if t.name == "maa-api-backup"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    return StartupEnv(tmp_path, monkeypatch)


def occupied_socket() -> tuple[int, socket.socket]:
    """真实占用中的本机端口：用于端口占用路径（不打桩 uvicorn 的失败）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    return sock.getsockname()[1], sock


# ===================== 启动边界 =====================


# 导入与工厂阶段被禁止的动作：一旦有线程/子进程/浏览器被启动，子进程即失败
SIDE_EFFECT_CODE = r"""
import json, sys, tempfile, threading, webbrowser
from pathlib import Path

BLOCKED = {"app", "gradio", "gradio_client"}
events = []


def audit_hook(event, args):
    # 子进程与系统调用用审计钩子记录：不替换 subprocess 里的类，避免打断正常导入
    if event in ("subprocess.Popen", "os.system", "os.execve", "os.spawnve"):
        events.append(("subprocess", event, str(args[:1])))
    elif event == "webbrowser.open":
        events.append(("browser", event, str(args[:1])))


sys.addaudithook(audit_hook)


class ImportBlocker:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in BLOCKED:
            raise ImportError(f"启动边界验证禁止导入: {fullname}")
        return None


sys.meta_path.insert(0, ImportBlocker())
sys.path.insert(0, sys.argv[1])

_orig_start = threading.Thread.start


def spy_start(self, *args, **kwargs):
    events.append(("thread", self.name))
    return _orig_start(self, *args, **kwargs)


threading.Thread.start = spy_start
webbrowser.open = lambda *a, **k: events.append(("browser", "webbrowser.open", ""))

tmp = Path(tempfile.mkdtemp(prefix="maa-api-boundary-"))
accounts_path = tmp / "accounts.json"
accounts_path.write_text(json.dumps([
    {"username": "iso", "password": "iso-pw", "display_name": "隔离",
     "level": "高中", "enabled": True},
], ensure_ascii=False), encoding="utf-8")

import api
from core.runtime import build_runtime
runtime = build_runtime(
    env_file=tmp / "missing.env",
    providers_file=tmp / "missing.providers.json",
    accounts_path=accounts_path,
    data_dir=tmp / "data",
)
app = api.create_api_app(runtime, frontend_dir=None)

data_dir = tmp / "data"
created = sorted(p.name for p in data_dir.iterdir()) if data_dir.exists() else []

print(json.dumps({
    "events": events,
    "gradio_loaded": "gradio" in sys.modules,
    "app_loaded": "app" in sys.modules,
    "uvicorn_loaded": "uvicorn" in sys.modules,
    "data_dir_exists": data_dir.exists(),
    "data_files": created,
    "has_app": app is not None,
}))
"""


def test_import_and_factory_start_no_side_effects():
    """导入 api 与 create_api_app 都不启动备份线程/浏览器/服务器，也不写配套文件。

    防住的独立风险：配套任务被挂到模块导入或应用工厂上，接口测试与多实例启动
    会被隐式带着起线程；同时确认新增的 core.backup/access 导入
    没有把 Gradio 依赖带回 API 层。
    """
    proc = subprocess.run(
        [sys.executable, "-c", SIDE_EFFECT_CODE, str(REPO_ROOT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        # 子进程可能用本地编码往 stderr 写告警：不能因此把整个验证判为读不到输出
        errors="replace",
        timeout=180,
    )
    assert proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    # 没有新线程、没有子进程、没有浏览器调用
    assert [e for e in result["events"] if e[0] == "thread"] == []
    assert [e for e in result["events"] if e[0] == "subprocess"] == []
    assert [e for e in result["events"] if e[0] == "browser"] == []
    assert result["gradio_loaded"] is False
    assert result["app_loaded"] is False
    assert result["uvicorn_loaded"] is False
    # create_api_app 只建应用：连数据目录都没被创建，更没有备份/地址/进程记录
    assert result["data_dir_exists"] is False
    created = set(result["data_files"])
    assert created == set()


def _fail_on_call(name):
    def raise_if_called(*args, **kwargs):
        raise AssertionError(f"{name} 被应用工厂调用")

    return raise_if_called


def test_create_api_app_does_not_call_companion_entries(tmp_path, monkeypatch):
    """应用工厂不调用任何配套入口（进程内的第二道边界，报错定位更快）。

    防住的独立风险：别人复用 create_api_app 时被动开启后台任务。
    """
    for name in (
        "run_backup",
        "daily_backup_loop",
        "save_address_info",
    ):
        monkeypatch.setattr(api, name, _fail_on_call(name))
    monkeypatch.setattr("webbrowser.open", _fail_on_call("webbrowser.open"))

    runtime = make_runtime(tmp_path)
    app = api.create_api_app(runtime, frontend_dir=None)
    assert app.state.runtime is runtime


# ===================== 每日备份 =====================


def test_backup_uses_runtime_data_dir_not_global(tmp_path, monkeypatch, env):
    """备份目录取自 runtime.data_dir：全局 DATA_DIR 完全不参与。

    防住的独立风险：实现改用（或在运行时改写）全局 data 路径，
    新版 API 就会备份到另一份（甚至真实）数据目录里。
    """
    sentinel = tmp_path / "全局data"
    sentinel.mkdir()
    (sentinel / "accounts.json").write_text("REAL", encoding="utf-8")
    monkeypatch.setattr("core.backup.DATA_DIR", sentinel)

    support = env.support(make_runtime(tmp_path))
    support.start_backup()
    zips = list((support.data_dir / "backups").glob("math-backup-*.zip"))
    support.close()

    assert zips, "备份没有写进 runtime 的数据目录"
    assert not (sentinel / "backups").exists()
    assert (sentinel / "accounts.json").read_text(encoding="utf-8") == "REAL"


def test_backup_failure_is_announced_not_success(tmp_path, monkeypatch, env, capsys):
    """备份失败必须明确提示，且与"今天已备份"、"已生成"分得开。

    防住的独立风险：失败与已存在都返回 None，启动方把失败报成成功。
    """
    monkeypatch.setattr(
        api,
        "run_backup",
        lambda path=None, data_dir=None: BackupResult(BACKUP_FAILED, None),
    )
    support = env.support(make_runtime(tmp_path))
    support.start_backup()
    support.report_backup(BackupResult(BACKUP_CREATED, Path("today.zip")))
    support.close()

    out = capsys.readouterr().out
    assert "备份失败" in out and "今日数据未打包" in out
    assert "已生成" in out  # 成功路径照常报告，两种结局不会混淆


def test_backup_thread_is_stopped_and_joined(tmp_path, env):
    """退出清理：停止信号置位且备份线程真正结束（不靠 daemon 属性拖着）。

    防住的独立风险：只置信号不 join，进程退出时备份仍在写盘、留下半成品。
    """
    support = env.support(make_runtime(tmp_path))
    support.start_backup()
    thread = support._backup_thread
    assert thread is not None and thread.is_alive()

    support.close()
    assert support._backup_stop.is_set()
    assert not thread.is_alive()
    assert backup_threads() == []


# ===================== 浏览器与地址跟随监听地址族 =====================


@pytest.mark.parametrize(
    "bind_host,expected_url,expected_probe_host",
    [
        ("::1", f"http://[::1]:{PORT}", "::1"),
        ("::", f"http://[::1]:{PORT}", "::1"),
        ("127.0.0.2", f"http://127.0.0.2:{PORT}", "127.0.0.2"),
        ("0.0.0.0", f"http://127.0.0.1:{PORT}", "127.0.0.1"),
        ("127.0.0.1", f"http://127.0.0.1:{PORT}", "127.0.0.1"),
    ],
)
def test_browser_and_address_follow_listen_family(
    tmp_path, env, bind_host, expected_url, expected_probe_host
):
    """浏览器地址、就绪探测与地址文件对同一监听地址给出一致结果。

    防住的独立风险：IPv6 监听被换成 127.0.0.1（本机实测连不上）、
    127.0.0.2 被统一改成 127.0.0.1，或把带方括号的 [::1] 交给 socket 探测
    （探测永远失败，浏览器永远不打开）。
    """
    support = env.support(make_runtime(tmp_path), host=bind_host, open_browser=True)
    support.write_address_info()

    assert support.browse_url == expected_url
    assert f"本机地址：{expected_url}" in support.address_file.read_text(encoding="utf-8")

    env.readiness_probes.clear()
    support._wait_and_open_browser(threading.Event())
    assert env.browser_opened[-1] == expected_url
    # socket 探测用不带方括号的原始主机名（方括号只对 URL 有意义）
    assert env.readiness_probes[0] == (expected_probe_host, PORT)
    support.close()


def test_real_probe_and_port_precheck_work_over_ipv6_loopback(tmp_path, env):
    """真实 ::1 临时监听端口：就绪探测能认出来，端口预检也能判出占用。

    防住的独立风险：探测与预检只支持 IPv4，--host ::1 启动时
    浏览器不打开、端口冲突也提示不出来。环境不支持 IPv6 时跳过并说明。
    """
    server = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        server.bind(("::1", 0))
        server.listen()
    except OSError as exc:
        pytest.skip(f"本机无法建立 IPv6 回环监听端口，未验证该路径：{exc}")
    port = server.getsockname()[1]
    try:
        assert _port_is_listening("::1", port), "真实 ::1 监听端口未被探测到"
        support = env.support(make_runtime(tmp_path), host="::1", port=port)
        with pytest.raises(OSError, match="无法监听"):
            support.ensure_port_free()
        support.close()
    finally:
        server.close()

    # 端口空闲时预检通过（同一个临时端口释放后再取一个）
    support = env.support(make_runtime(tmp_path), host="::1", port=free_ipv6_port())
    support.ensure_port_free()
    support.close()


def free_ipv6_port() -> int:
    server = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    server.bind(("::1", 0))
    port = server.getsockname()[1]
    server.close()
    return port


def test_main_interrupt_during_startup_propagates_and_cleans_up(
    tmp_path, env, monkeypatch
):
    """启动配套阶段（写地址文件时）被 Ctrl+C 打断：异常照原样传出，本次资源仍被收尾。

    防住的独立风险：中断被吞掉导致退出码变成成功，或异常路径下
    备份线程等配套资源留在原地。
    """
    runtime = make_runtime(tmp_path)
    monkeypatch.setattr(api, "build_runtime", lambda **kwargs: runtime)
    monkeypatch.delenv("AUTO_OPEN_BROWSER", raising=False)
    env.signal_at_address_write = signal.SIGINT

    with pytest.raises(KeyboardInterrupt):
        env.run_main(["--port", PORT], monkeypatch)

    assert env.uvicorn_calls == [], "被打断后仍然启动了服务"
    assert backup_threads() == []
    assert not (runtime.data_dir / "访问地址.txt").exists()


# ===================== 访问信息与浏览器 =====================


def test_loopback_bind_does_not_claim_phone_can_access(tmp_path, env):
    """仅监听回环：地址文件与控制台口径都不宣称手机可通过局域网访问。

    防住的独立风险：127.0.0.1 启动也写"手机访问 http://192.168.x.x"，
    手机照着连不上，还被当成程序故障。
    """
    support = env.support(make_runtime(tmp_path), host="127.0.0.1")
    entries = support.address_info()
    support.write_address_info()
    file_text = support.address_file.read_text(encoding="utf-8")

    assert entries["本机地址"] == f"http://127.0.0.1:{PORT}"
    assert "手机无法通过局域网连接" in entries["局域网地址"]
    assert FAKE_LAN_IP not in file_text  # 没给出其实不可用的局域网地址
    assert "只记录访问地址" in file_text


def test_wildcard_bind_publishes_actual_lan_address(tmp_path, env):
    """通配监听：地址信息按实际局域网 IP + 实际端口给出，不是写死的 7860。

    防住的独立风险：默认局域网地址写死 7860，换端口后手机连错地址。
    """
    support = env.support(make_runtime(tmp_path), host="0.0.0.0")
    support.write_address_info()
    text = support.address_file.read_text(encoding="utf-8")

    assert f"http://{FAKE_LAN_IP}:{PORT}" in text
    assert "7860" not in text


def test_address_info_holds_no_credentials(tmp_path, env):
    """地址文件不落任何凭据，也不会退回去写旧 Gradio 那份含密码的文件。

    防住的独立风险：新版沿用含账号密码的本地文件，地址一旦对外发布就把密码外泄。
    """
    runtime = make_runtime(tmp_path)
    support = env.support(runtime)
    support.write_address_info()
    text = support.address_file.read_text(encoding="utf-8")

    for account in runtime.accounts:
        assert account["password"] not in text
    assert not (support.data_dir / "当前访问密码.txt").exists()


def test_browser_waits_for_service_then_opens_loopback(tmp_path, env, monkeypatch, capsys):
    """浏览器：等服务可访问后才打开本机地址；服务没起来就超时提示。

    防住的独立风险：服务器尚未就绪就打开页面（用户看到连接被拒绝），
    或把 0.0.0.0 当成访问地址。
    """
    monkeypatch.setattr(api, "BROWSER_READY_TIMEOUT_SECONDS", 0.2)
    support = env.support(make_runtime(tmp_path), host="0.0.0.0", open_browser=True)

    env.serving = False
    started = time.monotonic()
    support._wait_and_open_browser(threading.Event())
    assert env.browser_opened == []
    assert "未自动打开浏览器" in capsys.readouterr().out
    assert time.monotonic() - started < 5  # 超时即返回，不无限等

    env.serving = True
    support._wait_and_open_browser(threading.Event())
    assert env.browser_opened == [f"http://127.0.0.1:{PORT}"]
    assert env.readiness_probes[0] == ("127.0.0.1", PORT)


def test_browser_can_be_disabled_and_stops_on_exit(tmp_path, env):
    """可关闭：open_browser=False 不创建等待线程；退出信号后不再弹浏览器。

    防住的独立风险：自动开浏览器无法关闭，或收尾之后才弹出窗口。
    """
    support = env.support(make_runtime(tmp_path), open_browser=False)
    support.open_browser_when_ready()
    assert support._browser_thread is None
    assert env.readiness_probes == []

    env.serving = False
    support = env.support(make_runtime(tmp_path), open_browser=True)
    support.open_browser_when_ready()
    assert support._browser_thread is not None
    support.close()
    assert env.browser_opened == []


# ===================== 两份运行状态互不串台 =====================


def test_two_runtimes_keep_backup_and_address_apart(tmp_path, env):
    """两个 runtime（不同目录、不同端口）互不写对方的配套文件。

    防住的独立风险：备份与地址文件共用全局路径，两份运行状态互相覆盖。
    """
    first_dir, second_dir = tmp_path / "first", tmp_path / "second"
    first = env.support(
        make_runtime(tmp_path / "a", data_dir=first_dir), port=8123
    )
    second = env.support(
        make_runtime(tmp_path / "b", data_dir=second_dir),
        host="0.0.0.0",
        port=9000,
    )
    for support in (first, second):
        support.start_backup()
        support.write_address_info()
    first_text = first.address_file.read_text(encoding="utf-8")
    second_text = second.address_file.read_text(encoding="utf-8")
    first.close()
    second.close()

    assert first.address_file != second.address_file
    assert first.address_file.parent == first_dir and second.address_file.parent == second_dir
    assert ":8123" in first_text and "9000" not in first_text
    assert ":9000" in second_text and FAKE_LAN_IP in second_text
    for dir_ in (first_dir, second_dir):
        assert list((dir_ / "backups").glob("math-backup-*.zip"))


# ===================== 启动入口与退出清理 =====================


def test_main_port_occupied_fails_before_serving(tmp_path, env, monkeypatch):
    """端口被占用：绑定前就失败，不开浏览器，备份线程已收尾。

    防住的独立风险：端口占用后仍继续启动或留下无人收尾的备份线程，
    失败被吞掉后退出码变成成功。
    """
    runtime = make_runtime(tmp_path)
    monkeypatch.setattr(api, "build_runtime", lambda **kwargs: runtime)
    port, blocker = occupied_socket()
    try:
        with pytest.raises(OSError, match="无法监听") as exc_info:
            env.run_main(["--port", port], monkeypatch)
    finally:
        blocker.close()

    assert "请改用其他端口" in str(exc_info.value)
    assert env.uvicorn_calls == []
    assert env.browser_opened == []
    assert not (runtime.data_dir / "访问地址.txt").exists()
    assert backup_threads() == []


def test_main_startup_failure_propagates_and_cleans_up(tmp_path, env, monkeypatch):
    """服务启动失败：异常原样传播（保留失败退出码），本次资源全部收尾。

    防住的独立风险：启动异常被吞掉，用户以为服务起来了。
    """
    runtime = make_runtime(tmp_path)
    monkeypatch.setattr(api, "build_runtime", lambda **kwargs: runtime)
    env.uvicorn_error = RuntimeError("模拟启动失败")

    with pytest.raises(RuntimeError, match="模拟启动失败"):
        env.run_main(["--port", PORT], monkeypatch)

    assert env.uvicorn_calls and env.uvicorn_calls[0][1] == {"host": "127.0.0.1", "port": PORT}
    assert backup_threads() == []


def test_main_keyboard_interrupt_cleans_up(tmp_path, env, monkeypatch):
    """Ctrl+C：KeyboardInterrupt 原样传播，备份线程与浏览器任务都收尾。

    防住的独立风险：中断退出留下后台线程没有收尾。
    """
    runtime = make_runtime(tmp_path)
    monkeypatch.setattr(api, "build_runtime", lambda **kwargs: runtime)
    env.uvicorn_error = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        env.run_main(["--port", PORT], monkeypatch)

    assert backup_threads() == []
    assert [t for t in threading.enumerate() if t.name == "maa-api-browser"] == []


def test_close_keeps_going_after_a_failed_step(tmp_path, env, capsys):
    """一处清理失败不影响后续：浏览器任务收尾报错后备份线程照样收到信号并结束。

    防住的独立风险：清理串行执行且无隔离，第一步抛错就跳过了线程收尾。
    """
    support = env.support(make_runtime(tmp_path), open_browser=True)
    support.start_backup()
    support.open_browser_when_ready()
    thread = support._backup_thread

    def failing_join(timeout=None):
        raise RuntimeError("模拟浏览器任务收尾失败")

    support._browser_thread.join = failing_join

    support.close()  # 不抛出
    out = capsys.readouterr().out

    assert "浏览器等待任务收尾失败" in out
    assert support._backup_stop.is_set()
    assert not thread.is_alive()


def test_main_writes_no_passwords(tmp_path, env, monkeypatch, capsys):
    """常规启动：不打印账号密码；--no-browser 时不开浏览器。

    防住的独立风险：新版沿用旧界面"把所有账号密码打到控制台"的做法，
    屏幕共享或远程会话里直接泄露凭据。
    """
    runtime = make_runtime(tmp_path)
    monkeypatch.setattr(api, "build_runtime", lambda **kwargs: runtime)
    monkeypatch.delenv("AUTO_OPEN_BROWSER", raising=False)

    env.run_main(["--port", PORT, "--no-browser"], monkeypatch)
    out = capsys.readouterr().out

    for account in runtime.accounts:
        assert account["password"] not in out
    assert env.browser_opened == [] and env.readiness_probes == []
    assert env.uvicorn_calls[0][1] == {"host": "127.0.0.1", "port": PORT}
    assert "手机无法通过局域网连接" in out


def test_main_opens_browser_once_service_is_up(tmp_path, env, monkeypatch):
    """默认（非隔离）启动会自动开浏览器：等服务就绪后打开本机地址；开关仍可关闭。

    防住的独立风险：接回自动开浏览器时忘了"等就绪"，或环境变量开关失效。
    """
    runtime = make_runtime(tmp_path)
    monkeypatch.setattr(api, "build_runtime", lambda **kwargs: runtime)
    monkeypatch.delenv("AUTO_OPEN_BROWSER", raising=False)
    env.uvicorn_blocks_until_browser = True

    env.run_main(["--port", PORT], monkeypatch)
    assert env.browser_opened == [f"http://127.0.0.1:{PORT}"]

    monkeypatch.setenv("AUTO_OPEN_BROWSER", "false")
    env.browser_opened.clear()
    env.browser_ready.clear()
    env.uvicorn_blocks_until_browser = False
    env.run_main(["--port", PORT], monkeypatch)
    assert env.browser_opened == []


def test_isolated_main_keeps_companions_in_temp_dir(tmp_path, env, monkeypatch, capsys):
    """--isolated --fake：配套文件全在临时目录，仓库 data 一字不动。

    防住的独立风险：隔离验证读到真实 .env/账号，或把真实 data 的备份、
    地址文件改掉。
    """
    monkeypatch.delenv("AUTO_OPEN_BROWSER", raising=False)
    watched_names = ("当前访问密码.txt", "访问地址.txt")
    before = _snapshot_repo_data(watched_names)

    env.run_main(["--isolated", "--fake", "--port", PORT], monkeypatch)
    out = capsys.readouterr().out
    temp_root = _isolated_temp_root(out)
    assert temp_root is not None, "没有打印隔离临时目录"
    try:
        app, kwargs = env.uvicorn_calls[0]
        runtime = app.state.runtime
        assert Path(runtime.data_dir) == temp_root / "data"
        assert kwargs == {"host": "127.0.0.1", "port": PORT}

        # 配套文件全部落在临时目录
        address_file = temp_root / "data" / "访问地址.txt"
        assert address_file.exists()
        assert list((temp_root / "data" / "backups").glob("math-backup-*.zip"))
        assert not (temp_root / "data" / "当前访问密码.txt").exists()

        # 隔离模式默认不自动开浏览器
        assert env.browser_opened == [] and env.readiness_probes == []

        # --fake 替换了所有可被选中的客户端
        from dev_fake import FakeStreamClient

        assert runtime.clients and all(
            isinstance(client, FakeStreamClient) for client in runtime.clients.values()
        )
        assert isinstance(runtime.client, FakeStreamClient)

        # 隔离账号允许打印（临时凭据），地址文件里仍不含任何凭据
        assert "[隔离账号]" in out
        text = address_file.read_text(encoding="utf-8")
        assert "sk-" not in text and f":{PORT}" in text
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    # 仓库真实 data 里的配套文件一字未动
    assert _snapshot_repo_data(watched_names) == before


def test_isolated_main_stops_backup_thread_on_failure(env, monkeypatch, capsys):
    """隔离启动中途失败：备份线程收尾，测试进程里不留后台任务。

    防住的独立风险：main() 提前失败时没走到 close()，备份线程一直挂在进程里。
    """
    monkeypatch.delenv("AUTO_OPEN_BROWSER", raising=False)
    env.uvicorn_error = RuntimeError("模拟启动即失败")
    temp_root = None
    try:
        with pytest.raises(RuntimeError, match="模拟启动即失败"):
            env.run_main(["--isolated", "--fake", "--port", PORT], monkeypatch)
        temp_root = _isolated_temp_root(capsys.readouterr().out)
    finally:
        if temp_root is not None:
            shutil.rmtree(temp_root, ignore_errors=True)

    assert backup_threads() == []
    assert [t for t in threading.enumerate() if t.name == "maa-api-browser"] == []


def _isolated_temp_root(output: str) -> Path | None:
    """从 --isolated 的输出里取临时数据目录。"""
    for line in output.splitlines():
        if line.startswith("[isolated] 临时数据目录："):
            return Path(line.split("：", 1)[1].strip())
    return None


def _snapshot_repo_data(names: tuple[str, ...]) -> dict[str, str | None]:
    """仓库真实 data 目录里配套文件的当前内容（不存在记为 None）。"""
    snapshot: dict[str, str | None] = {}
    for name in names:
        path = REPO_ROOT / "data" / name
        try:
            snapshot[name] = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            snapshot[name] = None
    return snapshot


# ===================== 静态服务不暴露配套文件 =====================


def test_companion_files_are_not_served(tmp_path, env):
    """备份 zip 与地址文件都不在任何静态挂载可达的路径上。

    防住的独立风险：把 data 目录静态挂出去（备份 zip 内含明文密码文件），
    或日后有人把静态根改成数据目录。
    """
    runtime = make_runtime(tmp_path)
    support = env.support(runtime)
    support.start_backup()
    support.write_address_info()
    support.close()

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    client = TestClient(api.create_api_app(runtime, frontend_dir=dist))

    assert client.get("/").status_code == 200
    backup_name = next(p.name for p in (support.data_dir / "backups").iterdir())
    for path in (
        "/访问地址.txt",
        f"/backups/{backup_name}",
        f"/{backup_name}",
        "/accounts.json",
        "/temp_images",
        "/usage_daily.json",
    ):
        assert client.get(path).status_code == 404, path


# ===================== Windows Ctrl+Break 退出保护 =====================


_HAS_SIGBREAK = hasattr(signal, "SIGBREAK")
requires_sigbreak = pytest.mark.skipif(
    not _HAS_SIGBREAK, reason="本平台没有 SIGBREAK（非 Windows），只验证空操作分支"
)


@requires_sigbreak
def test_sigbreak_guard_turns_break_into_interrupt_and_restores_previous():
    """守卫把控制台 Break 变成 KeyboardInterrupt，退出时恢复原处理器。

    防住的独立风险：Windows 上 Break 的默认处置是直接结束进程，
    启动入口的 finally 完全没机会跑，备份线程等后台任务被留在原地。
    """
    before = signal.getsignal(signal.SIGBREAK)
    restore = api.install_sigbreak_guard()
    try:
        assert signal.getsignal(signal.SIGBREAK) is api._sigbreak_handler
        with pytest.raises(KeyboardInterrupt):
            signal.raise_signal(signal.SIGBREAK)
        # 抛过异常后保护仍在（不会一次就失效）
        assert signal.getsignal(signal.SIGBREAK) is api._sigbreak_handler
    finally:
        restore()
    assert signal.getsignal(signal.SIGBREAK) is before


@requires_sigbreak
def test_main_break_during_startup_stage_still_cleans_up(tmp_path, env, monkeypatch):
    """Break 落在启动配套阶段（写地址文件时）：异常传出、启动入口的 finally 跑完、原处理器恢复。

    防住的独立风险：这一阶段 uvicorn 还没接管信号，Break 直接结束进程。
    """
    runtime = make_runtime(tmp_path)
    monkeypatch.setattr(api, "build_runtime", lambda **kwargs: runtime)
    before = signal.getsignal(signal.SIGBREAK)
    env.signal_at_address_write = signal.SIGBREAK

    with pytest.raises(KeyboardInterrupt):
        env.run_main(["--port", PORT], monkeypatch)

    assert env.uvicorn_calls == [], "Break 之后仍然启动了服务"
    assert backup_threads() == [], "启动入口的 finally 没跑到"
    assert signal.getsignal(signal.SIGBREAK) is before


@requires_sigbreak
def test_main_break_reraised_after_uvicorn_still_cleans_up(tmp_path, env, monkeypatch):
    """Uvicorn 收尾恢复并重发 Break 之后，仍能进入启动入口的 finally 完成清理。

    防住的独立风险：服务运行期间按 Break 时 ASGI 关闭流程走完了，
    但配套资源（备份线程、浏览器任务）没被回收。
    """
    runtime = make_runtime(tmp_path)
    monkeypatch.setattr(api, "build_runtime", lambda **kwargs: runtime)
    before = signal.getsignal(signal.SIGBREAK)
    env.signal_after_uvicorn_exit = signal.SIGBREAK

    with pytest.raises(KeyboardInterrupt):
        env.run_main(["--port", PORT], monkeypatch)

    assert len(env.uvicorn_calls) == 1
    assert backup_threads() == []
    assert [t for t in threading.enumerate() if t.name == "maa-api-browser"] == []
    assert signal.getsignal(signal.SIGBREAK) is before


def test_main_does_not_take_over_sigint(tmp_path, env, monkeypatch):
    """登记 Break 保护不碰 SIGINT：Ctrl+C 的原有行为不受影响。"""
    runtime = make_runtime(tmp_path)
    monkeypatch.setattr(api, "build_runtime", lambda **kwargs: runtime)
    sigint_before = signal.getsignal(signal.SIGINT)
    env.uvicorn_error = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        env.run_main(["--port", PORT], monkeypatch)

    assert signal.getsignal(signal.SIGINT) is sigint_before
    assert backup_threads() == []


def test_guard_restored_even_when_startup_fails(tmp_path, env, monkeypatch):
    """启动失败也要恢复原处理器，且异常不被清理掩盖。"""
    runtime = make_runtime(tmp_path)
    monkeypatch.setattr(api, "build_runtime", lambda **kwargs: runtime)
    before = signal.getsignal(signal.SIGBREAK) if _HAS_SIGBREAK else None
    env.uvicorn_error = OSError("模拟启动失败")

    with pytest.raises(OSError, match="模拟启动失败"):
        env.run_main(["--port", PORT], monkeypatch)

    if _HAS_SIGBREAK:
        assert signal.getsignal(signal.SIGBREAK) is before
    assert backup_threads() == []


class _SignalStub:
    """只顶掉 api 模块里的 signal 引用：登记照常，恢复那一次抛出 KeyboardInterrupt。

    测试进程自身的信号处置全程不被改动，因此不会泄漏到其它用例。
    """

    def __init__(self, real_module) -> None:
        self._real = real_module
        # 编号与 Windows 一致；存在的意义只是让守卫走到登记分支
        self.SIGBREAK = 21
        self.installed: list[int] = []
        self.restore_calls = 0

    def signal(self, sigint, handler):
        if handler is api._sigbreak_handler:
            self.installed.append(sigint)
            return self._real.SIG_DFL
        # 恢复阶段：模拟"就在恢复原处理器的瞬间又按了一次 Break"
        self.restore_calls += 1
        raise KeyboardInterrupt("模拟恢复原处理器时被中断")

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_restore_failure_does_not_replace_startup_error(tmp_path, env, monkeypatch, capsys):
    """恢复信号处理器时抛 KeyboardInterrupt：向外传播的仍是原来的启动异常。

    防住的独立风险：finally 里的恢复调用抛出 KeyboardInterrupt，把启动本身的
    OSError 顶掉 —— 退出码与诊断都变成"用户按了 Break"，端口占用等原因看不出来；
    备份线程等清理也可能被当成已完成。
    """
    runtime = make_runtime(tmp_path)
    monkeypatch.setattr(api, "build_runtime", lambda **kwargs: runtime)
    stub = _SignalStub(signal)
    monkeypatch.setattr(api, "signal", stub)
    env.uvicorn_error = OSError("模拟端口占用导致启动失败")
    disposition_before = (
        signal.getsignal(signal.SIGBREAK) if _HAS_SIGBREAK else None
    )

    raised: BaseException | None = None
    try:
        env.run_main(["--port", PORT], monkeypatch)
    except BaseException as exc:  # noqa: BLE001
        # 未修复时这里拿到的是 KeyboardInterrupt：必须先捕获再断言，
        # 不能让它直接冲出测试把整个 pytest 会话中断
        raised = exc

    assert stub.installed == [21], "守卫没登记，测试是空的"
    assert stub.restore_calls == 1, "没覆盖到恢复阶段"
    assert isinstance(raised, OSError), f"传播的是 {type(raised).__name__}: {raised}"
    assert "模拟端口占用导致启动失败" in str(raised)
    assert "恢复 Ctrl+Break 原处理器失败" in capsys.readouterr().out
    assert backup_threads() == [], "恢复异常打断了启动入口的清理"
    if _HAS_SIGBREAK:
        # 本测试只在 api 的引用上做替换，进程自身处置不应有任何变化
        assert signal.getsignal(signal.SIGBREAK) is disposition_before


def test_guard_is_noop_off_main_thread_or_without_sigbreak(tmp_path, env, monkeypatch, capsys):
    """非主线程不登记（signal 只能在主线程用）；没有 SIGBREAK 的平台上 main() 照常跑完。

    防住的独立风险：在非 Windows 或非主线程里调用 signal.signal 抛 ValueError，
    把正常启动带崩。
    """
    holder: dict = {}

    def worker():
        holder["restore"] = api.install_sigbreak_guard()

    before = signal.getsignal(signal.SIGBREAK) if _HAS_SIGBREAK else None
    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5)
    assert holder["restore"]() is None
    if _HAS_SIGBREAK:
        assert signal.getsignal(signal.SIGBREAK) is before, "非主线程却改掉了信号处置"
        assert "不在主线程" in capsys.readouterr().out

    monkeypatch.delattr(signal, "SIGBREAK", raising=False)
    runtime = make_runtime(tmp_path)
    monkeypatch.setattr(api, "build_runtime", lambda **kwargs: runtime)
    env.run_main(["--port", PORT], monkeypatch)
    assert len(env.uvicorn_calls) == 1
    assert backup_threads() == []


def test_create_api_app_does_not_change_signal_disposition(tmp_path):
    """应用工厂不改信号处置：保护只在启动入口登记。

    防住的独立风险：守卫被放进模块导入或 create_api_app，
    任何复用工厂的进程（含接口测试）都被改掉信号行为。
    """
    if not _HAS_SIGBREAK:
        pytest.skip("本平台没有 SIGBREAK，无需验证该副作用")
    before = signal.getsignal(signal.SIGBREAK)
    runtime = make_runtime(tmp_path)
    api.create_api_app(runtime, frontend_dir=None)
    assert signal.getsignal(signal.SIGBREAK) is before


# ===================== IPv6 监听的局域网地址口径 =====================


def test_ipv6_wildcard_address_info_and_file_match(tmp_path, env):
    """IPv6 通配监听：控制台口径与地址文件一致，且只探测一次。

    防住的独立风险：两处各算各的（或各探测一次），手机拿到两个不同地址。
    """
    env.lan_ipv6 = "2001:db8::77"
    support = env.support(make_runtime(tmp_path), host="::")
    entries = support.address_info()
    support.write_address_info()
    text = support.address_file.read_text(encoding="utf-8")

    assert entries["局域网地址"] == f"http://[2001:db8::77]:{PORT}"
    assert entries["局域网地址"] in text
    assert FAKE_LAN_IP not in text, "IPv6 监听仍然发了 IPv4 手机地址"
    assert env.lan_ipv6_probes == 1


def test_ipv6_wildcard_without_usable_address_is_honest(tmp_path, env):
    """拿不到 IPv6 局域网地址时如实说明，不用 IPv4、也不用 ::1 顶替。"""
    env.lan_ipv6 = None
    support = env.support(make_runtime(tmp_path), host="::")
    entries = support.address_info()
    support.write_address_info()
    text = support.address_file.read_text(encoding="utf-8")

    assert "尚未取得可用的 IPv6 局域网地址" in entries["局域网地址"]
    assert "尚未取得可用的 IPv6 局域网地址" in text
    assert FAKE_LAN_IP not in text
    assert f"http://[::1]:{PORT}" not in entries["局域网地址"]
    # 本机地址一项不受影响
    assert entries["本机地址"] == f"http://[::1]:{PORT}"


def test_main_reports_ipv6_hint(tmp_path, env, monkeypatch, capsys):
    """main() 以 :: 启动：打印与文件同口径。

    防住的独立风险：手机地址栏给 IPv4，IPv6 监听下手机连不上本机服务。
    """
    runtime = make_runtime(tmp_path)
    monkeypatch.setattr(api, "build_runtime", lambda **kwargs: runtime)
    monkeypatch.delenv("AUTO_OPEN_BROWSER", raising=False)
    env.lan_ipv6 = None

    env.run_main(["--host", "::", "--port", PORT, "--no-browser"], monkeypatch)
    out = capsys.readouterr().out

    assert "尚未取得可用的 IPv6 局域网地址" in out
    assert f"http://{FAKE_LAN_IP}:{PORT}" not in out
    file_text = (runtime.data_dir / "访问地址.txt").read_text(encoding="utf-8")
    assert "尚未取得可用的 IPv6 局域网地址" in file_text
    assert f"本机地址：http://[::1]:{PORT}" in file_text


def test_ipv4_wildcard_still_publishes_ipv4_lan_address(tmp_path, env):
    """IPv4 通配监听不受影响：手机地址仍是实际 IPv4 + 实际端口。

    防住的独立风险：为 IPv6 改口径时把原有的 IPv4 手机访问也改没了。
    """
    support = env.support(make_runtime(tmp_path), host="0.0.0.0")
    entries = support.address_info()
    support.write_address_info()

    assert entries["局域网地址"] == f"http://{FAKE_LAN_IP}:{PORT}"
    assert FAKE_LAN_IP in support.address_file.read_text(encoding="utf-8")
    assert env.lan_ipv6_probes == 0
