# -*- coding: utf-8 -*-
"""访问地址与随机密码测试。

覆盖 generate_password、地址族归一/连通性与 save_address_info
（新版 API 的地址文件，只写地址与更新时间，不含凭据）。
"""

import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from core.access import (
    _PASSWORD_ALPHABET,
    IPV6_LAN_UNAVAILABLE,
    address_entries,
    browse_host,
    build_url,
    generate_password,
    get_lan_ipv6,
    is_loopback_bind,
    is_owner_host,
    is_usable_lan_ipv6,
    save_address_info,
    url_host,
)


def test_password_length():
    assert len(generate_password()) == 8


def test_password_uses_safe_alphabet():
    pwd = generate_password(64)
    assert set(pwd) <= set(_PASSWORD_ALPHABET)
    assert not any(ch in pwd for ch in "0O1Il")


def test_password_random_different():
    assert generate_password() != generate_password()


def test_is_owner_host():
    assert is_owner_host("127.0.0.1")
    assert is_owner_host("::1")
    assert is_owner_host("localhost")
    assert not is_owner_host("192.168.3.83")
    assert not is_owner_host(None)


def test_browse_host_maps_by_address_family():
    """本机目标按地址族归一：IPv4 通配→127.0.0.1，IPv6 通配→::1，具体地址不动。

    防住的独立风险：把 0.0.0.0 当成访问地址（打不开），
    或把 ::1 / 127.0.0.2 这类监听地址统一换成 127.0.0.1 —— 实测连不上。
    """
    assert browse_host("0.0.0.0") == "127.0.0.1"
    assert browse_host("") == "127.0.0.1"
    assert browse_host("*") == "127.0.0.1"
    assert browse_host("::") == "::1"
    assert browse_host("::1") == "::1"          # 保持 IPv6，不换成 v4
    assert browse_host("127.0.0.1") == "127.0.0.1"
    assert browse_host("127.0.0.2") == "127.0.0.2"  # 具体监听地址不被替换
    assert browse_host("localhost") == "localhost"
    assert browse_host("192.168.3.83") == "192.168.3.83"
    assert browse_host("2001:db8::1") == "2001:db8::1"
    # 带方括号的写法与大小写都归一到同一无括号主机名
    assert browse_host("[::1]") == "::1"
    assert browse_host("0.0.0.0 ") == "127.0.0.1"


def test_ipv6_url_adds_brackets():
    """IPv6 在 URL 里必须加方括号；socket 用的主机名保持不带括号。

    防住的独立风险：拼出 http://::1:8123 这种非法 URL（端口被当成地址的一部分）。
    """
    assert url_host("::1") == "[::1]"
    assert url_host("::") == "[::]"
    assert url_host("2001:db8::1") == "[2001:db8::1]"
    assert url_host("127.0.0.2") == "127.0.0.2"
    assert url_host("[::1]") == "[::1]"          # 已带方括号不重复加
    assert build_url("::1", 8123) == "http://[::1]:8123"
    assert build_url("127.0.0.2", 8123) == "http://127.0.0.2:8123"
    # browse_host 的返回值可直接给 socket 用（不带方括号）
    assert "[" not in browse_host("::")


