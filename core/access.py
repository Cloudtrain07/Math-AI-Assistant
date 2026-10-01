"""公网访问与随机密码：生成访问密码、获取局域网 IP、写入访问信息文件。"""

from __future__ import annotations

import datetime
import secrets
import socket
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ACCESS_FILE = DATA_DIR / "当前访问密码.txt"

# 排除易混淆字符（0/O、1/I/l）
_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
DEFAULT_PASSWORD_LENGTH = 8

_OWNER_HOSTS = {"127.0.0.1", "::1", "localhost"}


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


def save_access_info(
    url: str | None,
    accounts: list[dict],
    lan_url: str | None = None,
    target: Path | None = None,
) -> Path:
    """把公网/局域网地址与全部访问账号写入访问信息文件，返回文件路径。"""
    target = Path(target) if target is not None else ACCESS_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    lan_url = lan_url or f"http://{get_lan_ip()}:7860"
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "数学学习 AI 助手 - 访问信息",
        f"生成时间：{now}",
        f"公网地址：{url if url else '（暂不可用，请检查网络后重启程序）'}",
        f"局域网地址：{lan_url}",
        "访问账号（用户名 / 密码）：",
    ]
    for account in accounts:
        display = account.get("display_name") or account.get("username")
        suffix = f"（{display}）" if display and display != account.get("username") else ""
        lines.append(f"  {account.get('username')} / {account.get('password')}{suffix}")
    lines += [
        "提示：密码固定，请妥善保管，只分享给信任的人。",
        "",
    ]
    target.write_text("\n".join(lines), encoding="utf-8")
    return target


def is_owner_host(ip: str | None) -> bool:
    """是否为主人本机（localhost）访问。"""
    return (ip or "") in _OWNER_HOSTS
