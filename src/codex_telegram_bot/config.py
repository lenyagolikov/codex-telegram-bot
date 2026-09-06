from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "codex-telegram-bot"
CONFIG_FILE_HEADER = "# Managed by Codex Telegram Bot. Do not commit this file.\n"
RUNTIME_CONFIG_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USER_IDS",
    "CODEX_CWD",
    "CODEX_BIN",
    "CODEX_MODEL",
    "CODEX_REASONING_EFFORT",
    "BOT_STATE_PATH",
    "TELEGRAM_IP_FAMILY",
    "POLL_TIMEOUT_SECONDS",
    "VOICE_TRANSCRIPTION_ENABLED",
    "WHISPER_MODEL",
    "WHISPER_LANGUAGE",
    "VOICE_MAX_DURATION_SECONDS",
    "VOICE_MAX_FILE_BYTES",
    "AUTO_APPROVE_SAFE_READ_ONLY",
    "AUTO_APPROVE_READ_ROOTS",
)
DESKTOP_CONFIG_KEYS = (
    "RUN_MODE",
    "REMOTE_SSH_HOST",
    "REMOTE_SSH_USER",
    "REMOTE_SSH_PORT",
    "REMOTE_SSH_IDENTITY_FILE",
    "REMOTE_INSTALL_DIR",
    "REMOTE_CODEX_CWD",
    "REMOTE_CODEX_BIN",
    "REMOTE_PYTHON_BIN",
)
CONFIG_KEYS = (*RUNTIME_CONFIG_KEYS, *DESKTOP_CONFIG_KEYS)
_SAFE_ENV_VALUE = re.compile(r"^[A-Za-z0-9_./:@,+\\-]*$")


class ConfigError(ValueError):
    pass


def default_config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / APP_NAME / ".env"


def default_state_path() -> Path:
    if sys.platform == "win32":
        base = Path(
            os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
        )
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / APP_NAME / "state.sqlite3"


def default_log_dir() -> Path:
    if sys.platform == "win32":
        base = Path(
            os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
        )
        return base / APP_NAME / "logs"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / APP_NAME
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / APP_NAME / "logs"


def find_config_path(explicit_path: Path | None = None) -> Path:
    if explicit_path is not None:
        return explicit_path.expanduser()
    if configured_path := os.environ.get("BOT_CONFIG_PATH", "").strip():
        return Path(configured_path).expanduser()
    working_directory_config = Path.cwd() / ".env"
    if working_directory_config.is_file():
        return working_directory_config
    return default_config_path()


def read_dotenv(path: Path) -> dict[str, str]:
    """Read the supported .env syntax without mutating the process environment."""
    if not path.is_file():
        return {}

    result: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"Invalid .env entry at line {line_number}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ConfigError(f"Invalid .env key at line {line_number}")
        result[key] = _decode_env_value(value, line_number)
    return result


def write_dotenv(path: Path, values: Mapping[str, str]) -> None:
    """Atomically write configuration with owner-only permissions where supported."""
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = format_dotenv(values)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        with suppress(PermissionError):
            path.chmod(0o600)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def format_dotenv(
    values: Mapping[str, str], keys: tuple[str, ...] = CONFIG_KEYS
) -> str:
    """Serialize supported values without exposing unrelated environment entries."""
    lines = [CONFIG_FILE_HEADER.rstrip("\n")]
    for key in keys:
        value = values.get(key)
        if value is None:
            continue
        lines.append(f"{key}={_encode_env_value(str(value))}")
    return "\n".join(lines) + "\n"


def _decode_env_value(value: str, line_number: int) -> str:
    if not value:
        return ""
    if value.startswith('"'):
        if not value.endswith('"'):
            raise ConfigError(f"Invalid quoted .env value at line {line_number}")
        try:
            import json

            decoded = json.loads(value)
        except (ValueError, TypeError) as error:
            raise ConfigError(
                f"Invalid quoted .env value at line {line_number}"
            ) from error
        if not isinstance(decoded, str):
            raise ConfigError(f"Invalid .env value at line {line_number}")
        return decoded
    if value.startswith("'"):
        if not value.endswith("'"):
            raise ConfigError(f"Invalid quoted .env value at line {line_number}")
        return value[1:-1]
    return value


