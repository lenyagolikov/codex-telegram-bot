from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codex_telegram_bot.gui import (
    CODEX_MODEL_CHOICES,
    DEFAULT_OPTION,
    GENERAL_TAB,
    LOCAL_TAB,
    REMOTE_TAB,
    _approval_reviewer_display,
    _approval_reviewer_value,
    _option_config_value,
    _option_display_value,
    _run_mode_for_tab,
    _set_macos_application_icon,
    _shortcut_action,
)


class ApprovalReviewerTests(unittest.TestCase):
    def test_maps_auto_review_to_user_facing_label(self) -> None:
        self.assertEqual(
            _approval_reviewer_display("auto_review"), "Подтверждать за меня"
        )
        self.assertEqual(
            _approval_reviewer_value("Подтверждать за меня"), "auto_review"
        )


class ModelOptionTests(unittest.TestCase):
    def test_current_codex_models_are_available(self) -> None:
        self.assertEqual(CODEX_MODEL_CHOICES[0], DEFAULT_OPTION)
        self.assertIn("gpt-6-astra", CODEX_MODEL_CHOICES)
        self.assertIn("gpt-5.6-sol", CODEX_MODEL_CHOICES)
        self.assertIn("gpt-5.6-terra", CODEX_MODEL_CHOICES)
        self.assertIn("gpt-5.6-luna", CODEX_MODEL_CHOICES)
        self.assertIn("gpt-5.5", CODEX_MODEL_CHOICES)
        self.assertIn("gpt-5.3-codex-spark", CODEX_MODEL_CHOICES)

    def test_default_model_round_trips_as_empty_configuration(self) -> None:
        self.assertEqual(_option_display_value("CODEX_MODEL", ""), DEFAULT_OPTION)
        self.assertEqual(_option_config_value("CODEX_MODEL", DEFAULT_OPTION), "")


class RunModeTests(unittest.TestCase):
    def test_launch_tabs_select_their_own_mode(self) -> None:
        self.assertEqual(_run_mode_for_tab(LOCAL_TAB, "remote"), "local")
        self.assertEqual(_run_mode_for_tab(REMOTE_TAB, "local"), "remote")

    def test_general_tab_preserves_last_launch_mode(self) -> None:
        self.assertEqual(_run_mode_for_tab(GENERAL_TAB, "remote"), "remote")
        self.assertEqual(_run_mode_for_tab(GENERAL_TAB, "local"), "local")

    def test_invalid_saved_mode_falls_back_to_local(self) -> None:
        self.assertEqual(_run_mode_for_tab(GENERAL_TAB, "invalid"), "local")


class MacosApplicationIconTests(unittest.TestCase):
    def test_sets_native_application_icon_on_macos(self) -> None:
        loaded_image = object()
        image_loader = Mock()
        image_loader.initWithContentsOfFile_.return_value = loaded_image
        image_class = Mock()
        image_class.alloc.return_value = image_loader
        application = Mock()
        application_class = Mock()
        application_class.sharedApplication.return_value = application
        appkit = types.ModuleType("AppKit")
        appkit.NSApplication = application_class
        appkit.NSImage = image_class

        with (
            patch("codex_telegram_bot.gui.sys.platform", "darwin"),
            patch.dict(sys.modules, {"AppKit": appkit}),
        ):
            result = _set_macos_application_icon(Path("/tmp/app-icon.png"))

        self.assertIs(result, loaded_image)
        image_loader.initWithContentsOfFile_.assert_called_once_with(
            "/tmp/app-icon.png"
        )
        application.setApplicationIconImage_.assert_called_once_with(loaded_image)

    def test_does_nothing_outside_macos(self) -> None:
        with patch("codex_telegram_bot.gui.sys.platform", "linux"):
            self.assertIsNone(
                _set_macos_application_icon(Path("/tmp/app-icon.png"))
            )


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
