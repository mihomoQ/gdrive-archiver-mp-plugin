"""Offline contracts for adding archiver upload speed to MP's downloader totals."""

import copy
import json
import tempfile
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from test_bridge import load_plugin


class _DashboardChainFixture:
    """Source-derived host dispatch contract, without an installed MP runtime.

    V2: MoviePilot@1528176beacc0f9f8d8d9cbf1adf048b0da67b5c
        app/chain/dashboard.py + app/chain/__init__.py (run_module).
    V3: MoviePilot@7584d4b9fcca7effdd8c1954eaab79e71c7ef56a
        app/chain/dashboard.py + app/runtime/extensions/module/dispatcher.py
        (_dispatch/_aggregate_provider_result), contracts.py ORDERED_LIST_MERGE.

    Both invoke the plugin extension before system providers. A nonempty scalar
    plugin return short-circuits the system; a list is extended with system rows.
    This fixture preserves that observable behavior, not unrelated host services.
    """

    def __init__(self, plugin, system_rows):
        self.modules = plugin.get_module()
        self.system_rows = system_rows
        self.system_calls = 0

    def downloader_info(self, downloader=None):
        plugin_result = self.modules["downloader_info"](downloader=downloader)
        if plugin_result and not isinstance(plugin_result, list):
            return plugin_result
        self.system_calls += 1
        rows = plugin_result if isinstance(plugin_result, list) else []
        selected = [row for name, row in self.system_rows.items() if not downloader or name == downloader]
        rows.extend(selected)
        return rows


