"""MoviePilot bridge: filesystem events, public status and acknowledged commands."""

import hashlib
import json
import math
import os
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional

from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType


@dataclass
class JobView:
    id: str = ""
    relative_path: str = ""
    status: str = ""
    files_total: int = 0
    files_done: int = 0
    bytes_total: int = 0
    bytes_done: int = 0
    last_error: str = ""
    updated_at: str = ""
    next_attempt: str = ""


@dataclass
class UploadView:
    job_id: str = ""
    relative_path: str = ""
    file: str = ""
    account: str = ""
    drive_id: str = ""
    phase: str = ""
    size: int = 0
    bytes: int = 0
    speed: float = 0
    eta_seconds: int = 0
    updated_at: str = ""


@dataclass
class AccountView:
    name: str = ""
    oauth_client: str = ""
    status: str = ""
    capacity_bytes: int = 0
    budget_bytes: int = 0
    reserved_bytes: int = 0
    remaining_bytes: int = 0
    cooldown_until: str = ""
    last_health_check: str = ""
    last_error: str = ""
    active_uploads: int = 0


@dataclass
class EventView:
    at: str = ""
    status: str = ""
    message: str = ""


@dataclass
class CommandRequest:
    action: str
    job_id: str = ""
    relative_path: str = ""


@dataclass
class CommandView:
    id: str = ""
    action: str = ""
    status: str = "error"
    message: str = ""
    created_at: str = ""


@dataclass
class StatusView:
    version: int = 2
    enabled: bool = False
    online: bool = False
    stale: bool = True
    message: str = ""
    updated_at: str = ""
    running: bool = False
    paused: bool = False
    upload_workers: int = 0
    max_uploads_per_account: int = 0
    speed_bytes_per_second: float = 0
    job_counts: Dict[str, int] = field(default_factory=dict)
    jobs: List[JobView] = field(default_factory=list)
    uploads: List[UploadView] = field(default_factory=list)
    accounts: List[AccountView] = field(default_factory=list)
    recent_events: List[EventView] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    commands: List[CommandView] = field(default_factory=list)


_ACTIONS = {"pause", "resume", "retry", "check_accounts", "enqueue"}
_HEX32 = re.compile(r"[a-f0-9]{32}\Z")
_HEX64 = re.compile(r"[a-f0-9]{64}\Z")


def _text(value: Any, limit: int = 1000) -> str:
    value = value if isinstance(value, str) else ""
    # Public daemon data should already be redacted. Keep accidental credentials
    # and template expressions out of MP's generic component renderer as well.
    value = re.sub(r'(?i)((?:access_token|refresh_token|client_secret|authorization)["\s:=]+)[^\s,}\"]+',
                   r'\1[redacted]', value)
    value = re.sub(r'(?i)Bearer\s+[^\s]+', 'Bearer [redacted]', value)
    return value.replace("{{", "｛｛").replace("}}", "｝｝")[:limit]


def _number(value: Any, integer: bool = False):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return 0
    if value < 0 or value > 2 ** 63 - 1 or not math.isfinite(value):
        return 0
    return int(value) if integer else float(value)


def _model(cls, value):
    if not isinstance(value, dict):
        return None
    result = {}
    for item in fields(cls):
        raw = value.get(item.name)
        result[item.name] = _text(raw) if item.type is str else _number(raw, item.type is int)
    return cls(**result)


def _time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, OverflowError):
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get(value: Any, name: str):
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


