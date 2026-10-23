"""Dashboard behavior and native-renderer contracts for both MoviePilot hosts."""

import html
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from test_bridge import load_plugin


def nodes(value):
    if isinstance(value, list):
        for item in value:
            yield from nodes(item)
    elif isinstance(value, dict):
        if "component" in value:
            yield value
        yield from nodes(value.get("content", []))


def texts(value):
    result = []
    for node in nodes(value):
        for text in (node.get("text"), *(node.get("props", {}).get(key) for key in ("text", "title", "subtitle"))):
            if isinstance(text, str):
                result.append(html.unescape(text))
    return result


def readable(value):
    return " | ".join(texts(value))


class DashboardContract:
    def setUp(self):
        self.module = load_plugin(self.version)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bridge = self.root / "bridge"
        self.bridge.mkdir()
        self.plugin = self.module.GDriveArchiverBridge()
        self.plugin.init_plugin({"enabled": True, "library_root": str(self.root / "media"), "bridge_dir": str(self.bridge)})

    def status(self, age=0, **overrides):
        raw = {"version": 2, "running": True, "paused": False,
               "updated_at": (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat(),
               "speed_bytes_per_second": 999999, "upload_workers": 3,
               "jobs": [], "uploads": [], "accounts": [], "job_counts": {}, **overrides}
        (self.bridge / "status.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        return raw

    def upload(self, **overrides):
        return {"job_id": "a" * 64, "relative_path": "Movies/Example", "file": "video.mkv",
                "account": "account.conf/personal", "phase": "uploading", "size": 1000, "bytes": 250,
                "speed": 125, "eta_seconds": 6, "updated_at": datetime.now(timezone.utc).isoformat(), **overrides}

    def widget(self):
        columns, options, content = self.plugin.get_dashboard("archive")
        self.assertEqual(12, columns["cols"])
        self.assertEqual(3, options["refresh"])
        return content

    def assert_metric(self, content, label, expected):
        cards = [node for node in nodes(content) if node["component"] == "VCard" and label in texts(node)]
        self.assertTrue(cards, f"missing metric card: {label}")
        card = min(cards, key=lambda node: len(list(nodes(node))))
        self.assertIn(str(expected), texts(card), f"wrong {label} value in {readable(card)}")

    def test_pause_uses_warning_status_not_success(self):
        self.status(paused=True, job_counts={"uploading": 14})
        content = self.widget()
        indicators = [node for node in nodes(content) if node["component"] == "VChip" and "暂停派发" in readable(node)]
        self.assertTrue(indicators, "pause must be visible in the status indicator")
        self.assertTrue(all(node.get("props", {}).get("color") in {"warning", "amber", "orange"} for node in indicators))
        self.assert_metric(content, "活动文件", 0)
        self.assert_metric(content, "待处理任务", 14)

    def test_raw_uploading_jobs_do_not_fabricate_active_transfers(self):
        self.status(job_counts={"uploading": 14}, uploads=[], accounts=[{"name": "one", "status": "healthy"}])
        state = self.plugin.get_status()
        self.assertEqual(0, state.speed_bytes_per_second)
        content = self.widget()
        self.assert_metric(content, "活动文件", 0)
        self.assert_metric(content, "待处理任务", 14)
        self.assertIn("等待任务", readable(content))
        self.assertNotIn("正在归档", readable(content))

    def test_offline_or_stale_snapshot_has_no_live_activity_or_eta(self):
        for overrides in ({"age": 16}, {"running": False}):
            with self.subTest(overrides=overrides):
                self.status(uploads=[self.upload()], **overrides)
                state = self.plugin.get_status()
                self.assertFalse(state.online)
                self.assertEqual(0, state.speed_bytes_per_second)
                self.assertFalse(state.uploads[0].active)
                self.assertEqual(0, state.uploads[0].speed)
                self.assertEqual(-1, state.uploads[0].eta_seconds)
                content = self.widget()
                self.assert_metric(content, "活动文件", 0)
                self.assertIn("归档器离线", readable(content))
                self.assertNotIn("正在归档", readable(content))

    def test_fresh_heartbeat_does_not_revive_old_or_preparing_uploads(self):
        self.status(uploads=[self.upload(updated_at="2000-01-01T00:00:00Z"), self.upload(phase="preparing", job_id="b" * 64)])
        state = self.plugin.get_status()
        self.assertTrue(state.online)
        self.assertTrue(all(not item.active and item.speed == 0 and item.eta_seconds == -1 for item in state.uploads))
        self.assertEqual(0, state.speed_bytes_per_second)
        self.assert_metric(self.widget(), "活动文件", 0)

    def test_pending_jobs_use_all_noncomplete_states_minus_actual_active_jobs(self):
        self.status(job_counts={"uploading": 14, "queued": 2, "failed": 3, "complete": 7,
                                "waiting_account": 4, "waiting_stable": 5, "retrying": 6},
                    uploads=[self.upload(), self.upload(file="second.mkv")])
        content = self.widget()
        self.assert_metric(content, "活动文件", 2)
        # Two active files belong to one job, so subtract one active job only.
        self.assert_metric(content, "待处理任务", 33)

    def test_partial_remaining_work_includes_every_unfinished_job_state(self):
        states = ["queued", "waiting_stable", "retrying", "waiting_account", "failed", "uploading"]
        jobs = [{"id": f"{index:064x}", "relative_path": f"Movies/Item-{index}", "status": state,
                 "files_total": 10, "files_done": 3, "bytes_total": 1000, "bytes_done": 300}
                for index, state in enumerate(states)]
        jobs.append({"id": "f" * 64, "relative_path": "Movies/Finished", "status": "complete",
                     "files_total": 100, "files_done": 100, "bytes_total": 10000, "bytes_done": 10000})
        self.status(jobs=jobs, job_counts={**dict.fromkeys(states, 1), "complete": 1},
                    uploads=[self.upload(job_id=f"{5:064x}", size=100, bytes=50)])
        content = readable(self.widget())
        self.assertIn("已列任务待传：42 个文件", content)
        self.assertIn(self.plugin._size(4150), content)

    def test_unknown_eta_is_dash_instead_of_zero_or_negative_seconds(self):
        for eta in (-1, None, 10 ** 1000, float("nan"), float("inf"), True, False):
            with self.subTest(eta=eta):
                self.status(uploads=[self.upload(eta_seconds=eta)])
                state = self.plugin.get_status()
                self.assertEqual(-1, state.uploads[0].eta_seconds)
                content = readable(self.widget())
                self.assertIn("—", content)
                self.assertNotIn("ETA 0s", content)
                self.assertNotIn("ETA -1s", content)
                self.assertNotIn("约 0 秒", content)
                self.assertEqual("—", self.plugin._eta(eta))

    def test_home_widget_is_bounded_and_distinct_from_detail(self):
        uploads = [self.upload(file=f"video-{index}.mkv", job_id=f"{index:064x}") for index in range(8)]
        accounts = [{"name": f"account-{index}.conf/personal", "status": "healthy", "budget_bytes": 750000000000,
                     "remaining_bytes": 500000000000} for index in range(8)]
        jobs = [{"id": f"{index:064x}", "relative_path": f"Movies/Task-{index}", "status": "queued"} for index in range(20)]
        events = [{"at": datetime.now(timezone.utc).isoformat(), "status": "error", "message": f"EVENT_ONLY_{index}"} for index in range(5)]
        self.status(uploads=uploads, accounts=accounts, jobs=jobs, recent_events=events, job_counts={"queued": 20})
        home, detail = self.widget(), self.plugin.get_page()
        self.assertNotEqual(home, detail)
        home_text, detail_text = readable(home), readable(detail)
        self.assertLessEqual(sum(f"video-{i}.mkv" in home_text for i in range(8)), 3)
        self.assertLessEqual(sum(f"account-{i}.conf/personal" in home_text for i in range(8)), 3)
        self.assertNotIn("EVENT_ONLY_0", home_text)
        self.assertIn("EVENT_ONLY_0", detail_text)
        self.assertIn("Movies/Task-19", detail_text)

    def test_native_component_schema_and_progress_are_valid(self):
        self.status(uploads=[self.upload()], accounts=[{"name": "account.conf/personal", "status": "healthy",
                    "budget_bytes": 1000, "reserved_bytes": 250, "remaining_bytes": 750}])
        for content in (self.widget(), self.plugin.get_page()):
            rendered = list(nodes(content))
            components = {node["component"] for node in rendered}
            self.assertTrue({"VCard", "VChip", "VProgressLinear"}.issubset(components))
            progress = []
            for node in rendered:
                self.assertIsInstance(node["component"], str)
                self.assertIsInstance(node.get("props", {}), dict)
                if "content" in node:
                    self.assertIsInstance(node["content"], list)
                if node["component"] == "VProgressLinear":
                    props = node.get("props", {})
                    value = props.get("model-value", props.get("modelValue"))
                    self.assertIsInstance(value, (int, float))
                    self.assertGreaterEqual(value, 0)
                    self.assertLessEqual(value, 100)
                    progress.append(value)
            self.assertIn(25, progress)

    def test_untrusted_fields_use_native_literal_text_not_html(self):
        payload = '<img src=x onerror=alert(1)> & {{7*7}} "quoted"'
        self.status(uploads=[self.upload(file=payload)], accounts=[{"name": payload, "status": "error", "last_error": payload}],
                    jobs=[{"relative_path": payload, "status": "failed", "last_error": payload}],
                    recent_events=[{"status": "error", "message": payload}], warnings=[payload])
        for content in (self.widget(), self.plugin.get_page()):
            fields = [node["text"] for node in nodes(content) if isinstance(node.get("text"), str)]
            # Official V2/V3 render config.text with Vue interpolation and
            # reserve config.html for v-html. Preescaping text would visibly
            # corrupt names containing ampersands, quotes, or angle brackets.
            self.assertTrue(any("<img" in item and "&" in item for item in fields))
            for value in fields:
                self.assertNotIn("{{7*7}}", value)
                self.assertNotIn("&lt;img", value)
            for node in nodes(content):
                self.assertNotIn("html", node)
                self.assertNotIn("innerHTML", node.get("props", {}))
                self.assertNotIn("outerHTML", node.get("props", {}))

    def test_homepage_feedback_resets_without_dispatching_control(self):
        self.plugin.init_plugin({"enabled": True, "library_root": str(self.root / "media"),
                                 "bridge_dir": str(self.bridge), "home_busy": True,
                                 "home_error": True, "home_message": "old feedback"})
        saved = self.plugin.configs[-1]
        self.assertFalse(saved["home_busy"])
        self.assertFalse(saved["home_error"])
        self.assertEqual("", saved["home_message"])
        self.assertFalse((self.bridge / "commands").exists())
        form, defaults = self.plugin.get_form()
        self.assertEqual("", defaults["control_action"])
        buttons = [n for n in nodes(form) if n.get("component") == "VBtn"]
        self.assertTrue(any(n.get("text") == "显示到首页" for n in buttons))



class V2DashboardTests(DashboardContract, unittest.TestCase):
    version = 2


class V3DashboardTests(DashboardContract, unittest.TestCase):
    version = 3


if __name__ == "__main__":
    unittest.main()
