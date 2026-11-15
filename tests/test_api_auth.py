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


def test_login_side_effects_not_duplicated(api_runtime):
    """登录副作用唯一：每次登录恰好 1 条日志、成功登录恰好 +1 次计数
    （解耦后由 core.login.verify_login 提供，新 API 与旧界面共用）。"""
    import datetime

    from conftest import MemoryDailyUsageStore, MemoryEventLogger

    store = MemoryDailyUsageStore()
    logger = MemoryEventLogger()
    api_runtime.daily_usage_store = store
    api_runtime.event_logger = logger
    tc = TestClient(create_api_app(api_runtime))
    account = _first_account(api_runtime)
    today = datetime.date.today().isoformat()
    tc.post(
        "/api/login",
        json={"username": account["username"], "password": "bad"},
    )
    tc.post(
        "/api/login",
        json={"username": account["username"], "password": account["password"]},
    )
    assert store.get_logins(today) == 1
    login_events = [parts for parts in logger.access_events if parts[0] == "登录"]
    assert len(login_events) == 2  # 失败 + 成功各一条，无重复


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


def test_me_level_from_account_and_fallback(api_runtime):
    """/api/me 的学段取自账号配置；会话有效但账号已不存在时回退默认学段。"""
    tc = TestClient(create_api_app(api_runtime))
    account = _first_account(api_runtime)
    tc.post(
        "/api/login",
        json={"username": account["username"], "password": account["password"]},
    )
    me = tc.get("/api/me").json()
    assert me["level"] == account["level"]

    api_runtime.accounts = []
    me = tc.get("/api/me").json()
    assert me["level"] == "高中"
    assert me["display_name"] == account["username"]


# ===================== 远程 HTTPS 边界：Secure Cookie 与公网域名形状 =====================
#
# 对应 Cloudflare Tunnel 接入的本地验证（隧道侧实测见部署文档的待测项）：
# - HTTPS 下发的会话 Cookie 带 Secure；HTTP（本机/局域网直连）不带，
#   两种传输都能登录；
# - 公网域名 Host + 同源 Origin：同源校验放行、local_admin=False、
#   管理接口仍被应用层拒绝；
# - openapi.json 不再挂载，路由清单不对外暴露。


def test_login_https_sets_secure_cookie(api_runtime):
    """HTTPS 下发的会话 Cookie 带 Secure（隧道/公网 HTTPS 场景）。"""
    tc = TestClient(create_api_app(api_runtime), base_url="https://testserver")
    account = _first_account(api_runtime)
    resp = tc.post(
        "/api/login",
        json={"username": account["username"], "password": account["password"]},
    )
    assert resp.status_code == 200
    cookie = resp.headers.get("set-cookie", "")
    assert "maa_session=" in cookie
    assert "Secure" in cookie


def test_login_http_cookie_has_no_secure_and_session_works(api_runtime):
    """HTTP（本机/局域网直连）登录不受 Secure 影响：Cookie 可下发、会话可用。"""
    tc = TestClient(create_api_app(api_runtime))
    account = _first_account(api_runtime)
    resp = tc.post(
        "/api/login",
        json={"username": account["username"], "password": account["password"]},
    )
    assert resp.status_code == 200
    cookie = resp.headers.get("set-cookie", "")
    assert "Secure" not in cookie
    assert tc.get("/api/me").status_code == 200


def test_public_hostname_request_shape(api_runtime):
    """隧道形状（公网域名 Host + 同源 Origin）：同源放行、local_admin=False。"""
    tc = TestClient(create_api_app(api_runtime), base_url="https://maa.example.com")
    account = _first_account(api_runtime)
    resp = tc.post(
        "/api/login",
        json={"username": account["username"], "password": account["password"]},
        headers={"Origin": "https://maa.example.com"},
    )
    assert resp.status_code == 200
    assert resp.json()["local_admin"] is False

    me = tc.get("/api/me")
    assert me.status_code == 200
    assert me.json()["local_admin"] is False

    # 公网管理接口仍被应用层拒绝（远程永不提权）
    admin = tc.get("/api/admin/stats")
    assert admin.status_code == 403

    # 写请求同源校验：公网域名 Origin 与 Host 一致 → 放行（保留公网 Host 口径）
    logout_resp = tc.post(
        "/api/logout", headers={"Origin": "https://maa.example.com"}
    )
    assert logout_resp.status_code == 200


def test_openapi_json_not_served(api_runtime):
    """openapi.json 不再挂载：一律 404，不暴露路由清单（docs 早已关闭）。"""
    tc = TestClient(create_api_app(api_runtime))
    assert tc.get("/openapi.json").status_code == 404
    assert tc.get("/docs").status_code == 404
