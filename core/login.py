"""登录流程：密码校验 + 访问日志 + 成功登录计数的唯一副作用点。

保证：
- 访问日志每次校验（无论成败）恰好记录一条；
- 登录计数只在成功后记一次，计数失败不影响密码校验结果。
本模块不导入 api.py 或任何界面模块。
"""

from __future__ import annotations

import datetime

from core.accounts import check_login


def verify_login(runtime, username: str, password: str) -> bool:
    """按 runtime 的账号清单校验登录，并记录访问日志与成功登录计数。"""
    ok = check_login(username, password, path=runtime.accounts_path)
    runtime.event_logger.log_access_event(
        "登录", f"账号={username}", f"结果={'成功' if ok else '失败'}"
    )
    if ok:
        try:
            runtime.daily_usage_store.add_login(datetime.date.today().isoformat())
        except Exception:
            # 登录次数记录失败不影响密码校验结果，留下失败信息即可
            runtime.event_logger.log_access_event("登录次数记录失败", f"账号={username}")
    return ok
