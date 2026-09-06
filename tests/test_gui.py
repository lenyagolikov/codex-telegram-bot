from __future__ import annotations

import unittest

from scooters_codex_telegram_bot.gui import (
    GENERAL_TAB,
    LOCAL_TAB,
    REMOTE_TAB,
    _run_mode_for_tab,
    _shortcut_action,
)


class RunModeTests(unittest.TestCase):
    def test_launch_tabs_select_their_own_mode(self) -> None:
        self.assertEqual(_run_mode_for_tab(LOCAL_TAB, "remote"), "local")
        self.assertEqual(_run_mode_for_tab(REMOTE_TAB, "local"), "remote")

    def test_general_tab_preserves_last_launch_mode(self) -> None:
        self.assertEqual(_run_mode_for_tab(GENERAL_TAB, "remote"), "remote")
        self.assertEqual(_run_mode_for_tab(GENERAL_TAB, "local"), "local")

    def test_invalid_saved_mode_falls_back_to_local(self) -> None:
        self.assertEqual(_run_mode_for_tab(GENERAL_TAB, "invalid"), "local")


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