class GDriveArchiverBridge(_PluginBase):
    plugin_name = "Google Drive 归档桥接"
    plugin_desc = "整理事件入队、归档状态仪表盘与宿主机控制。"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "2.0.0"
    plugin_author = "irisrclone"
    author_url = ""
    plugin_config_prefix = "gdrivearchiverbridge_"
    plugin_order = 90
    auth_level = 1

    def __init__(self):
        super().__init__()
        self._lock = threading.RLock()
        self._enabled = False
        self._notify_results = False
        self._library_root = "/media"
        self._bridge_dir = "/bridge"
        self._outbox_poll_seconds = 3
        self._receipts = []
        self._commands = []
        self._control_error = ""

    def init_plugin(self, config: dict = None):
        self.stop_service()
        config = dict(config) if isinstance(config, dict) else {}
        with self._lock:
            self._library_root = str(config.get("library_root") or "/media")
            self._bridge_dir = str(config.get("bridge_dir") or "/bridge")
            self._enabled = config.get("enabled") is True
            self._notify_results = config.get("notify_results") is True
            try:
                self._outbox_poll_seconds = max(2, min(5, int(config.get("outbox_poll_seconds") or 3)))
            except (ValueError, TypeError):
                self._outbox_poll_seconds = 3
            receipts = self.get_data("notice_receipts")
            self._receipts = [x for x in receipts if isinstance(x, str)][-256:] if isinstance(receipts, list) else []
            history = self.get_data("commands")
            self._commands = [x for x in history if isinstance(x, dict) and _HEX32.fullmatch(str(x.get("id", "")))][-20:] if isinstance(history, list) else []
            if config.get("onlyonce") is True:
                # Reset BEFORE submitting, so an MP reload cannot replay a control.
                config["onlyonce"] = False
                saved = self.update_config(config)
                if saved is False:
                    self._control_error = "未能清除执行一次开关，未提交操作。"
                else:
                    reply = self.submit_command(CommandRequest(
                        action=str(config.get("control_action") or ""),
                        job_id=str(config.get("control_job_id") or "").strip(),
                        relative_path=str(config.get("control_relative_path") or "").strip()))
                    self._control_error = reply.message if reply.status == "error" else ""

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command():
        return []

    def get_api(self):
        return [
            {"path": "/status", "endpoint": self.get_status, "methods": ["GET"], "auth": "bear",
             "summary": "读取归档器公开状态", "response_model": StatusView},
            {"path": "/command", "endpoint": self.submit_command, "methods": ["POST"], "auth": "bear",
             "summary": "提交归档操作（等待宿主机确认）", "response_model": CommandView},
            {"path": "/result/{command_id}", "endpoint": self.get_result, "methods": ["GET"], "auth": "bear",
             "summary": "读取归档操作结果", "response_model": CommandView},
        ]

    def get_service(self):
        if not self._enabled or not self._notify_results:
            return []
        return [{"id": self.__class__.__name__ + ".Outbox", "name": "Google Drive 归档结果通知",
                 "trigger": "interval", "func": self._poll_outbox,
                 "kwargs": {"seconds": self._outbox_poll_seconds, "max_instances": 1, "coalesce": True}}]

    def stop_service(self):
        # The MP scheduler owns get_service jobs. No private thread/scheduler is
        # created; an already-dispatched callback rechecks this flag under lock.
        with self._lock:
            self._enabled = False

    @staticmethod
    def _field(component, model, label, **extra):
        return {"component": component, "props": {"model": model, "label": label, **extra}}

    def get_form(self):
        fields_json = [
            self._field("VSwitch", "enabled", "启用插件"),
            self._field("VTextField", "library_root", "MoviePilot 媒体根目录"),
            self._field("VTextField", "bridge_dir", "共享 bridge 目录"),
            self._field("VSwitch", "notify_results", "通过 MoviePilot 发送结果通知"),
            self._field("VTextField", "outbox_poll_seconds", "通知轮询间隔（2–5 秒）", type="number"),
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text":
                "仪表盘每 3 秒刷新。下面选择操作并勾选执行一次后保存；提交不代表执行成功，请在仪表盘查看回执。暂停会等待当前文件完成。"}},
            self._field("VSelect", "control_action", "操作", items=[
                {"title": "暂停新上传", "value": "pause"}, {"title": "继续上传", "value": "resume"},
                {"title": "重试任务", "value": "retry"}, {"title": "重新检查账号", "value": "check_accounts"},
                {"title": "手动添加媒体目录", "value": "enqueue"}]),
            self._field("VTextField", "control_job_id", "重试任务 ID（留空重试全部异常任务）"),
            self._field("VTextField", "control_relative_path", "手动入队相对目录，例如 欧美剧集/剧名"),
            self._field("VSwitch", "onlyonce", "执行一次所选操作（保存后自动关闭）"),
        ]
        return [{"component": "VForm", "content": fields_json}], {
            "enabled": False, "library_root": "/media", "bridge_dir": "/bridge", "notify_results": False,
            "outbox_poll_seconds": 3, "control_action": "pause", "control_job_id": "",
            "control_relative_path": "", "onlyonce": False}

    def _bridge_path(self, *parts):
        root = Path(self._bridge_dir).resolve()
        result = root.joinpath(*parts)
        if root not in result.resolve().parents:
            raise ValueError("bridge 路径越界")
        return result

    @staticmethod
    def _read_json(path, limit=65536):
        if path.is_symlink():
            raise ValueError("不读取符号链接")
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("JSON 超过大小限制")
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError("需要 JSON 对象")
        return result

    @staticmethod
    def _atomic_write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def get_status(self) -> StatusView:
        view = StatusView(enabled=self._enabled)
        try:
            raw = self._read_json(self._bridge_path("status.json"), 2 * 1024 * 1024)
            if type(raw.get("version")) is not int or raw["version"] != 2:
                raise ValueError("需要支持状态协议 2 的宿主机归档器")
            view.updated_at = _text(raw.get("updated_at"))
            stamp = _time(view.updated_at)
            age = (datetime.now(timezone.utc) - stamp).total_seconds() if stamp else -1
            view.running = raw.get("running") is True
            view.stale = not (0 <= age <= 15)
            view.online = view.running and not view.stale
            view.paused = raw.get("paused") is True
            view.upload_workers = _number(raw.get("upload_workers"), True)
            view.max_uploads_per_account = _number(raw.get("max_uploads_per_account"), True)
            view.speed_bytes_per_second = _number(raw.get("speed_bytes_per_second")) if view.online else 0
            for name, cls, limit in [("jobs", JobView, 1000), ("uploads", UploadView, 100),
                                     ("accounts", AccountView, 500), ("recent_events", EventView, 50)]:
                rows = raw.get(name)
                setattr(view, name, [item for row in rows[:limit] if (item := _model(cls, row)) is not None]
                        if isinstance(rows, list) else [])
            for upload in view.uploads:
                stamp = _time(upload.updated_at)
                age = (datetime.now(timezone.utc) - stamp).total_seconds() if stamp else -1
                if not view.online or not (0 <= age <= 15):
                    upload.speed = 0
                    upload.eta_seconds = 0
            warnings = raw.get("warnings")
            view.warnings = [_text(x) for x in warnings[:20] if isinstance(x, str)] if isinstance(warnings, list) else []
            counts = raw.get("job_counts")
            if isinstance(counts, dict):
                for key, count in list(counts.items())[:32]:
                    if isinstance(key, str) and re.fullmatch(r"[a-z_]{1,32}", key) and type(count) is int:
                        view.job_counts[key] = _number(count, True)
            else:
                # Older protocol-2 daemons have no aggregate counts.
                for job in view.jobs:
                    key = job.status if re.fullmatch(r"[a-z_]{1,32}", job.status) else "unknown"
                    view.job_counts[key] = view.job_counts.get(key, 0) + 1
            total_jobs = sum(view.job_counts.values())
            if total_jobs > len(view.jobs):
                view.warnings.append(f"公开状态仅列出 {len(view.jobs)}/{total_jobs} 个任务；其余任务仍保留在宿主机队列中。")
            view.message = "暂停中（当前文件完成后停止派发）" if view.paused else "归档器在线"
            if not view.online:
                view.message = "归档器离线或状态已超过 15 秒；速度已清零"
        except (OSError, ValueError, RecursionError):
            view.message = "未取得有效状态：归档器离线、尚未升级或 bridge 配置不一致。事件入队仍可使用。"
        with self._lock:
            view.commands = [self.get_result(item["id"]) for item in reversed(self._commands[-20:])]
            if self._control_error:
                view.warnings.append(self._control_error)
        return view

    def _relative_directory(self, value):
        if not isinstance(value, str) or len(value) > 2048 or "\\" in value or ":" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("请输入媒体根目录下的相对目录")
        relative = PurePosixPath(value)
        if relative.is_absolute() or len(relative.parts) < 2 or any(x in (".", "..") for x in value.split("/")):
            raise ValueError("路径必须包含类别和媒体目录，不能使用绝对路径或 ..")
        root = Path(self._library_root).resolve()
        target = root.joinpath(*relative.parts).resolve()
        if root not in target.parents or not target.is_dir():
            raise ValueError("媒体目录不存在或不在媒体根目录内")
        return relative.as_posix()

    def submit_command(self, payload: CommandRequest) -> CommandView:
        if not isinstance(payload, CommandRequest):
            return CommandView(message="无效命令格式")
        with self._lock:
            action = payload.action
            if not self._enabled or action not in _ACTIONS:
                return CommandView(message="插件未启用或操作不受支持")
            if payload.job_id and (action != "retry" or not _HEX64.fullmatch(payload.job_id)):
                return CommandView(message="重试任务 ID 必须为 64 位小写十六进制")
            if payload.relative_path and action != "enqueue":
                return CommandView(message="只有手动入队可以指定目录")
            try:
                # Old/offline daemons cannot acknowledge controls; don't claim acceptance.
                raw = self._read_json(self._bridge_path("status.json"), 2 * 1024 * 1024)
                stamp = _time(raw.get("updated_at"))
                age = (datetime.now(timezone.utc) - stamp).total_seconds() if stamp else -1
                if raw.get("version") != 2 or raw.get("running") is not True or not (0 <= age <= 15):
                    raise ValueError("归档器离线或不支持控制，请检查状态")
                relative = self._relative_directory(payload.relative_path) if action == "enqueue" else ""
                command = {"version": 2, "id": uuid.uuid4().hex, "action": action, "created_at": _now()}
                if payload.job_id:
                    command["job_id"] = payload.job_id
                if relative:
                    command["relative_path"] = relative
                self._atomic_write(self._bridge_path("commands", command["id"] + ".json"), command)
                reply = CommandView(id=command["id"], action=action, status="pending",
                                    message="已提交，等待宿主机确认", created_at=command["created_at"])
                self._commands = (self._commands + [asdict(reply)])[-20:]
                self.save_data("commands", self._commands)
                return reply
            except (OSError, ValueError, RecursionError) as err:
                return CommandView(action=action, message=_text(str(err)))

    def get_result(self, command_id: str) -> CommandView:
        if not isinstance(command_id, str) or not _HEX32.fullmatch(command_id):
            return CommandView(message="无效命令 ID")
        saved = next((x for x in self._commands if x["id"] == command_id), {})
        try:
            raw = self._read_json(self._bridge_path("results", command_id + ".json"))
            if raw.get("version") != 2 or raw.get("id") != command_id or raw.get("action") not in _ACTIONS or raw.get("status") not in {"done", "error"}:
                raise ValueError("无效命令回执")
            return _model(CommandView, raw)
        except FileNotFoundError:
            return CommandView(id=command_id, action=_text(saved.get("action")), status="pending",
                               message="等待宿主机回执（尚未确认执行）", created_at=_text(saved.get("created_at")))
        except (OSError, ValueError, RecursionError):
            return CommandView(id=command_id, message="无法读取有效命令回执")

    def get_dashboard_meta(self):
        return [{"key": "archive", "name": "Google Drive 归档"}]

    def get_dashboard(self, key: str = "archive", **kwargs):
        return {"cols": 12}, {"title": "Google Drive 归档", "refresh": 3}, self.get_page()

    @staticmethod
    def _size(value):
        value = _number(value)
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if value < 1024 or unit == "TiB":
                return f"{value:.1f} {unit}"
            value /= 1024

    @staticmethod
    def _list(title, rows):
        return {"component": "VCard", "props": {"variant": "outlined", "class": "my-3"}, "content": [
            {"component": "VCardTitle", "text": title},
            {"component": "VList", "props": {"density": "compact"}, "content": [
                {"component": "VListItem", "props": {"title": _text(name), "subtitle": _text(detail, 2000)}}
                for name, detail in (rows or [("暂无记录", "")])]}]}

    def get_page(self):
        state = self.get_status()
        size = self._size
        page = [{"component": "VAlert", "props": {"type": "success" if state.online else "warning", "variant": "tonal",
                 "text": f"{state.message} · {size(state.speed_bytes_per_second)}/s · {len(state.uploads)} 个传输 · 更新 {state.updated_at or '无'}"}},
                {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "class": "my-2", "text":
                 "控制入口：插件设置 → 选择操作 → 执行一次 → 保存。下方回执 done 才表示宿主机确认。额度是本机滚动 24 小时预留估算，不是 Google 精确剩余额度。"}}]
        labels = {"queued": "排队", "waiting_stable": "等待稳定", "uploading": "上传", "retrying": "重试",
                  "waiting_account": "等待账号", "failed": "失败", "complete": "完成"}
        summary = " · ".join(f"{labels.get(status, status)} {count}" for status, count in state.job_counts.items())
        page.append({"component": "VAlert", "props": {"type": "info", "variant": "tonal", "class": "my-2",
                     "text": f"任务总计 {sum(state.job_counts.values())} · {summary or '暂无任务'}"}})
        if len(state.jobs) > 50:
            page.append({"component": "VAlert", "props": {"type": "info", "variant": "tonal",
                         "text": f"本页仅展示公开快照中的前 50/{len(state.jobs)} 项；任务总数见上方汇总。"}})
        page.append(self._list("正在传输", [(u.file or u.relative_path,
            f"{u.phase} · {u.account} · {size(u.bytes)}/{size(u.size)} · {size(u.speed)}/s · ETA {u.eta_seconds}s") for u in state.uploads]))
        page.append(self._list("任务（最多显示 50 项）", [(j.relative_path,
            f"{j.status} · {j.files_done}/{j.files_total} 文件 · {size(j.bytes_done)}/{size(j.bytes_total)} · ID {j.id} · {j.last_error}") for j in state.jobs[:50]]))
        account_rows = []
        for account in state.accounts:
            stamp = _time(account.cooldown_until)
            cooldown = max(0, int((stamp - datetime.now(timezone.utc)).total_seconds())) if stamp else 0
            account_rows.append((account.name, f"{account.status} · client {account.oauth_client} · 容量 {size(account.capacity_bytes)} · "
                f"本地24h预留 {size(account.reserved_bytes)} / 预算 {size(account.budget_bytes)} · 剩余 {size(account.remaining_bytes)} · "
                f"并发 {account.active_uploads} · 冷却 {cooldown}s · {account.last_error}"))
        page.append(self._list("账号", account_rows))
        page.append(self._list("操作回执", [(c.action + " · " + c.status, c.message + " · " + c.id) for c in state.commands]))
        page.append(self._list("最近事件", [(e.at + " · " + e.status, e.message) for e in reversed(state.recent_events)]))
        if state.warnings:
            page.append(self._list("提示", [("提示", message) for message in state.warnings]))
        return page

    @eventmanager.register(EventType.TransferComplete)
    def enqueue_transfer(self, event: Event):
        data = _get(event, "event_data")
        info = _get(data, "transferinfo")
        self._enqueue_directory(_get(info, "target_diritem"), "transfer_complete", _get(_get(info, "target_item"), "path"))

    @eventmanager.register(EventType.MetadataScrape)
    def enqueue_metadata_scrape(self, event: Event):
        self._enqueue_directory(_get(_get(event, "event_data"), "fileitem"), "metadata_scrape")

    def _enqueue_directory(self, item, trigger, completed_path=None):
        with self._lock:
            if not self._enabled:
                return
            target_path = _get(item, "path")
            if not isinstance(target_path, (str, Path)):
                return
            # Non-local storage events must never be interpreted as host paths.
            storage = _get(item, "storage")
            if storage and str(getattr(storage, "value", storage)).lower() not in {"local", "本地"}:
                return
            try:
                root = Path(self._library_root).resolve()
                target = Path(target_path).resolve()
                relative = self._relative_directory(target.relative_to(root).as_posix())
                completed_item = ""
                if completed_path:
                    completed_item = Path(completed_path).resolve().relative_to(target).as_posix()
                    if completed_item == ".":
                        completed_item = ""
                job_id = hashlib.sha256(relative.encode("utf-8")).hexdigest()
                queue_id = hashlib.sha256(f"{job_id}\x00{trigger}\x00{completed_item}".encode("utf-8")).hexdigest()
                job = {"version": 1, "id": job_id, "source_path": str(target), "relative_path": relative,
                       "trigger": trigger, "completed_item": completed_item, "created_at": _now()}
                self._atomic_write(self._bridge_path("inbox", queue_id + ".json"), job)
            except (OSError, ValueError):
                logger.warning("Google Drive 归档事件路径无效或无法写入 bridge；请检查媒体根目录和目录权限。")

    def _poll_outbox(self):
        if not self._lock.acquire(blocking=False):
            return
        try:
            if not self._enabled or not self._notify_results:
                return
            outbox = self._bridge_path("outbox")
            if not outbox.is_dir():
                return
            # Recover claims left by a stopped MP, including the v1 UUID format.
            for claimed in sorted(outbox.glob(".*.processing"))[:100]:
                self._notify_claim(claimed)
            for result_path in sorted(outbox.glob("*.json"))[:100]:
                if not self._enabled:
                    break
                claimed = result_path.with_name("." + result_path.name + ".processing")
                if claimed.exists():
                    continue
                try:
                    os.rename(result_path, claimed)
                except FileNotFoundError:
                    continue
                self._notify_claim(claimed)
        except (OSError, ValueError):
            logger.warning("Google Drive 归档通知队列暂时无法读取。")
        finally:
            self._lock.release()

    def _notify_claim(self, claimed):
        if not self._enabled:
            return
        try:
            result = self._read_json(claimed)
            if result.get("version") != 1 or not isinstance(result.get("status"), str):
                raise ValueError("无效通知格式")
            receipt = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
            if receipt not in self._receipts:
                title = _text(result.get("title")) or "Google Drive 归档：" + _text(result["status"])
                message = "\n".join(x for x in (_text(result.get("relative_path")), _text(result.get("message"), 3000)) if x)
                self.post_message(mtype=NotificationType.Plugin, title=title, text=message or "宿主机返回空结果。")
                self._receipts = (self._receipts + [receipt])[-256:]
                self.save_data("notice_receipts", self._receipts)
            claimed.unlink()
        except (ValueError, RecursionError):
            # Quarantine malformed data once instead of retrying it every 3s.
            claimed.replace(claimed.with_suffix(".invalid"))
            logger.warning("Google Drive 归档跳过无效通知。")
        except Exception:
            # Keep the claim for a later retry when the MP notification service
            # is unavailable. Never log raw provider errors or credentials.
            logger.warning("Google Drive 归档通知暂未发送，将稍后重试。")
