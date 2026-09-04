from __future__ import annotations

import unittest

from scooters_codex_telegram_bot.gui import _shortcut_action


class ShortcutTests(unittest.TestCase):
    def test_latin_keysyms_are_supported_on_every_platform(self) -> None:
        self.assertEqual(_shortcut_action("a", 999, "darwin"), "select_all")
        self.assertEqual(_shortcut_action("C", 999, "win32"), "copy")
        self.assertEqual(_shortcut_action("v", 999, "linux"), "paste")
        self.assertEqual(_shortcut_action("x", 999, "linux"), "cut")

    def test_macos_physical_keys_work_with_non_latin_layout(self) -> None:
        self.assertEqual(
            _shortcut_action("Cyrillic_ef", 999, "darwin"), "select_all"
        )
        self.assertEqual(_shortcut_action("Cyrillic_es", 999, "darwin"), "copy")
        self.assertEqual(_shortcut_action("Cyrillic_em", 999, "darwin"), "paste")
        self.assertEqual(_shortcut_action("Cyrillic_che", 999, "darwin"), "cut")

    def test_macos_cyrillic_character_keysyms_are_supported(self) -> None:
        self.assertEqual(_shortcut_action("ф", 999, "darwin"), "select_all")
        self.assertEqual(_shortcut_action("с", 999, "darwin"), "copy")
        self.assertEqual(_shortcut_action("м", 999, "darwin"), "paste")
        self.assertEqual(_shortcut_action("ч", 999, "darwin"), "cut")

    def test_unrelated_shortcut_is_ignored(self) -> None:
        self.assertIsNone(_shortcut_action("z", 6, "darwin"))


if __name__ == "__main__":
    unittest.main()
