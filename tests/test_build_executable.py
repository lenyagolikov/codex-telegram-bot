from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


def _load_build_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "build_executable.py"
    spec = importlib.util.spec_from_file_location("build_executable", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProjectVersionTests(unittest.TestCase):
    def test_reads_package_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            package_path = project_root / "src" / "codex_telegram_bot"
            package_path.mkdir(parents=True)
            (package_path / "__init__.py").write_text(
                '__version__ = "1.2.3"\n',
                encoding="utf-8",
            )

            self.assertEqual(_load_build_module()._project_version(project_root), "1.2.3")

    def test_macos_bundle_uses_distinct_desktop_identifier(self) -> None:
        self.assertEqual(
            _load_build_module().MACOS_BUNDLE_IDENTIFIER,
            "com.lenyagolikov.codex-telegram-bot.desktop",
        )


if __name__ == "__main__":
    unittest.main()
