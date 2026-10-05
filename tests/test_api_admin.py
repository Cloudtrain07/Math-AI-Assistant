# -*- coding: utf-8 -*-
"""管理员接口测试：本机权限判定、统计一致性、重置密码与敏感信息卫生。

权限矩阵围绕"本机管理"定位：未登录 401、远程 403、带转发头 403、
Host/Origin 非回环 403；合法本机请求放行。全部走隔离 runtime，
不读取真实配置。
"""

import datetime
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import create_api_app
from conftest import MemoryDailyUsageStore, MemoryEventLogger
from core.accounts import check_login
from core.usage_store import DailyUsageReadError

LOCAL_ADDR = ("127.0.0.1", 50000)
REMOTE_ADDR = ("203.0.113.7", 40000)
# Host 头与 Origin 都按"本机服务 + 回环"构造，与真实部署一致
BASE_URL = "http://127.0.0.1:8000"
LOCAL_ORIGIN = {"Origin": BASE_URL}


def make_client(app, addr):
    """指定 client 地址的测试客户端：模拟本机 / 远程来源。"""
    return TestClient(app, base_url=BASE_URL, client=addr)


@pytest.fixture
def env(api_runtime):
    """独立应用 + 内存日志；返回 (app, runtime)。"""
    api_runtime.event_logger = MemoryEventLogger()
    app = create_api_app(api_runtime)
    return app, api_runtime


def login_as(http, runtime, index=0):
    account = runtime.accounts[index]
    resp = http.post(
        "/api/login",
        json={"username": account["username"], "password": account["password"]},
        headers=LOCAL_ORIGIN,
    )
    assert resp.status_code == 200
    return account


# ---------- 权限判定 ----------


def test_admin_stats_requires_login(env):
    app, _ = env
    with make_client(app, LOCAL_ADDR) as http:
        resp = http.get("/api/admin/stats")
    assert resp.status_code == 401


def test_admin_stats_denied_for_remote_user(env):
    app, runtime = env
    with make_client(app, REMOTE_ADDR) as http:
        login_as(http, runtime)
        resp = http.get("/api/admin/stats")
    assert resp.status_code == 403
    assert "仅限本机" in resp.json()["detail"]


def test_admin_stats_denied_with_forwarded_headers(env):
    """转发头存在即拒绝（含空值头，不看其值）：伪造头不能借本机来源提权。"""
    app, runtime = env
    headers_list = [
        {"X-Forwarded-For": "127.0.0.1"},
        {"X-Forwarded-For": "1.2.3.4"},
        {"X-Forwarded-For": ""},
        {"Forwarded": "for=127.0.0.1"},
        {"Forwarded": ""},
        {"X-Real-IP": "127.0.0.1"},
        {"X-Real-IP": ""},
        {"Via": "1.1 proxy"},
        {"Via": ""},
        {"X-Forwarded-Host": "evil.example.com"},
    ]
    for extra in headers_list:
        with make_client(app, LOCAL_ADDR) as http:
            login_as(http, runtime)
            resp = http.get("/api/admin/stats", headers=extra)
            assert resp.status_code == 403, extra


def test_admin_reset_denied_with_empty_forwarded_header(env):
    """空值转发头同样不能访问重置密码接口，账号保持不变。"""
    app, runtime = env
    target = runtime.accounts[1]
    old_password = target["password"]
    with make_client(app, LOCAL_ADDR) as http:
        login_as(http, runtime)
        resp = http.post(
            "/api/admin/reset-password",
            json={"username": target["username"], "new_password": "should-not-apply"},
            headers={"X-Forwarded-For": "", **LOCAL_ORIGIN},
        )
    assert resp.status_code == 403
    assert check_login(target["username"], old_password, path=runtime.accounts_path)


def test_admin_denied_with_nonlocal_host_header(env):
    """client.host 是回环但 Host 头指向外部域名（如隧道域名）：拒绝。"""
    app, runtime = env
    with make_client(app, LOCAL_ADDR) as http:
        login_as(http, runtime)
        for host in ("evil.example.com", "trycloudflare.com"):
            resp = http.get("/api/admin/stats", headers={"Host": host})
            assert resp.status_code == 403, host


def test_admin_reset_denied_cross_origin(env):
    """重置密码必须通过写请求同源校验；被拒请求不修改账号。"""
    app, runtime = env
    target = runtime.accounts[1]
    old_password = target["password"]
    with make_client(app, LOCAL_ADDR) as http:
        login_as(http, runtime)
        resp = http.post(
            "/api/admin/reset-password",
            json={"username": target["username"], "new_password": "brand-new-pw"},
            headers={"Origin": "https://evil.example.com"},
        )
    assert resp.status_code == 403
    assert check_login(
        target["username"], old_password, path=runtime.accounts_path
    )


def test_me_reports_local_admin_flag(env):
    app, runtime = env
    with make_client(app, LOCAL_ADDR) as http:
        login_as(http, runtime)
        assert http.get("/api/me").json()["local_admin"] is True
    with make_client(app, REMOTE_ADDR) as http:
        login_as(http, runtime)
        assert http.get("/api/me").json()["local_admin"] is False


# ---------- 统计一致性 ----------


