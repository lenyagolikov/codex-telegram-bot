from __future__ import annotations

import unittest

from codex_telegram_bot.app_server import APP_SERVER_DISABLED_FEATURES


class AppServerConfigurationTests(unittest.TestCase):
    def test_uses_native_shell_instead_of_programmable_exec(self) -> None:
        self.assertIn("unified_exec", APP_SERVER_DISABLED_FEATURES)
        self.assertIn("code_mode", APP_SERVER_DISABLED_FEATURES)


if __name__ == "__main__":
    unittest.main()
