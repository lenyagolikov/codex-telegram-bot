from __future__ import annotations

import os
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_telegram_bot.service import (
    LEGACY_MACOS_SERVICE_LABELS,
    MACOS_SERVICE_LABEL,
    ServiceManager,
    linux_unit_text,
    macos_plist_bytes,
    service_environment_path,
)


class ServiceDefinitionTests(unittest.TestCase):
    def test_linux_unit_uses_absolute_config_and_service_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unit = linux_unit_text(
                ("/opt/Codex Telegram Bot/bin",),
                root / "settings" / ".env",
                root / "project with spaces",
                root / "logs",
                "/opt/homebrew/bin:/usr/bin:/bin",
            )

        self.assertIn('ExecStart="/opt/Codex Telegram Bot/bin" "--service"', unit)
        self.assertIn("WorkingDirectory=", unit)
        self.assertIn(r"project\x20with\x20spaces", unit)
        self.assertNotIn('WorkingDirectory="', unit)
        self.assertIn("Restart=always", unit)
        self.assertIn(
            'Environment="PATH=/opt/homebrew/bin:/usr/bin:/bin"', unit
        )
        self.assertIn("StandardError=append:", unit)

    def test_macos_plist_contains_background_arguments_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = plistlib.loads(
                macos_plist_bytes(
                    ("/Applications/CodexTelegramBot",),
                    root / ".env",
                    root / "workspace",
                    root / "logs",
                    "/opt/homebrew/bin:/usr/bin:/bin",
                )
            )

        self.assertEqual(
            payload["ProgramArguments"],
            [
                "/Applications/CodexTelegramBot",
                "--service",
                "--config",
                str(root / ".env"),
            ],
        )
        self.assertTrue(payload["RunAtLoad"])
        self.assertTrue(payload["KeepAlive"])
        self.assertEqual(
            payload["EnvironmentVariables"]["PATH"],
            "/opt/homebrew/bin:/usr/bin:/bin",
        )
        self.assertEqual(payload["StandardErrorPath"], str(root / "logs/bot.err.log"))

    def test_service_path_includes_codex_directory_and_standard_paths(self) -> None:
        environment_path = service_environment_path("/custom/node/bin/codex")
        directories = environment_path.split(":")

        self.assertEqual(directories[0], "/custom/node/bin")
        self.assertIn("/opt/homebrew/bin", directories)
        self.assertIn("/usr/bin", directories)

    def test_macos_stop_unloads_keepalive_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = ServiceManager(Path(directory) / ".env", platform="darwin")
            service_path = Path(directory) / "agent.plist"
            service_path.write_text("plist", encoding="utf-8")
            with (
                mock.patch.object(
                    service, "_macos_service_path", return_value=service_path
                ),
                mock.patch.object(service, "_run") as run,
            ):
                service.stop()

        arguments = run.call_args.args[0]
        self.assertEqual(arguments[:2], ["launchctl", "bootout"])
        self.assertEqual(arguments[-1], str(service_path))

    def test_remote_mode_disables_current_and_legacy_macos_pollers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = ServiceManager(Path(directory) / ".env", platform="darwin")
            with mock.patch.object(service, "_run") as run:
                service.suspend_for_remote()

        commands = [call.args[0] for call in run.call_args_list]
        for label in (MACOS_SERVICE_LABEL, *LEGACY_MACOS_SERVICE_LABELS):
            target = f"gui/{os.getuid()}/{label}"
            self.assertIn(["launchctl", "disable", target], commands)
            self.assertIn(["launchctl", "bootout", target], commands)

    def test_macos_start_bootstraps_stopped_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = ServiceManager(Path(directory) / ".env", platform="darwin")
            service_path = Path(directory) / "agent.plist"
            service_path.write_text("plist", encoding="utf-8")
            stopped = mock.Mock(returncode=113)
            with (
                mock.patch.object(
                    service, "_macos_service_path", return_value=service_path
                ),
                mock.patch.object(
                    service,
                    "_run",
                    side_effect=[mock.Mock(), stopped, mock.Mock()],
                ) as run,
            ):
                service.start()

        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args.args[0][:2], ["launchctl", "bootstrap"])

    def test_macos_install_reenables_service_before_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = ServiceManager(
                root / ".env",
                platform="darwin",
                log_dir=root / "logs",
            )
            service_path = root / "agent.plist"
            with (
                mock.patch.object(
                    service, "_macos_service_path", return_value=service_path
                ),
                mock.patch.object(service, "_run") as run,
            ):
                service.install(root)

        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[-2][:2], ["launchctl", "enable"])
        self.assertEqual(commands[-1][:2], ["launchctl", "bootstrap"])

    def test_windows_status_distinguishes_missing_task(self) -> None:
        service = ServiceManager(Path("settings.env"), platform="win32")
        missing = mock.Mock(returncode=1, stdout="", stderr="task not found")
        with mock.patch.object(service, "_run", return_value=missing):
            status = service.status()

        self.assertFalse(status.installed)
        self.assertFalse(status.running)
        self.assertEqual(status.description, "Фоновый запуск не установлен")


if __name__ == "__main__":
    unittest.main()
