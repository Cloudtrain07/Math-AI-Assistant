"""访问地址与随机密码：生成访问密码、获取局域网地址、写入访问地址文件。

save_address_info 供新版 API 启动时写地址文件，只含地址与更新时间，不含凭据。
"""

from __future__ import annotations

import datetime
import secrets
import socket
from pathlib import Path

# 排除易混淆字符（0/O、1/I/l）
_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
DEFAULT_PASSWORD_LENGTH = 8

_OWNER_HOSTS = {"127.0.0.1", "::1", "localhost"}
# 监听地址写法：回环只服务本机；通配（含空串）表示监听所有网卡（手机可局域网访问）。
# 通配要按地址族分别归一：0.0.0.0 只覆盖 IPv4，:: 只覆盖 IPv6，
# 把 IPv6 监听换成 127.0.0.1 会连不上。
_LOOPBACK_BINDS = {"127.0.0.1", "::1", "localhost"}
_WILDCARD_V4_BINDS = {"", "0.0.0.0", "*"}
_WILDCARD_V6_BINDS = {"::"}
_LOOPBACK_V4 = "127.0.0.1"
_LOOPBACK_V6 = "::1"


def generate_password(length: int = DEFAULT_PASSWORD_LENGTH) -> str:
    """随机生成 length 位密码，只使用不易混淆的字母数字。"""
    if length < 1:
        raise ValueError("密码长度必须为正整数")
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))


