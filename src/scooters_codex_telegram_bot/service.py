from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .config import APP_NAME, default_log_dir

MACOS_SERVICE_LABEL = "com.scooters.codex-telegram-bot"
WINDOWS_TASK_NAME = "Scooters Codex Telegram Bot"


class ServiceError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    installed: bool
    running: bool
    description: str


def runtime_command() -> tuple[str, ...]:
    if getattr(sys, "frozen", False):
        return (sys.executable,)
    if executable := shutil.which("scooters-codex-telegram-bot"):
        return (executable,)
    return (sys.executable, "-m", "scooters_codex_telegram_bot")


def linux_unit_text(
    command: Sequence[str], config_path: Path, working_directory: Path, log_dir: Path
) -> str:
    arguments = [*command, "--service", "--config", str(config_path)]
    return (
        "[Unit]\n"
        "Description=Scooters Codex Telegram Bot\n"
        "Wants=network-online.target\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={_systemd_quote(str(working_directory))}\n"
        f"ExecStart={' '.join(_systemd_quote(value) for value in arguments)}\n"
        "Restart=always\n"
        "RestartSec=10\n"
        "KillMode=control-group\n"
        "TimeoutStopSec=15\n"
        "UMask=0077\n"
        f"StandardOutput=append:{_systemd_quote(str(log_dir / 'bot.out.log'))}\n"
        f"StandardError=append:{_systemd_quote(str(log_dir / 'bot.err.log'))}\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def macos_plist_bytes(
    command: Sequence[str], config_path: Path, working_directory: Path, log_dir: Path
) -> bytes:
    arguments = [*command, "--service", "--config", str(config_path)]
    payload = {
        "Label": MACOS_SERVICE_LABEL,
        "ProgramArguments": arguments,
        "WorkingDirectory": str(working_directory),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "StandardOutPath": str(log_dir / "bot.out.log"),
        "StandardErrorPath": str(log_dir / "bot.err.log"),
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False)


