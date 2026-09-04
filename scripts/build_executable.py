from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


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

    arguments = [
        str(project_root / "run_desktop.py"),
        "--name=CodexTelegramBot",
        "--windowed",
        "--clean",
        "--noconfirm",
        f"--distpath={project_root / 'dist'}",
        f"--workpath={project_root / 'build' / 'pyinstaller'}",
        f"--specpath={project_root / 'build'}",
        "--collect-submodules=keyring.backends",
    ]
    if sys.platform == "darwin":
        arguments.extend(
            [
                "--onedir",
                "--osx-bundle-identifier=com.scooters.codex-telegram-bot",
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


if __name__ == "__main__":
    main()