def _connectable(url: str, timeout: float = 1.0) -> bool:
    """按 URL 里的主机与端口真实连接一次（IPv6 用 AF_INET6）。"""
    parts = urlsplit(url)
    family = socket.AF_INET6 if ":" in (parts.hostname or "") else socket.AF_INET
    client = socket.socket(family, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect((parts.hostname, parts.port))
        return True
    except OSError:
        return False
    finally:
        client.close()


def _listener(host: str, family: int):
    """在指定监听地址上开一个临时端口；环境不支持时跳过（不伪称已验证）。

    与 uvicorn 一样不设置 IPV6_V6ONLY，保持平台默认，验证的就是真实启动时的行为。
    """
    try:
        server = socket.socket(family, socket.SOCK_STREAM)
        server.bind((host, 0))
        server.listen()
    except OSError as exc:
        pytest.skip(f"本机无法在 {host} 上建立临时监听端口（环境不支持）：{exc}")
    return server


def test_generated_loopback_target_reaches_actual_listener():
    """绑定 ::1 / :: / 127.0.0.2 / 0.0.0.0 时，按规则生成的本机目标真能连上。

    防住的独立风险：地址族映射只在 IPv4 环境下看起来正确
    （::1 与 :: 监听时换成 127.0.0.1 实测连不上，浏览器和隧道都会失败）。
    """
    cases = (
        ("::1", socket.AF_INET6),
        ("::", socket.AF_INET6),
        ("127.0.0.2", socket.AF_INET),
        ("0.0.0.0", socket.AF_INET),
    )
    for bind_host, family in cases:
        server = _listener(bind_host, family)
        port = server.getsockname()[1]
        try:
            target = build_url(browse_host(bind_host), port)
            assert _connectable(target), f"{bind_host} 监听时目标 {target} 连不上"
        finally:
            server.close()


def test_wrong_family_target_does_not_connect():
    """反向对照：把 IPv6 或指定回环监听换成 127.0.0.1 确实连不上。

    防住的独立风险：上面的用例在"任何写法都能连"的环境里失去意义，
    这条对照保证 ::1 与 127.0.0.2 的目标确实必须由映射规则得出。
    """
    for bind_host, family in (("::1", socket.AF_INET6), ("127.0.0.2", socket.AF_INET)):
        server = _listener(bind_host, family)
        port = server.getsockname()[1]
        try:
            assert not _connectable(f"http://127.0.0.1:{port}"), (
                f"{bind_host} 的监听端口竟然能被 127.0.0.1 连上，"
                "该环境无法区分地址族，反向对照不成立"
            )
        finally:
            server.close()


def test_address_entries_use_family_matching_urls():
    """地址信息各项与实际监听地址族一致：IPv6 带方括号，通配 IPv4 用 v4 回环。"""
    assert address_entries("::1", 8123)["本机地址"] == "http://[::1]:8123"
    assert address_entries("::", 8123)["本机地址"] == "http://[::1]:8123"
    assert address_entries("127.0.0.2", 8123)["本机地址"] == "http://127.0.0.2:8123"
    assert address_entries("0.0.0.0", 8123)["本机地址"] == "http://127.0.0.1:8123"
    # IPv4 通配监听：局域网地址取实际 IPv4 + 实际端口
    assert address_entries("0.0.0.0", 8123, lan_ip="192.0.2.7")["局域网地址"] == (
        "http://192.0.2.7:8123"
    )
    # 监听地址一栏对 IPv6 也加方括号，避免与端口混读
    assert address_entries("::", 8123)["监听地址"] == "[::]:8123"
    # 具体绑定地址（非通配、非回环）直接用该地址对外，不乱探测
    assert address_entries("2001:db8::5", 8123)["局域网地址"] == "http://[2001:db8::5]:8123"


def test_ipv6_wildcard_publishes_matched_family_address():
    """IPv6 通配监听 + 可用的 IPv6 局域网地址 → 给出带方括号的 IPv6 手机地址。

    防住的独立风险：:: 监听却发 IPv4 链接（本机实测 IPv6 监听时 IPv4 连不上），
    手机照着打开只会失败。
    """
    entries = address_entries("::", 8123, lan_ip="192.0.2.7", lan_ipv6="2001:db8::77")
    assert entries["局域网地址"] == "http://[2001:db8::77]:8123"
    assert "192.0.2.7" not in " ".join(entries.values())
    # 已经带方括号的输入不重复包裹
    assert address_entries("::", 8123, lan_ipv6="[2001:db8::77]")["局域网地址"] == (
        "http://[2001:db8::77]:8123"
    )


def test_ipv6_wildcard_without_usable_address_says_so():
    """取不到同族地址时如实说明，不用 IPv4、也不用 ::1 冒充手机地址。

    防住的独立风险：把 ::1 当局域网地址发出去，手机连不上还被当成程序故障。
    """
    for unusable in ("", "  ", "::1", "fe80::1234", "febf::1", "::", "192.0.2.7"):
        entry = address_entries("::", 8123, lan_ip="192.0.2.7", lan_ipv6=unusable)
        text = entry["局域网地址"]
        assert text == IPV6_LAN_UNAVAILABLE, f"{unusable!r} 竟然给出了 {text}"
        assert "192.0.2.7" not in text
        assert "::1" not in text
    # 提示语本身要说清是 IPv6 监听、还没拿到地址
    assert "IPv6" in IPV6_LAN_UNAVAILABLE and "尚未取得" in IPV6_LAN_UNAVAILABLE


def test_ipv6_link_local_and_loopback_are_not_publishable():
    """可用 IPv6 的判定：链路本地（fe80::/10）、回环、未指定、IPv4 都不可用。

    防住的独立风险：链路本地地址还需要网卡范围标识，手机浏览器填不进去。
    """
    assert is_usable_lan_ipv6("2001:db8::77")
    assert is_usable_lan_ipv6("fd00::abcd")  # 站点本地/ULA 可用
    assert not is_usable_lan_ipv6("fe80::1")
    assert not is_usable_lan_ipv6("::1")
    assert not is_usable_lan_ipv6("::")
    assert not is_usable_lan_ipv6("192.0.2.7")
    assert not is_usable_lan_ipv6("")
    assert not is_usable_lan_ipv6(None)


def test_get_lan_ipv6_returns_none_when_probe_unavailable(monkeypatch):
    """探测失败（不支持 IPv6 或权限被拒）时返回 None，不编造地址。

    防住的独立风险：环境拿不到 IPv6 时回退成 IPv4 或 ::1，把不可达地址当成手机入口。
    """
    monkeypatch.setattr(
        "core.access.socket",
        _SocketStub(error=OSError("模拟本机不允许建立 IPv6 套接字")),
    )
    assert get_lan_ipv6() is None

    # 链路本地地址即使带网卡范围标识也算"没有可用地址"
    monkeypatch.setattr("core.access.socket", _SocketStub(address="fe80::42%12"))
    assert get_lan_ipv6() is None

    # 回环不是对外地址
    monkeypatch.setattr("core.access.socket", _SocketStub(address="::1"))
    assert get_lan_ipv6() is None

    monkeypatch.setattr("core.access.socket", _SocketStub(address="2001:db8::9"))
    assert get_lan_ipv6() == "2001:db8::9"


class _SocketStub:
    """顶掉 core.access 里引用的 socket 模块：探测只取源地址，不发任何包。"""

    AF_INET6 = 23
    SOCK_DGRAM = 2

    def __init__(self, address: str | None = None, error: OSError | None = None) -> None:
        self._address = address
        self._error = error

    def socket(self, *args, **kwargs):
        if self._error is not None:
            raise self._error
        return _FakeDatagram(self._address)


class _FakeDatagram:
    def __init__(self, address: str | None) -> None:
        self._address = address

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def connect(self, address):
        pass

    def getsockname(self):
        return (self._address or "", 0, 0, 0)


def test_is_loopback_bind_separates_loopback_from_wildcard():
    """回环监听与通配监听分得清：空串/0.0.0.0/:: 都算对外开放。

    防住的独立风险：把通配监听误判成"仅本机"，手机可用的地址被写成不可用；
    或把 ::1 误判成对外开放，给了手机其实连不上的地址。
    """
    assert is_loopback_bind("127.0.0.1")
    assert is_loopback_bind("127.0.0.2")
    assert is_loopback_bind("::1")
    assert is_loopback_bind("localhost")
    assert not is_loopback_bind("0.0.0.0")
    assert not is_loopback_bind("::")
    assert not is_loopback_bind("")
    assert not is_loopback_bind("192.168.3.83")


def test_address_info_loopback_only_does_not_claim_lan(tmp_path):
    """只监听回环时，明确写出手机无法通过局域网访问，也不给可达的局域网 URL。"""
    target = tmp_path / "访问地址.txt"
    text = save_address_info(
        "127.0.0.1", 8123, target=target, lan_ip="192.0.2.7"
    ).read_text(encoding="utf-8")
    assert "本机地址：http://127.0.0.1:8123" in text
    assert "手机无法通过局域网连接" in text
    assert "192.0.2.7" not in text       # 没有给出其实不可用的局域网地址
    assert "7860" not in text            # 端口是本次实际值，不是写死的旧端口


def test_address_info_uses_actual_host_and_port():
    """通配监听取实际局域网 IP + 实际端口；绑定具体地址时直接用该地址。

    防住的独立风险：默认局域网地址写死 7860，非默认端口下手机照着错误地址连。
    """
    entries = address_entries("0.0.0.0", 8123, lan_ip="192.0.2.7")
    assert entries["局域网地址"] == "http://192.0.2.7:8123"
    assert entries["本机地址"] == "http://127.0.0.1:8123"
    assert entries["公网地址"] == "未开启（本次启动未建立公网隧道）"
    assert address_entries("192.168.3.83", 8000)["局域网地址"] == "http://192.168.3.83:8000"


def test_address_info_holds_no_credentials(tmp_path):
    """地址文件只含地址与更新时间：随机密码、密钥形态的字符串都不出现。"""
    password = generate_password()
    target = tmp_path / "data" / "访问地址.txt"

    save_address_info("0.0.0.0", 8000, target=target, lan_ip="192.0.2.7")
    text = target.read_text(encoding="utf-8")
    assert password not in text
    assert "sk-" not in text
    assert "更新时间：" in text
