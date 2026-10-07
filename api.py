"""FastAPI 适配层：身份验证 → 输入校验 → 调用现有核心服务 → 事件映射。

本层只做协议适配：额度判断、阶段推进、上下文压缩、模型调用、历史保存、
用量结算与日志全部留在 core.chat_service，不在 API 层复制。

剩余耦合（已解除）：登录副作用与展示文案已下沉到
core.login（校验+日志+登录计数唯一入口）与 core.ui_text（共用文案与
用量/错误格式化）；旧 Gradio 界面已退役，本层不导入 app 或 gradio。

身份与会话：
- 复用 core.accounts.check_login（经 core.login.verify_login，
  含访问日志与登录计数）；
- 会话为服务端内存 token（secrets.token_urlsafe），经 HttpOnly + SameSite=Lax
  Cookie 下发，进程重启即失效需重新登录；不引入数据库；
- 写请求做同源校验（浏览器 POST 带 Origin 时必须与 Host 一致），
  配合 SameSite=Lax 覆盖跨站风险；不开放 CORS，不信任转发头。

聊天状态归属：
- 历史、阶段、会话累计用量按账号保存在服务端 ChatRegistry，浏览器不可伪造；
- 历史初值来自 history_store.load（磁盘为真相），每轮以核心 final 结果更新；
- 同一账号同时只允许一轮生成（busy 拒绝），避免并发轮次互踩保存标记；
- 清空一致性：begin_turn 在 registry 锁内**原子地**捕获 epoch 并预取本轮
  保存标记（turn_token 经核心的可选参数复用，核心缺省行为不变）；
  clear_account 在同一锁内按固定顺序执行——先 history_store.clear 推进
  保存计数，再重置内存状态并递增 epoch。两组更新各自原子，旧轮次
  不可能从窗口中取得混合状态：清空前开始的轮次拿的是清空前的标记，
  清空后保存必然 stale（磁盘由核心保存标记拦截，内存由 epoch 拦截）；
- 每日已产生用量记录在 daily_usage_store，清空不影响，也不重复记录。

流式契约（SSE，text/event-stream）：
- event: rejected  data:{status, message}          准备阶段被拒（含额度/长度/无客户端）
- event: start     data:{message}                   通过校验，开始生成（含本轮消息原文，供前端展示）
- event: delta     data:{answer?, reasoning?}       增量片段（核心累计快照的差分）
- ": ping" 注释行                                   核心心跳（每秒）转成 SSE 注释保活
- event: done      data:{...}                       收尾：最终文本、用量、保存/每日状态
                                                     （含 save_status: saved/failed/stale）

生命周期保证：响应在正常完成或客户端断连取消时，都会在请求处理返回处
确定性 close 同步生成器（ClosingStreamingResponse），busy 随之释放，
不依赖垃圾回收。断连只表示服务端停止消费并关闭上游流，
不代表模型侧已停止计费，界面文案不做该承诺。

图片上传与临时文件：
- 前端在点击发送后才把编辑确认的图片 POST /api/images（multipart），
  得到服务端生成的 image_id，再随 /api/chat 提交；选择、编辑、预览阶段
  不发生任何上传；
- 临时文件按账号存于 data_dir/temp_images/<账号>/，文件名随机，目录不
  对外静态服务；image_id 仅在本账号会话内可解析，跨账号引用查无此 id；
- 图片状态（path/fresh）保存在 ChatState 上并随核心每轮返回值写回，
  语义：同题追问每轮重新附带原图，新题/读图失败
由核心置空；被替换或被丢弃的临时文件在写回时同步删除；
- 图片挂载（mount_pending_image）在 begin_turn 之后、注册表锁内核验
  epoch 与文件存在性：挂载前发生清空或文件已被删除时拒绝本轮，
  不会把旧路径写回清空后的状态；
- 清空（clear_account）与显式移除（DELETE /api/images）的状态变更与
  文件释放在注册表锁内完成；生成中清空时文件物理删除延迟到旧轮次
  release 释放引用之后（只删清空时记录的精确路径，不误删清空后新上传）；
  启动时清理超过 24 小时的历史残留。

管理员能力（仅本机）：
- 判定在服务端：已登录会话 + client.host 为回环地址 + 不带任何转发头
  （Forwarded / X-Forwarded-* / X-Real-IP / Via）+ Host 头为回环 +
  Origin（浏览器写请求携带时）为回环。任一不满足一律 403；
- 统计接口读取失败返回 503 错误，不返回伪装成正常的全零数据；
- 重置密码复用 core.accounts.reset_password，成功后重载 runtime.accounts
  并记录审计日志；接口不返回也不记录任何密码；
- 局限（如实说明）：不设置转发头且不改写 Host 的同机反向代理在应用层
  无法与真实本机用户区分，此类部署需在部署侧保证（不要在回环上挂无
  转发头的代理）；
- 会话策略：重置密码不使现有会话失效（会话 token 与密码无关），
  旧密码不能再建立新会话；不提供"踢出所有设备"。

启动边界：
- 导入本模块与 create_api_app(runtime) 都不启动服务、备份线程或浏览器，
  配套任务只由 main() 通过 LaunchSupport 显式接上；
- LaunchSupport 是本次启动配套资源的唯一持有者：备份停止信号与线程、
  浏览器等待线程与落盘文件都按 runtime.data_dir + 实际端口归属，
  退出、Ctrl+C、端口占用与启动中途失败都只回收自己创建的那些；
- 地址信息文件只记录地址与更新时间，不含账号、密码或 API Key。
- 静态服务只暴露 frontend/dist：备份 zip、访问地址文件与临时图片都在 data_dir 内，
  不经任何 HTTP 路径公开。
- Windows 的控制台 Ctrl+Break 默认直接结束进程、绕过 finally，因此 main() 在启动入口
  登记 SIGBREAK 保护：处理器只把它变成 KeyboardInterrupt，不拿锁、不结束子进程，
  清理仍走 finally 与 close()，退出时恢复原处理器；导入本模块与应用工厂都不改信号处置。
"""

from __future__ import annotations

import argparse
import datetime
import io
import json
import os
import secrets
import signal
import socket
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import anyio
from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import iterate_in_threadpool

# 登录副作用与文案/格式化来自 core，本层不依赖任何界面层
from core import chat_service
from core import dialogue_state
from core.access import (
    address_entries,
    browse_host,
    build_url,
    get_lan_ipv6,
    is_ipv6_wildcard_bind,
    is_owner_host,
    save_address_info,
)
from core.accounts import load_accounts, reset_password
from core.backup import BACKUP_FAILED, BackupResult, daily_backup_loop, prune_backups, run_backup
from core.login import verify_login
from core.prompts import MODE_ANSWER, MODE_INTERACTIVE
from core.providers import get_model_thinking_capability, get_provider_supports_thinking
from core.runtime import AppRuntime, build_runtime
from core.storage import HistoryReadError, safe_account_name
from core.ui_text import (
    DAILY_FAILED_MSG,
    DAILY_STATS_FAILED_MSG,
    IMAGE_NOTE,
    IMAGE_ONLY_PROMPT,
    NO_CONTINUE_MSG,
    QUOTA_CHECK_FAILED_MSG,
    RESTORE_FAILED_MSG,
    SAVE_FAILED_MSG,
    SAVE_STALE_MSG,
    format_usage,
    friendly_error,
)
from core.usage import format_cumulative, new_usage_state
from core.usage_store import DailyUsageReadError
from core.vision_client import MAX_IMAGE_BYTES, SUPPORTED_EXTS

APP_ROOT = Path(__file__).resolve().parent
FRONTEND_DIST = APP_ROOT / "frontend" / "dist"

SESSION_COOKIE = "maa_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600

