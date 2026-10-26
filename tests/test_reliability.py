"""Delivery, notification concurrency and command-history recovery contracts."""

import copy
import json
import tempfile
import threading
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from test_bridge import load_plugin


class ReliabilityContract:
    def setUp(self):
        self.module = load_plugin(self.version)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.media = self.root / "media"
        self.folder = self.media / "Movies" / "Example"
        self.folder.mkdir(parents=True)
        self.bridge = self.root / "bridge"
        self.bridge.mkdir()
        self.config = {"enabled": True, "notify_results": False, "library_root": str(self.media), "bridge_dir": str(self.bridge)}
        self.plugin = self.module.GDriveArchiverBridge()
        self.plugin.init_plugin(self.config)
        self.write_status()

    def write_status(self):
        now = datetime.now(timezone.utc).isoformat()
        raw = {"version": 2, "running": True, "updated_at": now,
               "uploads": [{"job_id": "a" * 64, "file": "test.mkv", "phase": "uploading",
                            "size": 1000, "bytes": 10, "speed": 42, "updated_at": now}]}
        (self.bridge / "status.json").write_text(json.dumps(raw), encoding="utf-8")

    def event(self):
        return types.SimpleNamespace(event_data={"transferinfo": {
            "target_diritem": {"path": str(self.folder), "storage": "local"},
            "target_item": {"path": str(self.folder / "test.mkv")}}})

    def tick(self, plugin=None):
        for service in (plugin or self.plugin).get_service():
            service["func"]()

    def fail_delivery(self):
        with patch.object(self.plugin, "_atomic_write", side_effect=OSError("bridge unavailable")):
            self.plugin.enqueue_transfer(self.event())

    def restart(self, config=None):
        restarted = self.module.GDriveArchiverBridge()
        restarted.data = copy.deepcopy(self.plugin.data)
        restarted.init_plugin(config or self.config)
        return restarted

    def notice(self, bridge=None, message="first"):
        path = (bridge or self.bridge) / "outbox" / "notice.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1, "status": "complete", "title": "Test", "message": message}), encoding="utf-8")
        return path

    def result(self, reply, status="done"):
        path = self.bridge / "results" / (reply.id + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 2, "id": reply.id, "action": reply.action,
                                    "status": status, "message": status, "created_at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
        return path

    def test_failed_events_survive_restart_and_replay_without_notifications(self):
        times = [f"2026-10-26T12:00:0{index}+00:00" for index in range(4)]
        with patch.object(self.plugin, "_atomic_write", side_effect=OSError("bridge unavailable")), patch.object(self.module, "_now", side_effect=times):
            self.plugin.enqueue_transfer(self.event())
            next_episode = self.event()
            next_episode.event_data["transferinfo"]["target_item"]["path"] = str(self.folder / "episode-two.mkv")
            self.plugin.enqueue_transfer(next_episode)
            self.plugin.enqueue_metadata_scrape(types.SimpleNamespace(event_data={"fileitem": {"path": str(self.folder)}}))
            self.plugin.enqueue_transfer(self.event())
        pending = self.plugin.get_data("pending_jobs")
        self.assertEqual(1, len(pending or {}), "one media directory must have one durable record")
        restarted = self.restart()
        self.assertTrue(restarted.get_service(), "retry service must run with notify_results=false")
        self.tick(restarted)
        queued = list((self.bridge / "inbox").glob("*.json"))
        self.assertEqual(1, len(queued))
        self.assertEqual("metadata_scrape", json.loads(queued[0].read_text(encoding="utf-8"))["trigger"])
        self.assertEqual(times[-1], json.loads(queued[0].read_text(encoding="utf-8"))["created_at"])
        self.assertEqual({}, restarted.get_data("pending_jobs"))
        self.tick(restarted)
        self.assertEqual(1, len(list((self.bridge / "inbox").glob("*.json"))))
        self.assertEqual([], restarted.messages)

    def test_save_false_or_exception_keeps_retryable_event(self):
        for failure in (False, OSError("database unavailable")):
            with self.subTest(failure=str(failure)):
                options = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                with patch.object(self.plugin, "save_data", **options):
                    self.fail_delivery()
                self.assertTrue(self.plugin.get_service())
                self.tick()
                self.assertEqual(1, len(list((self.bridge / "inbox").glob("*.json"))))
                self.assertEqual({}, self.plugin.get_data("pending_jobs"))

    def test_disabled_retries_wait_and_configuration_changes_do_not_retarget(self):
        self.fail_delivery()
        service = self.plugin.get_service()
        self.assertTrue(service)
        self.plugin.stop_service()
        service[0]["func"]()
        self.assertFalse((self.bridge / "inbox").exists())
        new_media = self.root / "another-media"
        (new_media / "Movies" / "Example").mkdir(parents=True)
        new_bridge = self.root / "another-bridge"
        new_bridge.mkdir()
        self.plugin.init_plugin({**self.config, "library_root": str(new_media), "bridge_dir": str(new_bridge)})
        self.tick()
        self.assertFalse((new_bridge / "inbox").exists())
        self.assertEqual(1, len(self.plugin.get_data("pending_jobs") or {}))
        self.plugin.init_plugin(self.config)
        self.tick()
        job = json.loads(next((self.bridge / "inbox").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(str(self.folder.resolve()), job["source_path"])
        self.assertEqual({}, self.plugin.get_data("pending_jobs"))

    def test_pending_ack_failure_replays_idempotently(self):
        self.fail_delivery()
        with patch.object(self.plugin, "save_data", return_value=False):
            self.tick()
        self.assertEqual(1, len(list((self.bridge / "inbox").glob("*.json"))))
        self.assertEqual(1, len(self.plugin.get_data("pending_jobs") or {}))
        self.tick()
        self.assertEqual(1, len(list((self.bridge / "inbox").glob("*.json"))))
        self.assertEqual({}, self.plugin.get_data("pending_jobs"))

    def test_slow_notification_does_not_block_native_speed_or_stop(self):
        self.plugin.init_plugin({**self.config, "notify_results": True})
        self.notice()
        entered, release, queried, stopped = (threading.Event() for _ in range(4))
        threads = []

        def send(**kwargs):
            entered.set()
            release.wait(3)

        def query():
            self.plugin.downloader_info()
            queried.set()

        def stop():
            self.plugin.stop_service()
            stopped.set()

        with patch.object(self.plugin, "post_message", side_effect=send):
            try:
                threads.append(threading.Thread(target=self.plugin._poll_outbox))
                threads[-1].start()
                self.assertTrue(entered.wait(1))
                for target in (query, stop):
                    threads.append(threading.Thread(target=target))
                    threads[-1].start()
                self.assertTrue(queried.wait(0.5), "notification holds the native-speed state lock")
                self.assertTrue(stopped.wait(0.5), "stop_service waits for an external notification call")
            finally:
                release.set()
                for thread in threads:
                    thread.join(2)

    def test_old_notification_callback_cannot_ack_new_configuration(self):
        self.plugin.init_plugin({**self.config, "notify_results": True})
        self.notice(message="old")
        new_bridge = self.root / "new-bridge"
        new_notice = self.notice(new_bridge, "new")
        entered, release, reloaded = (threading.Event() for _ in range(3))

        def send(**kwargs):
            entered.set()
            release.wait(3)

        def reload_plugin():
            self.plugin.init_plugin({**self.config, "notify_results": True, "bridge_dir": str(new_bridge)})
            reloaded.set()

        with patch.object(self.plugin, "post_message", side_effect=send):
            poll = threading.Thread(target=self.plugin._poll_outbox)
            reload = threading.Thread(target=reload_plugin)
            try:
                poll.start()
                self.assertTrue(entered.wait(1))
                reload.start()
                self.assertTrue(reloaded.wait(0.5))
            finally:
                release.set()
                poll.join(2)
                if reload.ident is not None:
                    reload.join(2)
        self.assertTrue(new_notice.exists(), "old generation touched the new bridge")
        self.assertEqual(1, len(list((self.bridge / "outbox").glob(".*.processing"))))
        self.plugin._poll_outbox()
        self.assertEqual(["new"], [item["text"] for item in self.plugin.messages])

    def test_notification_receipt_requires_persisted_ack(self):
        self.plugin.init_plugin({**self.config, "notify_results": True})
        self.notice()
        with patch.object(self.plugin, "save_data", return_value=False):
            self.plugin._poll_outbox()
        self.assertEqual(1, len(list((self.bridge / "outbox").glob(".*.processing"))))
        self.assertEqual(1, len(self.plugin.messages))
        self.plugin._poll_outbox()
        self.assertEqual(1, len(self.plugin.messages), "retrying persistence must not send again in the same process")
        self.assertEqual([], list((self.bridge / "outbox").glob(".*.processing")))

    def test_notification_exception_does_not_quarantine_valid_json(self):
        self.plugin.init_plugin({**self.config, "notify_results": True})
        self.notice()
        with patch.object(self.plugin, "post_message", side_effect=ValueError("channel unavailable")):
            self.plugin._poll_outbox()
        self.assertEqual(1, len(list((self.bridge / "outbox").glob(".*.processing"))))
        self.assertEqual([], list((self.bridge / "outbox").glob("*.invalid")))

    def test_terminal_receipts_survive_cleanup_and_restart(self):
        for status in ("done", "error"):
            reply = self.plugin.submit_command(self.module.CommandRequest("pause"))
            result_path = self.result(reply, status)
            self.assertEqual(status, self.plugin.get_result(reply.id).status)
            restarted = self.restart()
            result_path.unlink()
            self.assertEqual(status, restarted.get_result(reply.id).status)

    def test_terminal_history_save_failure_is_retried(self):
        reply = self.plugin.submit_command(self.module.CommandRequest("pause"))
        result_path = self.result(reply)
        with patch.object(self.plugin, "save_data", return_value=False):
            self.assertEqual("done", self.plugin.get_result(reply.id).status)
        self.tick()
        restarted = self.restart()
        result_path.unlink()
        self.assertEqual("done", restarted.get_result(reply.id).status)

    def test_unknown_or_expired_receipt_is_not_pending(self):
        self.assertEqual("unknown", self.plugin.get_result("f" * 32).status)
        reply = self.plugin.submit_command(self.module.CommandRequest("pause"))
        self.plugin._commands[-1]["created_at"] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        self.assertEqual("expired", self.plugin.get_result(reply.id).status)

    def test_seen_terminal_receipt_does_not_rewrite_database_each_poll(self):
        reply = self.plugin.submit_command(self.module.CommandRequest("pause"))
        self.result(reply)
        with patch.object(self.plugin, "save_data", wraps=self.plugin.save_data) as save:
            self.assertEqual("done", self.plugin.get_result(reply.id).status)
            self.assertEqual("done", self.plugin.get_result(reply.id).status)
            self.tick()
            self.plugin.get_status()
        self.assertEqual(1, save.call_count)

    def test_queue_read_failure_cannot_overwrite_persisted_events(self):
        self.fail_delivery()
        restarted = self.module.GDriveArchiverBridge()
        restarted.data = copy.deepcopy(self.plugin.data)
        original = restarted.get_data

        def unavailable(key):
            if key == "pending_jobs":
                raise OSError("database unavailable")
            return original(key)

        with patch.object(restarted, "get_data", side_effect=unavailable), patch.object(restarted, "save_data", wraps=restarted.save_data) as save:
            restarted.init_plugin(self.config)
            self.tick(restarted)
            self.assertFalse(any(call.args[0] == "pending_jobs" for call in save.call_args_list))
        self.tick(restarted)
        self.assertEqual(1, len(list((self.bridge / "inbox").glob("*.json"))))
        self.assertEqual({}, restarted.get_data("pending_jobs"))

    def test_old_pending_control_cannot_read_another_bridge_receipt(self):
        reply = self.plugin.submit_command(self.module.CommandRequest("pause"))
        new_bridge = self.root / "new-bridge"
        (new_bridge / "results").mkdir(parents=True)
        (new_bridge / "results" / (reply.id + ".json")).write_text(json.dumps(
            {"version": 2, "id": reply.id, "action": "pause", "status": "done", "message": "unrelated"}), encoding="utf-8")
        self.plugin.init_plugin({**self.config, "bridge_dir": str(new_bridge)})
        self.assertEqual("unknown", self.plugin.get_result(reply.id).status)

    def test_bridge_path_validation_failure_is_retained_for_safe_retry(self):
        with patch.object(self.plugin, "_scoped_bridge_path", side_effect=ValueError("bridge path escaped")):
            self.plugin.enqueue_transfer(self.event())
        self.assertFalse((self.bridge / "inbox").exists())
        self.assertEqual(1, len(self.plugin.get_data("pending_jobs") or {}))
        self.tick()
        self.assertEqual(1, len(list((self.bridge / "inbox").glob("*.json"))))

    def test_notification_and_retry_are_separate_host_jobs(self):
        self.plugin.init_plugin({**self.config, "notify_results": True})
        services = self.plugin.get_service()
        self.assertEqual(2, len(services))
        self.assertEqual(2, len({item["id"] for item in services}))
        self.assertIn(self.plugin._poll_outbox, [item["func"] for item in services])

    def test_same_media_name_in_two_scopes_keeps_two_pending_records(self):
        self.fail_delivery()
        new_media = self.root / "new-media"
        new_folder = new_media / "Movies" / "Example"
        new_folder.mkdir(parents=True)
        new_bridge = self.root / "new-bridge"
        new_bridge.mkdir()
        self.plugin.init_plugin({**self.config, "library_root": str(new_media), "bridge_dir": str(new_bridge)})
        event = types.SimpleNamespace(event_data={"transferinfo": {"target_diritem": {"path": str(new_folder)}}})
        with patch.object(self.plugin, "_atomic_write", side_effect=OSError("bridge unavailable")):
            self.plugin.enqueue_transfer(event)
        self.assertEqual(2, len(self.plugin.get_data("pending_jobs")))
        self.tick()
        self.assertEqual(1, len(list((new_bridge / "inbox").glob("*.json"))))
        self.assertEqual(1, len(self.plugin.get_data("pending_jobs")))
        self.plugin.init_plugin(self.config)
        self.tick()
        self.assertEqual(1, len(list((self.bridge / "inbox").glob("*.json"))))
        self.assertEqual({}, self.plugin.get_data("pending_jobs"))


class V2ReliabilityTests(ReliabilityContract, unittest.TestCase):
    version = 2


class V3ReliabilityTests(ReliabilityContract, unittest.TestCase):
    version = 3


if __name__ == "__main__":
    unittest.main()
