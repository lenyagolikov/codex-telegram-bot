from __future__ import annotations

import asyncio
import logging
import os
import secrets
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .app_server import AppServerError, CodexAppServer
from .approvals import approval_path, assess_safe_read_only_approval
from .config import Config
from .state import OutboxMessage, StateStore, TaskRecord
from .telegram_api import TelegramApi, TelegramError
from .transcription import VoiceTranscriber, VoiceTranscriptionError

LOGGER = logging.getLogger(__name__)
TELEGRAM_TITLE_PREFIX = "[Telegram]"
OUTBOX_RETRY_SECONDS = 10
INTERACTION_TIMEOUT_SECONDS = 24 * 60 * 60
_MCP_FORM_CANCELLED = object()
_MCP_FORM_SKIPPED = object()
TELEGRAM_CLIENT_INSTRUCTIONS = """\
This Codex thread is controlled through a Telegram client. Use the shell,
filesystem, MCP, and web tools available in the session whenever the task needs
them. Never ask the user to provide an internal tool-call payload or protocol
JSON. Ask for input only when the information or decision genuinely has to come
from the user.
"""


@dataclass(slots=True)
class ActiveTurn:
    chat_id: int
    thread_id: str
    turn_id: str
    task_id: int = 0
    task_number: int = 0
    deltas_by_item: dict[str, list[str]] = field(default_factory=dict)
    latest_diff: str = ""
    started_at: float = field(default_factory=time.monotonic)
    last_activity_at: float = field(default_factory=time.monotonic)
    progress_item_ids: set[str] = field(default_factory=set)

    def touch(self) -> None:
        self.last_activity_at = time.monotonic()


@dataclass(slots=True)
class PendingCallback:
    chat_id: int
    kind: str
    future: asyncio.Future[str]
    message_id: int | None = None


@dataclass(slots=True)
class PendingTextInput:
    chat_id: int
    kind: str
    future: asyncio.Future[Any]
    allow_skip: bool = False
    task_id: int = 0
    message_id: int | None = None


