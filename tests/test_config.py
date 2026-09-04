from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scooters_codex_telegram_bot.config import (
    Config,
    ConfigError,
    default_config_path,
    default_log_dir,
    default_state_path,
    read_dotenv,
    write_dotenv,
)


class ConfigTests(unittest.TestCase):
    def test_explicit_config_file_is_loaded_without_overriding_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "bot.env"
            config_path.write_text(
                "TELEGRAM_BOT_TOKEN=from-file\n"
                "TELEGRAM_ALLOWED_USER_IDS=101,202\n"
                f"CODEX_BIN={sys.executable}\n"
                f"CODEX_CWD={root}\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"TELEGRAM_BOT_TOKEN": "from-environment"},
                clear=True,
            ):
                config = Config.from_environment(config_path)

            self.assertEqual(config.telegram_token, "from-environment")
            self.assertEqual(config.allowed_user_ids, frozenset({101, 202}))
            self.assertEqual(config.codex_cwd, root.resolve())

    def test_default_linux_paths_follow_xdg_variables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("scooters_codex_telegram_bot.config.sys.platform", "linux"),
                patch.dict(
                    os.environ,
                    {
                        "XDG_CONFIG_HOME": str(root / "config"),
                        "XDG_STATE_HOME": str(root / "state"),
                    },
                    clear=True,
                ),
            ):
                self.assertEqual(
                    default_config_path(),
                    root / "config" / "scooters-codex-telegram-bot" / ".env",
                )
                self.assertEqual(
                    default_state_path(),
                    root / "state" / "scooters-codex-telegram-bot" / "state.sqlite3",
                )

    def test_invalid_ip_family_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(
                os.environ,
                {
                    "TELEGRAM_BOT_TOKEN": "test-token",
                    "CODEX_BIN": sys.executable,
                    "CODEX_CWD": str(root),
                    "TELEGRAM_IP_FAMILY": "satellite",
                },
                clear=True,
            ), self.assertRaisesRegex(ConfigError, "TELEGRAM_IP_FAMILY"):
                Config.from_environment(root / "missing.env")

    def test_dotenv_round_trip_supports_spaces_and_quotes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings" / ".env"
            write_dotenv(
                path,
                {
                    "TELEGRAM_BOT_TOKEN": 'token with "quotes"',
                    "TELEGRAM_ALLOWED_USER_IDS": "101,202",
                    "CODEX_CWD": "/tmp/project with spaces",
                    "UNKNOWN_VALUE": "not-written",
                },
            )

            values = read_dotenv(path)

            self.assertEqual(values["TELEGRAM_BOT_TOKEN"], 'token with "quotes"')
            self.assertEqual(values["CODEX_CWD"], "/tmp/project with spaces")
            self.assertNotIn("UNKNOWN_VALUE", values)
            if sys.platform != "win32":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_from_mapping_does_not_mutate_process_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            values = {
                "TELEGRAM_BOT_TOKEN": "test-token",
                "CODEX_BIN": sys.executable,
                "CODEX_CWD": str(root),
            }
            with patch.dict(os.environ, {}, clear=True):
                config = Config.from_mapping(values)
                self.assertNotIn("TELEGRAM_BOT_TOKEN", os.environ)

            self.assertEqual(config.telegram_token, "test-token")

    def test_default_macos_log_path(self) -> None:
        with patch("scooters_codex_telegram_bot.config.sys.platform", "darwin"):
            self.assertEqual(
                default_log_dir(),
                Path.home()
                / "Library"
                / "Logs"
                / "scooters-codex-telegram-bot",
            )


if __name__ == "__main__":
    unittest.main()
