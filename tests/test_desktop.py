from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scooters_codex_telegram_bot.desktop import _app_bundle_for_executable


class MacosBundleTests(unittest.TestCase):
    def test_finds_bundle_from_packaged_executable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "CodexTelegramBot.app"
            executable = bundle / "Contents" / "MacOS" / "CodexTelegramBot"
            executable.parent.mkdir(parents=True)
            (bundle / "Contents" / "Info.plist").write_bytes(b"plist")

            self.assertEqual(_app_bundle_for_executable(executable), bundle)

    def test_ignores_standalone_executable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "CodexTelegramBot"

            self.assertIsNone(_app_bundle_for_executable(executable))


if __name__ == "__main__":
    unittest.main()
