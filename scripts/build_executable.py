from __future__ import annotations

import importlib.util
import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path

MACOS_BUNDLE_IDENTIFIER = "com.scooters.codex-telegram-bot.desktop"


def _project_version(project_root: Path) -> str:
    package_init = (
        project_root / "src" / "scooters_codex_telegram_bot" / "__init__.py"
    ).read_text(encoding="utf-8")
    version = re.search(
        r'(?m)^__version__\s*=\s*"(?P<value>[^"]+)"\s*$', package_init
    )
    if version is None:
        raise RuntimeError("project version not found in package __init__.py")
    return version.group("value")


def _finalize_macos_bundle(project_root: Path) -> None:
    bundle_path = project_root / "dist" / "CodexTelegramBot.app"
    plist_path = bundle_path / "Contents" / "Info.plist"
    with plist_path.open("rb") as stream:
        bundle_info = plistlib.load(stream)

    version = _project_version(project_root)
    bundle_info.update(
        {
            "CFBundleDisplayName": "Codex Telegram Bot",
            "CFBundleIdentifier": MACOS_BUNDLE_IDENTIFIER,
            "CFBundleName": "Codex Telegram Bot",
            "CFBundleShortVersionString": version,
            "CFBundleVersion": version,
        }
    )
    with plist_path.open("wb") as stream:
        plistlib.dump(bundle_info, stream, sort_keys=True)

    # Changing Info.plist invalidates PyInstaller's ad-hoc signature. Re-sign
    # the completed bundle so Gatekeeper and Launch Services see the updated
    # version and refresh the cached Dock icon.
    subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", str(bundle_path)],
        check=True,
    )


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    os.environ.setdefault(
        "PYINSTALLER_CONFIG_DIR", str(project_root / "build" / "pyinstaller-cache")
    )
    try:
        import PyInstaller.__main__
    except ImportError as error:
        raise SystemExit(
            "Install the desktop extra: python -m pip install '.[desktop]'"
        ) from error

    icon_path = (
        project_root
        / "src"
        / "scooters_codex_telegram_bot"
        / "assets"
        / "app-icon.png"
    )
    package_path = project_root / "src" / "scooters_codex_telegram_bot"
    arguments = [
        str(project_root / "run_desktop.py"),
        "--name=CodexTelegramBot",
        "--windowed",
        "--clean",
        "--noconfirm",
        f"--paths={project_root / 'src'}",
        f"--distpath={project_root / 'dist'}",
        f"--workpath={project_root / 'build' / 'pyinstaller'}",
        f"--specpath={project_root / 'build'}",
        f"--icon={icon_path}",
        f"--add-data={icon_path}:scooters_codex_telegram_bot/assets",
        f"--add-data={package_path}:remote_runtime/scooters_codex_telegram_bot",
        "--collect-all=customtkinter",
        "--collect-submodules=keyring.backends",
    ]
    if sys.platform == "darwin":
        arguments.extend(
            [
                "--onedir",
                f"--osx-bundle-identifier={MACOS_BUNDLE_IDENTIFIER}",
            ]
        )
    else:
        arguments.append("--onefile")
    if importlib.util.find_spec("faster_whisper") is not None:
        arguments.extend(
            [
                "--collect-all=faster_whisper",
                "--collect-all=ctranslate2",
            ]
        )
    PyInstaller.__main__.run(arguments)
    if sys.platform == "darwin":
        _finalize_macos_bundle(project_root)


if __name__ == "__main__":
    main()
