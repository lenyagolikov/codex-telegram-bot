from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from .config import Config, ConfigError, default_config_path, read_dotenv, write_dotenv
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

    ctk.set_appearance_mode("system")
    ctk.set_default_color_theme("blue")

    selected_config_path = (config_path or default_config_path()).expanduser().resolve()
    root = ctk.CTk(fg_color=WINDOW_BACKGROUND)
    root.title("Codex Telegram Bot")
    root.geometry("920x780")
    root.minsize(820, 700)

    icon_path = Path(__file__).resolve().parent / "assets" / "app-icon.png"
    header_icon = None
    if icon_path.is_file():
        icon_image = Image.open(icon_path)
        header_icon = ctk.CTkImage(
            light_image=icon_image,
            dark_image=icon_image,
            size=(66, 66),
        )
        try:
            native_icon = tk.PhotoImage(file=str(icon_path))
            root.iconphoto(True, native_icon)
            root._native_icon = native_icon  # type: ignore[attr-defined]
        except tk.TclError:
            pass

    class SettingsWindow:
        def __init__(self) -> None:
            self.values = read_dotenv(selected_config_path)
            if not self.values.get("TELEGRAM_BOT_TOKEN"):
                self.values["TELEGRAM_BOT_TOKEN"] = read_telegram_token() or ""
            self.service = ServiceManager(selected_config_path)
            self.variables: dict[str, tk.Variable] = {}
            self.action_buttons: list[ctk.CTkButton] = []
            self.status_label: ctk.CTkLabel
            self.token_entry: ctk.CTkEntry
            self._build()
            self._refresh_status()

        def _build(self) -> None:
            page = ctk.CTkFrame(root, fg_color="transparent")
            page.pack(fill="both", expand=True, padx=30, pady=26)

            header = ctk.CTkFrame(page, fg_color="transparent")
            header.pack(fill="x", pady=(0, 20))
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

            self.appearance_menu = ctk.CTkSegmentedButton(
                header,
                values=["Система", "Светлая", "Тёмная"],
                command=self._change_appearance,
                selected_color=ACCENT,
                selected_hover_color=ACCENT_HOVER,
                height=34,
            )
            self.appearance_menu.set("Система")
            self.appearance_menu.pack(side="right")

            content = ctk.CTkFrame(
                page,
                fg_color=CARD_BACKGROUND,
                corner_radius=20,
                border_width=1,
                border_color=("#E3E6ED", "#292D38"),
            )
            content.pack(fill="both", expand=True)

            tabs = ctk.CTkTabview(
                content,
                fg_color="transparent",
                segmented_button_selected_color=ACCENT,
                segmented_button_selected_hover_color=ACCENT_HOVER,
                segmented_button_unselected_hover_color=("#E9EAF2", "#303441"),
                corner_radius=16,
            )
            tabs.pack(fill="both", expand=True, padx=18, pady=(12, 6))
            basic = tabs.add("Основные")
            advanced = tabs.add("Дополнительно")
            basic.grid_columnconfigure(1, weight=1)
            advanced.grid_columnconfigure(1, weight=1)

            self._add_token_entry(basic, 0)
            self._add_entry(
                basic,
                1,
                "Разрешённые user ID",
                "TELEGRAM_ALLOWED_USER_IDS",
                hint="Несколько ID указываются через запятую",
            )
            self._add_path_entry(
                basic,
                2,
                "Рабочая папка Codex",
                "CODEX_CWD",
                choose_directory=True,
            )
            self._add_path_entry(
                basic,
                3,
                "Команда Codex",
                "CODEX_BIN",
                choose_directory=False,
            )
            self._add_entry(
                basic,
                4,
                "Модель",
                "CODEX_MODEL",
                hint="Оставьте пустым, чтобы использовать настройку Codex",
            )
            self._add_option(
                basic,
                5,
                "Reasoning effort",
                "CODEX_REASONING_EFFORT",
                ["По умолчанию", "low", "medium", "high", "xhigh", "max", "ultra"],
            )

            ctk.CTkLabel(
                basic,
                text=f"Файл настроек  ·  {selected_config_path}",
                text_color=MUTED,
                font=ctk.CTkFont(size=12),
                anchor="w",
                wraplength=690,
            ).grid(row=6, column=0, columnspan=3, sticky="ew", padx=14, pady=(18, 6))

            self._add_option(
                advanced,
                0,
                "Сеть Telegram",
                "TELEGRAM_IP_FAMILY",
                ["auto", "ipv4", "ipv6"],
                default="auto",
            )
            self._add_entry(
                advanced,
                1,
                "Таймаут polling",
                "POLL_TIMEOUT_SECONDS",
                default="30",
                suffix="сек.",
            )
            self._add_switch(
                advanced,
                2,
                "Распознавать голосовые сообщения",
                "VOICE_TRANSCRIPTION_ENABLED",
            )
            self._add_entry(
                advanced, 3, "Whisper-модель", "WHISPER_MODEL", default="small"
            )
            self._add_entry(
                advanced, 4, "Язык Whisper", "WHISPER_LANGUAGE", default="ru"
            )
            self._add_switch(
                advanced,
                5,
                "Автоматически подтверждать безопасное чтение",
                "AUTO_APPROVE_SAFE_READ_ONLY",
            )
            self._add_entry(
                advanced,
                6,
                "Разрешённые корни чтения",
                "AUTO_APPROVE_READ_ROOTS",
                hint="Пути через запятую; по умолчанию — рабочая папка",
            )

            warning = ctk.CTkFrame(
                advanced,
                fg_color=("#FFF7E6", "#2B2418"),
                corner_radius=12,
            )
            warning.grid(
                row=7, column=0, columnspan=3, sticky="ew", padx=14, pady=(16, 8)
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

            footer = ctk.CTkFrame(page, fg_color="transparent")
            footer.pack(fill="x", pady=(16, 0))
            status_card = ctk.CTkFrame(
                footer,
                fg_color=CARD_BACKGROUND,
                corner_radius=14,
                border_width=1,
                border_color=("#E3E6ED", "#292D38"),
            )
            status_card.pack(side="left")
            self.status_label = ctk.CTkLabel(
                status_card,
                text="●  Проверка…",
                text_color=MUTED,
                font=ctk.CTkFont(size=13, weight="bold"),
            )
            self.status_label.pack(side="left", padx=(14, 8), pady=10)
            ctk.CTkButton(
                status_card,
                text="↻",
                width=34,
                height=30,
                fg_color="transparent",
                hover_color=("#ECEEF4", "#2A2E39"),
                text_color=("#3A4050", "#DDE1EA"),
                command=self._refresh_status,
            ).pack(side="right", padx=(0, 6), pady=5)

            primary_actions = ctk.CTkFrame(footer, fg_color="transparent")
            primary_actions.pack(side="right")
            self._button(
                primary_actions,
                "Сохранить",
                self._save,
                secondary=True,
                width=112,
            ).pack(side="left", padx=(0, 8))
            self._button(
                primary_actions,
                "Сохранить и запустить",
                self._save_and_start,
                width=205,
            ).pack(side="left")

            service_actions = ctk.CTkFrame(page, fg_color="transparent")
            service_actions.pack(fill="x", pady=(10, 0))
            self._button(
                service_actions, "Остановить", self._stop, secondary=True, width=110
            ).pack(side="left")
            self._button(
                service_actions,
                "Перезапустить",
                self._restart,
                secondary=True,
                width=130,
            ).pack(side="left", padx=8)
            self._button(
                service_actions,
                "Удалить автозапуск",
                self._uninstall,
                secondary=True,
                width=160,
            ).pack(side="left")
            ctk.CTkButton(
                service_actions,
                text="Открыть логи",
                command=self._open_logs,
                width=120,
                height=36,
                corner_radius=10,
                fg_color="transparent",
                hover_color=("#E4E7EE", "#242832"),
                text_color=("#475066", "#C8CDDA"),
            ).pack(side="right")

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
            default = (
                str(Path.home()) if key == "CODEX_CWD" else shutil.which("codex") or "codex"
            )
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
            display_value = (
                "По умолчанию"
                if key == "CODEX_REASONING_EFFORT" and not saved_value
                else saved_value
            )
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

        @staticmethod
        def _change_appearance(value: str) -> None:
            modes = {"Система": "system", "Светлая": "light", "Тёмная": "dark"}
            ctk.set_appearance_mode(modes[value])

        def _configuration_values(self) -> dict[str, str]:
            result = dict(self.values)
            for key, variable in self.variables.items():
                value = variable.get()
                if isinstance(value, bool):
                    result[key] = "true" if value else "false"
                else:
                    normalized = str(value).strip()
                    if (
                        key == "CODEX_REASONING_EFFORT"
                        and normalized == "По умолчанию"
                    ):
                        normalized = ""
                    result[key] = normalized
            codex_bin = result.get("CODEX_BIN", "codex") or "codex"
            if resolved_codex_bin := shutil.which(codex_bin):
                result["CODEX_BIN"] = str(Path(resolved_codex_bin).resolve())
            result.setdefault("VOICE_MAX_DURATION_SECONDS", "600")
            result.setdefault("VOICE_MAX_FILE_BYTES", str(20 * 1024 * 1024))
            return result

        def _validate(self, values: dict[str, str]) -> Config:
            config = Config.from_mapping(values)
            if (
                config.voice_transcription_enabled
                and importlib.util.find_spec("faster_whisper") is None
            ):
                raise ConfigError(
                    "Голосовые сообщения включены, но этот дистрибутив собран без "
                    "faster-whisper. Установите voice-версию приложения."
                )
            return config

        def _save_configuration(self) -> Config:
            values = self._configuration_values()
            config = self._validate(values)
            token = values["TELEGRAM_BOT_TOKEN"]
            try:
                store_telegram_token(token)
            except SecretStoreError:
                values["TELEGRAM_BOT_TOKEN"] = token
            else:
                values.pop("TELEGRAM_BOT_TOKEN", None)
            write_dotenv(selected_config_path, values)
            self.values = values
            return config

        def _save(self) -> None:
            try:
                self._save_configuration()
            except (ConfigError, OSError) as error:
                messagebox.showerror("Ошибка настройки", str(error), parent=root)
                return
            messagebox.showinfo(
                "Настройки сохранены",
                f"Конфигурация сохранена в:\n{selected_config_path}",
                parent=root,
            )

        def _save_and_start(self) -> None:
            try:
                config = self._save_configuration()
            except (ConfigError, OSError) as error:
                messagebox.showerror("Ошибка настройки", str(error), parent=root)
                return
            self._run_service_action(
                lambda: self.service.install_and_start(config.codex_cwd),
                "Фоновый процесс установлен и запущен.",
            )

        def _stop(self) -> None:
            self._run_service_action(self.service.stop, "Фоновый процесс остановлен.")

        def _restart(self) -> None:
            self._run_service_action(
                self.service.restart, "Фоновый процесс перезапущен."
            )

        def _uninstall(self) -> None:
            self._run_service_action(
                self.service.uninstall,
                "Автозапуск удалён. Настройки и логи сохранены.",
            )

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
            try:
                status = self.service.status()
            except (ServiceError, OSError, subprocess.SubprocessError):
                self._set_status("Не удалось определить", WARNING)
            else:
                color = SUCCESS if status.running else MUTED
                self._set_status(status.description, color)

        def _set_status(self, text: str, color) -> None:
            self.status_label.configure(text=f"●  {text}", text_color=color)

        def _open_logs(self) -> None:
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

    SettingsWindow()
    root.mainloop()
