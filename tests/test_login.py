# -*- coding: utf-8 -*-
"""core.login.verify_login 的副作用测试。

登录事件记录与成功计数的常规路径由 API 层测试覆盖
（tests/test_api_auth.py）；这里只补计数写盘失败分支：
登录次数记录失败不影响密码校验结果，并留下明确失败信息。
"""

import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import MemoryDailyUsageStore, MemoryEventLogger
from core import login as login_module
from core.login import verify_login


class _Runtime:
    """verify_login 所需的最小运行对象。"""

    def __init__(self, store, logger):
        self.accounts_path = None
        self.daily_usage_store = store
        self.event_logger = logger


def test_add_login_failure_keeps_result_and_logs(monkeypatch):
    class FailingLoginStore(MemoryDailyUsageStore):
        def add_login(self, date):
            raise OSError("模拟登录次数写盘失败")

    store = FailingLoginStore()
    logger = MemoryEventLogger()
    runtime = _Runtime(store, logger)
    monkeypatch.setattr(
        login_module, "check_login", lambda u, p, path=None: True
    )

    assert verify_login(runtime, "user1", "pw") is True
    assert any(
        "登录次数记录失败" in str(args) for args in logger.access_events
    )
    assert store.get_logins(datetime.date.today().isoformat()) == 0
