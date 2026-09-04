from __future__ import annotations

import argparse
import asyncio
import importlib.util
import logging
import subprocess
import sys
from collections.abc import Sequence
from contextlib import suppress
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__
from .app_server import CodexAppServer
from .bot import TelegramCodexBot
from .config import Config, ConfigError, default_log_dir, find_config_path
from .service import ServiceError, ServiceManager
from .state import StateStore
from .telegram_api import TelegramApi
from .transcription import VoiceTranscriber


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scooters-codex-telegram-bot",
        description="Bridge private Telegram chats to Codex App Server.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="path to the .env configuration file",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate configuration without starting the bot",
    )
    parser.add_argument(
        "--show-config-path",
        action="store_true",
        help="print the configuration file path and exit",
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--gui", action="store_true", help="open the desktop setup window"
    )
    actions.add_argument(
        "--service",
        action="store_true",
        help="run the Telegram bot in service mode",
    )
    actions.add_argument(
        "--install-service",
        action="store_true",
        help="install background startup for the current user",
    )
    actions.add_argument(
        "--uninstall-service",
        action="store_true",
        help="remove background startup for the current user",
    )
    actions.add_argument(
        "--start-service", action="store_true", help="start the background process"
    )
    actions.add_argument(
        "--stop-service", action="store_true", help="stop the background process"
    )
    actions.add_argument(
        "--restart-service",
        action="store_true",
        help="restart the background process",
    )
    actions.add_argument(
        "--service-status",
        action="store_true",
        help="print background process status",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    config_path = find_config_path(args.config)
    if args.gui:
        from .gui import launch_gui

        launch_gui(config_path)
        return
    if args.show_config_path:
        print(config_path)
        return

    service_manager = ServiceManager(config_path)
    try:
        if args.service_status:
            print(service_manager.status().description)
            return
        if args.stop_service:
            service_manager.stop()
            return
        if args.restart_service:
            service_manager.restart()
            return
        if args.uninstall_service:
            service_manager.uninstall()
            return
        if args.start_service:
            service_manager.start()
            return
    except (ServiceError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(f"Service error: {error}") from error

    try:
        config = Config.from_environment(config_path)
        _validate_optional_features(config)
    except (ConfigError, ServiceError) as error:
        raise SystemExit(f"Configuration error: {error}") from error

    if args.install_service:
        try:
            service_manager.install_and_start(config.codex_cwd)
        except (ServiceError, OSError, subprocess.SubprocessError) as error:
            raise SystemExit(f"Service error: {error}") from error
        print("Background process installed and started.")
        return

    if args.check:
        _print_configuration_summary(config, config_path)
        return

    _configure_logging()
    voice_transcriber = (
        VoiceTranscriber(config.whisper_model, config.whisper_language)
        if config.voice_transcription_enabled
        else None
    )
    bot = TelegramCodexBot(
        config=config,
        telegram=TelegramApi(config.telegram_token, config.telegram_ip_family),
        app_server=CodexAppServer(config.codex_bin),
        state=StateStore(config.state_path),
        voice_transcriber=voice_transcriber,
    )
    with suppress(KeyboardInterrupt):
        asyncio.run(bot.run())


def _validate_optional_features(config: Config) -> None:
    if (
        config.voice_transcription_enabled
        and importlib.util.find_spec("faster_whisper") is None
    ):
        raise ConfigError(
            "voice transcription is enabled, but the voice extra is not installed; "
            "run: python -m pip install '.[voice]'"
        )


def _print_configuration_summary(config: Config, config_path: Path) -> None:
    print("Configuration is valid.")
    print(f"Config: {config_path}")
    print(f"Codex executable: {config.codex_bin}")
    print(f"Codex working directory: {config.codex_cwd}")
    print(f"State database: {config.state_path}")
    print(f"Allowed Telegram users: {len(config.allowed_user_ids)}")
    voice_status = (
        f"enabled ({config.whisper_model})"
        if config.voice_transcription_enabled
        else "disabled"
    )
    print(f"Voice transcription: {voice_status}")
    print(
        "Safe read-only auto-approval: "
        + ("enabled" if config.auto_approve_safe_read_only else "disabled")
    )


def _configure_logging() -> None:
    log_dir = default_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(
        log_dir / "bot.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    handlers: list[logging.Handler] = [file_handler]
    if sys.stderr is not None:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        handlers.append(stream_handler)
    logging.basicConfig(level=logging.INFO, handlers=handlers)


if __name__ == "__main__":
    main()
