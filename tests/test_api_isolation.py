# -*- coding: utf-8 -*-
"""新 API 独立性验证：子进程内屏蔽 app / gradio 导入后，api 仍可导入并完成关键路径。

不是只搜索 import 字样：真实执行 import api → create_api_app → 登录 →
会话 → 管理员拒绝 → SSE 被拒路径，全程禁止 app 与 gradio 进入 sys.modules。
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO_ROOT = Path(__file__).resolve().parents[1]

# 子进程脚本：安装导入屏蔽器（app / gradio / gradio_client 一律禁止），
# 然后独立组装隔离 runtime 并走一遍新 API 关键路径，结果以 JSON 打印。
ISOLATION_CODE = r"""
import json
import sys
import tempfile
from pathlib import Path

BLOCKED = {"app", "gradio", "gradio_client"}

class ImportBlocker:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in BLOCKED:
            raise ImportError(f"隔离验证禁止导入: {fullname}")
        return None

sys.meta_path.insert(0, ImportBlocker())
sys.path.insert(0, sys.argv[1])

with tempfile.TemporaryDirectory(prefix="maa-api-isolation-") as _tmp:
    tmp = Path(_tmp)
    accounts_path = tmp / "accounts.json"
    accounts_path.write_text(json.dumps([
        {"username": "iso_user", "password": "iso-pw-1", "display_name": "隔离",
         "level": "高中", "enabled": True},
    ], ensure_ascii=False), encoding="utf-8")

    from core.runtime import build_runtime
    runtime = build_runtime(
        env_file=tmp / "missing.env",
        providers_file=tmp / "missing.providers.json",
        accounts_path=accounts_path,
        data_dir=tmp / "data",
    )

    import api  # 若间接依赖 app/gradio 会在此抛 ImportError
    app = api.create_api_app(runtime, frontend_dir=None)

    from fastapi.testclient import TestClient
    client = TestClient(app, base_url="http://127.0.0.1:8000")

    # 关键路径：登录 → 会话 → 历史 → 远程形态的管理员拒绝 → 空消息被拒（SSE）
    login = client.post(
        "/api/login",
        json={"username": "iso_user", "password": "iso-pw-1"},
        headers={"Origin": "http://127.0.0.1:8000"},
    )
    assert login.status_code == 200, login.text
    me = client.get("/api/me")
    assert me.status_code == 200 and me.json()["username"] == "iso_user", me.text
    history = client.get("/api/history")
    assert history.status_code == 200, history.text
    admin = client.get("/api/admin/stats")
    assert admin.status_code == 403, admin.text  # TestClient 来源非回环 → 默认拒绝
    stream = client.post(
        "/api/chat",
        json={"message": " ", "mode": "互动模式"},
        headers={"Origin": "http://127.0.0.1:8000"},
    )
    assert stream.status_code == 200 and "rejected" in stream.text, stream.text

    print(json.dumps({
        "gradio_loaded": "gradio" in sys.modules,
        "app_loaded": "app" in sys.modules,
        "login": login.status_code,
        "me": me.json()["username"],
        "admin_status": admin.status_code,
        "stream_rejected": "rejected" in stream.text,
    }, ensure_ascii=False))
"""


def test_api_imports_and_serves_without_app_or_gradio():
    proc = subprocess.run(
        [sys.executable, "-c", ISOLATION_CODE, str(REPO_ROOT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["gradio_loaded"] is False
    assert result["app_loaded"] is False
    assert result["login"] == 200
    assert result["me"] == "iso_user"
    assert result["admin_status"] == 403
    assert result["stream_rejected"] is True