class SpeedContract:
    def setUp(self):
        self.module = load_plugin(self.version)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.bridge = Path(temporary.name)
        self.plugin = self.module.GDriveArchiverBridge()
        self.config = {"enabled": True, "library_root": "/media", "bridge_dir": str(self.bridge)}
        self.plugin.init_plugin(self.config)

    def stamp(self, age=0):
        return (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()

    def upload(self, speed=125.5, phase="uploading", age=0, **extra):
        return {"job_id": "a" * 64, "file": "video.mkv", "phase": phase, "size": 1000,
                "bytes": 250, "speed": speed, "updated_at": self.stamp(age), **extra}

    def status(self, age=0, **extra):
        raw = {"version": 2, "running": True, "paused": False, "updated_at": self.stamp(age),
               "uploads": [self.upload()], "speed_bytes_per_second": 999_999_999,
               "jobs": [], "accounts": [], "job_counts": {}, **extra}
        (self.bridge / "status.json").write_text(json.dumps(raw), encoding="utf-8")
        return raw

    def provider(self):
        self.assertTrue(callable(getattr(self.plugin, "get_module", None)), "plugin must expose the official module extension")
        modules = self.plugin.get_module()
        self.assertIsInstance(modules, dict)
        self.assertIn("downloader_info", modules)
        self.assertEqual(self.plugin.downloader_info, modules["downloader_info"])
        return modules["downloader_info"]

    def contribution(self, *args, **kwargs):
        rows = self.provider()(*args, **kwargs)
        self.assertIsInstance(rows, list, "a scalar DownloaderInfo would replace, rather than merge with, system providers")
        for row in rows:
            self.assertIsInstance(row, self.module.DownloaderInfo)
            for field in ("download_speed", "download_size", "upload_size", "free_space"):
                self.assertEqual(0, getattr(row, field), f"archiver must not fabricate {field}")
        return rows

    def test_registers_module_and_only_adds_fresh_uploading_speed(self):
        self.status(uploads=[self.upload(100.25), self.upload(249.75),
                             self.upload(900, phase="moving"), self.upload(800, phase="preparing"),
                             self.upload(700, phase="checking_size"), self.upload(600, age=16)])
        rows = self.contribution()
        self.assertEqual(1, len(rows))
        self.assertEqual(350, rows[0].upload_speed)
        self.assertEqual(self.plugin.get_status().speed_bytes_per_second, rows[0].upload_speed)

    def test_raw_total_does_not_substitute_for_active_uploads(self):
        self.status(uploads=[], speed_bytes_per_second=1234567, job_counts={"uploading": 14})
        self.assertEqual([], self.contribution())

    def test_paused_dispatch_still_reports_inflight_uploads(self):
        self.status(paused=True, uploads=[self.upload(222)])
        self.assertEqual(222, self.contribution()[0].upload_speed)

    def test_offline_stale_future_or_bad_snapshot_contributes_nothing(self):
        for extra in ({"age": 16}, {"running": False}, {"updated_at": "invalid"},
                      {"updated_at": self.stamp(-60)}, {"version": 1}, {"version": True}):
            with self.subTest(extra=extra):
                self.status(**extra)
                self.assertEqual([], self.contribution())
        for raw in ("[]", "not JSON", '{"version":2}', "x" * (2 * 1024 * 1024 + 1)):
            (self.bridge / "status.json").write_text(raw, encoding="utf-8")
            self.assertEqual([], self.contribution())
        (self.bridge / "status.json").unlink()
        self.assertEqual([], self.contribution())

    def test_invalid_upload_rates_cannot_poison_the_total(self):
        invalid = (float("nan"), float("inf"), float("-inf"), -1, True, "250", None, 10 ** 1000)
        self.status(uploads=[self.upload(value) for value in invalid] + [self.upload(42)])
        self.assertEqual(42, self.contribution()[0].upload_speed)
        self.status(uploads=[self.upload(value) for value in invalid])
        self.assertEqual([], self.contribution())

    def test_named_downloader_queries_never_receive_archiver_rows(self):
        self.status()
        with patch.object(self.plugin, "_read_json", side_effect=AssertionError("named queries must not read bridge")):
            for name in ("qbittorrent", "Transmission", "my-download-server"):
                self.assertEqual([], self.contribution(downloader=name))
        self.assertEqual(125.5, self.contribution(downloader="")[0].upload_speed)
        self.assertEqual(125.5, self.contribution(downloader=None)[0].upload_speed)

    def test_disabled_stopped_or_opted_out_plugins_contribute_nothing(self):
        self.status()
        for config in ({**self.config, "enabled": False}, {**self.config, "aggregate_upload_speed": False}):
            self.plugin.init_plugin(config)
            # Some hosts omit disabled registrations; invoke the provider itself
            # to verify it cannot leak stale rates after an already-bound call.
            self.assertEqual([], self.plugin.downloader_info())
        self.plugin.init_plugin(self.config)
        bound_provider = self.provider()
        self.plugin.stop_service()
        self.assertEqual([], bound_provider())

    def test_refresh_recomputes_without_accumulating_or_mutating_old_rows(self):
        self.status(uploads=[self.upload(90)])
        first = self.contribution()
        saved = copy.deepcopy(first)
        self.assertEqual(90, self.contribution()[0].upload_speed)
        self.status(uploads=[self.upload(25)])
        self.assertEqual(25, self.contribution()[0].upload_speed)
        self.assertEqual(saved, first)
        self.assertEqual(90, first[0].upload_speed)

    def test_rate_query_is_independent_of_command_result_files(self):
        self.status()
        self.plugin._commands = [{"id": "a" * 32, "action": "pause", "status": "pending"}]
        with patch.object(self.plugin, "get_result", side_effect=AssertionError("speed query must not read command receipts")):
            self.assertEqual(125.5, self.contribution()[0].upload_speed)

    def test_host_chain_list_merge_preserves_system_stats_across_refreshes(self):
        self.status(uploads=[self.upload(350)])
        self.provider()  # Verify official registration before constructing the fixture.
        model = self.module.DownloaderInfo
        systems = {
            "qbittorrent": model(download_speed=100, upload_speed=10, download_size=10000, upload_size=20000, free_space=90000),
            "Transmission": model(download_speed=300, upload_speed=15, download_size=30000, upload_size=40000, free_space=80000),
        }
        original = {name: asdict(row) for name, row in systems.items()}
        chain = _DashboardChainFixture(self.plugin, systems)
        for _ in range(3):
            combined = chain.downloader_info()
            self.assertIsInstance(combined, list)
            self.assertEqual(3, len(combined))
            self.assertEqual(375, sum(row.upload_speed for row in combined))
            self.assertEqual(400, sum(row.download_speed for row in combined))
            self.assertEqual(40000, sum(row.download_size for row in combined))
            self.assertEqual(60000, sum(row.upload_size for row in combined))
            self.assertEqual(170000, sum(row.free_space for row in combined))
            self.assertIs(systems["qbittorrent"], combined[1])
            self.assertIs(systems["Transmission"], combined[2])
        self.assertEqual(3, chain.system_calls)
        self.assertEqual(original, {name: asdict(row) for name, row in systems.items()})
        self.assertEqual([systems["qbittorrent"]], chain.downloader_info("qbittorrent"))
        self.assertEqual([], chain.downloader_info("not-present"))
        self.status(uploads=[])
        self.assertEqual(list(systems.values()), chain.downloader_info())
        self.assertEqual(original, {name: asdict(row) for name, row in systems.items()})


class V2SpeedTests(SpeedContract, unittest.TestCase):
    version = 2


class V3SpeedTests(SpeedContract, unittest.TestCase):
    version = 3


if __name__ == "__main__":
    unittest.main()