class TelegramCodexBot:
    def __init__(
        self,
        config: Config,
        telegram: TelegramApi,
        app_server: CodexAppServer,
        state: StateStore,
        voice_transcriber: VoiceTranscriber | None = None,
    ) -> None:
        self._config = config
        self._telegram = telegram
        self._app_server = app_server
        self._state = state
        self._voice_transcriber = voice_transcriber
        self._active_by_task: dict[int, ActiveTurn] = {}
        # Kept as a compatibility view for integrations which inspect the selected
        # chat. Task routing itself is keyed by task_id so turns can run concurrently.
        self._active_by_chat: dict[int, ActiveTurn] = {}
        self._active_by_turn: dict[str, ActiveTurn] = {}
        self._completed_before_start_response: set[str] = set()
        self._thread_statuses: dict[str, dict[str, Any]] = {}
        self._pending_callbacks: dict[str, PendingCallback] = {}
        self._pending_text_by_chat: dict[int, list[PendingTextInput]] = {}
        self._outbox_wakeup = asyncio.Event()
        app_server.notification_handler = self._handle_codex_notification
        app_server.server_request_handler = self._handle_codex_request

    async def run(self) -> None:
        await self._app_server.start()
        LOGGER.info("Bot started; Codex cwd=%s", self._config.codex_cwd)
        polling_task = asyncio.create_task(self._poll_telegram(), name="telegram-polling")
        health_task = asyncio.create_task(
            self._app_server.wait_until_stopped(), name="codex-health"
        )
        outbox_task = asyncio.create_task(
            self._deliver_outbox(), name="telegram-outbox"
        )
        tasks = {polling_task, health_task, outbox_task}
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._app_server.stop()
            self._state.close()

    async def _poll_telegram(self) -> None:
        offset = self._state.get_update_offset()
        while True:
            try:
                updates = await self._telegram.get_updates(
                    offset, self._config.poll_timeout_seconds
                )
                if updates:
                    LOGGER.info("Received %d Telegram update(s)", len(updates))
                for update in updates:
                    try:
                        await self._handle_update(update)
                    except TelegramError as error:
                        LOGGER.warning("Telegram update handling failed: %s", error)
                    except Exception:
                        LOGGER.exception("Unexpected Telegram update handling failure")
                    finally:
                        offset = int(update["update_id"]) + 1
                        self._state.set_update_offset(offset)
            except TelegramError as error:
                LOGGER.warning("Telegram polling failed; retrying: %s", error)
                await asyncio.sleep(3)

    async def _handle_update(self, update: dict[str, Any]) -> None:
        if callback := update.get("callback_query"):
            await self._handle_callback(callback)
            return
        message = update.get("message")
        if not message:
            return

        chat_id = int(message["chat"]["id"])
        if message["chat"].get("type") != "private":
            await self._telegram.send_message(
                chat_id, "Из соображений безопасности я работаю только в личном чате."
            )
            return
        user_id = int(message["from"]["id"])
        if not await self._authorize(chat_id, user_id):
            return

        if voice := message.get("voice"):
            text = await self._transcribe_voice_message(chat_id, voice)
            if text is None:
                return
        elif "text" in message:
            text = str(message["text"]).strip()
        else:
            return
        command_parts = text.split(maxsplit=1)
        command = command_parts[0].split("@", 1)[0].lower()
        command_argument = command_parts[1].strip() if len(command_parts) > 1 else ""
        message_type = command if command.startswith("/") else "prompt"
        LOGGER.info("Handling Telegram message type=%s", message_type)
        pending_input = self._pending_text_input_for_message(chat_id, message)
        if pending_input is not None:
            if command == "/cancel" and not command_argument:
                self._resolve_pending_text(pending_input, None)
                if pending_input.kind == "mcp-form":
                    await self._telegram.send_message(
                        chat_id,
                        "Форма MCP отменена. Codex попробует продолжить задачу "
                        "без этого действия.",
                    )
                else:
                    task = self._state.get_task_by_id(pending_input.task_id)
                    if task is not None:
                        await self._cancel_task(task)
                return
            elif command == "/skip" and pending_input.allow_skip:
                self._resolve_pending_text(pending_input, _MCP_FORM_SKIPPED)
                await self._telegram.send_message(chat_id, "Поле пропущено.")
                return
            elif not text.startswith("/"):
                await self._handle_pending_text(pending_input, text)
                return
            elif command not in {
                "/start",
                "/help",
                "/new",
                "/list",
                "/switch",
                "/result",
                "/archive",
                "/unarchive",
                "/status",
                "/cancel",
                "/diff",
            }:
                await self._telegram.send_message(
                    chat_id,
                    "Codex ждёт ответ на вопрос. Ответь текстом или используй /cancel"
                    + (" либо /skip." if pending_input.allow_skip else "."),
                )
                return
        if command in {"/start", "/help"}:
            await self._send_help(chat_id)
        elif command == "/new":
            await self._new_thread(chat_id, user_id, command_argument)
        elif command == "/list":
            await self._list_tasks(chat_id, command_argument)
        elif command == "/switch":
            await self._switch_task(chat_id, command_argument)
        elif command == "/result":
            await self._send_result(chat_id, command_argument)
        elif command == "/archive":
            await self._archive_task(chat_id, command_argument)
        elif command == "/unarchive":
            await self._unarchive_task(chat_id, command_argument)
        elif command == "/status":
            await self._send_status(chat_id, command_argument)
        elif command == "/cancel":
            await self._cancel_turn(chat_id, command_argument)
        elif command == "/diff":
            await self._send_diff(chat_id, command_argument)
        elif text.startswith("/"):
            await self._telegram.send_message(chat_id, "Неизвестная команда. Используй /help.")
        else:
            await self._submit_prompt(chat_id, user_id, int(message["message_id"]), text)

    async def _transcribe_voice_message(
        self, chat_id: int, voice: dict[str, Any]
    ) -> str | None:
        if not self._config.voice_transcription_enabled or self._voice_transcriber is None:
            await self._telegram.send_message(
                chat_id, "Обработка голосовых сообщений на этом хосте отключена."
            )
            return None

        duration = int(voice.get("duration") or 0)
        if duration > self._config.voice_max_duration_seconds:
            await self._telegram.send_message(
                chat_id,
                "Голосовое сообщение слишком длинное. Максимальная длительность — "
                f"{self._config.voice_max_duration_seconds // 60} мин.",
            )
            return None
        file_size = int(voice.get("file_size") or 0)
        if file_size > self._config.voice_max_file_bytes:
            await self._telegram.send_message(
                chat_id, "Голосовое сообщение слишком большое."
            )
            return None

        file_id = str(voice.get("file_id") or "")
        if not file_id:
            await self._telegram.send_message(
                chat_id, "Telegram не передал идентификатор голосового сообщения."
            )
            return None

        await self._telegram.send_typing(chat_id)
        descriptor, raw_path = tempfile.mkstemp(prefix="telegram-voice-", suffix=".ogg")
        os.close(descriptor)
        audio_path = Path(raw_path)
        audio_path.chmod(0o600)
        try:
            await self._telegram.download_file(
                file_id,
                audio_path,
                max_bytes=self._config.voice_max_file_bytes,
            )
            text = await self._voice_transcriber.transcribe(audio_path)
            LOGGER.info(
                "Voice message transcribed; duration_seconds=%d text_length=%d",
                duration,
                len(text),
            )
            return text
        except (TelegramError, VoiceTranscriptionError):
            LOGGER.exception("Voice message transcription failed")
            await self._telegram.send_message(
                chat_id,
                "Не удалось распознать голосовое сообщение. Попробуй записать его ещё раз "
                "или отправь запрос текстом.",
            )
            return None
        finally:
            audio_path.unlink(missing_ok=True)

    async def _handle_pending_text(
        self, pending: PendingTextInput, text: str
    ) -> None:
        self._resolve_pending_text(pending, text)
        await self._telegram.send_message(pending.chat_id, "Ответ передан Codex.")

    def _resolve_pending_text(
        self, pending: PendingTextInput, value: Any
    ) -> None:
        pending_inputs = self._pending_text_by_chat.get(pending.chat_id, [])
        if pending in pending_inputs:
            pending_inputs.remove(pending)
        if not pending_inputs:
            self._pending_text_by_chat.pop(pending.chat_id, None)
        if not pending.future.done():
            pending.future.set_result(value)

    def _pending_text_input_for_message(
        self, chat_id: int, message: dict[str, Any]
    ) -> PendingTextInput | None:
        pending_inputs = self._pending_text_by_chat.get(chat_id, [])
        if not pending_inputs:
            return None
        reply = message.get("reply_to_message") or {}
        reply_message_id = reply.get("message_id")
        if reply_message_id is not None:
            matched = next(
                (
                    pending
                    for pending in pending_inputs
                    if pending.message_id == int(reply_message_id)
                ),
                None,
            )
            if matched is not None:
                return matched
        selected = self._state.get_selected_task(chat_id)
        if selected is not None:
            matched = next(
                (pending for pending in pending_inputs if pending.task_id == selected.id),
                None,
            )
            if matched is not None:
                return matched
        return pending_inputs[0] if len(pending_inputs) == 1 else None

    async def _authorize(self, chat_id: int, user_id: int) -> bool:
        if user_id in self._config.allowed_user_ids:
            return True
        if not self._config.allowed_user_ids:
            text = (
                "Бот пока работает в режиме настройки. Твой Telegram user ID: "
                f"{user_id}\n\nДобавь его в TELEGRAM_ALLOWED_USER_IDS в .env и перезапусти бота."
            )
        else:
            text = f"Доступ запрещён. Твой Telegram user ID: {user_id}"
        await self._telegram.send_message(chat_id, text)
        return False

    async def _send_help(self, chat_id: int) -> None:
        await self._telegram.send_message(
            chat_id,
            "Я передаю сообщения в Codex на текущем хосте.\n\n"
            "/new [название] — создать и выбрать новую задачу\n"
            "/list — показать все задачи и их статусы\n"
            "/switch <номер> — переключиться на задачу\n"
            "/result <номер> — показать сохранённый результат\n"
            "/archive <номер> — скрыть завершённую задачу из списка\n"
            "/unarchive <номер> — восстановить задачу из архива\n"
            "/list archived — показать архив\n"
            "/status [номер] — показать подробный статус\n"
            "/cancel [номер] — остановить задачу\n"
            "/diff [номер] — показать последний diff задачи\n"
            "/help — эта справка\n\n"
            "Обычное или голосовое сообщение отправляется в выбранную задачу. "
            "Если она работает, сообщение уточняет текущий запуск. Иначе Codex "
            "продолжает тот же диалог новым запуском.",
        )

    async def _new_thread(
        self, chat_id: int, user_id: int, title: str = ""
    ) -> None:
        task = self._state.create_task(chat_id, user_id, title or None)
        await self._telegram.send_message(
            chat_id,
            f"Создана задача #{task.number}"
            + (f" — {task.title}" if task.title else "")
            + ". Она выбрана; отправь следующим сообщением промпт.",
        )

    async def _list_tasks(self, chat_id: int, argument: str = "") -> None:
        normalized = argument.casefold()
        if normalized not in {"", "all", "archived"}:
            await self._telegram.send_message(
                chat_id, "Используй /list, /list archived или /list all."
            )
            return
        archived = True if normalized == "archived" else None if normalized == "all" else False
        tasks = self._state.list_tasks(chat_id, archived=archived)
        if not tasks:
            message = (
                "Архив пуст."
                if normalized == "archived"
                else "Задач пока нет. Создай первую командой /new."
            )
            await self._telegram.send_message(
                chat_id, message
            )
            return
        selected = self._state.get_selected_task(chat_id)
        lines = ["Архив:" if normalized == "archived" else "Задачи:"]
        for task in tasks:
            marker = "→" if selected and task.id == selected.id else " "
            icon = "📦" if task.archived_at else _task_status_icon(task.status)
            title = task.title or "Без названия"
            lines.append(f"{marker} {icon} #{task.number} — {title}")
        if normalized == "archived":
            lines.append("\nВосстановить: /unarchive <номер>")
        else:
            lines.append("\nПереключиться: /switch <номер>")
        await self._telegram.send_message(chat_id, "\n".join(lines))

    async def _switch_task(self, chat_id: int, argument: str) -> None:
        task = await self._task_from_argument(chat_id, argument, require_argument=True)
        if task is None:
            return
        self._state.select_task(chat_id, task.number)
        active = self._active_by_task.get(task.id)
        state = "работает" if active else _task_status_label(task.status)
        await self._telegram.send_message(
            chat_id,
            f"Выбрана задача #{task.number} — {task.title or 'Без названия'}. "
            f"Состояние: {state}.",
        )

    async def _send_result(self, chat_id: int, argument: str) -> None:
        task = await self._task_from_argument(
            chat_id, argument, require_argument=True, allow_archived=True
        )
        if task is None:
            return
        if task.result_text:
            await self._telegram.send_message(
                chat_id,
                f"Результат задачи #{task.number}:\n\n{task.result_text}",
                formatted=task.result_formatted,
            )
            return
        if task.id in self._active_by_task:
            text = f"Задача #{task.number} ещё выполняется."
        else:
            text = f"У задачи #{task.number} пока нет сохранённого результата."
        await self._telegram.send_message(chat_id, text)

    async def _archive_task(self, chat_id: int, argument: str) -> None:
        task = await self._task_from_argument(
            chat_id, argument, require_argument=True, allow_archived=True
        )
        if task is None:
            return
        if task.archived_at is not None:
            await self._telegram.send_message(
                chat_id, f"Задача #{task.number} уже находится в архиве."
            )
            return
        if task.id in self._active_by_task:
            await self._telegram.send_message(
                chat_id,
                f"Задача #{task.number} ещё выполняется. Сначала останови её: "
                f"/cancel {task.number}",
            )
            return
        self._state.archive_task(chat_id, task.number)
        selected = self._state.get_selected_task(chat_id)
        suffix = (
            f" Теперь выбрана задача #{selected.number}."
            if selected is not None
            else " Активных задач не осталось."
        )
        await self._telegram.send_message(
            chat_id, f"Задача #{task.number} перемещена в архив.{suffix}"
        )

    async def _unarchive_task(self, chat_id: int, argument: str) -> None:
        task = await self._task_from_argument(
            chat_id, argument, require_argument=True, allow_archived=True
        )
        if task is None:
            return
        if task.archived_at is None:
            await self._telegram.send_message(
                chat_id, f"Задача #{task.number} не находится в архиве."
            )
            return
        self._state.unarchive_task(chat_id, task.number)
        await self._telegram.send_message(
            chat_id,
            f"Задача #{task.number} восстановлена. Чтобы выбрать её: "
            f"/switch {task.number}",
        )

    async def _send_status(self, chat_id: int, argument: str = "") -> None:
        task = await self._task_from_argument(chat_id, argument)
        if task is None:
            return
        thread_id = task.thread_id
        if thread_id is None:
            await self._telegram.send_message(
                chat_id, f"Задача #{task.number} создана, но промпт ещё не отправлен."
            )
            return

        active = self._active_by_task.get(task.id)
        status = self._thread_statuses.get(thread_id, {})
        turns: list[dict[str, Any]] = []
        live_check = "получено"
        try:
            response = await self._app_server.request(
                "thread/read", {"threadId": thread_id, "includeTurns": True}
            )
            thread = response["thread"]
            status = dict(thread.get("status") or {})
            turns = list(thread.get("turns") or [])
            self._thread_statuses[thread_id] = status
        except (AppServerError, KeyError, TypeError, ValueError) as error:
            LOGGER.warning("Could not read live Codex thread status: %s", error)
            live_check = "недоступно, показан последний известный статус"

        current_turn: dict[str, Any] | None = None
        if active is not None:
            current_turn = next(
                (turn for turn in reversed(turns) if str(turn.get("id")) == active.turn_id),
                None,
            )
        if current_turn is None and turns:
            current_turn = turns[-1]

        turn_status = str(current_turn.get("status", "")) if current_turn else ""
        if active is not None and turn_status in {"completed", "interrupted", "failed"}:
            self._unregister_active(active)
            active = None

        flags = set(status.get("activeFlags", []))
        if "waitingOnApproval" in flags:
            state = "Codex ожидает подтверждения действия."
        elif "waitingOnUserInput" in flags:
            state = "Codex ожидает ответа пользователя."
        elif status.get("type") == "systemError":
            state = "ошибка Codex. Создай новый диалог командой /new."
        elif turn_status == "interrupted":
            state = "последняя задача прервана."
        elif turn_status == "failed":
            state = "последняя задача завершилась с ошибкой."
        elif turn_status == "completed":
            state = "последняя задача завершена; ожидаю сообщение."
        elif turn_status == "inProgress" or status.get("type") == "active":
            state = "Codex работает."
        else:
            state = "ожидаю сообщение."

        lines = [
            f"Задача #{task.number}: {task.title or 'Без названия'}",
            f"Диалог: {thread_id}",
        ]
        if current_turn is not None:
            lines.append(f"Запуск: {current_turn.get('id')}")
        elif active is not None:
            lines.append(f"Запуск: {active.turn_id}")
        lines.append(f"Состояние: {state}")
        if active is not None and turn_status in {"", "inProgress"}:
            lines.extend(
                [
                    f"В работе: {_format_duration(time.monotonic() - active.started_at)}",
                    "Последнее событие Codex: "
                    f"{_format_duration(time.monotonic() - active.last_activity_at)} назад",
                ]
            )
        elif current_turn is not None and current_turn.get("durationMs") is not None:
            lines.append(
                "Длительность задачи: "
                f"{_format_duration(float(current_turn['durationMs']) / 1000)}"
            )
        lines.extend(
            [
                f"Канал App Server: {'подключён' if self._app_server.is_healthy else 'недоступен'}",
                f"Проверка живого статуса: {live_check}",
            ]
        )
        text = "\n".join(lines)
        await self._telegram.send_message(chat_id, text)

    async def _cancel_turn(self, chat_id: int, argument: str = "") -> None:
        task = await self._task_from_argument(chat_id, argument)
        if task is None:
            return
        await self._cancel_task(task)

    async def _cancel_task(self, task: TaskRecord) -> None:
        active = self._active_by_task.get(task.id)
        if active is None:
            await self._telegram.send_message(
                task.chat_id, f"Задача #{task.number} сейчас не выполняется."
            )
            return
        await self._app_server.request(
            "turn/interrupt",
            {"threadId": active.thread_id, "turnId": active.turn_id},
        )
        await self._telegram.send_message(
            task.chat_id, f"Останавливаю задачу #{task.number}."
        )

    async def _send_diff(self, chat_id: int, argument: str = "") -> None:
        task = await self._task_from_argument(chat_id, argument)
        if task is None:
            return
        active = self._active_by_task.get(task.id)
        diff = active.latest_diff if active and active.latest_diff else task.latest_diff
        if not diff:
            await self._telegram.send_message(
                chat_id, f"Для задачи #{task.number} diff пока нет."
            )
            return
        await self._telegram.send_message(chat_id, diff)

    async def _task_from_argument(
        self,
        chat_id: int,
        argument: str,
        *,
        require_argument: bool = False,
        allow_archived: bool = False,
    ) -> TaskRecord | None:
        if argument:
            try:
                number = int(argument.removeprefix("#"))
            except ValueError:
                number = 0
            task = self._state.get_task(chat_id, number) if number > 0 else None
            if task is None:
                await self._telegram.send_message(
                    chat_id, "Укажи существующий номер задачи, например /switch 2."
                )
            elif task.archived_at is not None and not allow_archived:
                await self._telegram.send_message(
                    chat_id,
                    f"Задача #{task.number} находится в архиве. Сначала восстанови её: "
                    f"/unarchive {task.number}",
                )
                return None
            return task
        if require_argument:
            await self._telegram.send_message(
                chat_id, "Укажи номер задачи, например /result 2."
            )
            return None
        task = self._state.get_selected_task(chat_id)
        if task is None:
            await self._telegram.send_message(
                chat_id, "Задач пока нет. Создай первую командой /new."
            )
        return task

    async def _submit_prompt(
        self, chat_id: int, user_id: int, message_id: int, text: str
    ) -> None:
        task = self._state.get_selected_task(chat_id)
        if task is None:
            task = self._state.create_task(chat_id, user_id, _title_from_prompt(text))
        elif not task.title:
            self._state.set_task_title(task.id, _title_from_prompt(text))
            task = self._state.get_task_by_id(task.id) or task
        active = self._active_by_task.get(task.id)
        input_items = [{"type": "text", "text": text}]
        try:
            if active is not None:
                active.touch()
                await self._app_server.request(
                    "turn/steer",
                    {
                        "threadId": active.thread_id,
                        "expectedTurnId": active.turn_id,
                        "input": input_items,
                        "clientUserMessageId": f"telegram:{chat_id}:{message_id}",
                    },
                )
                await self._telegram.send_message(chat_id, "Уточнение передано в текущую задачу.")
                return

            thread_id, thread_name = await self._ensure_thread(task)
            await self._ensure_telegram_title(thread_id, thread_name, text)
            await self._telegram.send_typing(chat_id)
            params: dict[str, Any] = {
                "threadId": thread_id,
                "input": input_items,
                "clientUserMessageId": f"telegram:{chat_id}:{message_id}",
                "cwd": str(self._config.codex_cwd),
                "sandboxPolicy": self._turn_sandbox_policy(),
            }
            if self._config.reasoning_effort is not None:
                params["effort"] = self._config.reasoning_effort
            response = await self._app_server.request("turn/start", params)
            turn_id = str(response["turn"]["id"])
            if turn_id in self._completed_before_start_response:
                self._completed_before_start_response.remove(turn_id)
                return
            active = self._active_by_turn.get(turn_id)
            if active is None:
                active = ActiveTurn(
                    chat_id=chat_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    task_id=task.id,
                    task_number=task.number,
                )
                self._register_active(active)
            self._state.set_task_running(task.id, turn_id)
            self._thread_statuses[thread_id] = {"type": "active", "activeFlags": []}
        except AppServerError as error:
            LOGGER.exception("Failed to submit a prompt")
            await self._telegram.send_message(chat_id, f"Codex не принял задачу: {error}")

    async def _ensure_thread(self, task: TaskRecord) -> tuple[str, str | None]:
        stored_thread_id = task.thread_id
        if stored_thread_id is not None:
            try:
                response = await self._app_server.request(
                    "thread/resume",
                    self._resume_thread_params(stored_thread_id),
                )
                self._remember_thread_status(response)
                thread = response["thread"]
                name = thread.get("name")
                return str(thread["id"]), str(name) if name else None
            except AppServerError:
                LOGGER.warning("Stored Codex thread cannot be resumed; creating a new one")

        response = await self._app_server.request("thread/start", self._start_thread_params())
        self._remember_thread_status(response)
        thread = response["thread"]
        thread_id = str(thread["id"])
        self._state.attach_thread(task.id, thread_id)
        name = thread.get("name")
        return thread_id, str(name) if name else None

    async def _ensure_telegram_title(
        self, thread_id: str, current_name: str | None, prompt: str
    ) -> None:
        if current_name and current_name.startswith(TELEGRAM_TITLE_PREFIX):
            return
        base_name = current_name or _title_from_prompt(prompt)
        name = f"{TELEGRAM_TITLE_PREFIX} {base_name}"[:160]
        try:
            await self._app_server.request(
                "thread/name/set", {"threadId": thread_id, "name": name}
            )
        except AppServerError as error:
            LOGGER.warning("Could not add Telegram thread title prefix: %s", error)

    def _common_thread_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "cwd": str(self._config.codex_cwd),
            "approvalPolicy": "on-request",
            "approvalsReviewer": self._config.approvals_reviewer,
            "sandbox": "workspace-write",
            "developerInstructions": TELEGRAM_CLIENT_INSTRUCTIONS,
        }
        if self._config.codex_model is not None:
            params["model"] = self._config.codex_model
        return params

    def _start_thread_params(self) -> dict[str, Any]:
        return {**self._common_thread_params(), "ephemeral": False}

    def _resume_thread_params(self, thread_id: str) -> dict[str, Any]:
        return {**self._common_thread_params(), "threadId": thread_id}

    def _turn_sandbox_policy(self) -> dict[str, Any]:
        writable_roots = [str(self._config.codex_cwd)]
        arc_cache = Path.home() / ".arc"
        if arc_cache.is_dir():
            writable_roots.append(str(arc_cache.resolve()))
        return {
            "type": "workspaceWrite",
            "writableRoots": writable_roots,
            # Arc mounts communicate with their local process through localhost.
            "networkAccess": True,
        }

    def _remember_thread_status(self, response: dict[str, Any]) -> None:
        thread = response["thread"]
        self._thread_statuses[str(thread["id"])] = dict(thread.get("status") or {})

    async def _handle_codex_notification(
        self, method: str, params: dict[str, Any]
    ) -> None:
        if method == "item/agentMessage/delta":
            active = self._find_or_create_active(params)
            if active is not None:
                active.touch()
                item_id = str(params["itemId"])
                active.deltas_by_item.setdefault(item_id, []).append(str(params["delta"]))
        elif method == "item/started":
            active = self._find_or_create_active(params)
            if active is not None:
                active.touch()
        elif method == "item/completed":
            active = self._find_or_create_active(params)
            if active is not None:
                active.touch()
                item = params.get("item") or {}
                if (
                    item.get("type") == "agentMessage"
                    and item.get("phase") == "commentary"
                ):
                    active.progress_item_ids.add(str(item.get("id")))
        elif method == "turn/diff/updated":
            active = self._find_or_create_active(params)
            if active is not None:
                active.touch()
                active.latest_diff = str(params["diff"])
                self._state.set_task_diff(active.task_id, active.latest_diff)
        elif method == "thread/status/changed":
            thread_id = str(params["threadId"])
            self._thread_statuses[thread_id] = dict(params["status"])
            task = self._state.get_task_by_thread_id(thread_id)
            if task is not None and (active := self._active_by_task.get(task.id)):
                active.touch()
        elif method == "turn/completed":
            await self._handle_turn_completed(params)

    def _find_or_create_active(self, params: dict[str, Any]) -> ActiveTurn | None:
        turn_id = str(params["turnId"])
        if active := self._active_by_turn.get(turn_id):
            return active
        thread_id = str(params["threadId"])
        task = self._state.get_task_by_thread_id(thread_id)
        if task is None:
            return None
        active = ActiveTurn(
            chat_id=task.chat_id,
            thread_id=thread_id,
            turn_id=turn_id,
            task_id=task.id,
            task_number=task.number,
        )
        self._register_active(active)
        self._state.set_task_running(task.id, turn_id)
        return active

    async def _handle_turn_completed(self, params: dict[str, Any]) -> None:
        turn = params["turn"]
        turn_id = str(turn["id"])
        active = self._active_by_turn.get(turn_id)
        if active is None:
            thread_id = str(params["threadId"])
            task = self._state.get_task_by_thread_id(thread_id)
            if task is None:
                return
            active = ActiveTurn(
                chat_id=task.chat_id,
                thread_id=thread_id,
                turn_id=turn_id,
                task_id=task.id,
                task_number=task.number,
            )
            self._completed_before_start_response.add(turn_id)

        status = str(turn["status"])
        self._thread_statuses[active.thread_id] = {"type": "idle"}
        active.touch()
        answer = self._extract_answer(turn, active)
        if status == "failed":
            error = turn.get("error") or {}
            answer = f"Codex завершил задачу с ошибкой: {error.get('message', 'unknown error')}"
        elif status == "interrupted" and not answer:
            answer = "Задача остановлена."
        elif not answer:
            answer = "Codex завершил задачу без текстового ответа."

        persisted_status = {
            "failed": "failed",
            "interrupted": "interrupted",
        }.get(status, "completed")
        self._state.complete_task(
            active.task_id,
            persisted_status,
            answer,
            formatted=True,
            latest_diff=active.latest_diff or None,
        )
        self._unregister_active(active)
        completion_label = {
            "failed": "завершилась с ошибкой",
            "interrupted": "прервана",
        }.get(status, "завершена")
        self._enqueue_outbox(
            active.chat_id,
            f"Задача #{active.task_number} {completion_label}.\n\n{answer}",
            formatted=True,
        )

    def _enqueue_outbox(self, chat_id: int, text: str, *, formatted: bool) -> int:
        message_id = self._state.enqueue_outbox(chat_id, text, formatted=formatted)
        LOGGER.info("Queued Telegram message in outbox; message_id=%d", message_id)
        self._outbox_wakeup.set()
        return message_id

    async def _deliver_outbox(self) -> None:
        while True:
            messages = self._state.get_outbox()
            if not messages:
                self._outbox_wakeup.clear()
                await self._outbox_wakeup.wait()
                continue

            has_failures = False
            for message in messages:
                if not await self._deliver_outbox_message(message):
                    has_failures = True

            if has_failures:
                self._outbox_wakeup.clear()
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        self._outbox_wakeup.wait(),
                        timeout=OUTBOX_RETRY_SECONDS,
                    )

    async def _deliver_outbox_message(self, message: OutboxMessage) -> bool:
        try:
            await self._telegram.send_message(
                message.chat_id,
                message.text,
                formatted=message.formatted,
            )
        except TelegramError as error:
            self._state.mark_outbox_attempt(message.id)
            LOGGER.warning(
                "Could not deliver outbox message %d (attempt %d); will retry: %s",
                message.id,
                message.attempts + 1,
                error,
            )
            return False

        self._state.delete_outbox(message.id)
        LOGGER.info("Delivered outbox message %d", message.id)
        return True

    @staticmethod
    def _extract_answer(turn: dict[str, Any], active: ActiveTurn) -> str:
        messages = [
            item
            for item in turn.get("items", [])
            if item.get("type") == "agentMessage"
            and str(item.get("id")) not in active.progress_item_ids
        ]
        final_messages = [item["text"] for item in messages if item.get("phase") == "final_answer"]
        if final_messages:
            return "\n\n".join(final_messages)
        if messages:
            return str(messages[-1]["text"])
        return "".join(
            "".join(parts)
            for item_id, parts in active.deltas_by_item.items()
            if item_id not in active.progress_item_ids
        )

    def _register_active(self, active: ActiveTurn) -> None:
        if active.task_id == 0:
            task = self._state.get_task_by_thread_id(active.thread_id)
            if task is not None:
                active.task_id = task.id
                active.task_number = task.number
        self._active_by_task[active.task_id] = active
        self._active_by_chat[active.chat_id] = active
        self._active_by_turn[active.turn_id] = active

    def _unregister_active(self, active: ActiveTurn) -> None:
        self._active_by_task.pop(active.task_id, None)
        if self._active_by_chat.get(active.chat_id) is active:
            self._active_by_chat.pop(active.chat_id, None)
        self._active_by_turn.pop(active.turn_id, None)

    async def _handle_codex_request(self, method: str, params: dict[str, Any]) -> Any:
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            return await self._handle_approval_request(method, params)
        if method == "item/tool/requestUserInput":
            return await self._handle_tool_user_input(params)
        if method == "mcpServer/elicitation/request":
            return await self._handle_mcp_elicitation(params)
        if method == "item/tool/call":
            LOGGER.warning(
                "Rejected unavailable dynamic tool %s",
                params.get("tool") or "<unknown>",
            )
            return {
                "success": False,
                "contentItems": [
                    {
                        "type": "inputText",
                        "text": (
                            "This Telegram client does not provide dynamic tools. "
                            "Retry using a built-in command, MCP, or web tool directly."
                        ),
                    }
                ],
            }
        raise AppServerError(f"Unsupported Codex request: {method}")

    async def _handle_approval_request(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, str]:
        thread_id = str(params["threadId"])
        task = self._state.get_task_by_thread_id(thread_id)
        if task is None:
            return {"decision": "decline"}
        chat_id = task.chat_id

        kind = "command" if "commandExecution" in method else "file"
        if kind == "command" and self._config.auto_approve_safe_read_only:
            assessment = assess_safe_read_only_approval(
                params, self._config.auto_approve_read_roots
            )
            if assessment.approved:
                LOGGER.info(
                    "Auto-approved read-only command; source=%s action_types=%s cwd=%s",
                    assessment.reason,
                    ",".join(assessment.action_types) or "<none>",
                    approval_path(params.get("cwd")) or "<unknown>",
                )
                return {"decision": "accept"}
            LOGGER.info(
                "Read-only auto-approval skipped; reason=%s action_types=%s cwd=%s",
                assessment.reason,
                ",".join(assessment.action_types) or "<none>",
                approval_path(params.get("cwd")) or "<unknown>",
            )

        self._thread_statuses[thread_id] = {
            "type": "active",
            "activeFlags": ["waitingOnApproval"],
        }
        if active := self._active_by_task.get(task.id):
            active.touch()

        if kind == "command":
            subject = params.get("command") or "Команда не указана"
            title = "Codex просит разрешить команду"
        else:
            subject = params.get("grantRoot") or "Изменение файлов в рабочей области"
            title = "Codex просит разрешить изменение файлов"
        reason = params.get("reason")
        text = f"{title}:\n\n{subject}"
        if reason:
            text += f"\n\nПричина: {reason}"
        text = text[:3200]

        token = secrets.token_urlsafe(8)
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "Разрешить один раз", "callback_data": f"cb:{token}:accept"},
                    {"text": "На эту сессию", "callback_data": f"cb:{token}:session"},
                ],
                [
                    {"text": "Отклонить", "callback_data": f"cb:{token}:decline"},
                    {"text": "Отклонить и стоп", "callback_data": f"cb:{token}:cancel"},
                ],
            ]
        }
        decision = await self._ask_with_buttons(
            chat_id,
            text,
            kind=f"approval:{kind}",
            keyboard=keyboard,
            timeout=INTERACTION_TIMEOUT_SECONDS,
            token=token,
            timeout_message=(
                "Ожидание подтверждения истекло спустя 24 часа. Действие отклонено; "
                "если Codex не сможет продолжить, повтори исходный запрос."
            ),
        )
        if decision is None:
            decision = "decline"
        self._thread_statuses[thread_id] = {"type": "active", "activeFlags": []}
        if active := self._active_by_task.get(task.id):
            active.touch()
        return {"decision": decision}

    async def _handle_tool_user_input(
        self, params: dict[str, Any]
    ) -> dict[str, dict[str, dict[str, list[str]]]]:
        thread_id = str(params["threadId"])
        task = self._state.get_task_by_thread_id(thread_id)
        questions = list(params.get("questions") or [])
        if task is None:
            return {"answers": {}}
        chat_id = task.chat_id

        self._mark_waiting(thread_id, chat_id, "waitingOnUserInput")
        answers: dict[str, dict[str, list[str]]] = {}
        timeout = _request_timeout(params.get("autoResolutionMs"))
        try:
            for question in questions:
                question_id = str(question["id"])
                header = str(question.get("header") or "Вопрос Codex")
                prompt = str(question.get("question") or "Ответь на вопрос Codex.")
                if question.get("isSecret"):
                    answers[question_id] = {"answers": []}
                    await self._send_safely(
                        chat_id,
                        f"{header}\n\n{prompt}\n\n"
                        "Секретные значения нельзя передавать через Telegram-бота. "
                        "Ответ на этот пункт пропущен.",
                    )
                    continue

                options = list(question.get("options") or [])
                if options:
                    details = "\n".join(
                        f"• {option['label']} — {option['description']}"
                        for option in options
                    )
                    token = secrets.token_urlsafe(8)
                    rows = [
                        [
                            {
                                "text": str(option["label"])[:50],
                                "callback_data": f"cb:{token}:{index}",
                            }
                        ]
                        for index, option in enumerate(options)
                    ]
                    if question.get("isOther"):
                        rows.append(
                            [{"text": "Другой ответ", "callback_data": f"cb:{token}:other"}]
                        )
                    rows.append([{"text": "Отмена", "callback_data": f"cb:{token}:cancel"}])
                    selected = await self._ask_with_buttons(
                        chat_id,
                        f"{header}\n\n{prompt}\n\n{details}",
                        kind="user-input",
                        keyboard={"inline_keyboard": rows},
                        timeout=timeout,
                        token=token,
                    )
                    if selected == "other":
                        value = await self._ask_for_text(
                            chat_id,
                            "Напиши свой вариант одним сообщением. /cancel отменит задачу.",
                            kind="user-input",
                            timeout=timeout,
                            task_id=task.id,
                        )
                    elif selected is not None and selected.isdigit():
                        index = int(selected)
                        value = (
                            str(options[index]["label"])
                            if 0 <= index < len(options)
                            else None
                        )
                    else:
                        value = None
                else:
                    value = await self._ask_for_text(
                        chat_id,
                        f"{header}\n\n{prompt}\n\n"
                        "Ответь одним сообщением. /cancel отменит задачу.",
                        kind="user-input",
                        timeout=timeout,
                        task_id=task.id,
                    )
                answers[question_id] = {
                    "answers": [] if value is None else [str(value)]
                }
            return {"answers": answers}
        finally:
            self._mark_active(thread_id, chat_id)

    async def _handle_mcp_elicitation(self, params: dict[str, Any]) -> dict[str, Any]:
        thread_id = str(params["threadId"])
        task = self._state.get_task_by_thread_id(thread_id)
        if task is None:
            return {"action": "decline"}
        chat_id = task.chat_id

        self._mark_waiting(thread_id, chat_id, "waitingOnUserInput")
        server_name = str(params.get("serverName") or "MCP")
        message = str(params.get("message") or "MCP-сервер запрашивает действие.")
        try:
            if params.get("mode") == "url":
                url = str(params.get("url") or "")
                token = secrets.token_urlsafe(8)
                keyboard = {
                    "inline_keyboard": [
                        [
                            {"text": "Готово", "callback_data": f"cb:{token}:accept"},
                            {"text": "Отклонить", "callback_data": f"cb:{token}:decline"},
                        ],
                        [{"text": "Отмена", "callback_data": f"cb:{token}:cancel"}],
                    ]
                }
                action = await self._ask_with_buttons(
                    chat_id,
                    f"MCP {server_name} запрашивает действие:\n\n{message}\n\n{url}",
                    kind="mcp-url",
                    keyboard=keyboard,
                    timeout=INTERACTION_TIMEOUT_SECONDS,
                    token=token,
                )
                return {"action": action or "decline"}

            schema = params.get("requestedSchema")
            properties = _mcp_schema_properties(schema)
            LOGGER.info(
                "MCP form requested; server=%s mode=%s fields=%s",
                server_name,
                params.get("mode") or "form",
                ",".join(properties) or "<none>",
            )
            if _schema_requests_secret(schema):
                await self._send_safely(
                    chat_id,
                    f"MCP {server_name} запросил секретные данные. "
                    "Я не передаю токены и пароли через Telegram, поэтому запрос отклонён.",
                )
                return {"action": "decline"}

            content = await self._collect_mcp_form(
                chat_id, task.id, server_name, message, schema
            )
            if content is _MCP_FORM_CANCELLED:
                return {"action": "cancel"}
            return {"action": "accept", "content": content}
        finally:
            self._mark_active(thread_id, chat_id)

    async def _collect_mcp_form(
        self,
        chat_id: int,
        task_id: int,
        server_name: str,
        message: str,
        schema: Any,
    ) -> dict[str, Any] | object:
        properties = _mcp_schema_properties(schema)
        if not properties:
            token = secrets.token_urlsafe(8)
            action = await self._ask_with_buttons(
                chat_id,
                f"MCP {server_name} запрашивает подтверждение:\n\n{message}",
                kind="mcp-form",
                keyboard={
                    "inline_keyboard": [
                        [
                            {
                                "text": "Разрешить",
                                "callback_data": f"cb:{token}:accept",
                            },
                            {
                                "text": "Отклонить",
                                "callback_data": f"cb:{token}:cancel",
                            },
                        ]
                    ]
                },
                timeout=INTERACTION_TIMEOUT_SECONDS,
                token=token,
            )
            return {} if action == "accept" else _MCP_FORM_CANCELLED

        required = (
            set(schema.get("required") or []) if isinstance(schema, dict) else set()
        )
        await self._send_safely(
            chat_id,
            f"MCP {server_name} запрашивает данные:\n\n{message}\n\n"
            f"Нужно ответить на {_pluralize_questions(len(properties))}. "
            "Технический JSON вводить не нужно.",
        )
        content: dict[str, Any] = {}
        for name, details in properties.items():
            value = await self._ask_mcp_form_field(
                chat_id,
                task_id,
                server_name,
                name,
                details,
                required=name in required,
            )
            if value is _MCP_FORM_CANCELLED:
                return _MCP_FORM_CANCELLED
            if value is _MCP_FORM_SKIPPED:
                continue
            content[name] = value
        return content

    async def _ask_mcp_form_field(
        self,
        chat_id: int,
        task_id: int,
        server_name: str,
        name: str,
        details: Any,
        *,
        required: bool,
    ) -> Any:
        details = details if isinstance(details, dict) else {}
        title = str(details.get("title") or name)
        description = str(details.get("description") or "").strip()
        requirement = (
            "Обязательное поле." if required else "Необязательное поле."
        )
        prompt_parts = [f"MCP {server_name}: {title}"]
        if description:
            prompt_parts.append(description)
        prompt_parts.append(requirement)
        prompt = "\n\n".join(prompt_parts)[:3200]

        choices = _mcp_enum_choices(details)
        field_type = _mcp_field_type(details)
        if field_type != "array" and choices and len(choices) <= 20:
            token = secrets.token_urlsafe(8)
            rows = [
                [
                    {
                        "text": label[:50],
                        "callback_data": f"cb:{token}:{index}",
                    }
                ]
                for index, (label, _) in enumerate(choices)
            ]
            if not required:
                rows.append(
                    [{"text": "Пропустить", "callback_data": f"cb:{token}:skip"}]
                )
            rows.append(
                [{"text": "Отменить форму", "callback_data": f"cb:{token}:cancel"}]
            )
            selected = await self._ask_with_buttons(
                chat_id,
                prompt,
                kind="mcp-form",
                keyboard={"inline_keyboard": rows},
                timeout=INTERACTION_TIMEOUT_SECONDS,
                token=token,
            )
            if selected in {None, "cancel"}:
                return _MCP_FORM_CANCELLED
            if selected == "skip":
                return _MCP_FORM_SKIPPED
            if selected.isdigit() and int(selected) < len(choices):
                return choices[int(selected)][1]
            return _MCP_FORM_CANCELLED

        if field_type == "boolean":
            token = secrets.token_urlsafe(8)
            rows = [
                [
                    {"text": "Да", "callback_data": f"cb:{token}:true"},
                    {"text": "Нет", "callback_data": f"cb:{token}:false"},
                ]
            ]
            if not required:
                rows.append(
                    [{"text": "Пропустить", "callback_data": f"cb:{token}:skip"}]
                )
            rows.append(
                [{"text": "Отменить форму", "callback_data": f"cb:{token}:cancel"}]
            )
            selected = await self._ask_with_buttons(
                chat_id,
                prompt,
                kind="mcp-form",
                keyboard={"inline_keyboard": rows},
                timeout=INTERACTION_TIMEOUT_SECONDS,
                token=token,
            )
            if selected in {None, "cancel"}:
                return _MCP_FORM_CANCELLED
            if selected == "skip":
                return _MCP_FORM_SKIPPED
            return selected == "true"

        if field_type == "array" and choices:
            variants = "\n".join(f"• {label}" for label, _ in choices)
            prompt += (
                "\n\nВарианты:\n"
                + variants
                + "\n\nПришли один или несколько вариантов через запятую."
            )
        elif field_type in {"integer", "number"}:
            prompt += "\n\nПришли число."
        else:
            prompt += "\n\nПришли ответ одним сообщением."
        prompt += " /cancel отменит только эту MCP-форму."
        if not required:
            prompt += " /skip пропустит поле."

        while True:
            raw_value = await self._ask_for_text(
                chat_id,
                prompt[:3900],
                kind="mcp-form",
                timeout=INTERACTION_TIMEOUT_SECONDS,
                allow_skip=not required,
                task_id=task_id,
            )
            if raw_value is None:
                return _MCP_FORM_CANCELLED
            if raw_value is _MCP_FORM_SKIPPED:
                return _MCP_FORM_SKIPPED
            try:
                return _parse_mcp_form_value(str(raw_value), details)
            except ValueError as error:
                await self._send_safely(chat_id, str(error))

    async def _ask_with_buttons(
        self,
        chat_id: int,
        text: str,
        *,
        kind: str,
        keyboard: dict[str, Any],
        timeout: float,
        token: str | None = None,
        timeout_message: str | None = None,
    ) -> str | None:
        token = token or secrets.token_urlsafe(8)
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        pending = PendingCallback(chat_id, kind, future)
        self._pending_callbacks[token] = pending
        try:
            try:
                pending.message_id = await self._telegram.send_message(
                    chat_id, text, reply_markup=keyboard
                )
            except TelegramError as error:
                LOGGER.warning("Could not deliver %s request; declining: %s", kind, error)
                return None
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError:
                if timeout_message:
                    self._enqueue_outbox(chat_id, timeout_message, formatted=False)
                return None
        finally:
            self._pending_callbacks.pop(token, None)
            if pending.message_id is not None:
                await self._clear_keyboard_safely(chat_id, pending.message_id)

    async def _ask_for_text(
        self,
        chat_id: int,
        prompt: str,
        *,
        kind: str,
        timeout: float,
        allow_skip: bool = False,
        task_id: int = 0,
    ) -> Any:
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        pending = PendingTextInput(chat_id, kind, future, allow_skip, task_id)
        pending_inputs = self._pending_text_by_chat.setdefault(chat_id, [])
        if any(item.task_id == task_id for item in pending_inputs):
            LOGGER.warning(
                "Cannot open two text input requests for task %s in chat %s",
                task_id,
                chat_id,
            )
            return None
        pending_inputs.append(pending)
        try:
            try:
                pending.message_id = await self._telegram.send_message(
                    chat_id,
                    prompt,
                    reply_markup={"force_reply": True, "selective": True},
                )
            except TelegramError as error:
                LOGGER.warning("Could not deliver %s text request: %s", kind, error)
                return None
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError:
                self._enqueue_outbox(
                    chat_id,
                    "Ожидание ответа истекло спустя 24 часа. Повтори исходный запрос, "
                    "чтобы продолжить задачу.",
                    formatted=False,
                )
                return None
        finally:
            pending_inputs = self._pending_text_by_chat.get(chat_id, [])
            if pending in pending_inputs:
                pending_inputs.remove(pending)
            if not pending_inputs:
                self._pending_text_by_chat.pop(chat_id, None)

    def _mark_waiting(self, thread_id: str, chat_id: int, flag: str) -> None:
        self._thread_statuses[thread_id] = {
            "type": "active",
            "activeFlags": [flag],
        }
        task = self._state.get_task_by_thread_id(thread_id)
        if task is not None and (active := self._active_by_task.get(task.id)):
            active.touch()

    def _mark_active(self, thread_id: str, chat_id: int) -> None:
        self._thread_statuses[thread_id] = {"type": "active", "activeFlags": []}
        task = self._state.get_task_by_thread_id(thread_id)
        if task is not None and (active := self._active_by_task.get(task.id)):
            active.touch()

    async def _send_safely(self, chat_id: int, text: str) -> None:
        try:
            await self._telegram.send_message(chat_id, text)
        except TelegramError as error:
            LOGGER.warning("Could not send Telegram status message: %s", error)

    async def _clear_keyboard_safely(self, chat_id: int, message_id: int) -> None:
        try:
            await self._telegram.clear_inline_keyboard(chat_id, message_id)
        except TelegramError as error:
            LOGGER.warning("Could not clear Telegram inline keyboard: %s", error)

    async def _handle_callback(self, callback: dict[str, Any]) -> None:
        callback_id = str(callback["id"])
        user_id = int(callback["from"]["id"])
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = int(chat.get("id", 0))
        if user_id not in self._config.allowed_user_ids:
            await self._answer_callback_safely(callback_id, "Доступ запрещён")
            return

        data = str(callback.get("data", ""))
        parts = data.split(":")
        if len(parts) != 3 or parts[0] != "cb":
            if len(parts) == 3 and parts[0] == "ap":
                await self._answer_callback_safely(
                    callback_id, "Запрос был создан старой версией бота и уже неактуален"
                )
                return
            await self._answer_callback_safely(callback_id, "Неизвестное действие")
            return
        _, token, action = parts
        pending = self._pending_callbacks.get(token)
        if pending is None or pending.chat_id != chat_id:
            await self._answer_callback_safely(callback_id, "Запрос уже неактуален")
            return
        self._pending_callbacks.pop(token, None)
        if not pending.future.done():
            decision = "acceptForSession" if action == "session" else action
            pending.future.set_result(decision)
        message_id = message.get("message_id") or pending.message_id
        pending.message_id = None
        if message_id is not None:
            await self._clear_keyboard_safely(chat_id, int(message_id))
        LOGGER.info("Handled Telegram callback kind=%s action=%s", pending.kind, action)
        await self._answer_callback_safely(callback_id, "Решение передано Codex")
        await self._send_safely(chat_id, "Ответ передан Codex.")

    async def _answer_callback_safely(self, callback_id: str, text: str) -> None:
        try:
            await self._telegram.answer_callback_query(callback_id, text)
        except TelegramError as error:
            LOGGER.warning("Could not answer Telegram callback: %s", error)


