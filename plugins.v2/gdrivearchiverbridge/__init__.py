"""MoviePilot-to-host bridge for gdrive-archiver."""

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType


class GDriveArchiverBridge(_PluginBase):
    plugin_name = "Google Drive 归档桥接"
    plugin_desc = "将整理完成目录原子写入宿主机 gdrive-archiver 队列。"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "1.0.1"
    plugin_author = "irisrclone"
    author_url = ""
    plugin_config_prefix = "gdrivearchiverbridge_"
    plugin_order = 90
    auth_level = 1

    _enabled = False
    _library_root = "/media"
    _bridge_dir = "/bridge"
    _notify_results = False
    _outbox_poll_seconds = 30

    def init_plugin(self, config: dict = None):
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._library_root = str(config.get("library_root") or "/media")
        self._bridge_dir = str(config.get("bridge_dir") or "/bridge")
        self._notify_results = bool(config.get("notify_results", False))
        try:
            self._outbox_poll_seconds = max(10, int(config.get("outbox_poll_seconds") or 30))
        except (TypeError, ValueError):
            self._outbox_poll_seconds = 30

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled or not self._notify_results:
            return []
        return [{
            "id": "GDriveArchiverBridgeOutbox",
            "name": "Google Drive 归档结果通知",
            "trigger": "interval",
            "func": self._poll_outbox,
            "kwargs": {"seconds": self._outbox_poll_seconds},
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [{
            "component": "VForm",
            "content": [{
                "component": "VRow",
                "content": [
                    self._field("VSwitch", "enabled", "启用插件", 4),
                    self._field("VSwitch", "notify_results", "读取宿主机结果通知", 4),
                    self._field("VTextField", "outbox_poll_seconds", "结果轮询秒数（最小 10）", 4,
                                {"type": "number"}),
                ],
            }, {
                "component": "VRow",
                "content": [
                    self._field("VTextField", "library_root", "MoviePilot 媒体根目录", 6),
                    self._field("VTextField", "bridge_dir", "共享 bridge 目录", 6),
                ],
            }, {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "text": "bridge 需同时挂载到宿主机；插件仅写队列，不上传也不删除文件。",
                },
            }],
        }], {
            "enabled": False,
            "library_root": "/media",
            "bridge_dir": "/bridge",
            "notify_results": False,
            "outbox_poll_seconds": 30,
        }

    @staticmethod
    def _field(component: str, model: str, label: str, md: int, extra: Dict[str, Any] = None) -> Dict[str, Any]:
        props = {"model": model, "label": label}
        if extra:
            props.update(extra)
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": md},
            "content": [{"component": component, "props": props}],
        }

    def get_page(self) -> List[dict]:
        return []

    @eventmanager.register(EventType.TransferComplete)
    def enqueue_transfer(self, event: Event):
        if not self._enabled:
            return
        event_data = event.event_data or {}
        transferinfo = event_data.get("transferinfo")
        target_diritem = getattr(transferinfo, "target_diritem", None)
        target_path = getattr(target_diritem, "path", None)
        if not target_path:
            return

        try:
            root = Path(self._library_root).resolve()
            target = Path(target_path).resolve()
            relative_path = target.relative_to(root)
            if not relative_path.parts:
                raise ValueError("目标目录不能是媒体根目录")
        except (OSError, ValueError) as err:
            logger.warning("Google Drive 归档桥接跳过目录 %s：%s", target_path, err)
            return

        relative = relative_path.as_posix()
        job_id = hashlib.sha256(relative.encode("utf-8")).hexdigest()
        job = {
            "version": 1,
            "id": job_id,
            "source_path": str(target),
            "relative_path": relative,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self._write_job(job)
            logger.info("Google Drive 归档已入队：%s", relative)
        except OSError as err:
            logger.error("Google Drive 归档入队失败 %s：%s", relative, err)

    def _write_job(self, job: Dict[str, Any]):
        inbox = Path(self._bridge_dir) / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        destination = inbox / f"{job['id']}.json"
        temporary = inbox / f".{job['id']}.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("x", encoding="utf-8") as file:
                json.dump(job, file, ensure_ascii=False, separators=(",", ":"))
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _poll_outbox(self):
        outbox = Path(self._bridge_dir) / "outbox"
        if not outbox.is_dir():
            return
        for result_path in sorted(outbox.glob("*.json")):
            self._notify_result(result_path)

    def _notify_result(self, result_path: Path):
        claimed = result_path.with_name(f".{result_path.name}.{uuid.uuid4().hex}.processing")
        try:
            os.replace(result_path, claimed)
        except FileNotFoundError:
            return

        try:
            result = json.loads(claimed.read_text(encoding="utf-8"))
            if not isinstance(result, dict):
                raise ValueError("结果必须是 JSON 对象")
            status = str(result.get("status") or "info")
            relative = str(result.get("relative_path") or "")
            message = str(result.get("message") or "")
            title = str(result.get("title") or f"Google Drive 归档：{status}")
            text = "\n".join(item for item in (relative, message) if item) or "宿主机上传器返回空结果。"
            self.post_message(mtype=NotificationType.Plugin, title=title, text=text)
            claimed.unlink()
        except (OSError, ValueError, json.JSONDecodeError) as err:
            logger.error("读取归档结果失败 %s：%s", claimed.name, err)
            if not result_path.exists():
                os.replace(claimed, result_path)

    def stop_service(self):
        pass