LEVELS = ("小学", "初中", "高中", "大学")
MODES = (MODE_INTERACTIVE, MODE_ANSWER)
DEFAULT_ANSWER_STYLE = "完整步骤+答案"
ANSWER_STYLES = ("完整步骤+答案", "简略过程+答案", "只给答案和结论")

# 上传图片的像素上限：只用于防解压炸弹，不做清晰度压缩。
# 依据：主流手机照片 12–48MP，50MP 覆盖绝大多数真实照片；
# 更大者（如 108MP 全尺寸直出）明确拒绝并提示先缩小，不静默处理。
# Pillow 自带的 DecompressionBomb 保护（约 179MP）仍作为最终兜底。
MAX_IMAGE_PIXELS = 50_000_000

# 非目标模型 / thinking_param=none 的显式思考强度沿用原有全局档位校验；
# 已核实目标模型（DeepSeek/Qwen3.7/Qwen3.8/MiMo）按模型级 accepted_efforts 校验
REASONING_EFFORTS = ("low", "high", "max")


# ===================== 会话与账号聊天状态 =====================


class SessionStore:
    """内存会话：token → username，绝对过期；进程重启全部失效。"""

    def __init__(self, ttl_seconds: int = SESSION_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._sessions: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def create(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = (username, time.monotonic() + self._ttl)
        return token

    def get(self, token: str | None) -> str | None:
        if not token:
            return None
        with self._lock:
            entry = self._sessions.get(token)
            if entry is None:
                return None
            username, expires = entry
            if time.monotonic() > expires:
                del self._sessions[token]
                return None
            return username

    def drop(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)


class TempImageStore:
    """按账号隔离的临时图片存储。

    - 文件名（image_id）由服务端随机生成，客户端不能提交本地路径或文件名；
    - 文件落在本账号子目录（safe_account_name 隔离），目录不在任何静态挂载内；
    - resolve 只在本账号的索引内查找，跨账号引用一律查无此 id；
    - delete 仅删除本账号目录内的路径，防止越界删除。
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._lock = threading.Lock()
        # username -> {image_id: path}；仅存内存，进程重启后旧 id 自然失效
        self._index: dict[str, dict[str, Path]] = {}

    def _account_dir(self, username: str) -> Path:
        return self._root / safe_account_name(username)

    def save(self, username: str, data: bytes, ext: str) -> tuple[str, Path]:
        image_id = secrets.token_urlsafe(16)
        account_dir = self._account_dir(username)
        account_dir.mkdir(parents=True, exist_ok=True)
        path = account_dir / f"{image_id}{ext}"
        path.write_bytes(data)
        with self._lock:
            self._index.setdefault(username, {})[image_id] = path
        return image_id, path

    def resolve(self, username: str, image_id: str) -> Path | None:
        with self._lock:
            return self._index.get(username, {}).get(image_id)

    def take_all(self, username: str) -> list[Path]:
        """取出并清空该账号的文件索引，返回精确路径（供延迟删除）。"""
        with self._lock:
            files = self._index.pop(username, {})
        return list(files.values())

    def delete(self, username: str, path: str | Path) -> None:
        """删除本账号目录内的一个临时文件；越界路径直接忽略。"""
        target = Path(path)
        try:
            target.relative_to(self._account_dir(username))
        except ValueError:
            return
        try:
            target.unlink()
        except OSError:
            pass
        with self._lock:
            files = self._index.get(username)
            if files:
                for key, value in list(files.items()):
                    if value == target:
                        del files[key]

    def delete_all(self, username: str) -> None:
        """删除该账号全部临时图片（清空 / 显式移除时使用）。"""
        with self._lock:
            files = list(self._index.pop(username, {}).values())
        for path in files:
            try:
                path.unlink()
            except OSError:
                pass
        account_dir = self._account_dir(username)
        try:
            account_dir.rmdir()
        except OSError:
            pass

    def cleanup_expired(self, max_age_hours: float = 24) -> None:
        """启动时清理历史残留（进程中断遗留的孤儿文件），尽力而为。"""
        import time as _time

        deadline = _time.time() - max_age_hours * 3600
        try:
            candidates = list(self._root.iterdir())
        except OSError:
            return
        for account_dir in candidates:
            if not account_dir.is_dir():
                continue
            for path in account_dir.iterdir():
                try:
                    if path.is_file() and path.stat().st_mtime < deadline:
                        path.unlink()
                except OSError:
                    pass


def _validate_image_bytes(data: bytes) -> tuple[int, int]:
    """独立校验上传图片：结构校验 → 像素上限 → 实际像素解码。

    三步都不信任扩展名与 Content-Type：
    - verify() 只查结构与 CRC，不解码像素，拦截损坏与截断文件；
    - 像素上限在解码前检查，解压炸弹在此被拒（同时限定了下一步
      实际解码的内存上界：≤50MP 的解码约数百 MB 以内）；
    - load() 真实解码像素：头部可读但像素数据损坏（如 IDAT 流损坏）
      的文件也在上传阶段被拒绝，不会等到发送时才由核心报读图失败。
    """
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
    except Exception:
        raise HTTPException(
            status_code=400, detail="图片文件损坏或无法解码，请换一张图片"
        )
    try:
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
            if width * height > MAX_IMAGE_PIXELS:
                raise HTTPException(
                    status_code=400,
                    detail=f"图片像素过大（{width}×{height}），请缩小到 5000 万像素以内再试",
                )
            img.load()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=400, detail="图片文件损坏或无法解码，请换一张图片"
        )
    return width, height


@dataclass
class ChatState:
    """单个账号的服务端聊天状态；history 初值来自磁盘。

    epoch 为清空计数：清空时递增，旧轮次完成后凭它判断状态是否仍有效。
    image_state 语义：path 为服务端临时文件路径，
    fresh 表示本回合刚上传（交给核心 route_image_for_turn 判定是否附图）。
    """

    history: list[dict]
    usage_state: dict
    stage_state: dict
    load_error: str | None = None
    epoch: int = 0
    image_state: dict = field(
        default_factory=lambda: {"path": None, "fresh": False}
    )


class ChatRegistry:
    """按账号保存聊天状态，并保证同一账号同时只有一轮生成。"""

    def __init__(self, image_store: TempImageStore | None = None) -> None:
        self._states: dict[str, ChatState] = {}
        self._busy: set[str] = set()
        self._lock = threading.Lock()
        self._image_store = image_store
        # 生成中清空时暂存的待删文件路径（精确路径，不含清空后的新上传）；
        # 旧轮次仍可能读取文件，物理删除延迟到 release 释放引用之后
        self._deferred_image_deletes: dict[str, list[str]] = {}

    def _build_state(self, runtime: AppRuntime, username: str) -> ChatState:
        load_error = None
        try:
            history = runtime.history_store.load(username)
        except HistoryReadError as exc:
            history = []
            load_error = RESTORE_FAILED_MSG.format(detail=exc)
        return ChatState(
            history=history,
            usage_state=new_usage_state(),
            stage_state=dialogue_state.new_stage_state(),
            load_error=load_error,
        )

    def get_state(self, runtime: AppRuntime, username: str) -> ChatState:
        with self._lock:
            state = self._states.get(username)
            if state is not None:
                return state
            state = self._build_state(runtime, username)
            self._states[username] = state
            return state

    def begin_turn(self, runtime: AppRuntime, username: str):
        """占住账号，并在同一锁内原子捕获（epoch, 保存标记）；busy 返回 None。

        turn_token 必须在此处与 epoch 一起取得：清空（clear_account）持同一把
        锁，不可能落在两次捕获之间，旧轮次拿不到清空后的新保存资格。
        锁序固定为 registry → history 守卫，clear_account 相同，无死锁。
        """
        with self._lock:
            if username in self._busy:
                return None
            self._busy.add(username)
            state = self._states.get(username)
            if state is None:
                state = self._build_state(runtime, username)
                self._states[username] = state
            epoch = state.epoch
            turn_token = runtime.history_store.start_turn(username)
            return state, epoch, turn_token

    def clear_account(self, runtime: AppRuntime, username: str) -> bool:
        """一致顺序的清空：先磁盘（推进保存计数），后内存（递增 epoch）。

        全程持有 registry 锁，与 begin_turn 串行化。磁盘清空失败时不动
        内存状态（与既有行为一致：等待用户重试）。返回磁盘清空是否成功。
        清空同时重置图片状态并删除该账号全部临时图片（含未发送的上传残留）；
        生成中清空时文件物理删除延迟到 release（旧轮次可能仍需读取），
        状态本身立即失效（epoch 递增，旧轮次无法写回或重新挂图）。
        """
        with self._lock:
            try:
                runtime.history_store.clear(username)
            except Exception:
                return False
            runtime.history_compressor.reset(username)
            state = self._states.get(username)
            if state is not None:
                state.history = []
                state.usage_state = new_usage_state()
                state.stage_state = dialogue_state.new_stage_state()
                state.load_error = None
                state.epoch += 1
                state.image_state = {"path": None, "fresh": False}
            if self._image_store is not None:
                if username in self._busy:
                    self._deferred_image_deletes.setdefault(username, []).extend(
                        str(p) for p in self._image_store.take_all(username)
                    )
                else:
                    self._image_store.delete_all(username)
            return True

    def clear_state(self, username: str) -> None:
        """清空内存状态并递增清空计数；进行中的旧轮次完成时据此放弃写回。"""
        with self._lock:
            state = self._states.get(username)
            if state is not None:
                state.history = []
                state.usage_state = new_usage_state()
                state.stage_state = dialogue_state.new_stage_state()
                state.load_error = None
                state.epoch += 1
                state.image_state = {"path": None, "fresh": False}

    def mount_pending_image(
        self, username: str, state: ChatState, epoch: int, image_id: str
    ) -> bool:
        """发送前挂载本次上传的图片（fresh=True，由核心判定是否附图）。

        图片归属、索引有效性与挂载在同一同步边界（注册表锁）内核验：
        - epoch 未变：begin_turn 之后没有发生过清空；
        - image_id 仍在本账号索引内：生成中清空会移除索引（延迟删除文件），
          release 已释放 busy、旧文件尚未物理删除的窗口里，仅凭 epoch 与
          path.exists() 会把旧图重新挂上并随后被删除——索引校验在此拒绝，
          已从账号索引移除的路径一律不接受；
        - 文件存在：兜底索引与磁盘不一致的残况。
        替换旧图时旧文件不再被引用，在同一锁内删除。
        锁序固定为 registry → store（clear_account/release 同序，无死锁）。
        """
        with self._lock:
            if self._states.get(username) is not state or state.epoch != epoch:
                return False
            if self._image_store is None:
                return False
            path = self._image_store.resolve(username, image_id)
            if path is None or not path.exists():
                return False
            old_path = state.image_state.get("path")
            state.image_state = {"path": str(path), "fresh": True}
            if old_path and old_path != str(path):
                self._image_store.delete(username, old_path)
            return True

    def sync_image_state(
        self, username: str, state: ChatState, epoch: int, image_state: dict
    ) -> bool:
        """轮次结束后写回核心给出的图片状态；期间被清空（epoch 变化）则放弃。

        核心在新题判定 / 读图失败时把 path 置 None——被替换或被丢弃的旧
        临时文件在此删除。写回与删除在同一把锁内完成，不会误删新状态引用的文件。
        """
        with self._lock:
            if self._states.get(username) is not state or state.epoch != epoch:
                return False
            old_path = state.image_state.get("path")
            state.image_state = dict(image_state)
            new_path = state.image_state.get("path")
            if old_path and old_path != new_path and self._image_store is not None:
                self._image_store.delete(username, old_path)
            return True

    def clear_image(self, username: str) -> bool:
        """用户显式移除图片：状态变更与文件释放在同一锁边界内完成。

        持锁删除保证不会出现在"清状态后、删文件前"的窗口里被新一轮
        begin_turn + 挂图插进来、随后误删新图的竞态；生成中拒绝移除。
        """
        with self._lock:
            if username in self._busy:
                return False
            state = self._states.get(username)
            if state is not None:
                state.image_state = {"path": None, "fresh": False}
            if self._image_store is not None:
                self._image_store.delete_all(username)
        return True

    def finish_turn(
        self,
        username: str,
        state: ChatState,
        epoch: int,
        *,
        history: list[dict],
        usage_state: dict,
        stage_state: dict,
    ) -> bool:
        """旧轮次完成时写回状态；期间发生过清空（epoch 变化）则放弃。"""
        with self._lock:
            if self._states.get(username) is not state or state.epoch != epoch:
                return False
            state.history = history
            state.usage_state = usage_state
            state.stage_state = stage_state
            return True

    def try_acquire(self, username: str) -> bool:
        with self._lock:
            if username in self._busy:
                return False
            self._busy.add(username)
            return True

    def release(self, username: str) -> None:
        """释放账号 busy，并在同一锁边界内执行延迟的临时图片删除。

        删除放在锁内：busy 释放与旧文件删除对后续请求原子可见，
        不存在"busy 已释放、文件未删"的可观察窗口；索引在清空时已移除，
        即使 unlink 失败（如 Windows 下文件仍被占用）旧图也无法再被挂载，
        残留文件由启动时的过期清理兜底。
        """
        with self._lock:
            self._busy.discard(username)
            deferred = self._deferred_image_deletes.pop(username, None)
            if deferred and self._image_store is not None:
                # 只删清空时记录的精确路径：此后新轮次挂载的是新上传文件，
                # 路径不同，不会误删；账号目录校验仍在 store.delete 内执行
                for path in deferred:
                    self._image_store.delete(username, path)


# ===================== 请求依赖 =====================


def _same_origin(request: Request) -> None:
    """写请求同源校验：浏览器会带 Origin；缺失（脚本/测试）时放行。"""
    origin = request.headers.get("origin")
    if not origin:
        return
    host = request.headers.get("host", "")
    if urllib.parse.urlsplit(origin).netloc != host:
        raise HTTPException(status_code=403, detail="跨站请求被拒绝")


def _session_cookie(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE)


def _current_user(request: Request) -> str:
    sessions: SessionStore = request.app.state.sessions
    username = sessions.get(_session_cookie(request))
    if username is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return username


# ===================== 本机管理员判定 =====================

# 与 core.access.is_owner_host 一致的回环判定；Host/Origin 头的回环形态
# （含端口）另行解析
_FORWARDED_HEADERS = (
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-real-ip",
    "via",
)


def _host_header_is_loopback(value: str) -> bool:
    """解析 Host/Origin 的 netloc（剥掉端口与 IPv6 方括号）后判回环。"""
    host = (value or "").strip().lower()
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    return is_owner_host(host)


def _is_local_admin_request(request: Request) -> bool:
    """本机直连判定：回环来源 + 无转发头 + 回环 Host/Origin，全部满足才放行。

    - 远程直连：client.host 非回环，拒绝；
    - 本机反代/隧道转来的外部请求：通常带转发头或 Host 已改写为外部域名，
      转发头**存在**即拒绝（含空值头，不看其内容），伪造头也无法借此提权；
    - uvicorn 默认 proxy_headers 会信任来自回环的 X-Forwarded-For 并改写
      client.host：转发头一票否决同时覆盖了这一路径；
    - 无法区分"不设转发头且不改写 Host 的同机反代"，如实按局限对待。
    """
    client_host = (request.client.host if request.client else "") or ""
    if not is_owner_host(client_host):
        return False
    if any(name in request.headers for name in _FORWARDED_HEADERS):
        return False
    if not _host_header_is_loopback(request.headers.get("host", "")):
        return False
    origin = request.headers.get("origin")
    if origin and not _host_header_is_loopback(urllib.parse.urlsplit(origin).netloc):
        return False
    return True


def _require_local_admin(request: Request) -> str:
    """管理员接口依赖：先要求已登录（401），再要求本机直连（403）。"""
    username = _current_user(request)
    if not _is_local_admin_request(request):
        raise HTTPException(status_code=403, detail="管理员功能仅限本机登录用户使用")
    return username


class LoginBody(BaseModel):
    username: str
    password: str


class AdminResetPasswordBody(BaseModel):
    username: str
    new_password: str


class TurnSettings(BaseModel):
    """界面设置：全部可缺省，缺省时按当前注入 runtime 的默认值解析。"""

    mode: str = MODE_INTERACTIVE
    level: str | None = None
    thinking: bool | None = None
    provider_id: str | None = None
    model: str | None = None
    answer_style: str | None = None
    reasoning_effort: str | None = None


class ChatBody(TurnSettings):
    message: str
    # /api/images 返回的服务端临时图片 id；缺省表示纯文本发送
    image_id: str | None = None


class ContinueBody(TurnSettings):
    """继续生成：不带消息文本，指令由服务端按服务端历史构造。"""


# ===================== SSE 事件映射 =====================


def _sse(name: str, payload: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _reject_message(status: str, turn: dict, runtime: AppRuntime) -> str:
    """把核心拒绝状态映射为界面文案（与 Gradio respond 保持一致语义）。"""
    if status == "empty":
        return "💡 请先输入问题。"
    if status == "too_long":
        return (
            f"⚠️ 消息过长（最多 {runtime.max_message_len:,} 字符），请精简后重试。"
        )
    if status == "quota_read_failed":
        return QUOTA_CHECK_FAILED_MSG
    if status == "no_client":
        return f"⚠️ {runtime.startup_hint}" if runtime.startup_hint else "⚠️ 模型客户端不可用。"
    return turn.get("reject_message") or "⚠️ 请求被拒绝。"


def _resolve_turn_settings(runtime: AppRuntime, username: str, body: TurnSettings) -> dict:
    """把前端提交的界面设置解析为核心调用参数。

    配置只来自当前注入的 runtime，不重新读取默认配置。区分"字段未提供"
    与"显式非法值"：显式提交（含空字符串）的非法服务商、模型、学段、
    思考强度或解答风格一律 400，不静默换成其他模型或当成缺省；
    字段缺省时按默认值解析——服务商取默认项，客户端缺失时保留启动回退
    （由核心以 no_client 拒绝并展示启动提示）。
    """
    settings = runtime.settings

    if body.level is not None:
        if body.level not in LEVELS:
            raise HTTPException(status_code=400, detail="无效的学段")
        level = body.level
    else:
        account_level = next(
            (
                a.get("level")
                for a in runtime.accounts
                if a["username"] == username
            ),
            None,
        )
        level = account_level if account_level in LEVELS else "高中"

    if runtime.ui_providers:
        if body.provider_id is not None:
            provider_id = body.provider_id
            provider = next(
                (p for p in runtime.ui_providers if p["id"] == provider_id), None
            )
            if provider is None:
                raise HTTPException(
                    status_code=400, detail="不支持的服务商，请刷新页面后重试"
                )
            # 显式选择后只使用该服务商的客户端，不可用即拒绝，不回退其他客户端
            active_client = runtime.clients.get(provider_id)
            if active_client is None:
                raise HTTPException(
                    status_code=400, detail="所选服务商当前不可用，请重新选择"
                )
        else:
            provider_id = (
                runtime.default_provider["id"] if runtime.default_provider else None
            )
            provider = next(
                (p for p in runtime.ui_providers if p["id"] == provider_id), None
            )
            active_client = (
                runtime.clients.get(provider_id)
                if provider_id is not None
                else None
            )
            if active_client is None:
                # 缺省路径保留启动回退：无客户端时由核心 no_client 拒绝
                active_client = runtime.client
        if body.model is not None:
            model = body.model
            if provider is None or model not in (provider.get("models") or []):
                raise HTTPException(
                    status_code=400, detail="所选服务商不支持该模型，请重新选择"
                )
        else:
            model = (
                provider.get("default_model")
                if provider is not None
                else (
                    settings.model if settings is not None else "deepseek-v4-flash"
                )
            )
    else:
        # 没有任何可展示服务商（组装回退）：不接受显式服务商或模型
        if body.provider_id is not None:
            raise HTTPException(
                status_code=400, detail="不支持的服务商，请刷新页面后重试"
            )
        if body.model is not None:
            raise HTTPException(
                status_code=400, detail="所选服务商不支持该模型，请重新选择"
            )
        provider = None
        model = settings.model if settings is not None else "deepseek-v4-flash"
        active_client = runtime.client

    if body.reasoning_effort is not None:
        effort = body.reasoning_effort
        # 模型级校验只用于已核实的适配目标（accepted_efforts 非 None）：
        # 该模型不提供档位（如 Qwen3.7、MiMo）或取值非法时明确拒绝，不调用模型。
        # 非目标模型与 thinking_param=none 保持原有全局档位校验，行为不变。
        accepted = get_model_thinking_capability(provider, model)["accepted_efforts"]
        if accepted is None:
            if effort not in REASONING_EFFORTS:
                raise HTTPException(status_code=400, detail="无效的思考强度")
        elif not accepted:
            raise HTTPException(
                status_code=400, detail="该模型不提供思考强度设置"
            )
        elif effort not in accepted:
            raise HTTPException(status_code=400, detail="无效的思考强度")
    else:
        effort = settings.reasoning_effort if settings is not None else "high"

    if body.answer_style is not None:
        answer_style = body.answer_style
        if answer_style not in ANSWER_STYLES:
            raise HTTPException(status_code=400, detail="无效的解答风格")
    else:
        answer_style = DEFAULT_ANSWER_STYLE

    thinking = (
        body.thinking
        if body.thinking is not None
        else (settings.thinking if settings is not None else False)
    )
    return {
        "client": active_client,
        "model": model,
        "level": level,
        "thinking": thinking,
        "reasoning_effort": effort,
        "answer_style": answer_style,
    }


def _done_payload(
    runtime: AppRuntime, reply: dict, result: dict, turn: dict
) -> dict:
    """核心 final 事件 → done 载荷：只含可序列化的展示数据。"""
    daily_result = result["daily_usage"]
    save_status = result["save_status"]
    return {
        "answer": _normalize_final(reply["answer"]),
        "reasoning_text": _normalize_final(reply["reasoning_text"]),
        "truncated": reply["truncated"],
        "error": friendly_error(result["error"]) if result["error"] else "",
        "empty_reply": not reply["answer"] and result["error"] is None,
        "turn_total": result["turn_total"],
        "usage_note": format_usage(reply["usage"]),
        "cumulative_usage": format_cumulative(result["usage_state"]),
        "save_status": save_status,
        "save_note": {
            None: "",
            "saved": "",
            "failed": SAVE_FAILED_MSG,
            "stale": SAVE_STALE_MSG,
        }[save_status],
        # 结构化状态：界面据此选择图标/样式，不靠搜索中文提示字符串判断
        "daily_status": daily_result["status"] if daily_result else None,
        "daily_note": {
            "recorded": "",
            "record_failed": DAILY_FAILED_MSG,
            "stats_failed": DAILY_STATS_FAILED_MSG,
        }.get(daily_result["status"], "") if daily_result is not None else "",
        "daily_used": daily_result["daily_used"] if daily_result else None,
        "global_used": daily_result["global_used"] if daily_result else None,
        "daily_limit": runtime.daily_limit,
        "global_daily_limit": runtime.global_daily_limit,
        "image_status": turn.get("image_status", ""),
        # 本轮结束后核心是否仍保留图片状态（false 表示图片已被消费/按新题丢弃）
        "image_kept": bool((turn.get("image_state") or {}).get("path")),
    }


def _normalize_final(text: str) -> str:
    from core.math_format import normalize_math_text

    return normalize_math_text(text, auto_close=True)


def chat_event_stream(
    runtime: AppRuntime,
    registry: ChatRegistry,
    username: str,
    body: TurnSettings,
    ip: str,
    resolved: dict,
    *,
    force_continue: bool = False,
    image_id: str | None = None,
):
    """核心 run_chat_turn 事件 → SSE 文本流（同步生成器，Starlette 线程池执行）。

    生成期间若账号被清空（epoch 变化），完成时不写回内存状态，
    done 的会话累计改按清空后的状态显示；磁盘侧由核心保存标记判 stale。

    force_continue=True 时为「继续生成」：在 begin_turn 取得的服务端状态上
    用 build_continue_message 构造指令并向核心传 force_continue，与普通发送
    共享 busy、清空守卫、SSE、用量结算与保存；无可继续题目以 no_continue
    拒绝且不调用模型。

    图片接入：image_id 非空时（普通发送且前端已上传），在本轮开始后经
    mount_pending_image 于注册表锁内重新验证索引有效性再挂到账号图片
    状态（fresh=True），交给核心 route_image_for_turn 判定附图 / 旧图丢弃 /
    不支持视觉拒绝——不复制任何图片业务逻辑；轮次结束（含被拒、断连关闭
    生成器）后经 sync_image_state 写回并清理被替换或被丢弃的临时文件。
    """
    turn = registry.begin_turn(runtime, username)
    if turn is None:
        yield _sse(
            "rejected",
            {
                "status": "busy",
                "message": "⏳ 上一条回答还在生成中，请等它结束后再发送。",
            },
        )
        return
    state, start_epoch, turn_token = turn
    prepared: dict | None = None

    try:
        if image_id is not None and not registry.mount_pending_image(
            username, state, start_epoch, image_id
        ):
            # 端点的 resolve 只是快速预检；权威校验在此处的锁内完成：
            # 挂载前发生清空（epoch 变化）、image_id 已被清空移出索引
            # （含 release 已释放 busy、文件尚未物理删除的窗口）或文件
            # 已删除时拒绝本轮，不调用模型，也不把旧路径写回状态
            yield _sse(
                "rejected",
                {
                    "status": "image_invalid",
                    "message": "⚠️ 图片已失效（对话已清空或文件被删除），请重新选择图片后发送。",
                },
            )
            return

        if force_continue:
            message = chat_service.build_continue_message(state.history, IMAGE_NOTE)
            if message is None:
                yield _sse("rejected", {"status": "no_continue", "message": NO_CONTINUE_MSG})
                return
        else:
            message = body.message

        level = resolved["level"]

        prev_answer = ""
        prev_reasoning = ""
        for event in chat_service.run_chat_turn(
            message=message,
            history=state.history,
            usage_state=state.usage_state,
            username=username,
            ip=ip,
            date=datetime.date.today().isoformat(),
            level=level,
            mode=body.mode,
            stage_state=state.stage_state,
            image_state=state.image_state,
            thinking=resolved["thinking"],
            model=resolved["model"],
            reasoning_effort=resolved["reasoning_effort"],
            answer_style=resolved["answer_style"],
            active_client=resolved["client"],
            history_compressor=runtime.history_compressor,
            history_store=runtime.history_store,
            daily_usage_store=runtime.daily_usage_store,
            event_logger=runtime.event_logger,
            max_message_len=runtime.max_message_len,
            max_history_messages=(
                runtime.settings.max_history_messages
                if runtime.settings is not None
                else None
            ),
            daily_limit=runtime.daily_limit,
            global_daily_limit=runtime.global_daily_limit,
            image_only_prompt=IMAGE_ONLY_PROMPT,
            image_note=IMAGE_NOTE,
            turn_token=turn_token,
            force_continue=force_continue,
        ):
            event_type = event["type"]
            turn = event["turn"]
            prepared = turn

            if event_type == "rejected":
                yield _sse(
                    "rejected",
                    {
                        "status": turn["status"],
                        "message": _reject_message(turn["status"], turn, runtime),
                    },
                )
                return

            if event_type == "ready":
                # start 携带本轮消息原文，供前端展示继续生成的指令
                yield _sse("start", {"message": message})
                continue

            reply = event["reply"]
            answer = reply["answer"]
            reasoning = reply["reasoning_text"]
            delta_answer = (
                answer[len(prev_answer):] if answer.startswith(prev_answer) else answer
            )
            delta_reasoning = (
                reasoning[len(prev_reasoning):]
                if reasoning.startswith(prev_reasoning)
                else reasoning
            )
            prev_answer, prev_reasoning = answer, reasoning

            if event_type == "stream":
                if delta_answer or delta_reasoning:
                    yield _sse(
                        "delta",
                        {"answer": delta_answer, "reasoning": delta_reasoning},
                    )
                else:
                    # 核心心跳片段（无增量）：SSE 注释行保活
                    yield ": ping\n\n"
                continue

            # final：先补发剩余增量，再交出收尾载荷
            if delta_answer or delta_reasoning:
                yield _sse(
                    "delta",
                    {"answer": delta_answer, "reasoning": delta_reasoning},
                )
            result = event["result"]
            persisted = registry.finish_turn(
                username,
                state,
                start_epoch,
                history=result["history"],
                usage_state=result["usage_state"],
                stage_state=turn["stage_state"],
            )
            payload = _done_payload(runtime, reply, result, turn)
            if not persisted:
                # 期间被清空：会话累计改按清空后的状态显示，不回填旧上下文
                payload["cumulative_usage"] = format_cumulative(state.usage_state)
            yield _sse("done", payload)
            return
    finally:
        # 写回核心给出的图片状态（被拒、断连、异常路径同样适用）；
        # 期间被清空则 epoch 已变，写回自动放弃，临时文件由清空删除
        if prepared is not None:
            registry.sync_image_state(
                username, state, start_epoch, prepared["image_state"]
            )
        registry.release(username)


class ClosingStreamingResponse(StreamingResponse):
    """SSE 流式响应：请求生命周期结束时确定性关闭同步生成器。

    Starlette 在正常完成或客户端断连取消时都会退出内部任务组并返回 __call__；
    在此 close 同步生成器，触发核心生成器链的 finally（关上游流、释放 busy），
    不依赖垃圾回收。close 可能在工作线程执行，异常不外泄。
    """

    def __init__(self, sync_generator, **kwargs):
        super().__init__(iterate_in_threadpool(sync_generator), **kwargs)
        self._sync_generator = sync_generator

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                await anyio.to_thread.run_sync(self._sync_generator.close)
            except Exception:
                pass


# ===================== 应用工厂 =====================


def create_api_app(
    runtime: AppRuntime,
    *,
    sessions: SessionStore | None = None,
    registry: ChatRegistry | None = None,
    image_store: TempImageStore | None = None,
    frontend_dir: Path | None = "default",
) -> FastAPI:
    """组装 FastAPI 应用；会话与状态对象可注入，便于测试隔离。"""
    app = FastAPI(title="数学学习 AI 助手 API", docs_url=None, redoc_url=None)
    app.state.runtime = runtime
    app.state.sessions = sessions if sessions is not None else SessionStore()
    if image_store is None:
        image_store = TempImageStore(Path(runtime.data_dir) / "temp_images")
    app.state.image_store = image_store
    app.state.registry = registry if registry is not None else ChatRegistry(image_store)
    try:
        image_store.cleanup_expired()
    except Exception:
        pass

    @app.post("/api/login", dependencies=[Depends(_same_origin)])
    def login(body: LoginBody, request: Request, response: Response):
        username = body.username.strip()
        ok = verify_login(runtime, username, body.password)
        if not ok:
            raise HTTPException(status_code=401, detail="用户名或密码不正确")
        token = app.state.sessions.create(username)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            samesite="lax",
            max_age=SESSION_TTL_SECONDS,
            path="/",
        )
        account = next(
            (a for a in runtime.accounts if a["username"] == username), None
        )
        return {
            "username": username,
            "display_name": account.get("display_name") if account else username,
            "level": account.get("level") if account else "高中",
            "local_admin": _is_local_admin_request(request),
        }

    @app.post("/api/logout", dependencies=[Depends(_same_origin)])
    def logout(request: Request, response: Response):
        app.state.sessions.drop(_session_cookie(request))
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/me")
    def me(request: Request, username: str = Depends(_current_user)):
        account = next(
            (a for a in runtime.accounts if a["username"] == username), None
        )
        return {
            "username": username,
            "display_name": account.get("display_name") if account else username,
            "level": account.get("level") if account else "高中",
            # 远程用户为 False：前端据此不展示、不请求管理员数据
            "local_admin": _is_local_admin_request(request),
        }

    @app.get("/api/history")
    def history(username: str = Depends(_current_user)):
        state = app.state.registry.get_state(runtime, username)
        return {"history": state.history, "notice": state.load_error or ""}

    @app.post("/api/clear", dependencies=[Depends(_same_origin)])
    def clear(username: str = Depends(_current_user)):
        # clear_account：同一锁内先磁盘后内存，旧轮次无法取混合状态
        ok = app.state.registry.clear_account(runtime, username)
        if not ok:
            return {"ok": False, "message": "⚠️ 清空未成功，请稍后重试。"}
        return {"ok": True, "message": "🗑️ 已开启新对话。"}

    @app.get("/api/settings")
    def read_settings(username: str = Depends(_current_user)):
        """界面设置清单：只含展示所需字段，全部来自当前注入的 runtime。

        不返回 base_url、api_key 环境变量、内部连接地址或客户端对象等
        非展示配置；默认模型与模型选项同源，前端无需二次推断。
        """
        rt = app.state.runtime
        account = next(
            (a for a in rt.accounts if a["username"] == username), None
        )
        level = account.get("level") if account else None
        # 每个模型的思考档位由共享的纯能力函数给出，前端不自行维护规则；
        # effort_levels 为空表示该模型不显示强度选择（如 Qwen3.7、MiMo）
        return {
            "providers": [
                {
                    "id": p["id"],
                    "name": p["name"],
                    "models": list(p.get("models") or []),
                    "default_model": p.get("default_model"),
                    "supports_thinking": get_provider_supports_thinking(p),
                    "model_thinking": {
                        m: {
                            "effort_levels": list(cap["effort_levels"]),
                            "default_effort": cap["default_effort"],
                        }
                        for m in (p.get("models") or [])
                        for cap in [get_model_thinking_capability(p, m)]
                    },
                }
                for p in rt.ui_providers
            ],
            "default_provider": (
                rt.default_provider["id"] if rt.default_provider else None
            ),
            "levels": list(LEVELS),
            "modes": list(MODES),
            "answer_styles": list(ANSWER_STYLES),
            "default_level": level if level in LEVELS else "高中",
            "default_mode": MODE_INTERACTIVE,
            "default_answer_style": DEFAULT_ANSWER_STYLE,
            "default_thinking": (
                rt.settings.thinking if rt.settings is not None else False
            ),
            "default_reasoning_effort": (
                rt.settings.reasoning_effort if rt.settings is not None else "high"
            ),
            "startup_hint": rt.startup_hint,
        }

    @app.post("/api/images", dependencies=[Depends(_same_origin)])
    async def upload_image(
        file: UploadFile = File(...),
        username: str = Depends(_current_user),
    ):
        """接收编辑确认后的待发送图片，返回服务端临时 image_id。

        - 大小按读取字节数校验（上限 MAX_IMAGE_BYTES），不信任 Content-Type；
        - 扩展名仅作为格式声明校验，真实解码与像素上限独立校验；
        - 文件名由服务端生成，按账号隔离存储，不对外暴露目录。
        """
        store: TempImageStore = app.state.image_store
        data = await file.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="图片超过 10MB，请压缩后再试")
        ext = Path(file.filename or "").suffix.lower()
        if ext not in SUPPORTED_EXTS:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的图片格式：{ext or '(无扩展名)'}，仅支持 jpg/png/webp/bmp/gif",
            )
        width, height = _validate_image_bytes(data)
        image_id, _path = store.save(username, data, ext)
        return {"image_id": image_id, "width": width, "height": height}

    @app.delete("/api/images", dependencies=[Depends(_same_origin)])
    def remove_image(username: str = Depends(_current_user)):
        """用户显式移除图片（含上一条消息附图，阻止后续轮次重新附带）。"""
        ok = app.state.registry.clear_image(username)
        if not ok:
            return {"ok": False, "message": "⚠️ 回答生成中，请结束后再移除图片。"}
        return {"ok": True}

    @app.post("/api/chat", dependencies=[Depends(_same_origin)])
    def chat(
        body: ChatBody,
        request: Request,
        username: str = Depends(_current_user),
    ):
        if body.mode not in MODES:
            raise HTTPException(status_code=400, detail="不支持的模式")
        image_id = None
        if body.image_id is not None:
            # 快速预检：未知或跨账号 id 直接 400，不启动 SSE 流；
            # 权威校验在挂载点（registry 锁内重验索引与 epoch）
            if app.state.image_store.resolve(username, body.image_id) is None:
                raise HTTPException(
                    status_code=400, detail="图片已失效，请重新选择或编辑后发送"
                )
            image_id = body.image_id
        resolved = _resolve_turn_settings(app.state.runtime, username, body)
        ip = (request.client.host if request.client else None) or "unknown"
        return ClosingStreamingResponse(
            chat_event_stream(
                app.state.runtime,
                app.state.registry,
                username,
                body,
                ip,
                resolved,
                image_id=image_id,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/continue", dependencies=[Depends(_same_origin)])
    def continue_turn(
        body: ContinueBody,
        request: Request,
        username: str = Depends(_current_user),
    ):
        """继续生成：服务端历史 + build_continue_message + force_continue=True。"""
        if body.mode not in MODES:
            raise HTTPException(status_code=400, detail="不支持的模式")
        resolved = _resolve_turn_settings(app.state.runtime, username, body)
        ip = (request.client.host if request.client else None) or "unknown"
        return ClosingStreamingResponse(
            chat_event_stream(
                app.state.runtime,
                app.state.registry,
                username,
                body,
                ip,
                resolved,
                force_continue=True,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # ===================== 管理员接口（仅本机直连的已登录用户） =====================

    @app.get("/api/admin/stats")
    def admin_stats(username: str = Depends(_require_local_admin)):
        """今日全局用量/登录/提问次数与各账号用量（结构化，不含密码）。

        读取失败返回 503 固定提示：明确报错，不返回伪装成正常的全零数据，
        响应与诊断日志都不含异常详情（可能带内部路径）。
        """
        rt = app.state.runtime
        today = datetime.date.today().isoformat()
        try:
            store = rt.daily_usage_store
            global_used = store.get_total(today)
            per_account = store.get_account_usage(today)
            logins = store.get_logins(today)
            questions = store.get_questions(today)
        except Exception as exc:
            # 诊断日志只记异常类型名：异常消息可能含内部路径、Key 等敏感信息
            try:
                rt.event_logger.log_access_event(
                    "用量统计读取失败", f"类型={type(exc).__name__}"
                )
            except Exception:
                pass
            raise HTTPException(
                status_code=503, detail="今日用量读取失败，请稍后重试"
            )
        return {
            "date": today,
            "global_used": global_used,
            "global_daily_limit": rt.global_daily_limit,
            "logins": logins,
            "questions": questions,
            "accounts": [
                {
                    "username": a["username"],
                    "display_name": a.get("display_name") or a["username"],
                    "enabled": bool(a.get("enabled", True)),
                    "used": int(per_account.get(a["username"], 0) or 0),
                }
                for a in rt.accounts
            ],
        }

    @app.post("/api/admin/reset-password", dependencies=[Depends(_same_origin)])
    def admin_reset_password(
        body: AdminResetPasswordBody,
        username: str = Depends(_require_local_admin),
    ):
        """本机管理员重置指定账号的密码（复用 core.accounts.reset_password）。

        成功后重载 runtime.accounts 保持清单一致，并记录审计日志；
        请求与响应、日志都不包含旧密码或新密码。
        """
        rt = app.state.runtime
        account_name = body.username.strip()
        if not account_name:
            raise HTTPException(status_code=400, detail="请选择要重置的账号")
        if not body.new_password.strip():
            raise HTTPException(status_code=400, detail="新密码不能为空")
        if not reset_password(account_name, body.new_password, path=rt.accounts_path):
            raise HTTPException(status_code=404, detail="账号不存在，请刷新后重试")
        rt.accounts = load_accounts(rt.accounts_path)
        rt.event_logger.log_access_event("重置密码", f"账号={account_name}")
        return {"ok": True, "username": account_name}

    if frontend_dir == "default":
        frontend_dir = FRONTEND_DIST
    if frontend_dir is not None and Path(frontend_dir).is_dir():
        app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")

    return app


def enable_fake_clients(runtime: AppRuntime, *, piece_delay: float = 0.02) -> None:
    """--fake 开发链路：把设置解析可能选中的客户端全部替换为假实现。

    必须覆盖两类路径，否则真实客户端仍会被选中：
    - 显式服务商选择读取 runtime.clients（不能只补空缺项，要整体替换）；
    - 缺省回退读取 runtime.client。
    假客户端能力（supports_vision 等）与该服务商配置声明一致，
    不把所有服务商都伪装成视觉模型。
    """
    from dev_fake import build_fake_clients

    clients = build_fake_clients(runtime.ui_providers, piece_delay=piece_delay)
    runtime.clients = clients
    default_id = runtime.default_provider["id"] if runtime.default_provider else None
    runtime.client = clients.get(default_id) if default_id else None
    if runtime.client is None and clients:
        runtime.client = next(iter(clients.values()))


# ===================== 启动配套任务（只由启动入口使用） =====================


ADDRESS_FILE_NAME = "访问地址.txt"
BROWSER_READY_TIMEOUT_SECONDS = 20.0
CLEANUP_JOIN_TIMEOUT_SECONDS = 5.0

# IPv6 局域网地址"还没探测过"的标记（探测结果可能是 None，不能拿 None 当未探测）
_UNRESOLVED = object()


def _port_is_listening(host: str, port: int, timeout: float = 0.2) -> bool:
    """TCP 探测服务是否已开始接受连接。

    uvicorn 先完成应用启动再绑定监听端口，因此端口可连即表示已在服务。
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    probe = socket.socket(family, socket.SOCK_STREAM)
    probe.settimeout(timeout)
    try:
        return probe.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        try:
            probe.close()
        except OSError:
            pass


def _sigbreak_handler(signum: int, frame: object) -> None:
    """把控制台 Ctrl+Break 变成与 Ctrl+C 相同的中断。

    只抛异常：不拿锁、不结束子进程，真正的资源清理由启动入口的 finally
    与 LaunchSupport.close 执行。
    """
    raise KeyboardInterrupt("收到 Ctrl+Break，正在退出")


def install_sigbreak_guard() -> Callable[[], None]:
    """登记主线程的 SIGBREAK 保护，返回恢复原处理器的方式（Windows 控制台 Break 键）。

    Windows 上 SIGBREAK 的默认处置是直接结束进程，会绕过启动入口的 finally，
    把已创建的备份线程等后台任务留在原地。登记后 Break 走与 Ctrl+C 相同的中断路径；
    Uvicorn 收尾时会把本处理器当作"原处理器"恢复并重发它捕获到的信号，
    于是服务运行期间按 Break 也能进到 finally。

    非 Windows、系统没有 SIGBREAK、或不在主线程时返回空操作：
    导入本模块与应用工厂都不改动进程的信号处置。
    """
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is None:
        return lambda: None
    if threading.current_thread() is not threading.main_thread():
        print("⚠️ 不在主线程启动，未登记 Ctrl+Break 保护（只影响控制台 Break 键）")
        return lambda: None
    try:
        previous = signal.signal(sigbreak, _sigbreak_handler)
    except (ValueError, OSError) as exc:
        print(f"⚠️ 未能登记 Ctrl+Break 保护：{exc}")
        return lambda: None

    def restore() -> None:
        try:
            signal.signal(sigbreak, previous)
        except BaseException as exc:
            # 只提示：恢复失败不能顶掉正在传播的启动异常
            print(f"⚠️ 恢复 Ctrl+Break 原处理器失败：{exc}")

    return restore


class LaunchSupport:
    """新版 API 的启动配套：每日备份、访问地址文件与自动开浏览器。

    归属明确，close() 只回收本次实际创建的对象：
    - 备份线程与停止信号记在实例上，备份目录取 runtime.data_dir，不改全局常量；
    - 地址文件写在 data_dir 内，只含地址与更新时间，不含密码等敏感信息。
    """

    def __init__(
        self,
        runtime: AppRuntime,
        *,
        host: str,
        port: int,
        open_browser: bool = False,
        backup_interval_seconds: int = 3600,
    ) -> None:
        self.data_dir = Path(runtime.data_dir)
        self.host = host
        self.port = port
        self.open_browser = open_browser
        self.backup_interval_seconds = backup_interval_seconds
        # 浏览器不能用通配监听写法：按地址族归一到本机可达的回环地址，
        # 具体监听地址（如 127.0.0.2、::1）保持原地址；URL 里 IPv6 由 build_url 加方括号，
        # socket 探测与端口预检仍用不带方括号的原始主机地址
        self.reachable_host = browse_host(host)
        self.browse_url = build_url(self.reachable_host, port)
        self.address_file = self.data_dir / ADDRESS_FILE_NAME
        self._lan_ipv6: object = _UNRESOLVED
        self._backup_stop: threading.Event | None = None
        self._backup_thread: threading.Thread | None = None
        self._browser_stop: threading.Event | None = None
        self._browser_thread: threading.Thread | None = None
        self._closed = False

    # ---- 每日备份 ----

    @staticmethod
    def report_backup(result: BackupResult) -> None:
        """备份结局提示：失败必须明确说出，不能与"今天已备份过"一样沉默。"""
        if result.status == "created":
            print(f"数据备份：已生成 {result.path}")
        elif result.status == "failed":
            print(
                "⚠️ 数据备份失败（磁盘空间或权限问题），今日数据未打包；"
                "服务仍可正常使用，可稍后手动备份 data 目录。"
            )

    def start_backup(self) -> None:
        """先补一次今日备份并按保留份数收敛旧备份，再起跨天检查线程（目录来自 runtime.data_dir）。"""
        result = run_backup(data_dir=self.data_dir)
        self.report_backup(result)
        # 备份写盘失败时旧备份是仅存的完整数据，不参与本次清理；
        # 成功或当天已存在都按保留份数收敛，每天启动几次也不积累
        if result.status != BACKUP_FAILED:
            prune_backups(data_dir=self.data_dir)
        stop_event = threading.Event()
        thread = threading.Thread(
            target=daily_backup_loop,
            kwargs={
                "interval_seconds": self.backup_interval_seconds,
                "stop_event": stop_event,
                "data_dir": self.data_dir,
                "on_result": self.report_backup,
            },
            daemon=True,
            name="maa-api-backup",
        )
        # 先登记再启动：启动过程中收到退出信号时 close() 也能收尾
        self._backup_stop = stop_event
        self._backup_thread = thread
        thread.start()

    # ---- 监听端口预检 ----

    def ensure_port_free(self) -> None:
        """绑定前确认端口可用：占用时立即失败，不创建浏览器任务。

        uvicorn 自身绑定失败同样会走到 close() 收尾，这里只是把最常见的
        "端口已被占用"提前说清楚，避免白等一次注定失败的启动。
        """
        family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
        probe = socket.socket(family, socket.SOCK_STREAM)
        try:
            probe.bind((self.host, self.port))
        except OSError as exc:
            raise OSError(
                f"端口 {self.host}:{self.port} 无法监听（{exc}），"
                "请改用其他端口，或先关闭正在占用该端口的程序。"
            ) from exc
        finally:
            try:
                probe.close()
            except OSError:
                pass

    # ---- 访问地址信息 ----

    def _lan_ipv6_for_display(self) -> str | None:
        """IPv6 通配监听时与手机同族的局域网地址：只在需要时探测一次。

        返回空串表示"已确认没有可用地址"，控制台与地址文件共用同一份结果，
        不会因为二次探测给出不同地址；非 IPv6 通配监听不探测（用不上）。
        """
        if not is_ipv6_wildcard_bind(self.host):
            return None
        if self._lan_ipv6 is _UNRESOLVED:
            self._lan_ipv6 = get_lan_ipv6() or ""
        return self._lan_ipv6  # type: ignore[return-value]

    def address_info(self) -> dict[str, str]:
        """本次启动的实际访问地址（与控制台输出、地址文件同一口径）。"""
        return address_entries(
            self.host,
            self.port,
            lan_ipv6=self._lan_ipv6_for_display(),
        )

    def write_address_info(self) -> Path:
        """写地址文件（不含账号密码）。"""
        return save_address_info(
            self.host,
            self.port,
            target=self.address_file,
            lan_ipv6=self._lan_ipv6_for_display(),
        )

    # ---- 自动打开浏览器 ----

    def open_browser_when_ready(self) -> None:
        """等服务开始接受连接后打开本机地址；不可用则超时提示，不阻塞启动。"""
        if not self.open_browser:
            return
        stop_event = threading.Event()
        thread = threading.Thread(
            target=self._wait_and_open_browser,
            args=(stop_event,),
            daemon=True,
            name="maa-api-browser",
        )
        self._browser_stop = stop_event
        self._browser_thread = thread
        thread.start()

    def _wait_and_open_browser(self, stop_event: threading.Event) -> None:
        deadline = time.monotonic() + BROWSER_READY_TIMEOUT_SECONDS
        while True:
            if stop_event.is_set():
                return  # 服务已在收尾，不再打开浏览器
            if _port_is_listening(self.reachable_host, self.port):
                try:
                    webbrowser.open(self.browse_url)
                except Exception as exc:
                    print(f"⚠️ 自动打开浏览器失败：{exc}，请手动访问 {self.browse_url}")
                return
            if time.monotonic() >= deadline:
                print(f"⚠️ 服务在 {BROWSER_READY_TIMEOUT_SECONDS:.0f} 秒内未就绪，"
                      f"未自动打开浏览器，请手动访问 {self.browse_url}")
                return
            stop_event.wait(0.2)

    # ---- 收尾 ----

    def close(self) -> None:
        """回收本次创建的配套资源：先置停止信号，再等待线程收尾。

        每一步失败都只提示、不抛出：清理错误不能掩盖原始启动错误，失败退出码要保留。
        """
        if self._closed:
            return
        self._closed = True
        if self._backup_stop is not None:
            self._backup_stop.set()
        if self._browser_stop is not None:
            self._browser_stop.set()
        try:
            if self._browser_thread is not None:
                self._browser_thread.join(timeout=CLEANUP_JOIN_TIMEOUT_SECONDS)
        except Exception as exc:
            print(f"⚠️ 浏览器等待任务收尾失败：{exc}")
        try:
            if self._backup_thread is not None:
                self._backup_thread.join(timeout=CLEANUP_JOIN_TIMEOUT_SECONDS)
        except Exception as exc:
            print(f"⚠️ 备份线程收尾失败（正在写盘的备份可能未完成）：{exc}")


def main() -> None:
    """新版 API 启动入口：组装依赖 → 配套任务 → 单进程服务 → 退出清理。

    启动异常（Ctrl+C、端口占用、写盘失败）原样向外传播以保留失败退出码，
    finally 只回收 LaunchSupport 本次实际创建的配套资源。
    """
    parser = argparse.ArgumentParser(description="数学学习 AI 助手（FastAPI 版）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--fake",
        action="store_true",
        help="开发联调：用内置假模型客户端替换全部可选中服务商客户端（不调用任何真实 API）",
    )
    parser.add_argument(
        "--isolated",
        action="store_true",
        help="自动验证用：配置/账号/数据全部落在临时目录，不读写真实 data 与 .env",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="启动后不自动打开浏览器（等同环境变量 AUTO_OPEN_BROWSER=false）",
    )
    args = parser.parse_args()

    if args.isolated:
        import tempfile

        temp_root = Path(tempfile.mkdtemp(prefix="maa-api-isolated-"))
        runtime = build_runtime(
            env_file=temp_root / "missing.env",
            providers_file=temp_root / "missing.providers.json",
            accounts_path=temp_root / "accounts.json",
            data_dir=temp_root / "data",
        )
        print(f"[isolated] 临时数据目录：{temp_root}")
    else:
        runtime = build_runtime()

    if args.fake:
        # 整体替换所有可选中客户端：既有真实客户端不得残留（含已配置密钥的服务商）
        enable_fake_clients(runtime)
        print("[dev] 已启用假模型客户端（不产生真实调用）")

    # 只允许输出隔离测试账号的凭据；--fake 不代表账号是假的，严禁打印真实密码
    if args.isolated:
        for account in runtime.accounts:
            print(f"[隔离账号] {account['username']} / {account['password']}")

    # 自动开浏览器沿用旧界面的 AUTO_OPEN_BROWSER 开关；
    # 隔离验证默认不打开浏览器
    auto_open_browser = (
        not args.no_browser
        and not args.isolated
        and os.getenv("AUTO_OPEN_BROWSER", "true").strip().lower() != "false"
    )
    support = LaunchSupport(
        runtime,
        host=args.host,
        port=args.port,
        open_browser=auto_open_browser,
    )
    # Windows 控制台 Break 默认直接结束进程，会绕过下面的 finally：先在主线程登记保护
    restore_sigbreak = install_sigbreak_guard()
    print("数学学习 AI 助手（新版 API）启动中…")
    try:
        support.start_backup()
        support.ensure_port_free()
        support.write_address_info()
        for label, value in support.address_info().items():
            print(f"{label}：{value}")
        print(f"访问地址信息已保存到：{support.address_file}")
        support.open_browser_when_ready()
        import uvicorn

        # 单进程运行：不启用 reload 与多 worker
        uvicorn.run(create_api_app(runtime), host=args.host, port=args.port)
    finally:
        try:
            support.close()
        finally:
            restore_sigbreak()


if __name__ == "__main__":
    main()
