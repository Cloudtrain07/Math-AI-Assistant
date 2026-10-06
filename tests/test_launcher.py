"""启动入口批处理的行为验证。

不运行真实服务：把仓库里的两个 .bat 复制进临时目录，
用替身 api.py / app.py 记录被调用时的参数，用临时目录里拼装的
"真 python.exe"（复制现有解释器 + PYTHONHOME）充当系统 Python 回退，
只验证批处理自身的调用契约（调谁、传什么参数、退出码是否保留），
不连接真实配置与真实数据。

替身必须是真正的 python.exe 而不是 python.bat：cmd 批处理直接调用
另一个 .bat（未加 call）会转移执行流程、不再返回主脚本收尾，
用 .bat 充当 Python 会跳过 `set "LAUNCH_EXIT=%errorlevel%"` 与
`exit /b %LAUNCH_EXIT%`，无法证明退出码收尾真实执行。
复制 python.exe 及其同目录运行时 DLL、并设 PYTHONHOME 指向现有
解释器的 base_prefix，即可在临时目录得到可用的独立解释器，
只用标准库、不安装任何东西。

仅在 Windows 上运行：被测对象是 cmd 批处理。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="启动入口是 Windows 批处理"
)

REPO_ROOT = Path(__file__).resolve().parents[1]
NEW_BAT = "启动数学助手.bat"
OLD_BAT = "启动数学助手_旧版.bat"

STUB_ENTRY = """\
import json, os, sys
from pathlib import Path
record_name = {record!r}
Path(record_name).write_text(
    json.dumps({{"program": record_name.removesuffix(".record.json"),
                "argv": sys.argv[1:]}}),
    encoding="utf-8",
)
exit_code = os.environ.get("STUB_EXIT_CODE")
if exit_code:
    sys.exit(int(exit_code))
"""


def stub_source(entry: str) -> str:
    return STUB_ENTRY.format(record=f"{entry}.record.json")


def copy_portable_python(dest_dir: Path) -> Path:
    """把现有解释器复制成 dest_dir/python.exe 独立替身，返回其路径。

    复制基础解释器的 python.exe 与同目录的 python*/vcruntime* DLL，
    运行时用 PYTHONHOME 指向 base_prefix 找到标准库（见模块 docstring）。
    """
    base_exe = Path(getattr(sys, "_base_executable", sys.executable))
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / "python.exe"
    shutil.copy2(base_exe, target)
    for dll in base_exe.parent.glob("*.dll"):
        if dll.name.lower().startswith(("python", "vcruntime")):
            shutil.copy2(dll, dest_dir / dll.name)
    return target


def make_stub_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """PATH 首位放入真 python.exe 替身目录，返回 (env, fakebin 目录)。"""
    fakebin = tmp_path / "fakebin"
    copy_portable_python(fakebin)
    env = os.environ.copy()
    env["PATH"] = f"{fakebin}{os.pathsep}{env.get('PATH', '')}"
    env["PYTHONHOME"] = sys.base_prefix
    return env, fakebin


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """一个可运行的替身项目目录：含 .env、前端构建占位与替身入口。"""
    root = tmp_path / "sandbox"
    root.mkdir()
    shutil.copy(REPO_ROOT / NEW_BAT, root / NEW_BAT)
    shutil.copy(REPO_ROOT / OLD_BAT, root / OLD_BAT)
    (root / ".env").write_text("DEEPSEEK_API_KEY=stub\n", encoding="utf-8")
    dist = root / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    for entry in ("api.py", "app.py"):
        (root / entry).write_text(
            stub_source(entry.removesuffix(".py")), encoding="utf-8"
        )
    return root


@pytest.fixture
def fake_python_env(tmp_path: Path) -> dict[str, str]:
    env, _ = make_stub_env(tmp_path)
    return env


def run_bat(sandbox: Path, name: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["cmd", "/c", str(sandbox / name)],
        cwd=sandbox,
        env=env,
        stdin=subprocess.DEVNULL,  # 末尾的 pause 在 stdin EOF 时立即返回
        capture_output=True,
        timeout=120,
    )


def read_record(sandbox: Path, entry: str) -> dict:
    return json.loads(
        (sandbox / f"{entry}.record.json").read_text(encoding="utf-8")
    )


def test_new_entry_calls_api_with_expected_args(sandbox, fake_python_env):
    result = run_bat(sandbox, NEW_BAT, fake_python_env)

    assert result.returncode == 0, result.stdout + result.stderr
    record = read_record(sandbox, "api")
    assert record["program"] == "api"
    assert record["argv"] == ["--host", "0.0.0.0", "--port", "8000"]


def test_old_entry_still_calls_app(sandbox, fake_python_env):
    result = run_bat(sandbox, OLD_BAT, fake_python_env)

    assert result.returncode == 0, result.stdout + result.stderr
    record = read_record(sandbox, "app")
    assert record["program"] == "app"
    assert "api.py" not in record["argv"]


def test_new_entry_fails_without_frontend_build(sandbox, fake_python_env):
    (sandbox / "frontend" / "dist" / "index.html").unlink()

    result = run_bat(sandbox, NEW_BAT, fake_python_env)

    assert result.returncode != 0
    assert not (sandbox / "api.record.json").exists(), "缺少构建时不得启动 API"


def test_new_entry_fails_without_env(sandbox, fake_python_env):
    (sandbox / ".env").unlink()

    result = run_bat(sandbox, NEW_BAT, fake_python_env)

    assert result.returncode != 0
    assert not (sandbox / "api.record.json").exists(), "缺少 .env 时不得启动 API"


def test_new_entry_survives_chinese_and_space_path(
    sandbox, tmp_path, fake_python_env
):
    root = tmp_path / "中文 目录"
    shutil.copytree(sandbox, root)

    result = run_bat(root, NEW_BAT, fake_python_env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert read_record(root, "api")["argv"] == [
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ]


def test_new_entry_preserves_nonzero_exit_code(sandbox, monkeypatch):
    """替身以 3 退出时主脚本收尾必须原样返回 3。

    这条断言就是收尾回归：若收尾被跳过（例如末尾被改成 exit /b 0），
    实际返回会是 0，本断言失败。
    """
    monkeypatch.setenv("STUB_EXIT_CODE", "3")
    env, _ = make_stub_env(sandbox.parent)

    result = run_bat(sandbox, NEW_BAT, env)

    assert result.returncode == 3


def test_venv_python_takes_priority_over_path_fallback(sandbox, tmp_path):
    """.venv\\Scripts\\python.exe 存在时必须被优先使用。

    PATH 上只放一个 python.bat 哨兵：一旦回退入口被调用，cmd 会把
    执行流程转移给哨兵并留下标记文件（且运行必然失败）。
    """
    copy_portable_python(sandbox / ".venv" / "Scripts")

    sentinel_dir = tmp_path / "sentinel"
    sentinel_dir.mkdir()
    marker = tmp_path / "fallback-invoked.marker"
    sentinel = sentinel_dir / "python.bat"
    sentinel.write_text(
        f'@echo off\n> "{marker}" echo fallback invoked\nexit /b 9\n',
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PATH"] = f"{sentinel_dir}{os.pathsep}{env.get('PATH', '')}"
    env["PYTHONHOME"] = sys.base_prefix

    result = run_bat(sandbox, NEW_BAT, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert read_record(sandbox, "api")["argv"] == [
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ]
    assert not marker.exists(), "存在 .venv 时不得回退到 PATH 中的 Python"