def get_lan_ip() -> str:
    """获取本机局域网 IP；失败时回退 127.0.0.1。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


# 只用来让系统选出本机的 IPv6 出口源地址：UDP connect 不发包、不做域名解析
_LAN_IPV6_PROBE = ("2001:4860:4860::8888", 80)

# IPv6 通配监听下拿不到同族地址时的统一说法（控制台与地址文件共用）
IPV6_LAN_UNAVAILABLE = (
    "当前为 IPv6 监听（::），尚未取得可用的 IPv6 局域网地址；"
    "手机需要与本机的 IPv6 网络可达"
)


def get_lan_ipv6() -> str | None:
    """本机对同网段可用的 IPv6 地址；取不到或只有不可用地址时返回 None。

    与 get_lan_ip 同一做法（UDP connect 只用于选出源地址，不发出任何包）。
    不新增网卡枚举依赖；环境不支持 IPv6 或权限被拒时按"取不到"处理。
    """
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as sock:
            sock.connect(_LAN_IPV6_PROBE)
            address = sock.getsockname()[0]
    except OSError:
        return None
    # 去掉可能带的网卡范围标识后再判断（链路本地地址即使带范围也不能给手机用）
    address = address.split("%", 1)[0].strip()
    return address if is_usable_lan_ipv6(address) else None


def _is_link_local_ipv6(address: str) -> bool:
    """是否落在链路本地段 fe80::/10（手机浏览器填这种地址用不了）。"""
    head = address.split(":", 1)[0].lower()
    try:
        value = int(head, 16)
    except ValueError:
        return False
    return 0xFE80 <= value <= 0xFEBF


def is_usable_lan_ipv6(address: str | None) -> bool:
    """能否把这个地址作为手机可用的 IPv6 局域网地址：同族且不是回环/链路本地/未指定。"""
    value = (address or "").strip().strip("[]")
    if not is_ipv6_host(value):
        return False
    if value in ("::", "::1") or is_owner_host(value):
        return False
    if _is_link_local_ipv6(value):
        return False
    return True


def is_owner_host(ip: str | None) -> bool:
    """是否为主人本机（localhost）访问。"""
    return (ip or "") in _OWNER_HOSTS


def normalize_bind_host(host: str | None) -> str:
    """监听地址的规范写法：去空白、小写、剥掉 URL 用的方括号。"""
    return (host or "").strip().lower().strip("[]")


def is_ipv6_host(host: str | None) -> bool:
    """是否写成 IPv6 地址（含 ::1、::、具体 v6）——以是否含冒号判断。"""
    return ":" in (host or "").strip().strip("[]")


def url_host(host: str | None) -> str:
    """URL 里的主机名写法：IPv6 必须加方括号，否则端口会被当成地址的一部分。"""
    value = (host or "").strip()
    if value.startswith("[") and value.endswith("]"):
        return value
    return f"[{value}]" if is_ipv6_host(value) else value


def build_url(host: str | None, port: int) -> str:
    """由主机与端口拼访问地址（IPv6 自动加方括号）。"""
    return f"http://{url_host(host)}:{port}"


def is_loopback_bind(host: str | None) -> bool:
    """启动监听地址是否只覆盖回环（含 127.* 与 ::1）；通配与具体网卡地址都不是。"""
    normalized = normalize_bind_host(host)
    return normalized in _LOOPBACK_BINDS or normalized.startswith("127.")


def is_ipv6_wildcard_bind(host: str | None) -> bool:
    """是否以 IPv6 通配方式监听（::）：此时手机可用地只有同地址族的地址。"""
    return normalize_bind_host(host) in _WILDCARD_V6_BINDS


def browse_host(host: str | None) -> str:
    """本机真正可达的监听地址：通配写法按地址族归一到对应回环地址。

    0.0.0.0（含空串）只覆盖 IPv4 → 127.0.0.1；:: 只覆盖 IPv6 → ::1；
    ::1 保持 IPv6；127.0.0.2 这类具体监听地址原样保留（换成 127.0.0.1 就连不上）。
    返回值不带方括号，可直接用于 socket 探测与浏览器地址。
    """
    normalized = normalize_bind_host(host)
    if normalized in _WILDCARD_V4_BINDS:
        return _LOOPBACK_V4
    if normalized in _WILDCARD_V6_BINDS:
        return _LOOPBACK_V6
    return normalized


def lan_host(host: str | None, lan_ip: str | None = None) -> str:
    """对外提供的局域网地址主机名：通配监听取局域网 IP，绑定地址原样使用。"""
    if normalize_bind_host(host) in _WILDCARD_V4_BINDS | _WILDCARD_V6_BINDS:
        return (lan_ip or get_lan_ip()).strip()
    return (host or "").strip()


def address_entries(
    host: str,
    port: int,
    *,
    lan_ip: str | None = None,
    lan_ipv6: str | None = None,
) -> dict[str, str]:
    """按实际监听配置算出各项访问地址（控制台输出与地址文件共用）。

    仅监听回环时明确说明手机无法通过局域网访问，不给出误导性的局域网地址；
    IPv6 通配监听只给同地址族的地址，不把 IPv4 局域网地址当成手机可用入口。
    """
    entries = {
        "监听地址": f"{url_host(host)}:{port}",
        "本机地址": build_url(browse_host(host), port),
    }
    normalized = normalize_bind_host(host)
    if is_loopback_bind(host):
        entries["局域网地址"] = "未监听局域网（仅本机可访问，手机无法通过局域网连接）"
    elif normalized in _WILDCARD_V6_BINDS:
        # lan_ipv6=None 表示自行探测；传空串表示"已确认没有可用地址"，不再探测
        candidate = get_lan_ipv6() if lan_ipv6 is None else lan_ipv6
        if is_usable_lan_ipv6(candidate):
            entries["局域网地址"] = build_url(candidate.strip().strip("[]"), port)
        else:
            entries["局域网地址"] = IPV6_LAN_UNAVAILABLE
    else:
        entries["局域网地址"] = build_url(lan_host(host, lan_ip), port)
    entries["公网地址"] = "未开启（本次启动未建立公网隧道）"
    return entries


def format_access_lines(
    host: str,
    port: int,
    *,
    lan_ip: str | None = None,
    lan_ipv6: str | None = None,
) -> list[str]:
    """访问地址信息文件的正文：标题、更新时间与各项地址。"""
    lines = [
        "数学学习 AI 助手（新版 API）- 访问地址",
        f"更新时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    entries = address_entries(host, port, lan_ip=lan_ip, lan_ipv6=lan_ipv6)
    lines += [f"{label}：{value}" for label, value in entries.items()]
    return lines


def save_address_info(
    host: str,
    port: int,
    *,
    target: Path | str,
    lan_ip: str | None = None,
    lan_ipv6: str | None = None,
) -> Path:
    """把本次启动的实际访问地址写入文件；只含地址与更新时间，不含任何凭据。"""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = format_access_lines(host, port, lan_ip=lan_ip, lan_ipv6=lan_ipv6)
    lines.append("提示：本文件只记录访问地址，不含账号、密码或 API Key。")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target
