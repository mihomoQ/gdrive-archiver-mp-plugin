"""POSIX title colons are names; Windows drive syntax must remain rejected."""

import types
import unittest
from pathlib import PurePosixPath
from unittest.mock import patch

from test_bridge import load_plugin


class ExistingPosixPath(PurePosixPath):
    def resolve(self):
        return self

    def is_dir(self):
        return True


class ColonPathTests(unittest.TestCase):
    def check_path(self, version, platform, relative):
        module = load_plugin(version)
        plugin = module.GDriveArchiverBridge()
        plugin._library_root = "/media"
        with patch.object(module, "Path", ExistingPosixPath), patch.object(module, "os", types.SimpleNamespace(name=platform)):
            return plugin._relative_directory(relative)

    def test_unix_movie_colon_is_preserved_in_both_versions(self):
        for version in (2, 3):
            for relative in (
                "欧美电影/Example: Winter Summit (2026) {tmdbid=12345}",
                "电影/欧美电影/Example: Winter Summit (2026) {tmdbid=12345}",
                "外语电影/Part 1: Arrival",
            ):
                with self.subTest(version=version, relative=relative):
                    self.assertEqual(relative, self.check_path(version, "posix", relative))

    def test_windows_title_colon_is_still_rejected(self):
        for version in (2, 3):
            with self.subTest(version=version), self.assertRaises(ValueError):
                self.check_path(version, "nt", "欧美电影/Title: subtitle")

    def test_drive_unc_and_escape_syntax_rejected_on_both_platforms(self):
        invalid = (
            "C:/media/Film", "C:media/Film", "c:media/Film", "Z:/Film/Extra",
            "//server/share/Film", "\\\\server\\share\\Film", "欧美电影\\Film",
            "/media/欧美电影/Film", "../欧美电影/Film", "欧美电影/../Film",
            "欧美电影/./Film", "欧美电影/Film\x00bad", "欧美电影/Film\nbad",
        )
        for version in (2, 3):
            for platform in ("posix", "nt"):
                for relative in invalid:
                    with self.subTest(version=version, platform=platform, relative=relative), self.assertRaises(ValueError):
                        self.check_path(version, platform, relative)

    def test_ordinary_relative_paths_unchanged(self):
        for version in (2, 3):
            for platform in ("posix", "nt"):
                with self.subTest(version=version, platform=platform):
                    self.assertEqual("欧美电影/Film", self.check_path(version, platform, "欧美电影/Film"))


if __name__ == "__main__":
    unittest.main()