def _format_duration(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    if total_seconds < 60:
        return f"{total_seconds} сек"
    minutes, seconds_remainder = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes} мин {seconds_remainder} сек"
    hours, minutes_remainder = divmod(minutes, 60)
    return f"{hours} ч {minutes_remainder} мин"


def _title_from_prompt(prompt: str) -> str:
    normalized = " ".join(prompt.split())
    if not normalized:
        return "Новый диалог"
    return normalized[:140]


def _task_status_icon(status: str) -> str:
    return {
        "new": "⚪️",
        "running": "🟡",
        "completed": "🟢",
        "failed": "🔴",
        "interrupted": "⚫️",
        "idle": "🔵",
    }.get(status, "⚪️")


def _task_status_label(status: str) -> str:
    return {
        "new": "ожидает первого сообщения",
        "running": "работает",
        "completed": "завершена",
        "failed": "завершилась с ошибкой",
        "interrupted": "прервана",
        "idle": "ожидает сообщения",
    }.get(status, status)


def _request_timeout(_auto_resolution_ms: Any) -> float:
    return INTERACTION_TIMEOUT_SECONDS


def _schema_requests_secret(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    sensitive_fragments = (
        "token",
        "password",
        "secret",
        "api_key",
        "apikey",
        "oauth",
        "credential",
    )
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, details in properties.items():
            normalized = str(name).lower().replace("-", "_")
            if any(fragment in normalized for fragment in sensitive_fragments):
                return True
            if isinstance(details, dict) and details.get("format") == "password":
                return True
    return False


def _mcp_schema_properties(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {}
    properties = schema.get("properties")
    return properties if isinstance(properties, dict) else {}


def _mcp_field_type(details: dict[str, Any]) -> str:
    value_type = details.get("type", "string")
    if isinstance(value_type, list):
        value_type = next((item for item in value_type if item != "null"), "string")
    return str(value_type)


def _mcp_enum_choices(details: dict[str, Any]) -> list[tuple[str, Any]]:
    source = details.get("items") if _mcp_field_type(details) == "array" else details
    if not isinstance(source, dict):
        return []
    enum = source.get("enum")
    if isinstance(enum, list):
        return [(str(value), value) for value in enum]

    one_of = source.get("oneOf")
    if not isinstance(one_of, list):
        return []
    choices: list[tuple[str, Any]] = []
    for item in one_of:
        if not isinstance(item, dict) or "const" not in item:
            return []
        value = item["const"]
        choices.append((str(item.get("title") or value), value))
    return choices


def _parse_mcp_form_value(raw_value: str, details: dict[str, Any]) -> Any:
    value_type = _mcp_field_type(details)
    value = raw_value.strip()
    if value_type == "integer":
        try:
            return int(value)
        except ValueError as error:
            raise ValueError("Нужно прислать целое число.") from error
    if value_type == "number":
        try:
            return float(value.replace(",", "."))
        except ValueError as error:
            raise ValueError("Нужно прислать число.") from error
    if value_type == "array":
        parts = [part.strip() for part in value.split(",") if part.strip()]
        if not parts:
            raise ValueError("Пришли хотя бы один вариант.")
        choices = _mcp_enum_choices(details)
        if not choices:
            return parts
        by_label = {
            str(candidate).casefold(): candidate
            for _, candidate in choices
        }
        by_label.update(
            {label.casefold(): candidate for label, candidate in choices}
        )
        unknown = [part for part in parts if part.casefold() not in by_label]
        if unknown:
            raise ValueError(
                "Неизвестный вариант: "
                + ", ".join(unknown)
                + ". Выбери из списка."
            )
        return [by_label[part.casefold()] for part in parts]
    return raw_value


def _pluralize_questions(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        suffix = "вопрос"
    elif count % 10 in {2, 3, 4} and count % 100 not in {12, 13, 14}:
        suffix = "вопроса"
    else:
        suffix = "вопросов"
    return f"{count} {suffix}"
