# -*- coding: utf-8 -*-
"""app 导入与显式创建边界测试：导入只提供定义，界面经 create_app 显式创建。

只覆盖本块引入的边界：导入副作用、创建入口独立性、_bind 对 Gradio 的
签名/Request/生成器契约、服务商切换回调的配置使用、
未认证首页的端点元数据收敛（进程内 HTTP 响应）；
回调业务行为见 test_respond 等既有测试。
"""

import inspect
import json
import re
import secrets
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gradio as gr
from fastapi.testclient import TestClient
from gradio.helpers import special_args
from gradio.routes import App

import app as app_module
from conftest import MemoryDailyUsageStore, MemoryEventLogger, MemoryHistoryStore
from core import dialogue_state
from core import usage as usage_module
from core.runtime import build_runtime

REPO_ROOT = Path(__file__).resolve().parents[1]

# 首页 HTML 内嵌的界面配置（window.gradio_config = {...};</script>）
_CONFIG_RE = re.compile(r"window\.gradio_config = (.+?);</script>", re.S)


def test_import_app_has_no_assembly_side_effects():
    """导入 app 只提供定义：不组装运行对象、不创建 Blocks。"""
    code = "\n".join([
        "import json, gradio as gr",
        "import core.runtime as cr",
        "blocks_created, assemblies = [], []",
        "class BlocksSpy(gr.Blocks):",
        "    def __init__(self, *a, **k):",
        "        blocks_created.append(1)",
        "        super().__init__(*a, **k)",
        "gr.Blocks = BlocksSpy",
        "cr.build_runtime = lambda *a, **k: assemblies.append(1)",
        "import app",
        "print(json.dumps([blocks_created, assemblies,",
        " hasattr(app, 'runtime'), hasattr(app, 'demo'), callable(app.create_app)]))",
    ])
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    blocks_created, assemblies, has_runtime, has_demo, has_create = json.loads(
        proc.stdout.strip().splitlines()[-1]
    )
    assert blocks_created == []   # Blocks 计数桩未被触发
    assert assemblies == []       # 组装入口未被调用
    assert not has_runtime        # 无模块级运行对象
    assert not has_demo           # 无模块级界面
    assert has_create


def _fake_runtime():
    """假依赖运行对象：三个 IO 依赖换成内存假实现（不构造真实客户端）。"""
    return build_runtime(
        history_store=MemoryHistoryStore(),
        daily_usage_store=MemoryDailyUsageStore(),
        event_logger=MemoryEventLogger(),
    )


def test_create_app_builds_independent_blocks():
    """create_app 用传入运行对象显式建界面：可重复创建，实例相互独立。"""
    demo1 = app_module.create_app(_fake_runtime())
    demo2 = app_module.create_app(_fake_runtime())
    assert isinstance(demo1, gr.Blocks)
    assert isinstance(demo2, gr.Blocks)
    assert demo1 is not demo2


def test_create_app_hides_api_endpoint_metadata():
    """show_api 收敛后，get_api_info（登录页内嵌 gradio_api_info 与 /info 的来源）为空。"""
    demo = app_module.create_app(_fake_runtime())
    info = demo.get_api_info()
    assert info["named_endpoints"] == {}
    assert info["unnamed_endpoints"] == {}
    # all_endpoints=true 也不得绕过 show_api=False（首页内部直接调用 api_info）
    assert demo.get_api_info(all_endpoints=True) == info
    # 未用 api_name=False 关闭调用能力：端点名仍可解析
    assert [fn._id for fn in demo.fns.values() if fn.api_name is not False]