def _encode_env_value(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ConfigError("Configuration values must not contain newlines")
    if _SAFE_ENV_VALUE.fullmatch(value):
        return value
    import json

    return json.dumps(value, ensure_ascii=False)


def _parse_user_ids(raw_value: str) -> frozenset[int]:
    if not raw_value.strip():
        return frozenset()

    result: set[int] = set()
    for value in raw_value.split(","):
        try:
            result.add(int(value.strip()))
        except ValueError as error:
            raise ConfigError(
                "TELEGRAM_ALLOWED_USER_IDS must contain numeric IDs"
            ) from error
    return frozenset(result)


def _parse_bool(
    environment: Mapping[str, str], name: str, default: bool = False
) -> bool:
    raw_value = environment.get(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


def _parse_positive_int(
    environment: Mapping[str, str], name: str, default: int
) -> int:
    raw_value = environment.get(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ConfigError(f"{name} must be an integer") from error
    if value <= 0:
        raise ConfigError(f"{name} must be positive")
    return value


@dataclass(frozen=True, slots=True)
class Config:
    telegram_token: str
    allowed_user_ids: frozenset[int]
    codex_cwd: Path
    codex_bin: str
    codex_model: str | None
    reasoning_effort: str | None
    state_path: Path
    poll_timeout_seconds: int = 30
    telegram_ip_family: str = "auto"
    voice_transcription_enabled: bool = False
    whisper_model: str = "small"
    whisper_language: str | None = "ru"
    voice_max_duration_seconds: int = 600
    voice_max_file_bytes: int = 20 * 1024 * 1024
    auto_approve_safe_read_only: bool = False
    auto_approve_read_roots: tuple[Path, ...] = ()

    @classmethod
    def from_environment(cls, config_path: Path | None = None) -> Config:
        environment = read_dotenv(find_config_path(config_path))
        environment.update(os.environ)
        return cls.from_mapping(environment)

    @classmethod
    def from_mapping(
        cls,
        environment: Mapping[str, str],
        *,
        validate_local_paths: bool = True,
    ) -> Config:
        token = environment.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            token = _read_token_from_keyring()
        if not token:
            raise ConfigError("TELEGRAM_BOT_TOKEN is not configured")

        codex_bin = environment.get("CODEX_BIN", "codex").strip() or "codex"
        if validate_local_paths and shutil.which(codex_bin) is None:
            raise ConfigError(f"Codex executable not found: {codex_bin}")

        codex_cwd = Path(
            environment.get("CODEX_CWD", str(Path.cwd()))
        ).expanduser()
        if validate_local_paths and not codex_cwd.is_dir():
            raise ConfigError(f"CODEX_CWD is not a directory: {codex_cwd}")
        if validate_local_paths:
            codex_cwd = codex_cwd.resolve()

        state_path_value = environment.get("BOT_STATE_PATH", "").strip()
        state_path = (
            Path(state_path_value).expanduser()
            if state_path_value
            else default_state_path()
        )
        model = environment.get("CODEX_MODEL", "").strip() or None
        effort = environment.get("CODEX_REASONING_EFFORT", "").strip() or None
        ip_family = environment.get("TELEGRAM_IP_FAMILY", "auto").strip().lower()
        if ip_family not in {"auto", "ipv4", "ipv6"}:
            raise ConfigError("TELEGRAM_IP_FAMILY must be auto, ipv4 or ipv6")

        whisper_language = environment.get("WHISPER_LANGUAGE", "ru").strip() or None
        read_roots_value = environment.get("AUTO_APPROVE_READ_ROOTS", "").strip()
        read_roots = tuple(
            Path(value.strip()).expanduser().resolve()
            for value in read_roots_value.split(",")
            if value.strip()
        ) or (codex_cwd,)

        return cls(
            telegram_token=token,
            allowed_user_ids=_parse_user_ids(
                environment.get("TELEGRAM_ALLOWED_USER_IDS", "")
            ),
            codex_cwd=codex_cwd,
            codex_bin=codex_bin,
            codex_model=model,
            reasoning_effort=effort,
            state_path=state_path,
            poll_timeout_seconds=_parse_positive_int(
                environment, "POLL_TIMEOUT_SECONDS", 30
            ),
            telegram_ip_family=ip_family,
            voice_transcription_enabled=_parse_bool(
                environment, "VOICE_TRANSCRIPTION_ENABLED"
            ),
            whisper_model=environment.get("WHISPER_MODEL", "small").strip()
            or "small",
            whisper_language=whisper_language,
            voice_max_duration_seconds=_parse_positive_int(
                environment, "VOICE_MAX_DURATION_SECONDS", 600
            ),
            voice_max_file_bytes=_parse_positive_int(
                environment, "VOICE_MAX_FILE_BYTES", 20 * 1024 * 1024
            ),
            auto_approve_safe_read_only=_parse_bool(
                environment, "AUTO_APPROVE_SAFE_READ_ONLY"
            ),
            auto_approve_read_roots=read_roots,
        )


def _read_token_from_keyring() -> str:
    try:
        from .secrets import read_telegram_token

        return read_telegram_token() or ""
    except (ImportError, OSError):
        return ""