def test_admin_stats_matches_isolated_store(env):
    """统计与隔离存储真实一致；各账号用量逐一对上。

    注入独立内存存储：默认文件存储落在会话级共享临时目录，
    登录计数会跨测试累计，无法做确定性断言。
    """
    app, runtime = env
    store = MemoryDailyUsageStore()
    runtime.daily_usage_store = store
    today = datetime.date.today().isoformat()
    store.add_usage(today, "alice", 123)
    store.add_usage(today, "bob", 77)
    store.add_login(today)
    store.add_question(today)

    with make_client(app, LOCAL_ADDR) as http:
        login_as(http, runtime)
        body = http.get("/api/admin/stats").json()

    assert body["date"] == today
    assert body["global_used"] == 200
    # 登录次数 = login_as 的成功登录（verify_login 副作用）+ 手动记录的 1 次
    assert body["logins"] == 2
    assert body["questions"] == 1
    accounts = {a["username"]: a for a in body["accounts"]}
    assert accounts["alice"]["used"] == 123
    assert accounts["bob"]["used"] == 77
    assert accounts["alice"]["display_name"] == "小明"
    assert accounts["alice"]["enabled"] is True


def test_admin_stats_read_failure_returns_error_not_fake_zero(env):
    """用量读取失败返回 503 固定提示，不返回伪装成正常的全零面板；
    异常消息含内部路径时，响应与诊断日志都不得泄露该路径。"""
    app, runtime = env

    class FailingReadStore(MemoryDailyUsageStore):
        def get_total(self, date):
            raise DailyUsageReadError(
                "每日用量文件读取失败：C:\\Users\\me\\secret\\usage_daily.json 损坏"
            )

    runtime.daily_usage_store = FailingReadStore()
    with make_client(app, LOCAL_ADDR) as http:
        login_as(http, runtime)
        resp = http.get("/api/admin/stats")
    assert resp.status_code == 503
    assert resp.json()["detail"] == "今日用量读取失败，请稍后重试"
    assert "usage_daily" not in resp.text
    assert "C:" not in resp.text
    log_text = "\n".join(
        " ".join(parts) for parts in runtime.event_logger.access_events
    )
    assert "usage_daily" not in log_text
    assert "C:" not in log_text


# ---------- 重置密码 ----------


def test_admin_reset_success_updates_runtime_and_login(env):
    app, runtime = env
    target = runtime.accounts[1]
    other = runtime.accounts[0]
    old_password = target["password"]
    new_password = "brand-new-pw"

    with make_client(app, LOCAL_ADDR) as http:
        login_as(http, runtime)
        resp = http.post(
            "/api/admin/reset-password",
            json={"username": target["username"], "new_password": new_password},
            headers=LOCAL_ORIGIN,
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "username": target["username"]}

    # runtime 账号清单已同步，旧密码失败、新密码可登录，另一账号不受影响
    assert check_login(target["username"], new_password, path=runtime.accounts_path)
    assert not check_login(target["username"], old_password, path=runtime.accounts_path)
    assert check_login(other["username"], other["password"], path=runtime.accounts_path)
    assert any(
        account["password"] == new_password for account in runtime.accounts
    )
    # 审计日志只记账号，不含密码
    assert any(
        parts[0] == "重置密码" and target["username"] in parts[1]
        for parts in runtime.event_logger.access_events
    )


def test_admin_reset_existing_session_stays_valid(env):
    """会话策略：重置密码不使现有会话失效（token 与密码无关）。"""
    app, runtime = env
    target = runtime.accounts[1]
    with make_client(app, LOCAL_ADDR) as http:
        login_as(http, runtime, index=1)
        resp = http.post(
            "/api/admin/reset-password",
            json={"username": target["username"], "new_password": "another-pw"},
            headers=LOCAL_ORIGIN,
        )
        assert resp.status_code == 200
        assert http.get("/api/me").status_code == 200


def test_admin_reset_validation_errors(env):
    app, runtime = env
    with make_client(app, LOCAL_ADDR) as http:
        login_as(http, runtime)
        empty_name = http.post(
            "/api/admin/reset-password",
            json={"username": "  ", "new_password": "x"},
            headers=LOCAL_ORIGIN,
        )
        empty_pw = http.post(
            "/api/admin/reset-password",
            json={"username": "alice", "new_password": "   "},
            headers=LOCAL_ORIGIN,
        )
        unknown = http.post(
            "/api/admin/reset-password",
            json={"username": "nobody", "new_password": "x"},
            headers=LOCAL_ORIGIN,
        )
    assert empty_name.status_code == 400
    assert empty_pw.status_code == 400
    assert unknown.status_code == 404


# ---------- 敏感信息卫生 ----------


def test_admin_responses_and_logs_contain_no_secrets(env):
    """响应体与日志不出现账号现有密码、新密码、Key 或会话 token。"""
    app, runtime = env
    target = runtime.accounts[1]
    old_password = target["password"]
    new_password = "s3cret-new-pw"

    with make_client(app, LOCAL_ADDR) as http:
        login_as(http, runtime)
        stats = http.get("/api/admin/stats")
        reset = http.post(
            "/api/admin/reset-password",
            json={"username": target["username"], "new_password": new_password},
            headers=LOCAL_ORIGIN,
        )
        stats_text = stats.text + reset.text
        assert old_password not in stats_text
        assert new_password not in stats_text
        assert "maa_session" not in reset.text
    all_logs = "\n".join(
        " ".join(parts) for parts in runtime.event_logger.access_events
    )
    assert old_password not in all_logs
    assert new_password not in all_logs
