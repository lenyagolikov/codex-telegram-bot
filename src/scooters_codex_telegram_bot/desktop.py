from __future__ import annotations

import sys

from .main import main as application_main


def main() -> None:
    if len(sys.argv) == 1:
        application_main(["--gui"])
    else:
        application_main()


if __name__ == "__main__":
    main()
