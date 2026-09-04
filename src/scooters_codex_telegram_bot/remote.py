from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .config import APP_NAME, RUNTIME_CONFIG_KEYS, format_dotenv
from .service import ServiceError, ServiceStatus

_REMOTE_NAME = re.compile(r"^[A-Za-z0-9._+-]+$")
_REMOTE_HOST = re.compile(r"^[A-Za-z0-9._:-]+$")
_EXCLUDED_PARTS = {
    ".git",
    ".idea",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "var",
}


@dataclass(frozen=True, slots=True)
class RemoteSettings:
    host: str
    user: str
    port: int
    install_dir: str
    codex_cwd: str
    codex_bin: str
    python_bin: str
    identity_file: Path | None = None

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> RemoteSettings:
        host = values.get("REMOTE_SSH_HOST", "").strip()
        user = values.get("REMOTE_SSH_USER", "").strip()
        if not host:
            raise ServiceError("Укажите SSH-хост удалённого сервера")
        if host.startswith("-") or not _REMOTE_HOST.fullmatch(host):
            raise ServiceError("SSH-хост содержит недопустимые символы")
        if user and (user.startswith("-") or not _REMOTE_NAME.fullmatch(user)):
            raise ServiceError("SSH-пользователь содержит недопустимые символы")

        raw_port = values.get("REMOTE_SSH_PORT", "22").strip() or "22"
        try:
            port = int(raw_port)
        except ValueError as error:
            raise ServiceError("SSH-порт должен быть числом") from error
        if not 1 <= port <= 65535:
            raise ServiceError("SSH-порт должен быть от 1 до 65535")

        install_dir = values.get(
            "REMOTE_INSTALL_DIR", f"~/.local/share/{APP_NAME}"
        ).strip()
        codex_cwd = values.get("REMOTE_CODEX_CWD", "~/arcadia").strip()
        codex_bin = values.get("REMOTE_CODEX_BIN", "codex").strip() or "codex"
        python_bin = (
            values.get("REMOTE_PYTHON_BIN", "/usr/bin/python3").strip()
            or "/usr/bin/python3"
        )
        for label, value in (
            ("Папка установки", install_dir),
            ("Рабочая папка Codex", codex_cwd),
            ("Команда Codex", codex_bin),
            ("Команда Python", python_bin),
        ):
            if not value or "\x00" in value or "\n" in value or "\r" in value:
                raise ServiceError(f"{label} задана некорректно")
        if python_bin.startswith("~"):
            raise ServiceError("Для удалённого Python укажите абсолютный путь")

        identity_value = values.get("REMOTE_SSH_IDENTITY_FILE", "").strip()
        identity_file = Path(identity_value).expanduser() if identity_value else None
        if identity_file is not None and not identity_file.is_file():
            raise ServiceError(f"SSH-ключ не найден: {identity_file}")

        return cls(
            host=host,
            user=user,
            port=port,
            install_dir=install_dir,
            codex_cwd=codex_cwd,
            codex_bin=codex_bin,
            python_bin=python_bin,
            identity_file=identity_file,
        )