class ServiceManager:
    def __init__(
        self,
        config_path: Path,
        *,
        command: Sequence[str] | None = None,
        platform: str | None = None,
        log_dir: Path | None = None,
    ) -> None:
        self.config_path = config_path.expanduser().resolve()
        self.command = tuple(command or runtime_command())
        self.platform = platform or sys.platform
        self.log_dir = (log_dir or default_log_dir()).expanduser().resolve()

    def install(self, working_directory: Path) -> None:
        working_directory = working_directory.expanduser().resolve()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        if self.platform == "darwin":
            self._install_macos(working_directory)
        elif self.platform == "win32":
            self._install_windows()
        else:
            self._install_linux(working_directory)

    def start(self) -> None:
        if self.platform == "darwin":
            service_path = self._macos_service_path()
            if not service_path.is_file():
                raise ServiceError("Background startup is not installed")
            domain = f"gui/{os.getuid()}"
            target = f"{domain}/{MACOS_SERVICE_LABEL}"
            self._run(["launchctl", "enable", target])
            status = self._run(["launchctl", "print", target], check=False)
            if status.returncode == 0:
                self._run(["launchctl", "kickstart", "-k", target])
            else:
                self._run(["launchctl", "bootstrap", domain, str(service_path)])
        elif self.platform == "win32":
            self._run(["schtasks", "/Run", "/TN", WINDOWS_TASK_NAME])
        else:
            self._run(["systemctl", "--user", "start", f"{APP_NAME}.service"])

    def stop(self) -> None:
        if self.platform == "darwin":
            service_path = self._macos_service_path()
            if service_path.is_file():
                self._run(
                    ["launchctl", "bootout", f"gui/{os.getuid()}", str(service_path)],
                    check=False,
                )
        elif self.platform == "win32":
            self._run(
                ["schtasks", "/End", "/TN", WINDOWS_TASK_NAME], check=False
            )
        else:
            self._run(
                ["systemctl", "--user", "stop", f"{APP_NAME}.service"],
                check=False,
            )

    def restart(self) -> None:
        if self.platform == "darwin":
            self.start()
        elif self.platform == "win32":
            self.stop()
            self.start()
        else:
            self._run(["systemctl", "--user", "restart", f"{APP_NAME}.service"])

    def uninstall(self) -> None:
        if self.platform == "darwin":
            service_path = self._macos_service_path()
            domain = f"gui/{os.getuid()}"
            if service_path.exists():
                self._run(
                    ["launchctl", "bootout", domain, str(service_path)], check=False
                )
                service_path.unlink()
        elif self.platform == "win32":
            self._run(
                ["schtasks", "/Delete", "/TN", WINDOWS_TASK_NAME, "/F"],
                check=False,
            )
        else:
            unit_path = self._linux_service_path()
            self._run(
                ["systemctl", "--user", "disable", "--now", f"{APP_NAME}.service"],
                check=False,
            )
            if unit_path.exists():
                unit_path.unlink()
            self._run(["systemctl", "--user", "daemon-reload"])

    def status(self) -> ServiceStatus:
        if self.platform == "darwin":
            installed = self._macos_service_path().is_file()
            target = f"gui/{os.getuid()}/{MACOS_SERVICE_LABEL}"
            result = self._run(["launchctl", "print", target], check=False)
        elif self.platform == "win32":
            result = self._run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    f"(Get-ScheduledTask -TaskName '{WINDOWS_TASK_NAME}').State.value__",
                ],
                check=False,
            )
            installed = result.returncode == 0
            running = installed and result.stdout.strip() == "4"
            if not installed:
                description = "Фоновый запуск не установлен"
            elif running:
                description = "Запущен"
            else:
                description = "Остановлен"
            return ServiceStatus(installed, running, description)
        else:
            installed = self._linux_service_path().is_file()
            result = self._run(
                ["systemctl", "--user", "is-active", f"{APP_NAME}.service"],
                check=False,
            )
        running = result.returncode == 0
        if not installed:
            description = "Фоновый запуск не установлен"
        elif running:
            description = "Запущен"
        else:
            description = "Остановлен"
        return ServiceStatus(installed, running, description)

    def install_and_start(self, working_directory: Path) -> None:
        self.install(working_directory)
        if self.platform != "darwin":
            self.start()

    def _install_linux(self, working_directory: Path) -> None:
        if shutil.which("systemctl") is None:
            raise ServiceError("systemctl is not available on this Linux host")
        unit_path = self._linux_service_path()
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(
            linux_unit_text(
                self.command,
                self.config_path,
                working_directory,
                self.log_dir,
            ),
            encoding="utf-8",
        )
        with suppress(PermissionError):
            unit_path.chmod(0o600)
        self._run(["systemctl", "--user", "daemon-reload"])
        self._run(["systemctl", "--user", "enable", f"{APP_NAME}.service"])

    def _install_macos(self, working_directory: Path) -> None:
        service_path = self._macos_service_path()
        service_path.parent.mkdir(parents=True, exist_ok=True)
        domain = f"gui/{os.getuid()}"
        target = f"{domain}/{MACOS_SERVICE_LABEL}"
        if service_path.exists():
            self._run(
                ["launchctl", "bootout", domain, str(service_path)], check=False
            )
        service_path.write_bytes(
            macos_plist_bytes(
                self.command,
                self.config_path,
                working_directory,
                self.log_dir,
            )
        )
        with suppress(PermissionError):
            service_path.chmod(0o600)
        self._run(["launchctl", "enable", target])
        self._run(["launchctl", "bootstrap", domain, str(service_path)])

    def _install_windows(self) -> None:
        task_command = subprocess.list2cmdline(
            [*self.command, "--service", "--config", str(self.config_path)]
        )
        self._run(
            [
                "schtasks",
                "/Create",
                "/TN",
                WINDOWS_TASK_NAME,
                "/TR",
                task_command,
                "/SC",
                "ONLOGON",
                "/RL",
                "LIMITED",
                "/F",
            ]
        )

    @staticmethod
    def _linux_service_path() -> Path:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        return base / "systemd" / "user" / f"{APP_NAME}.service"

    @staticmethod
    def _macos_service_path() -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{MACOS_SERVICE_LABEL}.plist"

    @staticmethod
    def _run(arguments: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(
            list(arguments),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=creation_flags,
        )
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-500:]
            raise ServiceError(detail or f"Command failed with code {result.returncode}")
        return result


def _systemd_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
