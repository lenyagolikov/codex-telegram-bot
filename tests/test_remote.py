from __future__ import annotations

import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scooters_codex_telegram_bot.remote import (
    RemoteServiceManager,
    RemoteSettings,
)
from scooters_codex_telegram_bot.service import ServiceError


class RemoteSettingsTests(unittest.TestCase):
    def test_parses_ssh_target_and_defaults(self) -> None:
        settings = RemoteSettings.from_mapping(
            {
                "REMOTE_SSH_HOST": "vm.example.net",
                "REMOTE_SSH_USER": "developer",
            }
        )

        self.assertEqual(settings.target, "developer@vm.example.net")
        self.assertEqual(settings.port, 22)
        self.assertEqual(settings.python_bin, "/usr/bin/python3")

    def test_rejects_option_like_host(self) -> None:
        with self.assertRaisesRegex(ServiceError, "SSH-хост"):
            RemoteSettings.from_mapping({"REMOTE_SSH_HOST": "-oProxyCommand=bad"})


class RemoteServiceManagerTests(unittest.TestCase):
    def _settings(self) -> RemoteSettings:
        return RemoteSettings.from_mapping(
            {
                "REMOTE_SSH_HOST": "vm.example.net",
                "REMOTE_SSH_USER": "developer",
                "REMOTE_INSTALL_DIR": "~/.local/share/bot",
                "REMOTE_CODEX_CWD": "~/arcadia",
            }
        )

    def test_runtime_archive_contains_importable_package_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "__init__.py").write_text("VERSION = 1\n", encoding="utf-8")
            (root / "approvals.py").write_text("", encoding="utf-8")
            (root / "gui.py").write_text("", encoding="utf-8")
            (root / "desktop.py").write_text("", encoding="utf-8")
            (root / "remote.py").write_text("", encoding="utf-8")
            (root / "secrets.py").write_text("", encoding="utf-8")
            assets = root / "assets"
            assets.mkdir()
            (assets / "app-icon.png").write_bytes(b"not-an-image")
            (root / "ignored.txt").write_text("ignored\n", encoding="utf-8")
            manager = RemoteServiceManager(
                self._settings(), ssh_bin=sys.executable, runtime_root=root
            )

            archive_path = manager._runtime_archive()
            try:
                with tarfile.open(archive_path, "r:gz") as archive:
                    names = archive.getnames()
            finally:
                archive_path.unlink(missing_ok=True)

        self.assertIn("scooters_codex_telegram_bot/__init__.py", names)
        self.assertIn("scooters_codex_telegram_bot/approvals.py", names)
        self.assertNotIn("scooters_codex_telegram_bot/ignored.txt", names)
        self.assertNotIn("scooters_codex_telegram_bot/gui.py", names)
        self.assertNotIn("scooters_codex_telegram_bot/desktop.py", names)
        self.assertNotIn("scooters_codex_telegram_bot/remote.py", names)
        self.assertNotIn("scooters_codex_telegram_bot/secrets.py", names)
        self.assertNotIn("scooters_codex_telegram_bot/assets/app-icon.png", names)

    def test_token_is_sent_in_stdin_and_never_in_remote_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "__init__.py").write_text("", encoding="utf-8")
            manager = RemoteServiceManager(
                self._settings(), ssh_bin=sys.executable, runtime_root=root
            )
            remote_calls: list[tuple[list[str], str | None]] = []

            def remote(arguments, *, input_text=None, check=True, timeout=30):
                remote_calls.append((list(arguments), input_text))
                return mock.Mock(returncode=0, stdout="", stderr="")

            with (
                mock.patch.object(
                    manager, "_remote_temp_path", return_value="/tmp/runtime.tgz"
                ),
                mock.patch.object(manager, "_upload"),
                mock.patch.object(manager, "_remote", side_effect=remote),
            ):
                manager.install_and_start(
                    {
                        "TELEGRAM_BOT_TOKEN": "super-secret-token",
                        "CODEX_BIN": "codex",
                        "CODEX_CWD": "~/arcadia",
                    }
                )

        flattened_arguments = " ".join(
            value for arguments, _ in remote_calls for value in arguments
        )
        self.assertNotIn("super-secret-token", flattened_arguments)
        payloads = [value for _, value in remote_calls if value]
        self.assertEqual(len(payloads), 1)
        self.assertIn("super-secret-token", json.loads(payloads[0])["config"])

    def test_scp_uses_uppercase_port_option(self) -> None:
        manager = RemoteServiceManager(self._settings(), ssh_bin=sys.executable)
        arguments = manager._scp_connection_arguments()

        self.assertIn("-P", arguments)
        self.assertNotIn("-p", arguments)


if __name__ == "__main__":
    unittest.main()