class RemoteServiceManager:
    """Deploy and control the bot on a Linux host through OpenSSH."""

    def __init__(
        self,
        settings: RemoteSettings,
        *,
        ssh_bin: str | None = None,
        runtime_root: Path | None = None,
    ) -> None:
        self.settings = settings
        self.ssh_bin = ssh_bin or shutil.which("ssh") or ""
        if not self.ssh_bin:
            raise ServiceError(
                "OpenSSH не найден. Установите системную команду ssh и повторите."
            )
        self.runtime_root = runtime_root or _runtime_source_root()

    def test_connection(self) -> str:
        result = self._remote(
            [self.settings.python_bin, "-c", _DIAGNOSTIC_SCRIPT], timeout=20
        )
        return result.stdout.strip() or "Соединение установлено"

    def install_and_start(self, runtime_values: Mapping[str, str]) -> None:
        token = runtime_values.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ServiceError("Telegram Bot Token не настроен")
        self.test_connection()

        archive = self._runtime_archive()
        remote_archive = ""
        try:
            remote_archive = self._remote_temp_path()
            self._upload(archive, remote_archive)
            payload_values = {
                key: str(runtime_values[key])
                for key in RUNTIME_CONFIG_KEYS
                if key in runtime_values
            }
            payload_values["BOT_STATE_PATH"] = str(
                PurePosixPath(self.settings.install_dir)
                / "state"
                / "state.sqlite3"
            )
            payload = {
                "archive": remote_archive,
                "install_dir": self.settings.install_dir,
                "codex_cwd": self.settings.codex_cwd,
                "codex_bin": self.settings.codex_bin,
                "config": format_dotenv(payload_values, RUNTIME_CONFIG_KEYS),
            }
            self._remote(
                [self.settings.python_bin, "-c", _INSTALL_SCRIPT],
                input_text=json.dumps(payload, ensure_ascii=False),
                timeout=180,
            )
        finally:
            archive.unlink(missing_ok=True)
            if remote_archive:
                with suppress(ServiceError):
                    self._remote(
                        [
                            self.settings.python_bin,
                            "-c",
                            _REMOVE_FILE_SCRIPT,
                            remote_archive,
                        ],
                        check=False,
                        timeout=20,
                    )

    def start(self) -> None:
        self._systemctl("start")

    def stop(self) -> None:
        self._systemctl("stop", check=False)

    def restart(self) -> None:
        self._systemctl("restart")

    def uninstall(self) -> None:
        self._systemctl("disable", "--now", check=False)
        self._remote(
            [self.settings.python_bin, "-c", _UNINSTALL_SCRIPT, APP_NAME],
            timeout=30,
        )

    def status(self) -> ServiceStatus:
        result = self._remote(
            [self.settings.python_bin, "-c", _STATUS_SCRIPT, APP_NAME],
            check=False,
            timeout=20,
        )
        if result.returncode == 255:
            raise ServiceError(_command_error(result))
        parts = result.stdout.strip().splitlines()
        installed = bool(parts and parts[0] == "installed")
        running = len(parts) > 1 and parts[1] == "active"
        if not installed:
            description = "На сервере автозапуск не установлен"
        elif running:
            description = "На сервере запущен"
        else:
            description = "На сервере остановлен"
        return ServiceStatus(installed, running, description)

    def logs(self, max_lines: int = 250) -> str:
        result = self._remote(
            [
                self.settings.python_bin,
                "-c",
                _LOGS_SCRIPT,
                self.settings.install_dir,
                str(max(1, min(max_lines, 1000))),
            ],
            timeout=30,
        )
        return result.stdout.rstrip() or "Логи пока пусты."

    def _runtime_archive(self) -> Path:
        if not self.runtime_root.is_dir():
            raise ServiceError(
                "Файлы удалённого runtime не найдены. Переустановите приложение."
            )
        descriptor, archive_name = tempfile.mkstemp(suffix=".tar.gz")
        os.close(descriptor)
        archive_path = Path(archive_name)
        try:
            with tarfile.open(archive_path, "w:gz") as archive:
                for source in sorted(self.runtime_root.rglob("*")):
                    relative = source.relative_to(self.runtime_root)
                    if any(part in _EXCLUDED_PARTS for part in relative.parts):
                        continue
                    if source.is_file() and (
                        source.suffix == ".py" or "assets" in relative.parts
                    ):
                        archive.add(
                            source,
                            arcname=(
                                PurePosixPath("scooters_codex_telegram_bot") / relative
                            ).as_posix(),
                            recursive=False,
                        )
            return archive_path
        except Exception:
            archive_path.unlink(missing_ok=True)
            raise

    def _remote_temp_path(self) -> str:
        result = self._remote(
            [
                self.settings.python_bin,
                "-c",
                "import tempfile; print(tempfile.mkstemp(prefix='codex-bot-', suffix='.tgz')[1])",
            ],
            timeout=20,
        )
        path = result.stdout.strip()
        if not path.startswith("/tmp/"):
            raise ServiceError("Удалённый сервер вернул небезопасный временный путь")
        return path

    def _upload(self, local_path: Path, remote_path: str) -> None:
        scp_bin = shutil.which("scp")
        if not scp_bin:
            raise ServiceError("OpenSSH scp не найден")
        arguments = [scp_bin, *self._scp_connection_arguments(), str(local_path)]
        arguments.append(f"{self.settings.target}:{remote_path}")
        result = _run(arguments, timeout=120)
        if result.returncode != 0:
            raise ServiceError(_command_error(result))

    def _systemctl(self, *arguments: str, check: bool = True) -> None:
        self._remote(
            ["systemctl", "--user", *arguments, f"{APP_NAME}.service"],
            check=check,
            timeout=30,
        )

    def _remote(
        self,
        arguments: Sequence[str],
        *,
        input_text: str | None = None,
        check: bool = True,
        timeout: int = 30,
    ) -> subprocess.CompletedProcess[str]:
        command = shlex.join(str(value) for value in arguments)
        result = _run(
            [
                self.ssh_bin,
                *self._connection_arguments(),
                self.settings.target,
                command,
            ],
            input_text=input_text,
            timeout=timeout,
        )
        if result.returncode == 255 or (check and result.returncode != 0):
            raise ServiceError(_command_error(result))
        return result

    def _connection_arguments(self) -> list[str]:
        arguments = [
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-p",
            str(self.settings.port),
        ]
        if self.settings.identity_file is not None:
            arguments.extend(["-i", str(self.settings.identity_file)])
        return arguments

    def _scp_connection_arguments(self) -> list[str]:
        arguments = [
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-P",
            str(self.settings.port),
        ]
        if self.settings.identity_file is not None:
            arguments.extend(["-i", str(self.settings.identity_file)])
        return arguments


