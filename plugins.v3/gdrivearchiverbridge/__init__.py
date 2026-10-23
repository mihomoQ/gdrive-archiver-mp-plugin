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

from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase
from app.schemas import DownloaderInfo, NotificationType
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
    eta_seconds: int = -1
    updated_at: str = ""
    active: bool = False


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
        if item.name == "eta_seconds":
            result[item.name] = int(raw) if type(raw) in (int, float) and 0 <= raw <= 2 ** 63 - 1 and math.isfinite(raw) else -1
        elif item.type is bool:
            result[item.name] = raw is True
        else:
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


HOME_DASHBOARD_ACTION = r'''async function () {
  if (model.home_busy) return;
  model.home_busy = true;
  model.home_error = false;
  model.home_message = '正在读取当前用户的首页设置…';
  let written = 0;
  try {
    const api = window.MoviePilotAPI;
    if (!api || typeof api.get !== 'function' || typeof api.post !== 'function') {
      throw new Error('当前前端未提供插件 API，请在首页设置中勾选 Google Drive 归档。');
    }
    const widget = __DASHBOARD_ID__;
    const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
    const record = value => value !== null && typeof value === 'object' && !Array.isArray(value);
    const copy = value => JSON.parse(JSON.stringify(value));
    const envelope = response => {
      if (record(response) && !own(response, 'success') && record(response.data) &&
          typeof response.data.success === 'boolean') return response.data;
      return response;
    };
    const payload = response => {
      const result = envelope(response);
      if (record(result) && typeof result.success === 'boolean') {
        if (!result.success) throw new Error('MoviePilot 拒绝了读取首页设置。');
        return result.data;
      }
      return result;
    };
    const valueOf = response => {
      const result = payload(response);
      if (!record(result) || !own(result, 'value')) throw new Error('首页设置响应格式不受支持，未覆盖现有配置。');
      return result.value;
    };
    const toggles = value => {
      if (value === null || value === undefined) return undefined;
      if (!record(value) || Object.values(value).some(flag => typeof flag !== 'boolean')) {
        throw new Error('首页显示项格式不受支持，未覆盖现有配置。');
      }
      return copy(value);
    };
    const profile = value => {
      if (value === null || value === undefined) return undefined;
      if (!record(value)) throw new Error('首页布局格式不受支持，未覆盖现有配置。');
      const wrapped = own(value, 'items') || own(value, 'enabled');
      const result = wrapped ? copy(value) : {items: copy(value)};
      result.items = result.items === undefined ? {} : result.items;
      if (!record(result.items) || Object.values(result.items).some(item => !record(item))) {
        throw new Error('首页布局格式不受支持，未覆盖现有配置。');
      }
      const enabled = toggles(result.enabled);
      if (enabled === undefined) delete result.enabled;
      else result.enabled = enabled;
      if (result.updatedAt !== undefined &&
          (!Number.isSafeInteger(result.updatedAt) || result.updatedAt <= 0)) {
        throw new Error('首页布局时间格式不受支持，未覆盖现有配置。');
      }
      return result;
    };
    const specs = [
      ['Dashboard', 'MP_DASHBOARD'],
      ['DashboardGridLayout', 'MP_DASHBOARD_GRID_LAYOUT'],
      ['DashboardGridLayoutTablet', 'MP_DASHBOARD_GRID_LAYOUT_TABLET'],
      ['DashboardGridLayoutMobile', 'MP_DASHBOARD_GRID_LAYOUT_MOBILE']
    ];
    // Read every remote and browser value before performing the first write.
    // A failure must not create a partial configuration from guessed defaults.
    const local = specs.map((spec, index) => {
      const raw = window.localStorage.getItem(spec[1]);
      const value = raw === null ? undefined : JSON.parse(raw);
      return index === 0 ? toggles(value) : profile(value);
    });
    const responses = await Promise.all([
      api.get('plugin/dashboard/meta'),
      ...specs.map(spec => api.get('user/config/' + spec[0]))
    ]);
    const meta = payload(responses[0]);
    if (!Array.isArray(meta) || !meta.some(item => record(item) &&
        (item.id + (item.key ? ':' + item.key : '')) === widget)) {
      throw new Error('请先保存并启用插件，再点击“显示到首页”。');
    }
    const remote = responses.slice(1).map((response, index) =>
      index === 0 ? toggles(valueOf(response)) : profile(valueOf(response)));
    const legacy = remote[0] === undefined ? (local[0] || {}) : remote[0];
    const updates = [{...legacy, [widget]: true}];
    for (let index = 1; index < specs.length; index++) {
      const server = remote[index];
      const browser = local[index];
      // Match MP's remote/local conflict rule, preserving the selected layout.
      const browserNewer = browser && browser.updatedAt !== undefined &&
        (!server || server.updatedAt === undefined || browser.updatedAt > server.updatedAt);
      const selected = copy(browserNewer ? browser : (server || browser || {items: {}}));
      const enabled = selected.enabled === undefined
        ? ((browser && browser.enabled) || legacy) : selected.enabled;
      selected.enabled = {...enabled, [widget]: true};
      selected.updatedAt = Math.max(Date.now(), (selected.updatedAt || 0) + 1);
      updates.push(selected);
    }
    // No DashboardOrder or widget coordinates are changed. MP places the new
    // item automatically and merges its own defaults for missing built-in flags.
    let cacheWarning = false;
    for (let index = 0; index < specs.length; index++) {
      const result = envelope(await api.post('user/config/' + specs[index][0], updates[index]));
      if (!record(result) || result.success !== true) throw new Error('MoviePilot 未确认保存首页设置。');
      written++;
      try { window.localStorage.setItem(specs[index][1], JSON.stringify(updates[index])); }
      catch (_) { cacheWarning = true; }
    }
    model.home_message = '已为当前用户显示到首页。请刷新 MoviePilot 页面后打开首页；原有组件、位置和显示选择均保留。' +
      (cacheWarning ? ' 浏览器缓存未同步，重新加载时以服务器设置为准。' : '');
  } catch (error) {
    model.home_error = true;
    const known = error && typeof error.message === 'string' &&
      /^(当前前端|MoviePilot|首页|请先保存)/.test(error.message) ? error.message : '读取或保存首页设置失败，请检查连接后重试。';
    model.home_message = known + (written ? ' 部分设置已保存；尚未全部完成，请重试。' : '');
  } finally {
    model.home_busy = false;
  }
}'''


