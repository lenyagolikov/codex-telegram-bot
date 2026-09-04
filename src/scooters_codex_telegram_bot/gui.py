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


def launch_gui(config_path: Path | None = None) -> None:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError as error:
        raise SystemExit(
            "The desktop interface requires tkinter. Install Tk support or use the CLI."
        ) from error

    selected_config_path = (config_path or default_config_path()).expanduser().resolve()
    root = tk.Tk()
    root.title("Codex Telegram Bot")
    root.geometry("760x690")
    root.minsize(680, 620)

    class SettingsWindow:
        def __init__(self) -> None:
            self.values = read_dotenv(selected_config_path)
            if not self.values.get("TELEGRAM_BOT_TOKEN"):
                self.values["TELEGRAM_BOT_TOKEN"] = read_telegram_token() or ""
            self.service = ServiceManager(selected_config_path)
            self.variables: dict[str, tk.Variable] = {}
            self.action_buttons: list[ttk.Button] = []
            self._build()
            self._refresh_status()

        def _build(self) -> None:
            container = ttk.Frame(root, padding=18)
            container.pack(fill="both", expand=True)

            ttk.Label(
                container,
                text="Codex Telegram Bot",
                font=("TkDefaultFont", 18, "bold"),
            ).pack(anchor="w")
            ttk.Label(
                container,
                text=(
                    "Настройте Telegram, выберите рабочую папку Codex и установите "
                    "фоновый запуск."
                ),
            ).pack(anchor="w", pady=(4, 14))

            notebook = ttk.Notebook(container)
            notebook.pack(fill="both", expand=True)
            basic = ttk.Frame(notebook, padding=14)
            advanced = ttk.Frame(notebook, padding=14)
            notebook.add(basic, text="Основные")
            notebook.add(advanced, text="Дополнительно")
            basic.columnconfigure(1, weight=1)
            advanced.columnconfigure(1, weight=1)

            self._add_entry(
                basic,
                0,
                "Telegram Bot Token",
                "TELEGRAM_BOT_TOKEN",
                show="•",
            )
            self._add_entry(
                basic,
                1,
                "Разрешённые user ID",
                "TELEGRAM_ALLOWED_USER_IDS",
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
            self._add_entry(basic, 4, "Модель (необязательно)", "CODEX_MODEL")

            effort = tk.StringVar(
                value=self.values.get("CODEX_REASONING_EFFORT", "")
            )
            self.variables["CODEX_REASONING_EFFORT"] = effort
            ttk.Label(basic, text="Reasoning effort").grid(
                row=5, column=0, sticky="w", padx=(0, 12), pady=7
            )
            ttk.Combobox(
                basic,
                textvariable=effort,
                values=("", "low", "medium", "high", "xhigh", "max", "ultra"),
                state="readonly",
            ).grid(row=5, column=1, sticky="ew", pady=7)

            config_label = ttk.Label(
                basic,
                text=f"Конфигурация: {selected_config_path}",
                foreground="#666666",
                wraplength=620,
            )
            config_label.grid(row=6, column=0, columnspan=3, sticky="w", pady=(18, 0))

            self._add_combo(
                advanced,
                0,
                "Сеть Telegram",
                "TELEGRAM_IP_FAMILY",
                ("auto", "ipv4", "ipv6"),
                "auto",
            )
            self._add_entry(
                advanced,
                1,
                "Таймаут polling, сек.",
                "POLL_TIMEOUT_SECONDS",
                "30",
            )
            self._add_checkbox(
                advanced,
                2,
                "Распознавать голосовые сообщения",
                "VOICE_TRANSCRIPTION_ENABLED",
            )
            self._add_entry(
                advanced, 3, "Whisper-модель", "WHISPER_MODEL", "small"
            )
            self._add_entry(
                advanced, 4, "Язык Whisper", "WHISPER_LANGUAGE", "ru"
            )
            self._add_checkbox(
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
            )

            warning = ttk.Label(
                advanced,
                text=(
                    "Автоподтверждение применяется только к операциям, которые Codex "
                    "классифицировал как чтение, поиск или просмотр файлов."
                ),
                foreground="#8a5a00",
                wraplength=620,
            )
            warning.grid(row=7, column=0, columnspan=3, sticky="w", pady=(18, 0))

            status_frame = ttk.Frame(container)
            status_frame.pack(fill="x", pady=(14, 8))
            ttk.Label(status_frame, text="Фоновый процесс:").pack(side="left")
            self.status_variable = tk.StringVar(value="Проверка…")
            ttk.Label(
                status_frame,
                textvariable=self.status_variable,
                font=("TkDefaultFont", 10, "bold"),
            ).pack(side="left", padx=(6, 0))
            ttk.Button(
                status_frame, text="Обновить", command=self._refresh_status
            ).pack(side="right")

            buttons = ttk.Frame(container)
            buttons.pack(fill="x")
            self._button(buttons, "Сохранить", self._save).pack(side="left")
            self._button(
                buttons, "Сохранить и запустить в фоне", self._save_and_start
            ).pack(side="left", padx=6)

            service_buttons = ttk.Frame(container)
            service_buttons.pack(fill="x", pady=(8, 0))
            self._button(service_buttons, "Остановить", self._stop).pack(side="left")
            self._button(service_buttons, "Перезапустить", self._restart).pack(
                side="left", padx=6
            )
            self._button(
                service_buttons, "Удалить автозапуск", self._uninstall
            ).pack(side="left")
            ttk.Button(
                service_buttons, text="Открыть логи", command=self._open_logs
            ).pack(side="right")

        def _add_entry(
            self,
            parent,
            row: int,
            label: str,
            key: str,
            default: str = "",
            *,
            show: str | None = None,
        ) -> None:
            variable = tk.StringVar(value=self.values.get(key, default))
            self.variables[key] = variable
            ttk.Label(parent, text=label).grid(
                row=row, column=0, sticky="w", padx=(0, 12), pady=7
            )
            ttk.Entry(parent, textvariable=variable, show=show).grid(
                row=row, column=1, columnspan=2, sticky="ew", pady=7
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
            ttk.Label(parent, text=label).grid(
                row=row, column=0, sticky="w", padx=(0, 12), pady=7
            )
            ttk.Entry(parent, textvariable=variable).grid(
                row=row, column=1, sticky="ew", pady=7
            )

            def browse() -> None:
                if choose_directory:
                    selected = filedialog.askdirectory(
                        parent=root, initialdir=variable.get() or str(Path.home())
                    )
                else:
                    selected = filedialog.askopenfilename(parent=root)
                if selected:
                    variable.set(selected)

            ttk.Button(parent, text="Выбрать…", command=browse).grid(
                row=row, column=2, padx=(8, 0), pady=7
            )

        def _add_combo(
            self,
            parent,
            row: int,
            label: str,
            key: str,
            choices: tuple[str, ...],
            default: str,
        ) -> None:
            variable = tk.StringVar(value=self.values.get(key, default))
            self.variables[key] = variable
            ttk.Label(parent, text=label).grid(
                row=row, column=0, sticky="w", padx=(0, 12), pady=7
            )
            ttk.Combobox(
                parent, textvariable=variable, values=choices, state="readonly"
            ).grid(row=row, column=1, columnspan=2, sticky="ew", pady=7)

        def _add_checkbox(
            self, parent, row: int, label: str, key: str
        ) -> None:
            enabled = self.values.get(key, "false").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            variable = tk.BooleanVar(value=enabled)
            self.variables[key] = variable
            ttk.Checkbutton(parent, text=label, variable=variable).grid(
                row=row, column=0, columnspan=3, sticky="w", pady=7
            )

        def _button(self, parent, text: str, callback: Callable[[], None]):
            button = ttk.Button(parent, text=text, command=callback)
            self.action_buttons.append(button)
            return button

        def _configuration_values(self) -> dict[str, str]:
            result = dict(self.values)
            for key, variable in self.variables.items():
                value = variable.get()
                if isinstance(value, bool):
                    result[key] = "true" if value else "false"
                else:
                    result[key] = str(value).strip()
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
                button.state(["disabled"])
            self.status_variable.set("Выполняется…")

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
                button.state(["!disabled"])
            self._refresh_status()

        def _refresh_status(self) -> None:
            try:
                status = self.service.status()
            except (ServiceError, OSError, subprocess.SubprocessError):
                self.status_variable.set("Не удалось определить")
            else:
                self.status_variable.set(status.description)

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
