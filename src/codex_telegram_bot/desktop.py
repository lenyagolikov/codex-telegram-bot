from __future__ import annotations

import subprocess
import sys
from contextlib import suppress
from pathlib import Path

from .main import main as application_main

LAUNCH_SERVICES_REGISTER = Path(
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
    "LaunchServices.framework/Support/lsregister"
)


def _app_bundle_for_executable(executable: str | Path) -> Path | None:
    path = Path(executable).expanduser().absolute()
    for parent in path.parents:
        if parent.suffix == ".app" and (parent / "Contents" / "Info.plist").is_file():
            return parent
    return None


def _register_current_macos_bundle() -> None:
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return
    bundle = _app_bundle_for_executable(sys.executable)
    if bundle is None or not LAUNCH_SERVICES_REGISTER.is_file():
        return
    # Registration improves Finder and Dock integration but must never prevent
    # the settings window from opening.
    with suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            [str(LAUNCH_SERVICES_REGISTER), "-f", str(bundle)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )


def main() -> None:
    _register_current_macos_bundle()
    if len(sys.argv) == 1:
        application_main(["--gui"])
    else:
        application_main()


if __name__ == "__main__":
    main()
