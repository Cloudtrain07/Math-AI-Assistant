# -*- coding: utf-8 -*-
"""多账号管理测试：生成、持久化、登录校验与密码重置。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.accounts import (
    _write_accounts,
    check_login,
    ensure_accounts,
    load_accounts,
    reset_password,
)


def test_ensure_creates_n_accounts(tmp_path):
    path = tmp_path / "accounts.json"
    accounts = ensure_accounts(5, path=path)
    assert len(accounts) == 5
    assert accounts[0]["username"] == "user1"
    assert accounts[-1]["username"] == "user5"
    assert all(a["password"] for a in accounts)
    assert path.exists()


def test_ensure_keeps_existing_accounts(tmp_path):
    path = tmp_path / "accounts.json"
    first = ensure_accounts(5, path=path)
    second = ensure_accounts(3, path=path)
    assert second == first
    assert len(second) == 5


def test_ensure_min_one_account(tmp_path):
    path = tmp_path / "accounts.json"
    assert len(ensure_accounts(0, path=path)) == 1


def test_check_login_ok_and_wrong(tmp_path):
    path = tmp_path / "accounts.json"
    accounts = ensure_accounts(2, path=path)
    account = accounts[0]
    assert check_login(account["username"], account["password"], path=path)
    assert not check_login(account["username"], "wrong", path=path)
    assert not check_login("nobody", "x", path=path)
    assert not check_login(account["username"], "", path=path)


def test_disabled_account_rejected(tmp_path):
    path = tmp_path / "accounts.json"
    accounts = ensure_accounts(1, path=path)
    accounts[0]["enabled"] = False
    _write_accounts(accounts, path)
    assert not check_login("user1", accounts[0]["password"], path=path)


def test_custom_username_supported(tmp_path):
    path = tmp_path / "accounts.json"
    _write_accounts(
        [
            {
                "username": "小明",
                "password": "abc123",
                "display_name": "小明",
                "level": "高中",
                "enabled": True,
            }
        ],
        path,
    )
    assert check_login("小明", "abc123", path=path)


def test_invalid_entries_filtered(tmp_path):
    path = tmp_path / "accounts.json"
    _write_accounts(
        [
            {"username": "ok", "password": "p1", "enabled": True},
            {"username": "bad user", "password": "p2", "enabled": True},
            {"username": "dup", "password": "p3", "enabled": True},
            {"username": "dup", "password": "p4", "enabled": True},
        ],
        path,
    )
    accounts = load_accounts(path)
    assert [a["username"] for a in accounts] == ["ok", "dup"]


def test_reset_password(tmp_path):
    path = tmp_path / "accounts.json"
    ensure_accounts(1, path=path)
    assert reset_password("user1", "newpass123", path=path)
    assert check_login("user1", "newpass123", path=path)
    assert not check_login("user1", "oldpass", path=path)
    assert not reset_password("nobody", "x", path=path)


def test_reset_password_rejects_empty(tmp_path):
    path = tmp_path / "accounts.json"
    ensure_accounts(1, path=path)
    assert not reset_password("user1", "   ", path=path)


def test_default_account_path_is_isolated():
    import core.accounts as accounts_module

    real_data_dir = (
        Path(accounts_module.__file__).resolve().parents[1] / "data"
    )
    account_file = accounts_module.ACCOUNTS_FILE.resolve()

    # 必须先确认默认路径已隔离，再调用可能读写账号的函数。
    assert not account_file.is_relative_to(real_data_dir.resolve())

    accounts = accounts_module.ensure_accounts(1)

    assert accounts
    assert account_file.is_file()
    assert accounts_module.load_accounts() == accounts


def test_check_login_non_ascii_password(tmp_path):
    """中文等非 ASCII 密码按精确匹配校验：正确可登录，错误正常拒绝。"""
    path = tmp_path / "accounts.json"
    accounts = [
        {
            "username": "user1",
            "password": "中文密码123",
            "display_name": "user1",
            "level": "高中",
            "enabled": True,
        }
    ]
    _write_accounts(accounts, path)
    assert check_login("user1", "中文密码123", path=path)
    assert not check_login("user1", "中文密码124", path=path)
    assert not check_login("user1", "中文密码", path=path)
    assert not check_login("user1", "", path=path)
