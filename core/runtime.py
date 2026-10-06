"""运行依赖组装：把配置、服务商、客户端与存储/日志对象装配为一个 AppRuntime。

组装只在显式调用 build_runtime 时读取环境与文件；导入本模块不产生任何副作用。
app 的全部运行状态都挂在唯一的 AppRuntime 实例上，回调与界面默认值统一读取字段。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from core import accounts as accounts_module
from core.compressor import HistoryCompressor
from core.config import Settings, get_base_settings, load_env_file
from core.llm_client import LLMClient
from core.logger import FileEventLogger
from core.providers import (
    get_available_providers,
    get_default_provider,
    load_providers,
)
from core.storage import get_history_store
from core.usage_store import DailyUsageStore

# data 目录位于项目根目录；测试可在收集前替换此常量隔离路径
DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# 区分"未提供 settings（需要读取配置）"与"显式注入 settings=None（无配置回退）"
_UNSET = object()


@dataclass
class AppRuntime:
    """应用运行状态的唯一来源；字段在组装时确定，测试可替换单个字段。"""

    data_dir: Path
    accounts_path: Path
    accounts: list[dict]
    settings: Settings | None
    all_providers: list[dict]
    available_providers: list[dict]
    ui_providers: list[dict]
    default_provider: dict | None
    clients: dict
    client: LLMClient | None
    startup_hint: str
    history_compressor: HistoryCompressor
    history_store: object
    daily_usage_store: object
    event_logger: object
    daily_limit: int
    global_daily_limit: int
    max_message_len: int


def build_runtime(
    *,
    env_file: str | Path | None = None,
    providers_file: str | Path | None = None,
    accounts_path: str | Path | None = None,
    data_dir: str | Path | None = None,
    account_count: int | None = None,
    settings=_UNSET,
    client_factory=LLMClient,
    history_store=None,
    daily_usage_store=None,
    event_logger=None,
) -> AppRuntime:
    """组装运行状态（账号、存储、日志、模型客户端）。

    - env_file/providers_file/accounts_path/data_dir 缺省时在调用时读取各模块
      的路径常量（与此前各模块自取默认值的行为一致）；
    - settings 缺省（_UNSET）时调用 get_base_settings() 读取配置；
      显式传 None 表示无配置，走限额等回退值；
    - history_store/daily_usage_store/event_logger 可注入假实现，缺省按 data_dir 构建。
    """
    load_env_file(env_file)

    if account_count is None:
        account_count = int(os.getenv("ACCOUNT_COUNT", "5") or "5")
    if accounts_path is None:
        accounts_path = accounts_module.ACCOUNTS_FILE
    accounts_path = Path(accounts_path)
    accounts = accounts_module.ensure_accounts(account_count, path=accounts_path)

    try:
        if settings is _UNSET:
            settings = get_base_settings()
        all_providers = load_providers(providers_file)
        available_providers = get_available_providers(all_providers)
        clients = {p["id"]: client_factory(p, settings) for p in available_providers}
        default_provider = get_default_provider(all_providers)
        client = clients.get(default_provider["id"]) if default_provider else None
        ui_providers = available_providers or all_providers
        startup_hint = ("未找到可用的 API Key（DeepSeek / OpenAI），请检查 .env 后重启。" if not client else "")
        # 客户端已在上方按服务商构建
    except ValueError as exc:
        # 初始化失败（如客户端构造抛出 ValueError）时应用仍可启动，
        # 全部字段落入回退值，界面给出提示
        settings = None
        all_providers = []
        available_providers = []
        clients = {}
        default_provider = None
        client = None
        ui_providers = []
        startup_hint = str(exc)

    if data_dir is None:
        data_dir = DEFAULT_DATA_DIR
    data_dir = Path(data_dir)
    if history_store is None:
        history_store = get_history_store(data_dir)
    if daily_usage_store is None:
        daily_usage_store = DailyUsageStore(data_dir / "usage_daily.json")
    if event_logger is None:
        event_logger = FileEventLogger(
            data_dir,
            model=settings.model if settings is not None else None,
            thinking=settings.thinking if settings is not None else None,
            reasoning_effort=(
                settings.reasoning_effort if settings is not None else None
            ),
        )

    return AppRuntime(
        data_dir=data_dir,
        accounts_path=accounts_path,
        accounts=accounts,
        settings=settings,
        all_providers=all_providers,
        available_providers=available_providers,
        ui_providers=ui_providers,
        default_provider=default_provider,
        clients=clients,
        client=client,
        startup_hint=startup_hint,
        history_compressor=HistoryCompressor(),
        history_store=history_store,
        daily_usage_store=daily_usage_store,
        event_logger=event_logger,
        daily_limit=settings.daily_token_limit if settings is not None else 1_000_000,
        global_daily_limit=(
            settings.global_daily_token_limit if settings is not None else 3_000_000
        ),
        max_message_len=settings.max_message_len if settings else 2000,
    )