def _sentinel_accounts(tmp_path):
    """写一份唯一合成账号的临时清单，返回 (路径, 用户名, 密码)。"""
    username = f"sentinel{secrets.token_hex(6)}"
    password = f"pw{secrets.token_hex(6)}"
    path = tmp_path / "accounts.json"
    path.write_text(
        json.dumps(
            [
                {
                    "username": username,
                    "password": password,
                    "display_name": username,
                    "level": "高中",
                    "enabled": True,
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path, username, password


def _http_client(tmp_path):
    """按 launch 的方式挂上 auth 回调，取进程内 ASGI 应用（不起真实服务器）。"""
    accounts_path, username, password = _sentinel_accounts(tmp_path)
    runtime = build_runtime(
        accounts_path=accounts_path,
        history_store=MemoryHistoryStore(),
        daily_usage_store=MemoryDailyUsageStore(),
        event_logger=MemoryEventLogger(),
    )
    demo = app_module.create_app(runtime)
    # 复现 launch 的登录相关赋值（launch 参数本身由 test_app_startup 覆盖）
    demo.auth = app_module._bind(app_module._check_login, runtime)
    demo.auth_message = "请输入访问用户名与密码"
    client = TestClient(App.create_app(demo, mcp_server=False))
    return client, demo, username, password


def test_unauthenticated_home_hides_endpoint_metadata(tmp_path):
    """未认证首页的实际响应：默认与 all_endpoints=true 都没有事件元数据与账号取值。"""
    client, _demo, username, _password = _http_client(tmp_path)

    for params in ({}, {"all_endpoints": "true"}):
        response = client.get("/", params=params)
        assert response.status_code == 200, params
        body = response.text
        assert '"auth_required":true' in body          # 登录页照常渲染
        assert "请输入访问用户名与密码" in body
        assert '"named_endpoints":{}' in body
        assert '"unnamed_endpoints":{}' in body
        assert "python_type" not in body               # 参数类型元数据未泄漏
        assert username not in body                    # 合成账号未泄漏
        assert '"components":[]' in body               # 界面组件清单为空


def test_protected_routes_require_login(tmp_path):
    """未认证的 /info（含 all_endpoints）与 /config 等受保护接口继续要求认证。"""
    client, _demo, _username, _password = _http_client(tmp_path)

    for url in (
        "/gradio_api/info",
        "/gradio_api/info?all_endpoints=true",
        "/gradio_api/openapi.json",
        "/config",
        "/gradio_api/login_check",
    ):
        assert client.get(url).status_code == 401, url


def test_logged_in_home_events_and_info_available(tmp_path):
    """登录后：首页渲染完整界面配置、事件绑定与账号取值可用，/info 仍为空清单。"""
    client, demo, username, password = _http_client(tmp_path)

    login = client.post("/login", data={"username": username, "password": password})
    assert login.status_code == 200
    assert login.json()["success"] is True

    home = client.get("/")
    assert home.status_code == 200
    assert '"auth_required"' not in home.text
    config = json.loads(_CONFIG_RE.search(home.text).group(1))
    assert config["dependencies"]                      # 事件绑定仍在
    assert username in home.text                       # 账号取值只对登录者可见

    empty_info = {"named_endpoints": {}, "unnamed_endpoints": {}}
    for params in ({}, {"all_endpoints": "true"}):
        info = client.get("/gradio_api/info", params=params)
        assert info.status_code == 200
        assert info.json() == empty_info
    assert all(fn.show_api is False for fn in demo.fns.values())


def test_bind_plain_callback_hides_runtime_and_injects_request(
    app_runtime, monkeypatch
):
    """普通回调绑定：对外签名不含 runtime，Request 注入到位，调用取得本 app 的 runtime。"""
    store = MemoryHistoryStore()
    store._messages["user1"] = [{"role": "user", "content": "之前的问题"}]
    monkeypatch.setattr(app_runtime, "history_store", store)

    bound = app_module._bind(app_module.restore_history, app_runtime)
    assert list(inspect.signature(bound).parameters) == ["request"]

    request = type("Request", (), {"username": "user1"})()
    processed, _, _ = special_args(bound, [], request)
    assert len(processed) == 1 and processed[0] is request  # 注入位置正确

    history, message = bound(*processed)
    assert [m["content"] for m in history] == ["之前的问题"]  # 取得本 app 的 runtime
    assert message == ""


def test_bind_generator_callback_respond_streams(app_runtime, monkeypatch):
    """生成器回调绑定（respond）：签名、注入位置、输入数量、生成器识别与产出。"""

    class _FakeUsage:
        total_tokens = 123

    class _FakeClient:
        def chat_stream_with_usage(
            self, messages, thinking=None, reasoning_effort=None, model=None
        ):
            yield "你好", None, None, None
            yield "世界", None, _FakeUsage(), "stop"

    monkeypatch.setattr(app_runtime, "client", _FakeClient())
    monkeypatch.setattr(app_runtime, "history_store", MemoryHistoryStore())
    logger = MemoryEventLogger()
    monkeypatch.setattr(app_runtime, "event_logger", logger)
    monkeypatch.setattr(app_runtime, "daily_usage_store", MemoryDailyUsageStore())

    bound = app_module._bind(app_module.respond, app_runtime)
    params = list(inspect.signature(bound).parameters)
    assert "runtime" not in params
    assert params[0] == "message" and params[-1] == "attach_image"
    assert params.index("request") == 12  # 12 个组件输入之后
    assert inspect.isgeneratorfunction(bound)

    request = type("Request", (), {"client": None, "username": "user1"})()
    inputs = [
        "1+1=?",
        [],
        "高中",
        "互动模式",
        False,
        "测试模型",
        "high",
        None,
        usage_module.new_usage_state(),
        dialogue_state.new_stage_state(),
        {"path": None, "fresh": False},
        None,
    ]
    processed, _, _ = special_args(bound, list(inputs), request)
    assert len(processed) == 15           # 12 输入 + Request + 2 个默认参数
    assert processed[12] is request       # Request 注入位置正确

    events = list(bound(*processed))
    assert events[-1][0][-1]["content"] == "你好世界"  # 生成器产出正常
    assert logger.chat_events              # 经本 app 的 runtime 记录日志


def _providers_file(tmp_path, provider_id, model):
    """写一份仅含一个自定义服务商的临时配置，返回文件路径。"""
    path = tmp_path / f"providers_{provider_id}.json"
    path.write_text(
        json.dumps(
            [
                {
                    "id": provider_id,
                    "name": f"服务商-{provider_id}",
                    "base_url": f"https://{provider_id}.invalid/v1",
                    "api_key_env": f"{provider_id.upper()}_API_KEY",
                    "models": [model],
                    "default_model": model,
                }
            ]
        ),
        encoding="utf-8",
    )
    return path


def _provider_change_fn(demo):
    """取 create_app 绑定的服务商切换回调（经 _bind，签名只剩 provider_id）。"""
    fns = [
        bound.fn
        for bound in demo.fns.values()
        if bound.fn is not None  # 停止按钮等取消型绑定没有可调用对象
        and list(inspect.signature(bound.fn).parameters) == ["provider_id"]
    ]
    assert len(fns) == 1
    return fns[0]


def test_provider_change_uses_runtime_providers(tmp_path):
    """服务商切换回调从当前 runtime 取服务商，两个 runtime 不串配置。

    默认配置中的 deepseek 不在注入配置里：若回调回退到默认配置会返回模型清单，
    因此对其返回空清单同时证明配置隔离与"找不到服务商"的原有行为。
    """
    rt_review = build_runtime(
        providers_file=_providers_file(tmp_path, "review", "review-model"),
        history_store=MemoryHistoryStore(),
        daily_usage_store=MemoryDailyUsageStore(),
        event_logger=MemoryEventLogger(),
    )
    rt_other = build_runtime(
        providers_file=_providers_file(tmp_path, "other", "other-model"),
        history_store=MemoryHistoryStore(),
        daily_usage_store=MemoryDailyUsageStore(),
        event_logger=MemoryEventLogger(),
    )

    change_review = _provider_change_fn(app_module.create_app(rt_review))
    models, thinking, effort = change_review("review")
    assert models["choices"] == ["review-model"]
    assert models["value"] == "review-model"
    assert thinking["visible"] is True  # 默认 thinking_param 非 none，思考控件显示
    assert effort["visible"] is True

    missing_models, missing_thinking, missing_effort = change_review("deepseek")
    assert missing_models["choices"] == []
    assert missing_thinking["visible"] is False
    assert missing_effort["visible"] is False

    change_other = _provider_change_fn(app_module.create_app(rt_other))
    other_models, _t, _e = change_other("other")
    assert other_models["choices"] == ["other-model"]