def homepage_action_fields(plugin):
    action = HOME_DASHBOARD_ACTION.replace(
        "__DASHBOARD_ID__", json.dumps(plugin.__class__.__name__ + ":archive")
    )
    return [
        {"component": "VBtn", "props": {
            "variant": "tonal", "color": "primary", "prepend-icon": "mdi-view-dashboard-edit",
            "disabled": "{{ home_busy }}", "loading": "{{ home_busy }}", "onClick": action,
        }, "text": "显示到首页"},
        {"component": "VAlert", "props": {
            "show": "{{ !!home_message }}", "text": "{{ home_message }}", "variant": "tonal", "class": "mt-3",
            "type": "{{ home_busy ? 'info' : (home_error ? 'warning' : 'success') }}",
        }},
    ]


class GDriveArchiverBridge(_PluginBase):
    plugin_name = "Google Drive 归档桥接"
    plugin_desc = "整理事件入队、归档状态仪表盘与宿主机控制。"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "3.2.0"
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
        self._aggregate_upload_speed = True
        self._library_root = "/media"
        self._bridge_dir = "/bridge"
        self._outbox_poll_seconds = 3
        self._receipts = []
        self._commands = []
        self._control_error = ""

    def init_plugin(self, config: dict = None):
        self.stop_service()
        config = dict(config) if isinstance(config, dict) else {}
        home_reset = any(config.get(key, default) != default for key, default in
                         (("home_busy", False), ("home_error", False), ("home_message", "")))
        config.update(home_busy=False, home_error=False, home_message="")
        if home_reset and config.get("onlyonce") is not True:
            self.update_config(config)
        with self._lock:
            self._library_root = str(config.get("library_root") or "/media")
            self._bridge_dir = str(config.get("bridge_dir") or "/bridge")
            self._enabled = config.get("enabled") is True
            self._notify_results = config.get("notify_results") is True
            self._aggregate_upload_speed = config.get("aggregate_upload_speed", True) is True
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

    def get_module(self):
        if not self._enabled or not self._aggregate_upload_speed:
            return {}
        return {"downloader_info": self.downloader_info}

    def downloader_info(self, downloader: Optional[str] = None):
        # A list appends to MP's system modules; a single object would replace
        # the real downloaders. Never add global archiver traffic to a named one.
        with self._lock:
            if not self._enabled or not self._aggregate_upload_speed or downloader:
                return []
            try:
                _, view = self._read_transfer_status()
            except (OSError, ValueError, RecursionError):
                return []
        if view.speed_bytes_per_second <= 0:
            return []
        return [DownloaderInfo(upload_speed=view.speed_bytes_per_second)]

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
        fields_json = homepage_action_fields(self) + [
            self._field("VSwitch", "enabled", "启用插件"),
            self._field("VSwitch", "aggregate_upload_speed", "将归档上传速度计入首页实时速率"),
            self._field("VTextField", "library_root", "MoviePilot 媒体根目录"),
            self._field("VTextField", "bridge_dir", "共享 bridge 目录"),
            self._field("VSwitch", "notify_results", "通过 MoviePilot 发送结果通知"),
            self._field("VTextField", "outbox_poll_seconds", "通知轮询间隔（2–5 秒）", type="number"),
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text":
                "首页仪表盘每 3 秒刷新；可在主页仪表盘设置中添加“Google Drive 归档”。先选择操作，再勾选执行一次并保存；请以操作回执为准。暂停会跨重启保留，需要执行继续上传解除。"}},
            self._field("VSelect", "control_action", "操作", items=[
                {"title": "请选择操作", "value": ""},
                {"title": "暂停新上传", "value": "pause"}, {"title": "继续上传", "value": "resume"},
                {"title": "重试任务", "value": "retry"}, {"title": "重新检查账号", "value": "check_accounts"},
                {"title": "手动添加媒体目录", "value": "enqueue"}]),
            self._field("VTextField", "control_job_id", "重试任务 ID（留空重试全部异常任务）"),
            self._field("VTextField", "control_relative_path", "手动入队相对目录，例如 欧美剧集/剧名"),
            self._field("VSwitch", "onlyonce", "执行一次所选操作（保存后自动关闭）"),
        ]
        return [{"component": "VForm", "content": fields_json}], {
            "home_busy": False, "home_error": False, "home_message": "",
            "enabled": False, "library_root": "/media", "bridge_dir": "/bridge", "notify_results": False,
            "aggregate_upload_speed": True, "outbox_poll_seconds": 3, "control_action": "", "control_job_id": "",
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

    def _read_transfer_status(self):
        """Share freshness rules without reading task history or command receipts."""
        view = StatusView(enabled=self._enabled)
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
        rows = raw.get("uploads")
        view.uploads = [item for row in rows[:100] if (item := _model(UploadView, row)) is not None] if isinstance(rows, list) else []
        for upload in view.uploads:
            stamp = _time(upload.updated_at)
            age = (datetime.now(timezone.utc) - stamp).total_seconds() if stamp else -1
            upload.active = view.online and 0 <= age <= 15 and upload.phase in {"uploading", "moving", "checking_size"}
            if not upload.active:
                upload.speed = 0
                upload.eta_seconds = -1
        view.speed_bytes_per_second = sum(u.speed for u in view.uploads if u.active and u.phase == "uploading")
        return raw, view

    def get_status(self) -> StatusView:
        view = StatusView(enabled=self._enabled)
        try:
            raw, view = self._read_transfer_status()
            for name, cls, limit in [("jobs", JobView, 1000), ("accounts", AccountView, 500),
                                     ("recent_events", EventView, 50)]:
                rows = raw.get(name)
                setattr(view, name, [item for row in rows[:limit] if (item := _model(cls, row)) is not None]
                        if isinstance(rows, list) else [])
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
        return {"cols": 12, "md": 12}, {"title": "Google Drive 归档", "refresh": 3}, self._render_archive(self.get_status(), True)

    @staticmethod
    def _view_node(component, text=None, content=None, **props):
        """Use MP's escaped text interpolation; never emit a raw html node."""
        node = {"component": component}
        # MP's renderer always supplies a default text slot. VIcon gives that
        # slot priority over its icon prop, so an empty slot hides the icon.
        if component == "VIcon" and text is None and isinstance(props.get("icon"), str):
            text = props["icon"]
        if component == "VCard" and props.get("variant") == "outlined":
            props["style"] = props.get("style", "") + ";border:1px solid rgba(var(--v-theme-on-surface),0.12)"
        if props:
            node["props"] = props
        if text is not None:
            node["text"] = _text(str(text), 2000)
        if content is not None:
            node["content"] = content
        return node

    @staticmethod
    def _size(value):
        value = _number(value)
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if value < 1024 or unit == "TiB":
                return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024

    @staticmethod
    def _eta(value):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 < value <= 2 ** 63 - 1 or not math.isfinite(value):
            return "—"
        seconds = int(value)
        if seconds < 60:
            return "不足 1 分钟"
        minutes = (seconds + 59) // 60
        if minutes < 60:
            return f"约 {minutes} 分钟"
        hours, minutes = divmod(minutes, 60)
        return f"约 {hours} 小时" + (f" {minutes} 分钟" if minutes else "")

    @staticmethod
    def _ago(value):
        stamp = _time(value)
        if not stamp:
            return "暂无记录"
        seconds = int((datetime.now(timezone.utc) - stamp).total_seconds())
        if seconds < -5:
            return "时间异常"
        if seconds < 10:
            return "刚刚"
        if seconds < 60:
            return f"{seconds} 秒前"
        if seconds < 3600:
            return f"{seconds // 60} 分钟前"
        if seconds < 86400:
            return f"{seconds // 3600} 小时前"
        return f"{seconds // 86400} 天前"

    @staticmethod
    def _status_label(value):
        return {"queued": "等待上传", "waiting_stable": "等待文件稳定", "uploading": "上传中",
                "preparing": "准备中", "moving": "移动到团队盘", "checking_size": "检查文件大小",
                "retrying": "等待重试", "waiting_account": "等待可用账号", "failed": "失败待处理",
                "complete": "已完成", "healthy": "可用", "disabled": "已禁用", "cooldown": "冷却中",
                "budget_exhausted": "预算已用完", "unknown": "待检查", "error": "异常", "paused": "已暂停",
                "done": "已执行", "pending": "等待回执", "rejected": "已拒绝", "info": "提示",
                "warning": "提醒", "success": "成功", "recovered": "已恢复", "enabled": "已启用"}.get(value, "状态更新")

    @staticmethod
    def _status_color(value):
        return {"healthy": "success", "complete": "success", "done": "success", "success": "success",
                "disabled": "error", "failed": "error", "error": "error", "rejected": "error",
                "cooldown": "warning", "budget_exhausted": "warning", "retrying": "warning",
                "waiting_account": "warning", "paused": "warning", "warning": "warning",
                "uploading": "primary", "moving": "primary", "checking_size": "primary"}.get(value, "secondary")

    def _view_summary(self, state):
        active = [u for u in state.uploads if state.online and getattr(u, "active", False)]
        active_jobs = {u.job_id for u in active if u.job_id}
        unfinished = [j for j in state.jobs if j.status != "complete"]
        counts = state.job_counts or {}
        pending = max(0, sum(_number(v, True) for k, v in counts.items() if k != "complete") - len(active_jobs))
        remaining = sum(max(0, j.bytes_total - j.bytes_done) for j in unfinished)
        known_jobs = {j.id for j in unfinished}
        remaining -= sum(min(u.bytes, u.size) for u in active if u.job_id in known_jobs)
        return {"active": active, "active_jobs": active_jobs, "pending": pending,
                "speed": sum(u.speed for u in active if u.phase == "uploading"),
                "healthy": sum(a.status == "healthy" for a in state.accounts),
                "errors": sum(_number(counts.get(k), True) for k in ("failed", "retrying", "waiting_account")),
                "remaining_files": sum(max(0, j.files_total - j.files_done) for j in unfinished),
                "remaining_bytes": max(0, remaining)}

    def _overview(self, state, summary):
        n = self._view_node
        active = len(summary["active"])
        if not state.online:
            title, color, icon = "归档器离线", "grey", "mdi-cloud-alert-outline"
            subtitle = "显示上次快照，等待归档器连接" if state.updated_at else "等待宿主机状态，请检查 bridge 目录"
        elif state.paused:
            title, color, icon = "暂停派发", "warning", "mdi-pause-circle-outline"
            subtitle = "当前文件完成后暂停新上传" if active else "已暂停新上传，队列将继续保留"
        elif active:
            title, color, icon = "正在归档", "success", "mdi-cloud-upload-outline"
            subtitle = "文件上传至个人盘，再移入目标团队盘"
        elif summary["errors"] or (summary["pending"] and not summary["healthy"]):
            title, color, icon = "存在待处理任务", "error", "mdi-alert-circle-outline"
            subtitle = "请在详情中查看任务与账号异常"
        else:
            title, color, icon = "等待任务", "primary", "mdi-cloud-check-outline"
            subtitle = "等待文件稳定或新的整理完成事件"
        if not state.enabled:
            subtitle = "插件未启用，整理事件不会自动入队"
        header = n("VRow", content=[
            n("VCol", content=[n("VIcon", icon=icon, color=color, size=28),
                n("div", text="Google Drive", **{"class": "text-subtitle-1 font-weight-bold"}),
                n("VChip", text=title, color=color, size="small", variant="tonal")],
                cols=12, sm=8, **{"class": "d-flex align-center flex-wrap ga-2"}),
            n("VCol", text="最近同步 · " + self._ago(state.updated_at), cols=12, sm=4,
                **{"class": "text-caption text-medium-emphasis text-sm-end"})], align="center", dense=True)
        tiles = [("活动文件", str(active), f"上限 {state.upload_workers or '—'} 路", "mdi-file-upload", color),
                 ("当前速度", self._size(summary["speed"]) + "/s", "当前传输合计" if active else "当前没有活动上传", "mdi-speedometer", "primary"),
                 ("待处理任务", str(summary["pending"]), "含等待、重试与失败任务", "mdi-format-list-bulleted", "warning"),
                 ("健康账号", f"{summary['healthy']}/{len(state.accounts)}" if state.online else "—",
                  "可参与新上传" if state.online else "快照过期，等待复查", "mdi-account-check-outline", "success" if state.online else "grey")]
        stats = []
        for label, value, hint, tile_icon, tile_color in tiles:
            stats.append(n("VCol", content=[n("VCard", content=[n("VCardText", content=[
                n("div", content=[n("VIcon", icon=tile_icon, size=17, color=tile_color), n("span", text=label)],
                  **{"class": "d-flex align-center ga-1 text-caption text-medium-emphasis"}),
                n("div", text=value, **{"class": "text-h6 font-weight-bold my-1", "style": "font-variant-numeric:tabular-nums;overflow-wrap:anywhere"}),
                n("div", text=hint, **{"class": "text-caption text-medium-emphasis"})], **{"class": "pa-3"})],
                variant="outlined", rounded="lg", **{"class": "h-100"})], cols=6, sm=3))
        return n("VCard", content=[n("VCardText", content=[header,
            n("div", text=subtitle, **{"class": "text-body-2 text-medium-emphasis mt-1 mb-3"}),
            n("VRow", content=stats, dense=True),
            n("div", text=f"已列任务待传：{summary['remaining_files']} 个文件 · {self._size(summary['remaining_bytes'])}",
              **{"class": "text-caption text-medium-emphasis mt-3"})], **{"class": "pa-4"})], variant="flat", rounded="lg")

    def _transfer_cards(self, state, limit):
        n = self._view_node
        uploads = sorted(state.uploads, key=lambda u: not getattr(u, "active", False))[:limit]
        if not uploads:
            text = "已暂停新上传" if state.paused else "当前没有活动传输"
            return [n("VCard", content=[n("VCardText", content=[
                n("VIcon", icon="mdi-upload", color="secondary", size=22),
                n("span", text=text, **{"class": "text-body-2"})], **{"class": "d-flex align-center ga-2 py-4"})],
                variant="tonal", rounded="lg")]
        cards = []
        for upload in uploads:
            fresh = state.online and getattr(upload, "active", False)
            color = "primary" if fresh else "grey"
            phase = self._status_label(upload.phase) if fresh else ("准备中" if state.online and upload.phase == "preparing" else "上次传输快照")
            percent = min(100, max(0, upload.bytes * 100 / upload.size)) if upload.size else 0
            filename = upload.file or upload.relative_path or "未命名文件"
            eta = self._eta(upload.eta_seconds) if fresh and upload.phase == "uploading" else "—"
            speed = self._size(upload.speed) + "/s" if fresh and upload.phase == "uploading" else "—"
            cards.append(n("VCard", content=[n("VCardText", content=[
                n("div", text=filename, **{"class": "text-body-2 font-weight-medium mb-2", "style": "display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere"}),
                n("div", content=[n("VChip", text=phase, color=color, size="x-small", variant="tonal"),
                    n("VChip", text=upload.account or "分配账号中", size="x-small", variant="outlined")],
                  **{"class": "d-flex flex-wrap align-center ga-2 mb-3"}),
                n("VProgressLinear", **{"model-value": round(percent, 1), "height": 6, "rounded": True, "color": color}),
                n("div", content=[n("span", text=f"{self._size(min(upload.bytes, upload.size))} / {self._size(upload.size)} · {percent:.0f}%"),
                    n("span", text=f"{speed} · 剩余 {eta}")], **{"class": "d-flex justify-space-between flex-wrap ga-1 text-caption text-medium-emphasis mt-2"})],
                **{"class": "pa-3"})], variant="outlined", rounded="lg", **{"class": "mb-2"}))
        return cards

    def _account_cards(self, state, limit, compact=False):
        n = self._view_node
        accounts = sorted(state.accounts, key=lambda a: (not (state.online and a.active_uploads), a.status == "healthy"))[:limit]
        if not accounts:
            return [n("VAlert", text="尚未发现上传账号", variant="tonal", type="info", density="compact")]
        cards = []
        for account in accounts:
            color = self._status_color(account.status) if state.online else "grey"
            percent = min(100, max(0, account.reserved_bytes * 100 / account.budget_bytes)) if account.budget_bytes else 0
            status = self._status_label(account.status) + (" · 上次状态" if not state.online else "")
            cooldown = _time(account.cooldown_until)
            seconds = (cooldown - datetime.now(timezone.utc)).total_seconds() if cooldown else -1
            meta = "自带 OAuth" if account.oauth_client == "custom" else "内置 OAuth" if account.oauth_client == "builtin" else "OAuth 未知"
            meta += " · 总容量 " + self._size(account.capacity_bytes)
            if account.status == "cooldown":
                meta += " · 冷却剩余 " + self._eta(seconds)
            if compact:
                content = [n("div", text=account.name or "未命名账号", **{"class": "text-caption font-weight-medium mb-1",
                    "style": "display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere"}),
                    n("div", content=[n("VChip", text=status, color=color, size="x-small", variant="tonal"),
                        n("span", text=self._ago(account.last_health_check), **{"class": "text-caption text-medium-emphasis"})],
                        **{"class": "d-flex justify-space-between align-center flex-wrap ga-1 mb-2"}),
                    n("VProgressLinear", **{"model-value": round(percent, 1), "height": 4, "rounded": True, "color": color}),
                    n("div", text=f"预算剩余 {self._size(account.remaining_bytes)} · 已预留 {percent:.0f}%",
                      **{"class": "text-caption text-medium-emphasis mt-1"})]
                if account.last_error:
                    content.append(n("div", text=account.last_error[:140], **{"class": "text-caption text-error mt-1",
                        "style": "display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere"}))
                cards.append(n("VCard", content=[n("VCardText", content=content, **{"class": "pa-2"})], variant="outlined", rounded="lg", **{"class": "mb-2"}))
                continue
            content = [n("div", content=[n("span", text=account.name or "未命名账号", **{"class": "text-body-2 font-weight-medium", "style": "overflow-wrap:anywhere"}),
                n("VChip", text=status, color=color, size="x-small", variant="tonal")], **{"class": "d-flex align-center justify-space-between flex-wrap ga-2"}),
                n("div", text=meta, **{"class": "text-caption text-medium-emphasis mt-1 mb-2"}),
                n("VProgressLinear", **{"model-value": round(percent, 1), "height": 4, "rounded": True, "color": color}),
                n("div", text=f"本机 24h 预留 {self._size(account.reserved_bytes)} / {self._size(account.budget_bytes)} · 预算剩余 {self._size(account.remaining_bytes)}",
                  **{"class": "text-caption text-medium-emphasis mt-2"}),
                n("div", text="最近检查 · " + self._ago(account.last_health_check), **{"class": "text-caption text-medium-emphasis mt-1"})]
            if account.last_error:
                content.append(n("div", text=account.last_error, **{"class": "text-caption text-error mt-2", "style": "overflow-wrap:anywhere"}))
            cards.append(n("VCard", content=[n("VCardText", content=content, **{"class": "pa-3"})], variant="outlined", rounded="lg", **{"class": "mb-2"}))
        return cards

    def _detail_panel(self, title, content):
        n = self._view_node
        return n("VExpansionPanel", content=[n("VExpansionPanelTitle", text=title), n("VExpansionPanelText", content=content)])

    def _detail_rows(self, rows):
        n = self._view_node
        return [n("VList", content=[n("VListItem", content=[
            n("div", text=title, **{"class": "text-body-2 font-weight-medium", "style": "overflow-wrap:anywhere"}),
            n("div", text=detail, **{"class": "text-caption text-medium-emphasis mt-1", "style": "white-space:pre-line;overflow-wrap:anywhere"})],
            **{"class": "py-3"}) for title, detail in rows], density="compact")] if rows else [n("div", text="暂无记录", **{"class": "text-body-2 text-medium-emphasis py-3"})]

    def _render_archive(self, state, compact):
        n = self._view_node
        summary = self._view_summary(state)
        nodes = [self._overview(state, summary), n("VDivider", **{"class": "my-3"})]
        transfers = [n("div", text="传输进度" if state.online else "上次传输进度", **{"class": "text-subtitle-2 font-weight-bold mb-2"})]
        transfers.extend(self._transfer_cards(state, 3 if compact else 12))
        if len(state.uploads) > (3 if compact else 12):
            transfers.append(n("div", text=f"显示前 {3 if compact else 12}/{len(state.uploads)} 条传输", **{"class": "text-caption text-medium-emphasis mb-3"}))
        if compact:
            accounts = [n("div", text="账号与上传预算", **{"class": "text-subtitle-2 font-weight-bold mb-2"}),
                n("div", content=[n("VChip", text=f"{summary['healthy']} 个可用" if state.online else "上次账号状态", color="success" if state.online else "grey", size="x-small", variant="tonal"),
                    n("VChip", text=f"{len(state.accounts) - summary['healthy']} 个不可用", color="warning" if state.online else "grey", size="x-small", variant="tonal")],
                    **{"class": "d-flex flex-wrap ga-1 mb-2"})]
            accounts.extend(self._account_cards(state, 3, True))
            if len(state.accounts) > 3:
                accounts.append(n("div", text=f"显示前 3/{len(state.accounts)} 个账号，当前使用账号优先", **{"class": "text-caption text-medium-emphasis mb-2"}))
            nodes.append(n("VRow", content=[n("VCol", content=transfers, cols=12, md=8), n("VCol", content=accounts, cols=12, md=4)], dense=True))
            if state.warnings:
                nodes.append(n("VAlert", content=[n("div", text=state.warnings[0][:180],
                    style="display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere")],
                    type="warning", variant="tonal", density="compact", **{"class": "my-2"}))
            nodes.append(n("div", text="预算为本机滚动 24 小时预留估算。完整队列与操作回执请查看插件详情；操作入口位于插件设置。",
                           **{"class": "text-caption text-medium-emphasis mt-3"}))
            return nodes
        nodes.extend(transfers)
        jobs = []
        for job in state.jobs[:50]:
            label = self._status_label(job.status)
            if job.status == "uploading" and job.id not in summary["active_jobs"]:
                label = "已暂停" if state.paused else "等待调度"
            detail = f"{label} · {job.files_done}/{job.files_total} 个文件 · {self._size(job.bytes_done)}/{self._size(job.bytes_total)}"
            detail += "\n最近更新 " + self._ago(job.updated_at) + " · 任务 ID " + job.id
            if job.last_error:
                detail += "\n" + job.last_error
            jobs.append((job.relative_path or "未命名任务", detail))
        actions = {"pause": "暂停新上传", "resume": "恢复上传", "retry": "重试任务", "check_accounts": "检查账号", "enqueue": "手动入队"}
        panels = [self._detail_panel(f"任务队列 · 显示 {len(jobs)}/{sum(state.job_counts.values())} 项", self._detail_rows(jobs)),
                  self._detail_panel(f"上传账号 · 显示 {min(50, len(state.accounts))}/{len(state.accounts)} 项", self._account_cards(state, 50)),
                  self._detail_panel("操作回执 · 最近 20 条", self._detail_rows([
                      (actions.get(c.action, "操作") + " · " + self._status_label(c.status), c.message + "\n" + self._ago(c.created_at) + " · " + c.id)
                      for c in state.commands[:20]])),
                  self._detail_panel("最近事件 · 最近 20 条", self._detail_rows([
                      (self._status_label(e.status) + " · " + self._ago(e.at), e.message) for e in list(reversed(state.recent_events))[:20]]))]
        if state.warnings:
            panels.append(self._detail_panel("提示与诊断", self._detail_rows([("提示", x) for x in state.warnings[:10]])))
        nodes.append(n("VExpansionPanels", content=panels, multiple=True, variant="accordion", **{"class": "mt-4"}))
        nodes.append(n("VAlert", text="操作入口：插件设置 → 选择操作 → 执行一次 → 保存。以“已执行”回执为准。预算是本机滚动 24 小时预留估算；待传数量仅统计已列出且已扫描的任务。",
                       type="info", variant="tonal", density="compact", **{"class": "mt-4"}))
        return nodes

    def get_page(self):
        return self._render_archive(self.get_status(), False)


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
