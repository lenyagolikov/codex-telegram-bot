from __future__ import annotations

import getpass
import importlib.util
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from .config import (
    RUNTIME_CONFIG_KEYS,
    Config,
    ConfigError,
    default_config_path,
    read_dotenv,
    write_dotenv,
)
from .remote import RemoteServiceManager, RemoteSettings
from .secrets import SecretStoreError, read_telegram_token, store_telegram_token
from .service import ServiceError, ServiceManager

ACCENT = "#6C5CE7"
ACCENT_HOVER = "#5948D8"
SUCCESS = "#2ECF82"
WARNING = "#F5A524"
MUTED = ("#687086", "#8E96AA")
WINDOW_BACKGROUND = ("#F2F4F8", "#0D0F14")
CARD_BACKGROUND = ("#FFFFFF", "#171A22")
FIELD_BACKGROUND = ("#F7F8FB", "#20242E")
GENERAL_TAB = "Общие настройки"
LOCAL_TAB = "Локальный запуск"
REMOTE_TAB = "Удалённый запуск"
DEFAULT_OPTION = "По умолчанию"
CODEX_MODEL_CHOICES = [
    DEFAULT_OPTION,
    "gpt-6-astra",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.3-codex-spark",
]
APPROVAL_REVIEWER_LABELS = {
    "user": "Спрашивать меня",
    "auto_review": "Подтверждать за меня",
}


def _approval_reviewer_display(value: str) -> str:
    return APPROVAL_REVIEWER_LABELS.get(value, APPROVAL_REVIEWER_LABELS["user"])


def _approval_reviewer_value(display_value: str) -> str:
    for value, label in APPROVAL_REVIEWER_LABELS.items():
        if display_value == label:
            return value
    return "user"


def _option_display_value(key: str, saved_value: str) -> str:
    if key in {"CODEX_MODEL", "CODEX_REASONING_EFFORT"} and not saved_value:
        return DEFAULT_OPTION
    if key == "APPROVALS_REVIEWER":
        return _approval_reviewer_display(saved_value)
    return saved_value


def _option_config_value(key: str, display_value: str) -> str:
    if (
        key in {"CODEX_MODEL", "CODEX_REASONING_EFFORT"}
        and display_value == DEFAULT_OPTION
    ):
        return ""
    if key == "APPROVALS_REVIEWER":
        return _approval_reviewer_value(display_value)
    return display_value


def _set_macos_application_icon(icon_path: Path) -> object | None:
    if sys.platform != "darwin":
        return None
    try:
        from AppKit import NSApplication, NSImage
    except ImportError:
        return None
    image = NSImage.alloc().initWithContentsOfFile_(str(icon_path))
    if image is None:
        return None
    NSApplication.sharedApplication().setApplicationIconImage_(image)
    return image


def _run_mode_for_tab(tab_name: str, current_mode: str) -> str:
    if tab_name == LOCAL_TAB:
        return "local"
    if tab_name == REMOTE_TAB:
        return "remote"
    return current_mode if current_mode in {"local", "remote"} else "local"


def _shortcut_action(keysym: str, keycode: int, platform: str) -> str | None:
    actions = {
        "a": "select_all",
        "c": "copy",
        "v": "paste",
        "x": "cut",
        # Physical A/C/V/X keys in the Russian keyboard layout. Depending on
        # the Tcl/Tk version, keysyms arrive either as names or as characters.
        "cyrillic_ef": "select_all",
        "cyrillic_es": "copy",
        "cyrillic_em": "paste",
        "cyrillic_che": "cut",
        "ф": "select_all",
        "с": "copy",
        "м": "paste",
        "ч": "cut",
    }
    if action := actions.get(keysym.lower()):
        return action
    platform_keycodes = {
        "darwin": {0: "select_all", 8: "copy", 9: "paste", 7: "cut"},
        "win32": {65: "select_all", 67: "copy", 86: "paste", 88: "cut"},
    }
    keycodes = platform_keycodes.get(
        platform,
        {38: "select_all", 54: "copy", 55: "paste", 53: "cut"},
    )
    return keycodes.get(keycode)


