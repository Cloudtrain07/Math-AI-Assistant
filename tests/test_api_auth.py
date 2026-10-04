# -*- coding: utf-8 -*-
"""新 API 层身份验证测试：会话建立、失效、退出与同源防护。

复用 conftest 的隔离环境（临时账号/数据目录，不读真实配置）。
"""

import pytest
from fastapi.testclient import TestClient

from api import SessionStore, create_api_app


@pytest.fixture
def client(api_runtime):
    app = create_api_app(api_runtime)
    return TestClient(app), api_runtime


def _first_account(runtime) -> dict:
    return runtime.accounts[0]


def test_unauthenticated_chat_rejected(client):
    tc, _ = client
    resp = tc.post("/api/chat", json={"message": "1+1=?"})
    assert resp.status_code == 401


def test_unauthenticated_history_me_rejected(client):
    tc, _ = client
    assert tc.get("/api/me").status_code == 401
    assert tc.get("/api/history").status_code == 401


def test_login_failure_does_not_create_session(client):
    tc, runtime = client
    account = _first_account(runtime)
    resp = tc.post(
        "/api/login",
        json={"username": account["username"], "password": "错误密码"},
    )
    assert resp.status_code == 401
    assert "maa_session" not in tc.cookies
    # 失败登录后仍无法访问需要身份的接口
    assert tc.get("/api/me").status_code == 401


def test_login_success_sets_session_and_me(client):
    tc, runtime = client
    account = _first_account(runtime)
    resp = tc.post(
        "/api/login",
        json={"username": account["username"], "password": account["password"]},
    )
    assert resp.status_code == 200
    cookie = resp.headers.get("set-cookie", "")
    assert "maa_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie.lower() or "samesite=lax" in cookie.lower()

    me = tc.get("/api/me")
    assert me.status_code == 200
    assert me.json()["username"] == account["username"]


def test_login_logs_access_event(api_runtime):
    from conftest import MemoryEventLogger

    logger = MemoryEventLogger()
    api_runtime.event_logger = logger
    tc = TestClient(create_api_app(api_runtime))
    account = _first_account(api_runtime)
    tc.post(
        "/api/login",
        json={"username": account["username"], "password": "bad"},
    )
    tc.post(
        "/api/login",
        json={"username": account["username"], "password": account["password"]},
    )
    results = [parts for parts in logger.access_events if parts[0] == "登录"]
    assert any("失败" in part for part in results[0])
    assert any("成功" in part for part in results[1])


def test_logout_invalidates_session(client):
    tc, runtime = client
    account = _first_account(runtime)
    tc.post(
        "/api/login",
        json={"username": account["username"], "password": account["password"]},
    )
    assert tc.post("/api/logout").status_code == 200
    assert tc.get("/api/me").status_code == 401


def test_cross_origin_write_rejected(client):
    tc, runtime = client
    account = _first_account(runtime)
    resp = tc.post(
        "/api/login",
        json={"username": account["username"], "password": account["password"]},
        headers={"Origin": "https://evil.example.com"},
    )
    assert resp.status_code == 403


def test_session_expiry(api_runtime):
    from api import SESSION_COOKIE

    store = SessionStore(ttl_seconds=0)
    token = store.create("user1")
    assert store.get(token) is None


def test_login_body_trusts_no_client_identity_fields(client):
    """请求正文无法指定他人身份：身份只来自服务端会话。"""
    tc, runtime = client
    account = _first_account(runtime)
    other = runtime.accounts[1]
    tc.post(
        "/api/login",
        json={"username": account["username"], "password": account["password"]},
    )
    me = tc.get("/api/me").json()
    assert me["username"] == account["username"]
    assert me["username"] != other["username"]
