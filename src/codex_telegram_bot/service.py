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

MACOS_SERVICE_LABEL = "com.lenyagolikov.codex-telegram-bot"
WINDOWS_TASK_NAME = "Codex Telegram Bot"


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
    if executable := shutil.which("codex-telegram-bot"):
        return (executable,)
    return (sys.executable, "-m", "codex_telegram_bot")


def service_environment_path(codex_bin: str) -> str:
    """Build a stable PATH for services started outside an interactive shell."""
    directories: list[str] = []

    def add(directory: str | Path) -> None:
        value = str(directory).strip()
        if value and value not in directories:
            directories.append(value)

    expanded_codex_bin = os.path.expanduser(codex_bin)
    if os.path.dirname(expanded_codex_bin):
        # Keep the symlink's directory: npm commonly installs `codex` next to `node`.
        add(Path(os.path.abspath(expanded_codex_bin)).parent)
    elif resolved_codex_bin := shutil.which(expanded_codex_bin):
        add(Path(resolved_codex_bin).parent)

    if resolved_node := shutil.which("node"):
        add(Path(resolved_node).parent)

    for directory in os.environ.get("PATH", "").split(os.pathsep):
        add(directory)

    # GUI apps and service managers on macOS often do not inherit Homebrew paths.
    for directory in (
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ):
        add(directory)

    return os.pathsep.join(directories)


def linux_unit_text(
    command: Sequence[str],
    config_path: Path,
    working_directory: Path,
    log_dir: Path,
    environment_path: str | None = None,
) -> str:
    arguments = [*command, "--service", "--config", str(config_path)]
    environment = (
        f"Environment={_systemd_quote(f'PATH={environment_path}')}\n"
        if environment_path
        else ""
    )
    return (
        "[Unit]\n"
        "Description=Codex Telegram Bot\n"
        "Wants=network-online.target\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={_systemd_path(working_directory)}\n"
        f"{environment}"
        f"ExecStart={' '.join(_systemd_quote(value) for value in arguments)}\n"
        "Restart=always\n"
        "RestartSec=10\n"
        "KillMode=control-group\n"
        "TimeoutStopSec=15\n"
        "UMask=0077\n"
        f"StandardOutput=append:{_systemd_path(log_dir / 'bot.out.log')}\n"
        f"StandardError=append:{_systemd_path(log_dir / 'bot.err.log')}\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def macos_plist_bytes(
    command: Sequence[str],
    config_path: Path,
    working_directory: Path,
    log_dir: Path,
    environment_path: str | None = None,
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
    if environment_path:
        payload["EnvironmentVariables"] = {"PATH": environment_path}
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

    def install(self, working_directory: Path, codex_bin: str = "codex") -> None:
        working_directory = working_directory.expanduser().resolve()
        environment_path = service_environment_path(codex_bin)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        if self.platform == "darwin":
            self._install_macos(working_directory, environment_path)
        elif self.platform == "win32":
            self._install_windows()
        else:
            self._install_linux(working_directory, environment_path)

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

    def install_and_start(
        self, working_directory: Path, codex_bin: str = "codex"
    ) -> None:
        self.install(working_directory, codex_bin)
        if self.platform != "darwin":
            self.start()

    def _install_linux(
        self, working_directory: Path, environment_path: str
    ) -> None:
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
                environment_path,
            ),
            encoding="utf-8",
        )
        with suppress(PermissionError):
            unit_path.chmod(0o600)
        self._run(["systemctl", "--user", "daemon-reload"])
        self._run(["systemctl", "--user", "enable", f"{APP_NAME}.service"])

    def _install_macos(
        self, working_directory: Path, environment_path: str
    ) -> None:
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
                environment_path,
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


def _systemd_path(value: str | Path) -> str:
    """Encode a path for directives that do not accept shell-style quoting."""
    result: list[str] = []
    for character in str(value):
        if character in {"\n", "\r", "\x00"}:
            raise ServiceError("Systemd paths must not contain control characters")
        if character == "%":
            result.append("%%")
        elif character == "\\":
            result.append(r"\x5c")
        elif character == " ":
            result.append(r"\x20")
        elif character == "\t":
            result.append(r"\x09")
        else:
            result.append(character)
    return "".join(result)