def launch_gui(config_path: Path | None = None) -> None:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox

        import customtkinter as ctk
        from PIL import Image
    except ImportError as error:
        raise SystemExit(
            "The desktop interface requires the desktop extra; "
            "run: python -m pip install '.[desktop]'"
        ) from error

    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

    selected_config_path = (config_path or default_config_path()).expanduser().resolve()
    root = ctk.CTk(fg_color=WINDOW_BACKGROUND)
    root.title("Codex Telegram Bot")
    root.geometry("860x640")
    root.minsize(720, 540)

    icon_path = Path(__file__).resolve().parent / "assets" / "app-icon.png"
    header_icon = None
    macos_application_icon = None
    if icon_path.is_file():
        macos_application_icon = _set_macos_application_icon(icon_path)
        icon_image = Image.open(icon_path)
        header_icon = ctk.CTkImage(
            light_image=icon_image,
            dark_image=icon_image,
            size=(58, 58),
        )
        try:
            native_icon = tk.PhotoImage(file=str(icon_path))
            root.iconphoto(True, native_icon)
            root._native_icon = native_icon  # type: ignore[attr-defined]
        except tk.TclError:
            pass
    root._macos_application_icon = macos_application_icon  # type: ignore[attr-defined]

    class SettingsWindow:
        def __init__(self) -> None:
            self.values = read_dotenv(selected_config_path)
            if not self.values.get("TELEGRAM_BOT_TOKEN"):
                self.values["TELEGRAM_BOT_TOKEN"] = read_telegram_token() or ""
            self.service = ServiceManager(selected_config_path)
            self.variables: dict[str, tk.Variable] = {}
            self.action_buttons: list[ctk.CTkButton] = []
            self.edit_widgets: list[tk.Entry | tk.Text] = []
            self.context_edit_widget: tk.Entry | tk.Text | None = None
            self.status_label: ctk.CTkLabel
            self.token_entry: ctk.CTkEntry
            self.run_mode = _run_mode_for_tab(
                "", self.values.get("RUN_MODE", "local")
            )
            self._build()
            self._install_edit_support()
            self.tabs.set(GENERAL_TAB)
            self._change_tab()

        def _build(self) -> None:
            page = ctk.CTkFrame(root, fg_color="transparent")
            page.pack(fill="both", expand=True, padx=20, pady=16)
            page.grid_columnconfigure(0, weight=1)
            page.grid_rowconfigure(1, weight=1)

            header = ctk.CTkFrame(page, fg_color="transparent")
            header.grid(row=0, column=0, sticky="ew", pady=(0, 14))
            if header_icon is not None:
                ctk.CTkLabel(header, text="", image=header_icon).pack(
                    side="left", padx=(0, 16)
                )
            title_block = ctk.CTkFrame(header, fg_color="transparent")
            title_block.pack(side="left", fill="x", expand=True)
            ctk.CTkLabel(
                title_block,
                text="Codex Telegram Bot",
                font=ctk.CTkFont(size=27, weight="bold"),
                anchor="w",
            ).pack(fill="x")
            ctk.CTkLabel(
                title_block,
                text="Настройте личный Telegram-интерфейс для Codex",
                text_color=MUTED,
                font=ctk.CTkFont(size=14),
                anchor="w",
            ).pack(fill="x", pady=(4, 0))

            content = ctk.CTkFrame(
                page,
                fg_color=CARD_BACKGROUND,
                corner_radius=20,
                border_width=1,
                border_color=("#E3E6ED", "#292D38"),
            )
            content.grid(row=1, column=0, sticky="nsew")

            self.tabs = ctk.CTkTabview(
                content,
                fg_color="transparent",
                segmented_button_selected_color=ACCENT,
                segmented_button_selected_hover_color=ACCENT_HOVER,
                segmented_button_unselected_hover_color=("#E9EAF2", "#303441"),
                corner_radius=16,
            )
            self.tabs.pack(fill="both", expand=True, padx=18, pady=(12, 6))
            general_tab = self.tabs.add(GENERAL_TAB)
            local_tab = self.tabs.add(LOCAL_TAB)
            remote_tab = self.tabs.add(REMOTE_TAB)
            general = self._scrollable_tab(general_tab)
            local = self._scrollable_tab(local_tab)
            remote = self._scrollable_tab(remote_tab)
            general.grid_columnconfigure(1, weight=1)
            local.grid_columnconfigure(1, weight=1)
            remote.grid_columnconfigure(1, weight=1)

            self._add_token_entry(general, 0)
            self._add_entry(
                general,
                1,
                "Разрешённые user ID",
                "TELEGRAM_ALLOWED_USER_IDS",
                hint="Несколько ID указываются через запятую",
            )
            self._add_option(
                general,
                2,
                "Модель",
                "CODEX_MODEL",
                CODEX_MODEL_CHOICES,
                default="",
            )
            self._add_option(
                general,
                3,
                "Reasoning effort",
                "CODEX_REASONING_EFFORT",
                ["По умолчанию", "low", "medium", "high", "xhigh", "max", "ultra"],
            )

            self._add_option(
                general,
                4,
                "Подтверждения Codex",
                "APPROVALS_REVIEWER",
                list(APPROVAL_REVIEWER_LABELS.values()),
                default="user",
            )
            self._add_option(
                general,
                5,
                "Сеть Telegram",
                "TELEGRAM_IP_FAMILY",
                ["auto", "ipv4", "ipv6"],
                default="auto",
            )
            self._add_entry(
                general,
                6,
                "Таймаут polling",
                "POLL_TIMEOUT_SECONDS",
                default="30",
                suffix="сек.",
            )
            self._add_switch(
                general,
                7,
                "Распознавать голосовые сообщения",
                "VOICE_TRANSCRIPTION_ENABLED",
            )
            self._add_entry(
                general, 8, "Whisper-модель", "WHISPER_MODEL", default="small"
            )
            self._add_entry(
                general, 9, "Язык Whisper", "WHISPER_LANGUAGE", default="ru"
            )
            self._add_switch(
                general,
                10,
                "Автоматически подтверждать безопасное чтение",
                "AUTO_APPROVE_SAFE_READ_ONLY",
            )
            self._add_entry(
                general,
                11,
                "Разрешённые корни чтения",
                "AUTO_APPROVE_READ_ROOTS",
                hint="Пути через запятую; по умолчанию — рабочая папка",
            )

            warning = ctk.CTkFrame(
                general,
                fg_color=("#FFF7E6", "#2B2418"),
                corner_radius=12,
            )
            warning.grid(
                row=12, column=0, columnspan=3, sticky="ew", padx=14, pady=(16, 8)
            )
            ctk.CTkLabel(
                warning,
                text=(
                    "Автоподтверждение работает только для чтения, поиска и "
                    "просмотра файлов внутри разрешённых папок."
                ),
                text_color=("#8A5A00", "#F6C66C"),
                justify="left",
                anchor="w",
                wraplength=680,
            ).pack(fill="x", padx=14, pady=11)

            ctk.CTkLabel(
                general,
                text=f"Файл настроек  ·  {selected_config_path}",
                text_color=MUTED,
                font=ctk.CTkFont(size=12),
                anchor="w",
                wraplength=690,
            ).grid(
                row=13, column=0, columnspan=3, sticky="ew", padx=14, pady=(12, 6)
            )

            self._add_path_entry(
                local,
                0,
                "Рабочая папка Codex",
                "CODEX_CWD",
                choose_directory=True,
            )
            self._add_path_entry(
                local,
                1,
                "Команда Codex",
                "CODEX_BIN",
                choose_directory=False,
            )
            ctk.CTkLabel(
                local,
                text=(
                    "Локальный запуск использует Codex и рабочую папку на этом "
                    "компьютере."
                ),
                text_color=MUTED,
                font=ctk.CTkFont(size=12),
                anchor="w",
                justify="left",
                wraplength=660,
            ).grid(row=2, column=0, columnspan=3, sticky="ew", padx=14, pady=(12, 6))

            self._add_entry(
                remote,
                0,
                "SSH-хост",
                "REMOTE_SSH_HOST",
                hint="Например, host.example.net",
            )
            self._add_entry(
                remote,
                1,
                "SSH-пользователь",
                "REMOTE_SSH_USER",
                default=getpass.getuser(),
            )
            self._add_entry(
                remote,
                2,
                "SSH-порт",
                "REMOTE_SSH_PORT",
                default="22",
            )
            self._add_path_entry(
                remote,
                3,
                "SSH-ключ",
                "REMOTE_SSH_IDENTITY_FILE",
                choose_directory=False,
            )
            self._add_entry(
                remote,
                4,
                "Папка установки",
                "REMOTE_INSTALL_DIR",
                default="~/.local/share/codex-telegram-bot",
            )
            self._add_entry(
                remote,
                5,
                "Рабочая папка Codex",
                "REMOTE_CODEX_CWD",
                default="~/arcadia",
            )
            self._add_entry(
                remote, 6, "Команда Codex", "REMOTE_CODEX_BIN", default="codex"
            )
            self._add_entry(
                remote,
                7,
                "Команда Python",
                "REMOTE_PYTHON_BIN",
                default="/usr/bin/python3",
            )
            self._button(
                remote,
                "Проверить подключение",
                self._test_remote_connection,
                secondary=True,
                width=185,
            ).grid(row=8, column=1, sticky="w", padx=14, pady=(16, 6))
            ctk.CTkLabel(
                remote,
                text=(
                    "На сервер передаются только runtime и настройки бота. "
                    "Токен хранится там в файле с правами 600."
                ),
                text_color=MUTED,
                font=ctk.CTkFont(size=12),
                anchor="w",
                justify="left",
                wraplength=660,
            ).grid(row=9, column=0, columnspan=3, sticky="ew", padx=14, pady=(10, 6))

            footer = ctk.CTkFrame(page, fg_color="transparent")
            footer.grid(row=2, column=0, sticky="ew", pady=(12, 0))
            self.status_card = ctk.CTkFrame(
                footer,
                fg_color=CARD_BACKGROUND,
                corner_radius=14,
                border_width=1,
                border_color=("#E3E6ED", "#292D38"),
            )
            self.status_card.pack(side="left")
            self.status_label = ctk.CTkLabel(
                self.status_card,
                text="●  Проверка…",
                text_color=MUTED,
                font=ctk.CTkFont(size=13, weight="bold"),
            )
            self.status_label.pack(side="left", padx=(14, 8), pady=10)
            ctk.CTkButton(
                self.status_card,
                text="↻",
                width=34,
                height=30,
                fg_color="transparent",
                hover_color=("#ECEEF4", "#2A2E39"),
                text_color=("#3A4050", "#DDE1EA"),
                command=self._refresh_status,
            ).pack(side="right", padx=(0, 6), pady=5)
            self.management_button = self._button(
                footer,
                "Управление",
                self._open_service_controls,
                secondary=True,
                width=118,
            )
            self.management_button.pack(side="left", padx=(10, 0))

            self.run_mode_hint = ctk.CTkLabel(
                footer,
                text="Выберите локальный или удалённый запуск",
                text_color=MUTED,
                font=ctk.CTkFont(size=13, weight="bold"),
            )

            primary_actions = ctk.CTkFrame(footer, fg_color="transparent")
            primary_actions.pack(side="right")
            self._button(
                primary_actions,
                "Сохранить",
                self._save,
                secondary=True,
                width=112,
            ).pack(side="left", padx=(0, 8))
            self.start_button = self._button(
                primary_actions,
                "Сохранить и запустить",
                self._save_and_start,
                width=245,
            )
            self.start_button.pack(side="left")
            self.tabs.configure(command=self._change_tab)

        @staticmethod
        def _scrollable_tab(parent):
            body = ctk.CTkScrollableFrame(
                parent,
                fg_color="transparent",
                corner_radius=0,
                scrollbar_button_color=("#C9CDD8", "#343947"),
                scrollbar_button_hover_color=("#AEB4C2", "#464C5C"),
            )
            body.pack(fill="both", expand=True, padx=(0, 2), pady=(2, 0))
            return body

        def _add_token_entry(self, parent, row: int) -> None:
            variable = tk.StringVar(value=self.values.get("TELEGRAM_BOT_TOKEN", ""))
            self.variables["TELEGRAM_BOT_TOKEN"] = variable
            self._field_label(parent, row, "Telegram Bot Token")
            field = ctk.CTkFrame(parent, fg_color="transparent")
            field.grid(row=row, column=1, columnspan=2, sticky="ew", padx=14, pady=8)
            field.grid_columnconfigure(0, weight=1)
            self.token_entry = ctk.CTkEntry(
                field,
                textvariable=variable,
                show="•",
                height=40,
                corner_radius=10,
                border_width=1,
                fg_color=FIELD_BACKGROUND,
            )
            self.token_entry.grid(row=0, column=0, sticky="ew")
            self._register_edit_widget(self.token_entry)
            ctk.CTkButton(
                field,
                text="Показать",
                width=86,
                height=40,
                corner_radius=10,
                fg_color="transparent",
                hover_color=("#ECEEF4", "#2A2E39"),
                text_color=("#475066", "#C8CDDA"),
                command=self._toggle_token,
            ).grid(row=0, column=1, padx=(8, 0))

        def _add_entry(
            self,
            parent,
            row: int,
            label: str,
            key: str,
            default: str = "",
            *,
            hint: str | None = None,
            suffix: str | None = None,
        ) -> None:
            variable = tk.StringVar(value=self.values.get(key, default))
            self.variables[key] = variable
            self._field_label(parent, row, label, hint)
            entry = ctk.CTkEntry(
                parent,
                textvariable=variable,
                height=40,
                corner_radius=10,
                border_width=1,
                fg_color=FIELD_BACKGROUND,
            )
            entry.grid(
                row=row,
                column=1,
                columnspan=1 if suffix else 2,
                sticky="ew",
                padx=14,
                pady=8,
            )
            self._register_edit_widget(entry)
            if suffix:
                ctk.CTkLabel(parent, text=suffix, text_color=MUTED).grid(
                    row=row, column=2, sticky="w", padx=(0, 14)
                )

        def _add_path_entry(
            self,
            parent,
            row: int,
            label: str,
            key: str,
            *,
            choose_directory: bool,
        ) -> None:
            if key == "CODEX_CWD":
                default = str(Path.home())
            elif key == "CODEX_BIN":
                default = shutil.which("codex") or "codex"
            else:
                default = ""
            variable = tk.StringVar(value=self.values.get(key, default))
            self.variables[key] = variable
            self._field_label(parent, row, label)
            entry = ctk.CTkEntry(
                parent,
                textvariable=variable,
                height=40,
                corner_radius=10,
                border_width=1,
                fg_color=FIELD_BACKGROUND,
            )
            entry.grid(row=row, column=1, sticky="ew", padx=14, pady=8)
            self._register_edit_widget(entry)

            def browse() -> None:
                if choose_directory:
                    selected = filedialog.askdirectory(
                        parent=root, initialdir=variable.get() or str(Path.home())
                    )
                else:
                    selected = filedialog.askopenfilename(parent=root)
                if selected:
                    variable.set(selected)

            ctk.CTkButton(
                parent,
                text="Выбрать",
                command=browse,
                width=92,
                height=40,
                corner_radius=10,
                fg_color=("#EDEBFF", "#292540"),
                hover_color=("#DDD9FF", "#353052"),
                text_color=("#5144C7", "#B9B1FF"),
            ).grid(row=row, column=2, sticky="e", padx=(0, 14), pady=8)

        def _add_option(
            self,
            parent,
            row: int,
            label: str,
            key: str,
            choices: list[str],
            default: str = "",
        ) -> None:
            saved_value = self.values.get(key, default)
            display_value = _option_display_value(key, saved_value)
            variable = tk.StringVar(value=display_value)
            self.variables[key] = variable
            self._field_label(parent, row, label)
            ctk.CTkOptionMenu(
                parent,
                variable=variable,
                values=choices,
                height=40,
                corner_radius=10,
                fg_color=FIELD_BACKGROUND,
                button_color=("#E4E1FF", "#34304B"),
                button_hover_color=("#D8D3FF", "#403A5C"),
                text_color=("#202432", "#F1F3F8"),
                dropdown_fg_color=CARD_BACKGROUND,
                dropdown_hover_color=("#EDEBFF", "#302C46"),
            ).grid(row=row, column=1, columnspan=2, sticky="ew", padx=14, pady=8)

        def _add_switch(self, parent, row: int, label: str, key: str) -> None:
            enabled = self.values.get(key, "false").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            variable = tk.BooleanVar(value=enabled)
            self.variables[key] = variable
            ctk.CTkSwitch(
                parent,
                text=label,
                variable=variable,
                progress_color=ACCENT,
                button_hover_color=ACCENT_HOVER,
                font=ctk.CTkFont(size=14, weight="bold"),
            ).grid(
                row=row,
                column=0,
                columnspan=3,
                sticky="w",
                padx=14,
                pady=12,
            )

        @staticmethod
        def _field_label(parent, row: int, label: str, hint: str | None = None) -> None:
            label_frame = ctk.CTkFrame(parent, fg_color="transparent")
            label_frame.grid(row=row, column=0, sticky="w", padx=14, pady=8)
            ctk.CTkLabel(
                label_frame,
                text=label,
                font=ctk.CTkFont(size=14, weight="bold"),
                anchor="w",
            ).pack(anchor="w")
            if hint:
                ctk.CTkLabel(
                    label_frame,
                    text=hint,
                    text_color=MUTED,
                    font=ctk.CTkFont(size=11),
                    anchor="w",
                ).pack(anchor="w", pady=(2, 0))

        def _button(
            self,
            parent,
            text: str,
            callback: Callable[[], None],
            *,
            secondary: bool = False,
            width: int = 130,
        ):
            if secondary:
                colors = {
                    "fg_color": CARD_BACKGROUND,
                    "hover_color": ("#E7E9F0", "#242832"),
                    "text_color": ("#343A4A", "#E4E7EF"),
                    "border_width": 1,
                    "border_color": ("#D6DAE4", "#343947"),
                }
            else:
                colors = {
                    "fg_color": ACCENT,
                    "hover_color": ACCENT_HOVER,
                    "text_color": "#FFFFFF",
                    "border_width": 0,
                }
            button = ctk.CTkButton(
                parent,
                text=text,
                command=callback,
                width=width,
                height=40,
                corner_radius=11,
                font=ctk.CTkFont(size=13, weight="bold"),
                **colors,
            )
            self.action_buttons.append(button)
            return button

        def _toggle_token(self) -> None:
            self.token_entry.configure(show="" if self.token_entry.cget("show") else "•")

        def _register_edit_widget(self, widget) -> None:
            editable = self._editable_widget(widget)
            if editable is not None:
                self.edit_widgets.append(editable)

        def _install_edit_support(self) -> None:
            modifiers = ["Control"]
            if sys.platform == "darwin":
                modifiers.insert(0, "Command")
            for widget in self.edit_widgets:
                for modifier in modifiers:
                    widget.bind(
                        f"<{modifier}-KeyPress>",
                        self._handle_edit_shortcut,
                        add="+",
                    )
                widget.bind("<Button-2>", self._show_edit_context_menu, add="+")
                widget.bind("<Button-3>", self._show_edit_context_menu, add="+")
                if sys.platform == "darwin":
                    widget.bind(
                        "<Control-Button-1>",
                        self._show_edit_context_menu,
                        add="+",
                    )

            accelerator = "⌘" if sys.platform == "darwin" else "Ctrl+"
            menu_bar = tk.Menu(root)
            edit_menu = tk.Menu(menu_bar, tearoff=False)
            edit_menu.add_command(
                label="Вырезать",
                accelerator=f"{accelerator}X",
                command=lambda: self._perform_edit_action("cut"),
            )
            edit_menu.add_command(
                label="Копировать",
                accelerator=f"{accelerator}C",
                command=lambda: self._perform_edit_action("copy"),
            )
            edit_menu.add_command(
                label="Вставить",
                accelerator=f"{accelerator}V",
                command=lambda: self._perform_edit_action("paste"),
            )
            edit_menu.add_separator()
            edit_menu.add_command(
                label="Выбрать всё",
                accelerator=f"{accelerator}A",
                command=lambda: self._perform_edit_action("select_all"),
            )
            menu_bar.add_cascade(label="Правка", menu=edit_menu)
            root.configure(menu=menu_bar)
            self._menu_bar = menu_bar

            context_menu = tk.Menu(root, tearoff=False)
            context_menu.add_command(
                label="Вырезать",
                command=lambda: self._perform_edit_action("cut", from_context=True),
            )
            context_menu.add_command(
                label="Копировать",
                command=lambda: self._perform_edit_action("copy", from_context=True),
            )
            context_menu.add_command(
                label="Вставить",
                command=lambda: self._perform_edit_action("paste", from_context=True),
            )
            context_menu.add_separator()
            context_menu.add_command(
                label="Выбрать всё",
                command=lambda: self._perform_edit_action(
                    "select_all", from_context=True
                ),
            )
            self._context_menu = context_menu

        def _handle_edit_shortcut(self, event):
            action = _shortcut_action(event.keysym, event.keycode, sys.platform)
            if action is None:
                return None
            return self._apply_edit_action(event.widget, action)

        def _show_edit_context_menu(self, event):
            widget = self._editable_widget(event.widget)
            if widget is None:
                return None
            self.context_edit_widget = widget
            widget.focus_set()
            try:
                self._context_menu.tk_popup(event.x_root, event.y_root)
            finally:
                self._context_menu.grab_release()
            return "break"

        def _perform_edit_action(
            self, action: str, *, from_context: bool = False
        ) -> None:
            widget = self.context_edit_widget if from_context else root.focus_get()
            if widget is not None:
                self._apply_edit_action(widget, action)
            if from_context:
                self.context_edit_widget = None

        @staticmethod
        def _editable_widget(widget):
            if isinstance(widget, (tk.Entry, tk.Text)):
                return widget
            for attribute in ("_entry", "_textbox"):
                candidate = getattr(widget, attribute, None)
                if isinstance(candidate, (tk.Entry, tk.Text)):
                    return candidate
            return None

        def _apply_edit_action(self, widget, action: str):
            widget = self._editable_widget(widget)
            if widget is None:
                return None

            if action == "select_all":
                if isinstance(widget, tk.Entry):
                    widget.selection_range(0, tk.END)
                    widget.icursor(tk.END)
                else:
                    widget.tag_add(tk.SEL, "1.0", "end-1c")
                    widget.mark_set(tk.INSERT, "end-1c")
                return "break"

            if action in {"copy", "cut"}:
                try:
                    if isinstance(widget, tk.Entry):
                        first = int(widget.index("sel.first"))
                        last = int(widget.index("sel.last"))
                        selected_text = widget.get()[first:last]
                    else:
                        first = widget.index("sel.first")
                        last = widget.index("sel.last")
                        selected_text = widget.get(first, last)
                except tk.TclError:
                    return "break"
                root.clipboard_clear()
                root.clipboard_append(selected_text)
                if action == "cut":
                    widget.delete(first, last)
                return "break"

            try:
                clipboard_text = root.clipboard_get()
            except tk.TclError:
                return "break"
            try:
                if isinstance(widget, tk.Entry):
                    if widget.selection_present():
                        widget.delete("sel.first", "sel.last")
                    widget.insert(widget.index("insert"), clipboard_text)
                else:
                    if widget.tag_ranges(tk.SEL):
                        widget.delete("sel.first", "sel.last")
                    widget.insert(tk.INSERT, clipboard_text)
            except tk.TclError:
                return "break"
            return "break"

        def _change_tab(self) -> None:
            selected_tab = self.tabs.get()
            self.run_mode = _run_mode_for_tab(selected_tab, self.run_mode)
            if selected_tab == GENERAL_TAB:
                self.status_card.pack_forget()
                self.management_button.pack_forget()
                self.start_button.pack_forget()
                self.run_mode_hint.pack(side="left")
                return

            self.run_mode_hint.pack_forget()
            self.status_card.pack(side="left")
            self.management_button.pack(side="left", padx=(10, 0))
            self.start_button.configure(
                text=(
                    "Сохранить и запустить на сервере"
                    if self._is_remote()
                    else "Сохранить и запустить локально"
                )
            )
            self.start_button.pack(side="left")
            self._refresh_status()

        def _is_remote(self) -> bool:
            return self.run_mode == "remote"

        def _configuration_values(self) -> dict[str, str]:
            result = dict(self.values)
            result["RUN_MODE"] = "remote" if self._is_remote() else "local"
            for key, variable in self.variables.items():
                value = variable.get()
                if isinstance(value, bool):
                    result[key] = "true" if value else "false"
                else:
                    normalized = str(value).strip()
                    result[key] = _option_config_value(key, normalized)
            codex_bin = result.get("CODEX_BIN", "codex") or "codex"
            if resolved_codex_bin := shutil.which(codex_bin):
                # Preserve the symlink path: npm installs `codex` beside `node`,
                # which lets the background service reconstruct a usable PATH.
                result["CODEX_BIN"] = os.path.abspath(resolved_codex_bin)
            result.setdefault("VOICE_MAX_DURATION_SECONDS", "600")
            result.setdefault("VOICE_MAX_FILE_BYTES", str(20 * 1024 * 1024))
            return result

        def _runtime_values(self, values: dict[str, str]) -> dict[str, str]:
            runtime = {
                key: values[key] for key in RUNTIME_CONFIG_KEYS if key in values
            }
            if self._is_remote():
                runtime["CODEX_CWD"] = values.get("REMOTE_CODEX_CWD", "~/arcadia")
                runtime["CODEX_BIN"] = values.get("REMOTE_CODEX_BIN", "codex")
                read_roots = runtime.get("AUTO_APPROVE_READ_ROOTS", "").strip()
                if not read_roots or read_roots == values.get("CODEX_CWD", "").strip():
                    runtime["AUTO_APPROVE_READ_ROOTS"] = runtime["CODEX_CWD"]
            return runtime

        def _validate(self, values: dict[str, str]) -> Config:
            runtime = self._runtime_values(values)
            config = Config.from_mapping(
                runtime, validate_local_paths=not self._is_remote()
            )
            if self._is_remote():
                RemoteSettings.from_mapping(values)
            if (
                not self._is_remote()
                and
                config.voice_transcription_enabled
                and importlib.util.find_spec("faster_whisper") is None
            ):
                raise ConfigError(
                    "Голосовые сообщения включены, но этот дистрибутив собран без "
                    "faster-whisper. Установите voice-версию приложения."
                )
            return config

        def _save_configuration(self) -> tuple[Config, dict[str, str]]:
            values = self._configuration_values()
            config = self._validate(values)
            runtime_values = self._runtime_values(values)
            runtime_values["TELEGRAM_BOT_TOKEN"] = config.telegram_token
            token = config.telegram_token
            try:
                store_telegram_token(token)
            except SecretStoreError:
                values["TELEGRAM_BOT_TOKEN"] = token
            else:
                values.pop("TELEGRAM_BOT_TOKEN", None)
            write_dotenv(selected_config_path, values)
            self.values = values
            return config, runtime_values

        def _save(self) -> None:
            try:
                self._save_configuration()
            except (ConfigError, ServiceError, OSError) as error:
                messagebox.showerror("Ошибка настройки", str(error), parent=root)
                return
            messagebox.showinfo(
                "Настройки сохранены",
                f"Конфигурация сохранена в:\n{selected_config_path}",
                parent=root,
            )

        def _save_and_start(self) -> None:
            try:
                config, runtime_values = self._save_configuration()
                values = self._configuration_values()
            except (ConfigError, ServiceError, OSError) as error:
                messagebox.showerror("Ошибка настройки", str(error), parent=root)
                return
            if self._is_remote():
                remote = self._remote_service(values)

                def start_remote() -> None:
                    self.service.suspend_for_remote()
                    remote.install_and_start(runtime_values)

                action = start_remote
                message = "Бот развёрнут и запущен на удалённом сервере."
            else:

                def start_local() -> None:
                    self._stop_configured_remote(values)
                    self.service.install_and_start(config.codex_cwd, config.codex_bin)

                action = start_local
                message = "Локальный фоновый процесс установлен и запущен."
            self._run_service_action(
                action,
                message,
            )

        def _open_service_controls(self) -> None:
            window = ctk.CTkToplevel(root)
            window.title("Управление процессом")
            window.geometry("430x280")
            window.resizable(False, False)
            window.transient(root)
            window.grab_set()

            ctk.CTkLabel(
                window,
                text="Управление процессом",
                font=ctk.CTkFont(size=22, weight="bold"),
                anchor="w",
            ).pack(fill="x", padx=24, pady=(22, 4))
            ctk.CTkLabel(
                window,
                text=(
                    "Действия применяются к удалённому запуску."
                    if self._is_remote()
                    else "Действия применяются к локальному запуску."
                ),
                text_color=MUTED,
                font=ctk.CTkFont(size=13),
                anchor="w",
            ).pack(fill="x", padx=24, pady=(0, 18))

            actions = ctk.CTkFrame(window, fg_color="transparent")
            actions.pack(fill="both", expand=True, padx=24, pady=(0, 22))
            actions.grid_columnconfigure((0, 1), weight=1)
            actions.grid_rowconfigure((0, 1), weight=1)

            def invoke(callback: Callable[[], None]) -> None:
                window.grab_release()
                window.destroy()
                callback()

            buttons = (
                ("Остановить", self._stop, 0, 0),
                ("Перезапустить", self._restart, 0, 1),
                ("Открыть логи", self._open_logs, 1, 0),
                ("Удалить автозапуск", self._confirm_uninstall, 1, 1),
            )
            for text, callback, row, column in buttons:
                ctk.CTkButton(
                    actions,
                    text=text,
                    command=lambda selected=callback: invoke(selected),
                    height=44,
                    corner_radius=11,
                    fg_color=CARD_BACKGROUND,
                    hover_color=("#E7E9F0", "#242832"),
                    text_color=("#343A4A", "#E4E7EF"),
                    border_width=1,
                    border_color=("#D6DAE4", "#343947"),
                    font=ctk.CTkFont(size=13, weight="bold"),
                ).grid(row=row, column=column, sticky="nsew", padx=5, pady=5)

        def _confirm_uninstall(self) -> None:
            if not messagebox.askyesno(
                "Удалить автозапуск?",
                "Фоновый процесс будет остановлен, а автозапуск удалён. "
                "Настройки и логи сохранятся.",
                parent=root,
            ):
                return
            self._uninstall()

        def _stop(self) -> None:
            try:
                action = self._active_service().stop
            except ServiceError as error:
                messagebox.showerror("Ошибка настройки", str(error), parent=root)
                return
            self._run_service_action(action, "Фоновый процесс остановлен.")

        def _restart(self) -> None:
            try:
                action = self._active_service().restart
            except ServiceError as error:
                messagebox.showerror("Ошибка настройки", str(error), parent=root)
                return
            self._run_service_action(
                action, "Фоновый процесс перезапущен."
            )

        def _uninstall(self) -> None:
            try:
                action = self._active_service().uninstall
            except ServiceError as error:
                messagebox.showerror("Ошибка настройки", str(error), parent=root)
                return
            self._run_service_action(
                action,
                "Автозапуск удалён. Настройки и логи сохранены.",
            )

        def _test_remote_connection(self) -> None:
            try:
                remote = self._remote_service(self._configuration_values())
            except ServiceError as error:
                messagebox.showerror("Ошибка настройки", str(error), parent=root)
                return
            self._run_service_action(
                remote.test_connection,
                "SSH-подключение и пользовательский systemd доступны.",
            )

        def _active_service(self):
            if self._is_remote():
                return self._remote_service(self._configuration_values())
            return self.service

        @staticmethod
        def _remote_service(values: dict[str, str]) -> RemoteServiceManager:
            return RemoteServiceManager(RemoteSettings.from_mapping(values))

        def _stop_configured_remote(self, values: dict[str, str]) -> None:
            if not values.get("REMOTE_SSH_HOST", "").strip():
                return
            self._remote_service(values).stop()

        def _run_service_action(
            self, action: Callable[[], None], success_message: str
        ) -> None:
            for button in self.action_buttons:
                button.configure(state="disabled")
            self._set_status("Выполняется…", WARNING)

            def worker() -> None:
                try:
                    action()
                except (ServiceError, OSError, subprocess.SubprocessError) as error:
                    root.after(
                        0,
                        lambda message=str(error): messagebox.showerror(
                            "Ошибка фонового процесса", message, parent=root
                        ),
                    )
                else:
                    root.after(
                        0,
                        lambda: messagebox.showinfo(
                            "Готово", success_message, parent=root
                        ),
                    )
                finally:
                    root.after(0, self._finish_service_action)

            threading.Thread(target=worker, daemon=True).start()

        def _finish_service_action(self) -> None:
            for button in self.action_buttons:
                button.configure(state="normal")
            self._refresh_status()

        def _refresh_status(self) -> None:
            if self._is_remote():
                self._refresh_remote_status()
                return
            try:
                status = self.service.status()
            except (ServiceError, OSError, subprocess.SubprocessError):
                self._set_status("Не удалось определить", WARNING)
            else:
                color = SUCCESS if status.running else MUTED
                self._set_status(status.description, color)

        def _refresh_remote_status(self) -> None:
            try:
                service = self._remote_service(self._configuration_values())
            except ServiceError:
                self._set_status("Укажите SSH-хост", MUTED)
                return
            self._set_status("Проверка сервера…", WARNING)

            def worker() -> None:
                try:
                    status = service.status()
                except (ServiceError, OSError, subprocess.SubprocessError):
                    root.after(
                        0,
                        lambda: self._set_status(
                            "Сервер недоступен", WARNING
                        ) if self._is_remote() else None,
                    )
                    return
                color = SUCCESS if status.running else MUTED
                root.after(
                    0,
                    lambda: self._set_status(status.description, color)
                    if self._is_remote()
                    else None,
                )

            threading.Thread(target=worker, daemon=True).start()

        def _set_status(self, text: str, color) -> None:
            self.status_label.configure(text=f"●  {text}", text_color=color)

        def _open_logs(self) -> None:
            if self._is_remote():
                self._open_remote_logs()
                return
            try:
                self.service.log_dir.mkdir(parents=True, exist_ok=True)
                if sys.platform == "win32":
                    os.startfile(self.service.log_dir)  # type: ignore[attr-defined]
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", str(self.service.log_dir)])
                else:
                    subprocess.Popen(["xdg-open", str(self.service.log_dir)])
            except OSError as error:
                messagebox.showerror("Не удалось открыть логи", str(error), parent=root)

        def _open_remote_logs(self) -> None:
            try:
                service = self._remote_service(self._configuration_values())
            except ServiceError as error:
                messagebox.showerror("Ошибка настройки", str(error), parent=root)
                return
            self._set_status("Загрузка логов…", WARNING)

            def worker() -> None:
                try:
                    logs = service.logs()
                except (ServiceError, OSError, subprocess.SubprocessError) as error:
                    root.after(
                        0,
                        lambda message=str(error): messagebox.showerror(
                            "Не удалось загрузить логи", message, parent=root
                        ),
                    )
                else:
                    root.after(0, lambda: self._show_logs_window(logs))
                finally:
                    root.after(0, self._refresh_status)

            threading.Thread(target=worker, daemon=True).start()

        @staticmethod
        def _show_logs_window(logs: str) -> None:
            window = ctk.CTkToplevel(root)
            window.title("Логи удалённого бота")
            window.geometry("900x600")
            textbox = ctk.CTkTextbox(
                window,
                corner_radius=12,
                font=ctk.CTkFont(family="Menlo", size=12),
            )
            textbox.pack(fill="both", expand=True, padx=16, pady=16)
            textbox.insert("1.0", logs)
            textbox.configure(state="disabled")

    SettingsWindow()
    root.mainloop()
