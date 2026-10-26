"""Host-contract tests; no MoviePilot server, account or network is required."""

import copy
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from dataclasses import asdict, dataclass, fields, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


@dataclass
class HostDownloaderInfo:
    download_speed: float = 0.0
    upload_speed: float = 0.0
    download_size: float = 0.0
    upload_size: float = 0.0
    free_space: float = 0.0


class HostPlugin:
    def __init__(self):
        self.data = {}
        self.messages = []
        self.configs = []

    def get_data(self, key):
        return copy.deepcopy(self.data.get(key))

    def save_data(self, key, value):
        self.data[key] = copy.deepcopy(value)

    def update_config(self, value):
        self.configs.append(copy.deepcopy(value))
        return True

    def post_message(self, **kwargs):
        self.messages.append(kwargs)


def load_plugin(version):
    # Import at the same name used by MP, not a second top-level plugin name.
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    for name in ("app", "app.plugins", "app.schemas", "app.schemas.types",
                 "app.core", "app.core.event", "app.log", "app.sdk", "app.sdk.events",
                 "app.sdk.logging", "app.sdk.plugin"):
        module = types.ModuleType(name)
        module.__path__ = []
        sys.modules[name] = module
    events = types.SimpleNamespace(TransferComplete="transfer", MetadataScrape="scrape")
    manager = types.SimpleNamespace(register=lambda event: lambda function: function)
    logger = types.SimpleNamespace(warning=lambda *args: None)
    for name in ("app.core.event", "app.sdk.events"):
        sys.modules[name].Event = types.SimpleNamespace
        sys.modules[name].eventmanager = manager
    for name in ("app.plugins", "app.sdk.plugin"):
        sys.modules[name]._PluginBase = HostPlugin
    for name in ("app.log", "app.sdk.logging"):
        sys.modules[name].logger = logger
    sys.modules["app.schemas"].DownloaderInfo = HostDownloaderInfo
    sys.modules["app.schemas"].NotificationType = types.SimpleNamespace(Plugin="plugin")
    sys.modules["app.schemas.types"].EventType = events
    # Make the wrong host imports fail: tests must exercise the correct variant.
    unavailable = ("app.sdk.events", "app.sdk.logging", "app.sdk.plugin") if version == 2 else ("app.core.event", "app.log")
    for name in unavailable:
        del sys.modules[name]
    if version == 3:
        del sys.modules["app.plugins"]._PluginBase
    name = "app.plugins.gdrivearchiverbridge"
    spec = importlib.util.spec_from_file_location(name, ROOT / f"plugins.v{version}" / "gdrivearchiverbridge" / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class BridgeContract:
    def setUp(self):
        self.module = load_plugin(self.version)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.media = self.root / "media"
        self.folder = self.media / "欧美剧集" / "Example"
        self.folder.mkdir(parents=True)
        self.bridge = self.root / "bridge"
        self.bridge.mkdir()
        self.plugin = self.module.GDriveArchiverBridge()
        self.config = {"enabled": True, "library_root": str(self.media), "bridge_dir": str(self.bridge), "notify_results": True}
        self.plugin.init_plugin(self.config)

    def write(self, relative, value):
        path = self.bridge / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def status(self, age=0, **extra):
        value = {"version": 2, "running": True, "updated_at": (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat(),
                 "speed_bytes_per_second": 123, "jobs": [], "accounts": [], "uploads": [], **extra}
        self.write("status.json", value)
        return value

    def notice(self, **extra):
        return {"version": 1, "job_id": "a" * 64, "status": "complete", "title": "归档完成",
                "relative_path": "欧美剧集/Example", "message": "大小检查通过", "created_at": "2026-10-21T00:00:00Z", **extra}

    def test_both_event_payload_shapes_group_episode(self):
        episode = self.folder / "Season 1" / "S01E01.mkv"
        episode.parent.mkdir()
        episode.touch()
        for dictionary in (False, True):
            item = {"path": str(self.folder), "storage": "local"}
            target = {"path": str(episode)}
            info = {"target_diritem": item, "target_item": target}
            if not dictionary:
                info = types.SimpleNamespace(target_diritem=types.SimpleNamespace(**item), target_item=types.SimpleNamespace(**target))
            self.plugin.enqueue_transfer(types.SimpleNamespace(event_data={"transferinfo": info}))
        queued = list((self.bridge / "inbox").glob("*.json"))
        self.assertEqual(1, len(queued))
        job = json.loads(queued[0].read_text(encoding="utf-8"))
        self.assertEqual("欧美剧集/Example", job["relative_path"])
        self.assertEqual("Season 1/S01E01.mkv", job["completed_item"])
        self.assertEqual(1, job["version"])

    def test_metadata_scrape_directory_and_grouped_path(self):
        grouped = self.media / "剧集" / "欧美剧集" / "Example"
        grouped.mkdir(parents=True)
        self.plugin.enqueue_metadata_scrape(types.SimpleNamespace(event_data={"fileitem": {"path": str(grouped)}}))
        job = json.loads(next((self.bridge / "inbox").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual("剧集/欧美剧集/Example", job["relative_path"])
        self.assertEqual("metadata_scrape", job["trigger"])

    def test_outside_root_cloud_and_file_events_rejected(self):
        file = self.folder / "x.nfo"
        file.touch()
        for item in ({"path": str(self.root)}, {"path": str(self.media)}, {"path": str(file)},
                     {"path": str(self.folder), "storage": "rclone"}, None):
            self.plugin._enqueue_directory(item, "metadata_scrape")
        self.assertFalse((self.bridge / "inbox").exists())

    def test_status_online_and_stale_zeroes_all_live_speed(self):
        stamp = datetime.now(timezone.utc).isoformat()
        for age, online in [(0, True), (16, False)]:
            self.status(age, uploads=[{"file": "a.mkv", "phase": "uploading", "size": 200, "bytes": 100, "speed": 50, "eta_seconds": 2, "updated_at": stamp}])
            result = self.plugin.get_status()
            self.assertEqual(online, result.online)
            self.assertEqual(50 if online else 0, result.speed_bytes_per_second)
            self.assertEqual(50 if online else 0, result.uploads[0].speed)

    def test_old_upload_speed_zero_even_with_fresh_daemon(self):
        self.status(uploads=[{"speed": 50, "eta_seconds": 2, "updated_at": "2000-01-01T00:00:00Z"}])
        result = self.plugin.get_status()
        self.assertTrue(result.online)
        self.assertEqual(0, result.uploads[0].speed)

    def test_job_counts_include_truncated_pending_queue(self):
        self.status(job_counts={"queued": 1200, "complete": 5}, jobs=[{"status": "queued", "relative_path": "category/media"}])
        result = self.plugin.get_status()
        self.assertEqual({"queued": 1200, "complete": 5}, result.job_counts)
        self.assertIn("1/1205", " ".join(result.warnings))
        self.assertIn("1205", json.dumps(self.plugin.get_page(), ensure_ascii=False))

    def test_job_counts_fall_back_for_older_protocol_two(self):
        self.status(jobs=[{"status": "queued"}, {"status": "queued"}, {"status": "complete"}])
        result = self.plugin.get_status()
        self.assertEqual({"queued": 2, "complete": 1}, result.job_counts)
        self.assertFalse(result.warnings)

    def test_job_counts_only_accept_bounded_integer_keys(self):
        self.status(job_counts={"queued": 9, "complete": "100", "uploading": True, "failed": -4,
                                "{{malicious}}": 100, "a" * 33: 100})
        result = self.plugin.get_status()
        self.assertEqual({"queued": 9, "failed": 0}, result.job_counts)

    def test_public_status_whitelist_and_defensive_numbers(self):
        self.status(accounts=[{"name": "a", "access_token": "SECRET", "budget_bytes": -1}],
                    uploads=[None, {"speed": "oops", "size": 10 ** 1000}], jobs="bad", warnings=[None, "client_secret=SECRET"])
        result = self.plugin.get_status()
        self.assertNotIn("SECRET", json.dumps(asdict(result)))
        self.assertEqual(0, result.accounts[0].budget_bytes)
        self.assertEqual([], result.jobs)

    def test_missing_bad_and_oversized_status_stay_offline(self):
        self.assertFalse(self.plugin.get_status().online)
        for raw in ('[]', '{"version":1}', '{"version":2,"updated_at":null}', 'x' * (2 * 1024 * 1024 + 1)):
            (self.bridge / "status.json").write_text(raw, encoding="utf-8")
            self.assertFalse(self.plugin.get_status().online)

    def test_commands_require_live_daemon_and_never_fake_success(self):
        request = self.module.CommandRequest("pause")
        self.assertEqual("error", self.plugin.submit_command(request).status)
        self.status()
        reply = self.plugin.submit_command(request)
        self.assertEqual("pending", reply.status)
        self.assertEqual("pending", self.plugin.get_result(reply.id).status)
        command = json.loads((self.bridge / "commands" / (reply.id + ".json")).read_text())
        self.assertEqual(2, command["version"])
        self.assertEqual("pause", command["action"])
        self.write("results/" + reply.id + ".json", {**command, "status": "done", "message": "paused"})
        self.assertEqual("done", self.plugin.get_result(reply.id).status)

    def test_all_commands_and_manual_directory_scope(self):
        self.status()
        for action in ("pause", "resume", "retry", "check_accounts"):
            self.assertEqual("pending", self.plugin.submit_command(self.module.CommandRequest(action)).status)
        self.assertEqual("pending", self.plugin.submit_command(self.module.CommandRequest("retry", job_id="a" * 64)).status)
        self.assertEqual("pending", self.plugin.submit_command(self.module.CommandRequest("enqueue", relative_path="欧美剧集/Example")).status)
        for path in ("../media/欧美剧集/Example", "/欧美剧集/Example", "欧美剧集", "C:/media/x", "欧美剧集/../Example", "欧美剧集\\Example", "missing/folder"):
            self.assertEqual("error", self.plugin.submit_command(self.module.CommandRequest("enqueue", relative_path=path)).status)
        self.assertEqual("error", self.plugin.submit_command(self.module.CommandRequest("delete")).status)
        self.assertEqual("error", self.plugin.submit_command(self.module.CommandRequest("retry", job_id="../../x")).status)
        self.assertEqual("error", self.plugin.get_result("../../state").status)

    def test_config_once_resets_before_submit_and_survives_reinit(self):
        self.status()
        self.plugin.init_plugin({**self.config, "onlyonce": True, "control_action": "pause"})
        self.assertFalse(self.plugin.configs[-1]["onlyonce"])
        self.assertEqual(1, len(list((self.bridge / "commands").glob("*.json"))))
        self.plugin.init_plugin(self.plugin.configs[-1])
        self.assertEqual(1, len(list((self.bridge / "commands").glob("*.json"))))

    def test_failed_config_reset_does_not_submit(self):
        self.status()
        with patch.object(self.plugin, "update_config", return_value=False):
            self.plugin.init_plugin({**self.config, "onlyonce": True, "control_action": "pause"})
        self.assertFalse((self.bridge / "commands").exists())

    def test_notice_claim_recovery_and_dedup_across_reload(self):
        self.write("outbox/.old.json.abc.processing", self.notice())
        self.plugin._poll_outbox()
        self.assertEqual(1, len(self.plugin.messages))
        self.plugin.init_plugin(self.config)
        self.write("outbox/repeated.json", self.notice())
        self.plugin._poll_outbox()
        self.assertEqual(1, len(self.plugin.messages))
        self.assertEqual([], list((self.bridge / "outbox").iterdir()))

    def test_notification_failure_keeps_claim_for_retry(self):
        self.write("outbox/one.json", self.notice())
        with patch.object(self.plugin, "post_message", side_effect=RuntimeError("unavailable")):
            self.plugin._poll_outbox()
        self.assertEqual(1, len(list((self.bridge / "outbox").glob(".*.processing"))))
        self.plugin._poll_outbox()
        self.assertEqual(1, len(self.plugin.messages))

    def test_invalid_notice_quarantined_and_disabled_callbacks_noop(self):
        self.write("outbox/invalid.json", {"version": 2})
        self.plugin._poll_outbox()
        self.assertEqual(1, len(list((self.bridge / "outbox").glob("*.invalid"))))
        self.write("outbox/invalid.json", {"version": 2})
        self.plugin._poll_outbox()
        self.assertEqual([], list((self.bridge / "outbox").glob(".*.processing")))
        self.write("outbox/valid.json", self.notice())
        service = self.plugin.get_service()[0]
        self.plugin.stop_service()
        service["func"]()
        self.assertTrue((self.bridge / "outbox" / "valid.json").exists())
        self.assertEqual([], self.plugin.get_service())

    def test_native_dashboard_and_api_schema_contract(self):
        self.status()
        columns, options, page = self.plugin.get_dashboard("archive")
        self.assertEqual(3, options["refresh"])
        self.assertEqual(12, columns["cols"])
        self.assertTrue(page)
        self.assertIn("24", json.dumps(page))
        routes = self.plugin.get_api()
        self.assertEqual(["/status", "/command", "/result/{command_id}"], [r["path"] for r in routes])
        self.assertTrue(all(r["auth"] == "bear" and is_dataclass(r["response_model"]) for r in routes))
        form, model = self.plugin.get_form()
        self.assertIn("onlyonce", model)
        self.assertIn("check_accounts", json.dumps(form))

    def test_only_public_bridge_files_are_read(self):
        self.status()
        self.write("state.json", {"access_token": "SECRET"})
        self.assertNotIn("SECRET", json.dumps(asdict(self.plugin.get_status())))
        self.assertTrue((self.bridge / "state.json").exists())


class V2Tests(BridgeContract, unittest.TestCase):
    version = 2


class V3Tests(BridgeContract, unittest.TestCase):
    version = 3


class DistributionTests(unittest.TestCase):
    def test_self_contained_versions_stay_in_parity(self):
        v2 = (ROOT / "plugins.v2/gdrivearchiverbridge/__init__.py").read_text(encoding="utf-8")
        v3 = (ROOT / "plugins.v3/gdrivearchiverbridge/__init__.py").read_text(encoding="utf-8")
        v2 = v2.replace("app.core.event", "app.sdk.events").replace("app.log import", "app.sdk.logging import").replace("app.plugins import", "app.sdk.plugin import").replace('plugin_version = "2.3.0"', 'plugin_version = "3.3.0"')
        self.assertEqual(v2, v3)
        for version in (2, 3):
            manifest = json.loads((ROOT / f"package.v{version}.json").read_text(encoding="utf-8"))["GDriveArchiverBridge"]
            self.assertEqual(f"{version}.3.0", manifest["version"])
            self.assertIn(f"v{version}.3.0", manifest["history"])
            self.assertIn(f">={version}.0.0", manifest["system_version"])


if __name__ == "__main__":
    unittest.main()