def _runtime_source_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "remote_runtime" / APP_NAME.replace(  # type: ignore[attr-defined]
            "-", "_"
        )
    return Path(__file__).resolve().parent


def _run(
    arguments: Sequence[str],
    *,
    input_text: str | None = None,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.run(
            list(arguments),
            input=input_text,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=creation_flags,
        )
    except subprocess.TimeoutExpired as error:
        raise ServiceError("Удалённая операция превысила время ожидания") from error


def _command_error(result: subprocess.CompletedProcess[str]) -> str:
    detail = (result.stderr or result.stdout).strip()
    if "Permission denied" in detail or "publickey" in detail:
        return "SSH-доступ отклонён. Проверьте пользователя и SSH-ключ."
    if "Host key verification failed" in detail:
        return "SSH-хост ещё не подтверждён. Один раз подключитесь к нему в терминале."
    if "Could not resolve hostname" in detail:
        return "SSH-хост не найден. Проверьте адрес и подключение к сети/VPN."
    if "Connection timed out" in detail or "Operation timed out" in detail:
        return "SSH-сервер не ответил вовремя."
    return detail[-1200:] or f"Удалённая команда завершилась с кодом {result.returncode}"


_DIAGNOSTIC_SCRIPT = r"""
import os, platform, shutil, subprocess, sys
if platform.system() != "Linux":
    raise SystemExit("Удалённый запуск поддерживает Linux с systemd")
if sys.version_info < (3, 10):
    raise SystemExit("На сервере требуется Python 3.10 или новее")
if not shutil.which("systemctl"):
    raise SystemExit("На сервере не найден systemctl")
result = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True)
if result.returncode:
    detail = result.stderr.decode(errors="replace")
    raise SystemExit("Недоступен пользовательский systemd: " + detail)
linger = subprocess.run(
    ["loginctl", "show-user", str(os.getuid()), "-p", "Linger", "--value"],
    capture_output=True,
    text=True,
)
if linger.returncode or linger.stdout.strip().lower() != "yes":
    raise SystemExit(
        "Для работы после выхода из SSH включите linger: "
        "loginctl enable-linger $USER"
    )
version = f"{sys.version_info.major}.{sys.version_info.minor}"
print(f"Подключено: {platform.node()} · Python {version} · systemd и linger доступны")
"""

_INSTALL_SCRIPT = r"""
import json, os, pathlib, shlex, shutil, subprocess, sys, tarfile

payload = json.load(sys.stdin)
home = pathlib.Path.home()
expand = lambda value: pathlib.Path(os.path.expanduser(value)).resolve()
install = expand(payload["install_dir"])
cwd = expand(payload["codex_cwd"])
archive = pathlib.Path(payload["archive"])
if not cwd.is_dir():
    raise SystemExit(f"Рабочая папка Codex не найдена: {cwd}")

codex_setting = payload["codex_bin"]
shell = os.environ.get("SHELL", "/bin/sh")
if "/" in codex_setting:
    codex = expand(codex_setting)
else:
    found = subprocess.run(
        [shell, "-lic", f"command -v {shlex.quote(codex_setting)}"],
        capture_output=True, text=True,
    )
    codex = pathlib.Path(found.stdout.strip().splitlines()[-1]) if found.stdout.strip() else None
if codex is None or not codex.is_file() or not os.access(codex, os.X_OK):
    raise SystemExit(f"Codex не найден на сервере: {codex_setting}")
login_environment = subprocess.run(
    [shell, "-lic", "printf '\\n__BOT_PATH__=%s\\n' \"$PATH\""],
    capture_output=True,
    text=True,
)
login_path = ""
for line in login_environment.stdout.splitlines():
    if line.startswith("__BOT_PATH__="):
        login_path = line.removeprefix("__BOT_PATH__=")

runtime_new = install / "runtime.new"
runtime = install / "runtime"
runtime_old = install / "runtime.old"
for path in (install, install / "config", install / "state", install / "logs"):
    path.mkdir(parents=True, exist_ok=True)
if runtime_new.exists():
    shutil.rmtree(runtime_new)
runtime_new.mkdir()
with tarfile.open(archive, "r:gz") as bundle:
    root = runtime_new.resolve()
    for member in bundle.getmembers():
        target = (runtime_new / member.name).resolve()
        if root != target and root not in target.parents:
            raise SystemExit("Архив содержит небезопасный путь")
    bundle.extractall(runtime_new)
if runtime_old.exists():
    shutil.rmtree(runtime_old)
if runtime.exists():
    runtime.rename(runtime_old)
runtime_new.rename(runtime)
if runtime_old.exists():
    shutil.rmtree(runtime_old)

config_path = install / "config" / ".env"
config_path.write_text(payload["config"], encoding="utf-8")
config_path.chmod(0o600)
voice_enabled = any(
    line.strip().lower() == "voice_transcription_enabled=true"
    for line in payload["config"].splitlines()
)
if voice_enabled:
    dependency = subprocess.run(
        [sys.executable, "-c", "import faster_whisper"],
        capture_output=True,
        text=True,
    )
    if dependency.returncode:
        raise SystemExit(
            "Для голосовых сообщений установите faster-whisper в выбранный "
            "удалённый Python или укажите Python готового virtualenv"
        )

def quote(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'

path_entries = [
    str(codex.parent),
    str(pathlib.Path(sys.executable).parent),
    *login_path.split(os.pathsep),
    "/usr/local/bin", "/usr/bin", "/bin",
]
path_value = os.pathsep.join(dict.fromkeys(value for value in path_entries if value))
unit = (
    "[Unit]\n"
    "Description=Scooters Codex Telegram Bot\n"
    "Wants=network-online.target\nAfter=network-online.target\n\n"
    "[Service]\nType=simple\n"
    f"WorkingDirectory={quote(cwd)}\n"
    f"Environment={quote('PATH=' + path_value)}\n"
    f"Environment={quote('PYTHONPATH=' + str(runtime))}\n"
    f"ExecStart={quote(sys.executable)} -m scooters_codex_telegram_bot "
    f"--service --config {quote(config_path)}\n"
    "Restart=always\nRestartSec=10\nKillMode=control-group\nTimeoutStopSec=15\nUMask=0077\n"
    f"StandardOutput=append:{quote(install / 'logs' / 'bot.out.log')}\n"
    f"StandardError=append:{quote(install / 'logs' / 'bot.err.log')}\n\n"
    "[Install]\nWantedBy=default.target\n"
)
unit_path = home / ".config" / "systemd" / "user" / "scooters-codex-telegram-bot.service"
unit_path.parent.mkdir(parents=True, exist_ok=True)
unit_path.write_text(unit, encoding="utf-8")
unit_path.chmod(0o600)
subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
subprocess.run(
    ["systemctl", "--user", "enable", "--now", "scooters-codex-telegram-bot.service"],
    check=True,
)
print("Удалённый бот установлен и запущен")
"""

_STATUS_SCRIPT = r"""
import pathlib, subprocess, sys
name = sys.argv[1]
unit = pathlib.Path.home() / ".config" / "systemd" / "user" / f"{name}.service"
print("installed" if unit.is_file() else "missing")
status = subprocess.run(
    ["systemctl", "--user", "is-active", f"{name}.service"],
    capture_output=True,
    text=True,
)
print("active" if status.returncode == 0 else "inactive")
"""

_LOGS_SCRIPT = r"""
import collections, os, pathlib, sys
root = pathlib.Path(os.path.expanduser(sys.argv[1])).resolve() / "logs"
limit = int(sys.argv[2])
for name in ("bot.err.log", "bot.out.log", "bot.log"):
    path = root / name
    if path.is_file():
        print(f"===== {name} =====")
        with path.open(encoding="utf-8", errors="replace") as stream:
            print("".join(collections.deque(stream, maxlen=limit)), end="")
"""

_UNINSTALL_SCRIPT = r"""
import pathlib, subprocess, sys
name = sys.argv[1]
unit = pathlib.Path.home() / ".config" / "systemd" / "user" / f"{name}.service"
unit.unlink(missing_ok=True)
subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
"""

_REMOVE_FILE_SCRIPT = "import pathlib, sys; pathlib.Path(sys.argv[1]).unlink(missing_ok=True)"
