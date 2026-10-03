"""多账号管理：账号清单的生成、持久化与登录校验。

账号清单保存在 data/accounts.json（已 gitignore）：
- 首次启动自动生成 ACCOUNT_COUNT（默认 5）个账号，用户名 user1..userN、随机密码；
- 之后账号数量与内容完全由该文件决定：增删条目即可调节人数，重启生效；
- 密码为固定密码，由主人自行管理（可直接编辑文件或在主人面板重置）。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
from pathlib import Path

from core.access import generate_password

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ACCOUNTS_FILE = DATA_DIR / "accounts.json"
DEFAULT_ACCOUNT_COUNT = 5

_lock = threading.Lock()

# 用户名允许：字母数字下划线、中文、连字符，长度 1~32
_SAFE_USERNAME = re.compile(r"^[\w\u4e00-\u9fff-]{1,32}$")

# 数据目录不可写时的降级账号存储（本次运行有效）
_memory_accounts: list[dict] | None = None


def _sanitize_accounts(accounts: object) -> list[dict]:
    """过滤非法条目，保证每个账号都有合法用户名与密码。"""
    result: list[dict] = []
    if not isinstance(accounts, list):
        return result
    seen: set[str] = set()
    for item in accounts:
        if not isinstance(item, dict):
            continue
        username = str(item.get("username", "")).strip()
        password = str(item.get("password", "")).strip()
        if not _SAFE_USERNAME.match(username) or not password or username in seen:
            continue
        seen.add(username)
        result.append(
            {
                "username": username,
                "password": password,
                "display_name": str(item.get("display_name") or username),
                "level": str(item.get("level") or "高中"),
                "enabled": bool(item.get("enabled", True)),
            }
        )
    return result


def _read_accounts(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return _sanitize_accounts(data)


def _write_accounts(accounts: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(accounts, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, path)


def ensure_accounts(
    count: int = DEFAULT_ACCOUNT_COUNT, path: Path | None = None
) -> list[dict]:
    """确保账号清单存在；首次启动按 count 生成随机账号，之后不再改动已有清单。"""
    global _memory_accounts
    path = Path(path) if path is not None else ACCOUNTS_FILE
    with _lock:
        accounts = _read_accounts(path)
        if accounts:
            return accounts
        count = max(1, int(count))
        accounts = [
            {
                "username": f"user{i}",
                "password": generate_password(),
                "display_name": f"user{i}",
                "level": "高中",
                "enabled": True,
            }
            for i in range(1, count + 1)
        ]
        try:
            _write_accounts(accounts, path)
            _memory_accounts = None
        except OSError as exc:
            # 数据目录不可写时降级为内存账号（登录/重置同样走内存，重启后重新生成）
            _memory_accounts = accounts
            print(f"[警告] 无法写入账号清单 {path}（{exc}），本次运行使用内存账号。")
        return accounts


def load_accounts(path: Path | None = None) -> list[dict]:
    """读取当前账号清单；文件缺失或损坏时返回空列表。"""
    path = Path(path) if path is not None else ACCOUNTS_FILE
    with _lock:
        if not path.exists() and _memory_accounts is not None:
            return [dict(account) for account in _memory_accounts]
        return _read_accounts(path)


def get_account(username: str, path: Path | None = None) -> dict | None:
    """按用户名查找账号；不存在时返回 None。"""
    for account in load_accounts(path):
        if account["username"] == username:
            return account
    return None


def check_login(username: str, password: str, path: Path | None = None) -> bool:
    """校验用户名与密码；未启用账号拒绝登录。"""
    account = get_account(username, path)
    if account is None or not account.get("enabled", True):
        return False
    # compare_digest 不支持非 ASCII 字符串，改按 UTF-8 字节比较，
    # 字节相等与字符串精确相等语义一致，中文密码可正常校验
    return secrets.compare_digest(
        account["password"].encode("utf-8"),
        (password or "").encode("utf-8"),
    )


def reset_password(
    username: str, new_password: str, path: Path | None = None
) -> bool:
    """重置账号密码；成功返回 True。"""
    new_password = (new_password or "").strip()
    if not new_password:
        return False
    path = Path(path) if path is not None else ACCOUNTS_FILE
    with _lock:
        accounts = _read_accounts(path)
        memory_mode = False
        if not accounts and _memory_accounts is not None:
            accounts = _memory_accounts
            memory_mode = True
        for account in accounts:
            if account["username"] == username:
                account["password"] = new_password
                if memory_mode:
                    # 内存账号：直接生效，不落盘
                    return True
                try:
                    _write_accounts(accounts, path)
                except OSError:
                    return False
                return True
    return False
