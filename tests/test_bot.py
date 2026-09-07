from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from codex_telegram_bot.approvals import (
    assess_safe_read_only_approval,
    is_safe_read_only_approval,
)
from codex_telegram_bot.bot import (
    STALLED_TASK_REMINDER_SECONDS,
    TELEGRAM_CLIENT_INSTRUCTIONS,
    ActiveTurn,
    TelegramCodexBot,
)
from codex_telegram_bot.config import Config
from codex_telegram_bot.state import OutboxMessage, TaskRecord
from codex_telegram_bot.telegram_api import TelegramError

CHAT_ID = 101
USER_ID = 202
THREAD_ID = "thread-1"
TURN_ID = "turn-1"


class FakeTelegram:
    def __init__(self, *, fail_send: bool = False) -> None:
        self.fail_send = fail_send
        self.sent: list[tuple[int, str, dict | None]] = []
        self.formatted: list[bool] = []
        self.cleared: list[tuple[int, int]] = []
        self.callback_answers: list[tuple[str, str]] = []
        self.downloads: list[tuple[str, Path, int]] = []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict | None = None,
        formatted: bool = False,
    ) -> int:
        if self.fail_send:
            raise TelegramError("simulated network failure")
        self.sent.append((chat_id, text, reply_markup))
        self.formatted.append(formatted)
        return len(self.sent)

    async def clear_inline_keyboard(self, chat_id: int, message_id: int) -> None:
        self.cleared.append((chat_id, message_id))

    async def answer_callback_query(self, callback_id: str, text: str) -> None:
        self.callback_answers.append((callback_id, text))

    async def send_typing(self, chat_id: int) -> None:
        return None

    async def download_file(
        self,
        file_id: str,
        destination: Path,
        *,
        max_bytes: int,
    ) -> None:
        self.downloads.append((file_id, destination, max_bytes))
        destination.write_bytes(b"fake telegram voice")


class FakeVoiceTranscriber:
    def __init__(self, text: str = "Сделай ревью PR") -> None:
        self.text = text
        self.paths: list[Path] = []

    async def transcribe(self, audio_path: Path) -> str:
        self.paths.append(audio_path)
        return self.text


class FakeAppServer:
    def __init__(self) -> None:
        self.notification_handler = None
        self.server_request_handler = None
        self.is_healthy = True
        self.responses: dict[str, dict] = {}
        self.requests: list[tuple[str, dict]] = []
        self.restart_calls = 0

    async def request(self, method: str, params: dict) -> dict:
        self.requests.append((method, params))
        return self.responses[method]

    async def restart(self) -> None:
        self.restart_calls += 1


class FakeState:
    def __init__(self) -> None:
        self.outbox: list[OutboxMessage] = []
        self.next_outbox_id = 1
        self.next_task_id = 2
        self.tasks = {
            1: TaskRecord(
                id=1,
                chat_id=CHAT_ID,
                user_id=USER_ID,
                number=1,
                thread_id=THREAD_ID,
                title="Review",
                status="idle",
                current_turn_id=None,
                result_text=None,
                result_formatted=True,
                latest_diff=None,
                created_at="now",
                updated_at="now",
                completed_at=None,
                archived_at=None,
            )
        }
        self.selected = {CHAT_ID: 1}

    def _replace(self, task_id: int, **changes: object) -> TaskRecord:
        task = self.tasks[task_id]
        values = {
            field: getattr(task, field)
            for field in TaskRecord.__dataclass_fields__
        }
        values.update(changes)
        updated = TaskRecord(**values)
        self.tasks[task_id] = updated
        return updated

    def create_task(
        self, chat_id: int, user_id: int, title: str | None = None
    ) -> TaskRecord:
        number = max(
            (task.number for task in self.tasks.values() if task.chat_id == chat_id),
            default=0,
        ) + 1
        task = TaskRecord(
            id=self.next_task_id,
            chat_id=chat_id,
            user_id=user_id,
            number=number,
            thread_id=None,
            title=title,
            status="new",
            current_turn_id=None,
            result_text=None,
            result_formatted=True,
            latest_diff=None,
            created_at="now",
            updated_at="now",
            completed_at=None,
            archived_at=None,
        )
        self.next_task_id += 1
        self.tasks[task.id] = task
        self.selected[chat_id] = task.id
        return task

    def create_attached_task(
        self,
        chat_id: int,
        user_id: int,
        thread_id: str,
        title: str | None,
        *,
        status: str,
        result_text: str | None,
        result_formatted: bool,
    ) -> TaskRecord:
        task = self.create_task(chat_id, user_id, title)
        return self._replace(
            task.id,
            thread_id=thread_id,
            status=status,
            result_text=result_text,
            result_formatted=result_formatted,
            completed_at="now"
            if status in {"completed", "failed", "interrupted"}
            else None,
        )

    def list_tasks(
        self, chat_id: int, *, archived: bool | None = False
    ) -> list[TaskRecord]:
        return sorted(
            (
                task
                for task in self.tasks.values()
                if task.chat_id == chat_id
                and (
                    archived is None
                    or (task.archived_at is not None) == archived
                )
            ),
            key=lambda task: task.number,
        )

    def get_task(self, chat_id: int, number: int) -> TaskRecord | None:
        return next(
            (
                task
                for task in self.tasks.values()
                if task.chat_id == chat_id and task.number == number
            ),
            None,
        )

    def get_task_by_id(self, task_id: int) -> TaskRecord | None:
        return self.tasks.get(task_id)

    def get_task_by_thread_id(self, thread_id: str) -> TaskRecord | None:
        return next(
            (task for task in self.tasks.values() if task.thread_id == thread_id),
            None,
        )

    def get_selected_task(self, chat_id: int) -> TaskRecord | None:
        task_id = self.selected.get(chat_id)
        task = self.tasks.get(task_id) if task_id is not None else None
        return task if task is not None and task.archived_at is None else None

    def select_task(self, chat_id: int, number: int) -> TaskRecord | None:
        task = self.get_task(chat_id, number)
        if task is not None and task.archived_at is None:
            self.selected[chat_id] = task.id
            return task
        return None

    def archive_task(self, chat_id: int, number: int) -> TaskRecord | None:
        task = self.get_task(chat_id, number)
        if task is None:
            return None
        archived = self._replace(task.id, archived_at="now")
        if self.selected.get(chat_id) == task.id:
            candidates = self.list_tasks(chat_id)
            if candidates:
                self.selected[chat_id] = candidates[-1].id
            else:
                self.selected.pop(chat_id, None)
        return archived

    def unarchive_task(self, chat_id: int, number: int) -> TaskRecord | None:
        task = self.get_task(chat_id, number)
        return self._replace(task.id, archived_at=None) if task is not None else None

    def attach_thread(self, task_id: int, thread_id: str) -> None:
        self._replace(task_id, thread_id=thread_id)

    def set_task_title(self, task_id: int, title: str) -> None:
        self._replace(task_id, title=title)

    def set_task_running(self, task_id: int, turn_id: str) -> None:
        self._replace(task_id, status="running", current_turn_id=turn_id)

    def complete_task(
        self,
        task_id: int,
        status: str,
        result_text: str,
        *,
        formatted: bool,
        latest_diff: str | None = None,
    ) -> None:
        self._replace(
            task_id,
            status=status,
            current_turn_id=None,
            result_text=result_text,
            result_formatted=formatted,
            latest_diff=latest_diff,
        )

    def set_task_diff(self, task_id: int, diff: str) -> None:
        self._replace(task_id, latest_diff=diff)

    def get_chat_id(self, thread_id: str) -> int | None:
        task = self.get_task_by_thread_id(thread_id)
        return task.chat_id if task else None

    def get_thread_id(self, chat_id: int) -> str | None:
        task = self.get_selected_task(chat_id)
        return task.thread_id if task else None

    def enqueue_outbox(self, chat_id: int, text: str, *, formatted: bool) -> int:
        message_id = self.next_outbox_id
        self.next_outbox_id += 1
        self.outbox.append(
            OutboxMessage(message_id, chat_id, text, formatted, attempts=0)
        )
        return message_id

    def get_outbox(self, limit: int = 20) -> list[OutboxMessage]:
        return self.outbox[:limit]

    def mark_outbox_attempt(self, message_id: int) -> None:
        self.outbox = [
            OutboxMessage(
                message.id,
                message.chat_id,
                message.text,
                message.formatted,
                message.attempts + 1,
            )
            if message.id == message_id
            else message
            for message in self.outbox
        ]

    def delete_outbox(self, message_id: int) -> None:
        self.outbox = [message for message in self.outbox if message.id != message_id]


def make_bot(
    telegram: FakeTelegram | None = None,
    app_server: FakeAppServer | None = None,
    state: FakeState | None = None,
    voice_transcriber: FakeVoiceTranscriber | None = None,
    *,
    voice_enabled: bool = False,
    auto_approve: bool = False,
    approvals_reviewer: str = "user",
) -> TelegramCodexBot:
    config = Config(
        telegram_token="not-a-real-token",
        allowed_user_ids=frozenset({USER_ID}),
        codex_cwd=Path("/tmp"),
        codex_bin="codex",
        codex_model=None,
        reasoning_effort=None,
        state_path=Path("/tmp/not-used.sqlite3"),
        approvals_reviewer=approvals_reviewer,
        voice_transcription_enabled=voice_enabled,
        auto_approve_safe_read_only=auto_approve,
        auto_approve_read_roots=(Path("/tmp"),),
    )
    return TelegramCodexBot(
        config,
        telegram or FakeTelegram(),  # type: ignore[arg-type]
        app_server or FakeAppServer(),  # type: ignore[arg-type]
        state or FakeState(),  # type: ignore[arg-type]
        voice_transcriber,  # type: ignore[arg-type]
    )


class TelegramCodexBotTests(unittest.IsolatedAsyncioTestCase):
    async def test_release_restarts_app_server_without_active_tasks(self) -> None:
        telegram = FakeTelegram()
        app_server = FakeAppServer()
        bot = make_bot(telegram=telegram, app_server=app_server)
        bot._thread_statuses[THREAD_ID] = {"type": "idle"}

        await bot._handle_update(
            {
                "message": {
                    "chat": {"id": CHAT_ID, "type": "private"},
                    "from": {"id": USER_ID},
                    "message_id": 406,
                    "text": "/release",
                }
            }
        )

        self.assertEqual(app_server.restart_calls, 1)
        self.assertEqual(bot._thread_statuses, {})
        self.assertIn("Треды освобождены", telegram.sent[-1][1])

    async def test_release_is_rejected_while_a_task_is_running(self) -> None:
        telegram = FakeTelegram()
        app_server = FakeAppServer()
        bot = make_bot(telegram=telegram, app_server=app_server)
        bot._register_active(
            ActiveTurn(
                CHAT_ID,
                THREAD_ID,
                TURN_ID,
                task_id=1,
                task_number=1,
            )
        )

        await bot._handle_update(
            {
                "message": {
                    "chat": {"id": CHAT_ID, "type": "private"},
                    "from": {"id": USER_ID},
                    "message_id": 407,
                    "text": "/release",
                }
            }
        )

        self.assertEqual(app_server.restart_calls, 0)
        self.assertIn("выполняются задачи #1", telegram.sent[-1][1])

    async def test_voice_message_is_transcribed_and_submitted_as_prompt(self) -> None:
        telegram = FakeTelegram()
        transcriber = FakeVoiceTranscriber("Проверь изменения в scooters-core")
        app_server = FakeAppServer()
        app_server.responses.update(
            {
                "thread/resume": {
                    "thread": {
                        "id": THREAD_ID,
                        "name": "[Telegram] Review",
                        "status": {"type": "idle"},
                    }
                },
                "turn/start": {"turn": {"id": TURN_ID}},
            }
        )
        bot = make_bot(
            telegram=telegram,
            app_server=app_server,
            voice_transcriber=transcriber,
            voice_enabled=True,
        )

        await bot._handle_update(
            {
                "message": {
                    "chat": {"id": CHAT_ID, "type": "private"},
                    "from": {"id": USER_ID},
                    "message_id": 404,
                    "voice": {
                        "file_id": "voice-file-1",
                        "duration": 12,
                        "file_size": 1024,
                    },
                }
            }
        )

        self.assertEqual(telegram.downloads[0][0], "voice-file-1")
        self.assertEqual(len(transcriber.paths), 1)
        self.assertFalse(transcriber.paths[0].exists())
        turn_start = next(
            params for method, params in app_server.requests if method == "turn/start"
        )
        self.assertEqual(
            turn_start["input"],
            [{"type": "text", "text": "Проверь изменения в scooters-core"}],
        )
        self.assertEqual(turn_start["cwd"], "/tmp")
        self.assertEqual(turn_start["sandboxPolicy"]["type"], "workspaceWrite")
        self.assertIn("/tmp", turn_start["sandboxPolicy"]["writableRoots"])
        self.assertTrue(turn_start["sandboxPolicy"]["networkAccess"])

    async def test_voice_message_over_duration_limit_is_rejected(self) -> None:
        telegram = FakeTelegram()
        transcriber = FakeVoiceTranscriber()
        bot = make_bot(
            telegram=telegram,
            voice_transcriber=transcriber,
            voice_enabled=True,
        )

        await bot._handle_update(
            {
                "message": {
                    "chat": {"id": CHAT_ID, "type": "private"},
                    "from": {"id": USER_ID},
                    "message_id": 405,
                    "voice": {
                        "file_id": "voice-file-2",
                        "duration": 601,
                        "file_size": 1024,
                    },
                }
            }
        )

        self.assertEqual(telegram.downloads, [])
        self.assertEqual(transcriber.paths, [])
        self.assertIn("слишком длинное", telegram.sent[-1][1])

    async def test_safe_read_only_command_is_auto_approved(self) -> None:
        telegram = FakeTelegram()
        bot = make_bot(telegram=telegram, auto_approve=True)

        result = await bot._handle_codex_request(
            "item/commandExecution/requestApproval",
            {
                "threadId": THREAD_ID,
                "itemId": "command-1",
                "command": "sed -n '1,80p' src/main.py",
                "cwd": "/tmp/project",
                "commandActions": [{"type": "read", "path": "src/main.py"}],
                "availableDecisions": ["accept", "decline"],
            },
        )

        self.assertEqual(result, {"decision": "accept"})
        self.assertEqual(telegram.sent, [])

    def test_read_only_auto_approval_rejects_unsafe_requests(self) -> None:
        roots = (Path("/tmp/project"),)
        base = {
            "command": "sed -n '1,80p' src/main.py",
            "cwd": "/tmp/project",
            "commandActions": [{"type": "read", "path": "src/main.py"}],
            "availableDecisions": ["accept", "decline"],
        }
        cases = [
            {**base, "commandActions": [{"type": "unknown"}]},
            {**base, "networkApprovalContext": {"host": "example.test"}},
            {
                **base,
                "command": "cat .env",
                "commandActions": [{"type": "read", "path": ".env"}],
            },
            {
                **base,
                "commandActions": [{"type": "read", "path": "/etc/hosts"}],
            },
            {
                **base,
                "additionalPermissions": {
                    "fileSystem": {"write": ["/tmp/project"]}
                },
            },
        ]

        for params in cases:
            with self.subTest(params=params):
                self.assertFalse(is_safe_read_only_approval(params, roots))

    def test_allowlisted_arc_reads_are_auto_approved_when_unclassified(self) -> None:
        roots = (Path("/tmp/project"),)
        commands = [
            "arc status",
            "arc diff -- src/main.py",
            "arc show deadbeef",
            "arc info",
            "arc ls src",
            "arc log -n 5",
            "arc root",
            "arc pr status",
            "arc pr changes 12345",
            "/bin/zsh -lc 'arc status'",
        ]

        for command in commands:
            with self.subTest(command=command):
                assessment = assess_safe_read_only_approval(
                    {
                        "command": command,
                        "cwd": "/tmp/project",
                        "commandActions": [{"type": "unknown"}],
                        "availableDecisions": ["accept", "decline"],
                    },
                    roots,
                )
                self.assertTrue(assessment.approved)
                self.assertEqual(assessment.reason, "allowlisted_arc_read")

    def test_arc_read_allowlist_rejects_mutation_and_shell_composition(self) -> None:
        roots = (Path("/tmp/project"),)
        commands = [
            "arc checkout trunk",
            "arc commit -m change",
            "arc status; rm -rf output",
            "arc status && arc checkout trunk",
            "arc diff --ext-diff=/tmp/helper",
            "arc log --template custom",
            "/bin/zsh -lc 'arc status | tee status.txt'",
        ]

        for command in commands:
            with self.subTest(command=command):
                assessment = assess_safe_read_only_approval(
                    {
                        "command": command,
                        "cwd": "/tmp/project",
                        "commandActions": [{"type": "unknown"}],
                    },
                    roots,
                )
                self.assertFalse(assessment.approved)
                self.assertEqual(assessment.reason, "unclassified_command")

    def test_arc_read_allowlist_allows_only_loopback_network_context(self) -> None:
        roots = (Path("/tmp/project"),)
        base = {
            "command": "arc status",
            "cwd": "/tmp/project",
            "commandActions": [{"type": "unknown"}],
        }

        loopback = assess_safe_read_only_approval(
            {**base, "networkApprovalContext": {"host": "127.0.0.1"}}, roots
        )
        external = assess_safe_read_only_approval(
            {**base, "networkApprovalContext": {"host": "arc.example.test"}}, roots
        )

        self.assertTrue(loopback.approved)
        self.assertFalse(external.approved)
        self.assertEqual(external.reason, "external_network_access")

    def test_auto_approval_reports_a_non_sensitive_rejection_reason(self) -> None:
        assessment = assess_safe_read_only_approval(
            {
                "command": "arc checkout trunk",
                "cwd": "/tmp/project",
                "commandActions": [{"type": "unknown"}],
            },
            (Path("/tmp/project"),),
        )

        self.assertFalse(assessment.approved)
        self.assertEqual(assessment.reason, "unclassified_command")
        self.assertEqual(assessment.action_types, ("unknown",))

    async def test_submit_prompt_does_not_send_started_acknowledgement(self) -> None:
        telegram = FakeTelegram()
        app_server = FakeAppServer()
        app_server.responses.update(
            {
                "thread/resume": {
                    "thread": {
                        "id": THREAD_ID,
                        "name": "[Telegram] Review",
                        "status": {"type": "idle"},
                    }
                },
                "turn/start": {"turn": {"id": TURN_ID}},
            }
        )
        bot = make_bot(telegram=telegram, app_server=app_server)

        await bot._submit_prompt(CHAT_ID, USER_ID, 303, "Review this change")

        self.assertEqual(telegram.sent, [])

    async def test_does_not_send_commentary_progress(self) -> None:
        telegram = FakeTelegram()
        bot = make_bot(telegram=telegram)
        active = ActiveTurn(CHAT_ID, THREAD_ID, TURN_ID)
        bot._register_active(active)

        await bot._handle_codex_notification(
            "item/completed",
            {
                "threadId": THREAD_ID,
                "turnId": TURN_ID,
                "item": {
                    "id": "message-1",
                    "type": "agentMessage",
                    "phase": "commentary",
                    "text": "Проверяю изменения и связанные вызовы.",
                },
            },
        )
        self.assertEqual(telegram.sent, [])
        self.assertIn("message-1", active.progress_item_ids)

    async def test_turn_completion_sends_only_formatted_final_answer(self) -> None:
        telegram = FakeTelegram()
        state = FakeState()
        bot = make_bot(telegram=telegram, state=state)
        bot._register_active(ActiveTurn(CHAT_ID, THREAD_ID, TURN_ID))

        await bot._handle_codex_notification(
            "turn/completed",
            {
                "threadId": THREAD_ID,
                "turn": {
                    "id": TURN_ID,
                    "status": "completed",
                    "items": [
                        {
                            "id": "commentary-1",
                            "type": "agentMessage",
                            "phase": "commentary",
                            "text": "Проверяю изменения.",
                        },
                        {
                            "id": "final-1",
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": "**Готово.** Используй `result`.",
                        },
                    ],
                },
            },
        )

        self.assertEqual(telegram.sent, [])
        self.assertEqual(len(state.outbox), 1)
        self.assertEqual(
            state.outbox[0].text,
            "Задача #1 завершена.\n\n**Готово.** Используй `result`.",
        )

        self.assertTrue(await bot._deliver_outbox_message(state.outbox[0]))

        self.assertEqual(
            telegram.sent,
            [
                (
                    CHAT_ID,
                    "Задача #1 завершена.\n\n**Готово.** Используй `result`.",
                    None,
                )
            ],
        )
        self.assertEqual(telegram.formatted, [True])
        self.assertEqual(state.outbox, [])

    async def test_failed_final_delivery_remains_in_outbox_for_retry(self) -> None:
        telegram = FakeTelegram(fail_send=True)
        state = FakeState()
        bot = make_bot(telegram=telegram, state=state)
        message_id = state.enqueue_outbox(CHAT_ID, "Готово", formatted=True)

        self.assertFalse(await bot._deliver_outbox_message(state.outbox[0]))

        self.assertEqual(len(state.outbox), 1)
        self.assertEqual(state.outbox[0].id, message_id)
        self.assertEqual(state.outbox[0].attempts, 1)

    async def test_adds_telegram_prefix_to_thread_title(self) -> None:
        app_server = FakeAppServer()
        app_server.responses["thread/name/set"] = {}
        bot = make_bot(app_server=app_server)

        await bot._ensure_telegram_title(
            THREAD_ID,
            "Провести ревью PR",
            "Этот текст не должен заменить существующее название",
        )

        self.assertEqual(app_server.requests[-1][0], "thread/name/set")
        self.assertEqual(
            app_server.requests[-1][1]["name"],
            "[Telegram] Провести ревью PR",
        )

    async def test_failed_approval_delivery_returns_decline(self) -> None:
        bot = make_bot(telegram=FakeTelegram(fail_send=True))

        result = await bot._handle_codex_request(
            "item/commandExecution/requestApproval",
            {"threadId": THREAD_ID, "command": "true", "reason": "read-only"},
        )

        self.assertEqual(result, {"decision": "decline"})
        self.assertEqual(bot._pending_callbacks, {})

    async def test_thread_instructions_do_not_forbid_available_tools(self) -> None:
        bot = make_bot()

        start_params = bot._start_thread_params()
        resume_params = bot._resume_thread_params(THREAD_ID)

        self.assertEqual(
            start_params["developerInstructions"], TELEGRAM_CLIENT_INSTRUCTIONS
        )
        self.assertEqual(
            resume_params["developerInstructions"], TELEGRAM_CLIENT_INSTRUCTIONS
        )
        self.assertIn("Use the shell", TELEGRAM_CLIENT_INSTRUCTIONS)
        self.assertNotIn("Never call those tools", TELEGRAM_CLIENT_INSTRUCTIONS)
        self.assertIn("internal tool-call payload", TELEGRAM_CLIENT_INSTRUCTIONS)
        self.assertIn("JSON", TELEGRAM_CLIENT_INSTRUCTIONS)

    async def test_auto_review_is_forwarded_to_started_and_resumed_threads(self) -> None:
        bot = make_bot(approvals_reviewer="auto_review")

        self.assertEqual(
            bot._start_thread_params()["approvalsReviewer"], "auto_review"
        )
        self.assertEqual(
            bot._resume_thread_params(THREAD_ID)["approvalsReviewer"],
            "auto_review",
        )

    async def test_dynamic_tool_call_returns_retryable_failure(self) -> None:
        bot = make_bot()

        result = await bot._handle_codex_request(
            "item/tool/call",
            {
                "threadId": THREAD_ID,
                "turnId": TURN_ID,
                "callId": "call-1",
                "tool": "exec",
                "arguments": "return tools.exec_command({cmd: 'true'})",
            },
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["contentItems"][0]["type"], "inputText")
        self.assertIn("built-in", result["contentItems"][0]["text"])

    async def test_mcp_url_elicitation_accepts_callback(self) -> None:
        telegram = FakeTelegram()
        bot = make_bot(telegram=telegram)
        task = asyncio.create_task(
            bot._handle_codex_request(
                "mcpServer/elicitation/request",
                {
                    "threadId": THREAD_ID,
                    "serverName": "browser",
                    "mode": "url",
                    "message": "Open authorization page",
                    "elicitationId": "e-1",
                    "url": "https://example.test/auth",
                },
            )
        )
        await asyncio.sleep(0)
        token = next(iter(bot._pending_callbacks))

        await bot._handle_callback(
            {
                "id": "callback-1",
                "from": {"id": USER_ID},
                "message": {"message_id": 1, "chat": {"id": CHAT_ID}},
                "data": f"cb:{token}:accept",
            }
        )

        self.assertEqual(await task, {"action": "accept"})
        self.assertEqual(telegram.cleared, [(CHAT_ID, 1)])

    async def test_mcp_form_collects_text_field_without_user_json(self) -> None:
        telegram = FakeTelegram()
        bot = make_bot(telegram=telegram)
        task = asyncio.create_task(
            bot._handle_codex_request(
                "mcpServer/elicitation/request",
                {
                    "threadId": THREAD_ID,
                    "serverName": "intrasearch",
                    "mode": "form",
                    "message": "Оцени источник",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {
                            "comment": {
                                "type": "string",
                                "title": "Комментарий",
                            }
                        },
                        "required": ["comment"],
                    },
                },
            )
        )
        await asyncio.sleep(0)

        self.assertIn(CHAT_ID, bot._pending_text_by_chat)
        self.assertNotIn(
            "Ответь одним JSON-объектом",
            "\n".join(message for _, message, _ in telegram.sent),
        )
        await bot._handle_update(
            {
                "message": {
                    "chat": {"id": CHAT_ID, "type": "private"},
                    "from": {"id": USER_ID},
                    "message_id": 10,
                    "text": "Источник полезен",
                }
            }
        )

        self.assertEqual(
            await task,
            {
                "action": "accept",
                "content": {"comment": "Источник полезен"},
            },
        )

    async def test_mcp_form_uses_buttons_for_enum(self) -> None:
        telegram = FakeTelegram()
        bot = make_bot(telegram=telegram)
        task = asyncio.create_task(
            bot._handle_codex_request(
                "mcpServer/elicitation/request",
                {
                    "threadId": THREAD_ID,
                    "serverName": "intrasearch",
                    "mode": "form",
                    "message": "Выбери оценку",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {
                            "rating": {
                                "type": "string",
                                "title": "Релевантность",
                                "enum": ["irrelevant", "relevant", "vital"],
                            }
                        },
                        "required": ["rating"],
                    },
                },
            )
        )
        await asyncio.sleep(0)
        token = next(iter(bot._pending_callbacks))

        await bot._handle_callback(
            {
                "id": "callback-form-enum",
                "from": {"id": USER_ID},
                "message": {"message_id": 2, "chat": {"id": CHAT_ID}},
                "data": f"cb:{token}:1",
            }
        )

        self.assertEqual(
            await task,
            {"action": "accept", "content": {"rating": "relevant"}},
        )

    async def test_cancel_mcp_form_does_not_interrupt_codex_turn(self) -> None:
        telegram = FakeTelegram()
        app_server = FakeAppServer()
        bot = make_bot(telegram=telegram, app_server=app_server)
        bot._register_active(ActiveTurn(CHAT_ID, THREAD_ID, TURN_ID))
        task = asyncio.create_task(
            bot._handle_codex_request(
                "mcpServer/elicitation/request",
                {
                    "threadId": THREAD_ID,
                    "serverName": "intrasearch",
                    "mode": "form",
                    "message": "Нужен комментарий",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {"comment": {"type": "string"}},
                        "required": ["comment"],
                    },
                },
            )
        )
        await asyncio.sleep(0)

        await bot._handle_update(
            {
                "message": {
                    "chat": {"id": CHAT_ID, "type": "private"},
                    "from": {"id": USER_ID},
                    "message_id": 11,
                    "text": "/cancel",
                }
            }
        )

        self.assertEqual(await task, {"action": "cancel"})
        self.assertFalse(
            any(method == "turn/interrupt" for method, _ in app_server.requests)
        )
        self.assertIn(CHAT_ID, bot._active_by_chat)

    async def test_optional_mcp_form_field_can_be_skipped(self) -> None:
        telegram = FakeTelegram()
        bot = make_bot(telegram=telegram)
        task = asyncio.create_task(
            bot._handle_codex_request(
                "mcpServer/elicitation/request",
                {
                    "threadId": THREAD_ID,
                    "serverName": "intrasearch",
                    "mode": "form",
                    "message": "Необязательное пояснение",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {"comment": {"type": "string"}},
                    },
                },
            )
        )
        await asyncio.sleep(0)

        await bot._handle_update(
            {
                "message": {
                    "chat": {"id": CHAT_ID, "type": "private"},
                    "from": {"id": USER_ID},
                    "message_id": 12,
                    "text": "/skip",
                }
            }
        )

        self.assertEqual(await task, {"action": "accept", "content": {}})

    async def test_interaction_timeout_is_queued_for_reliable_delivery(self) -> None:
        telegram = FakeTelegram()
        state = FakeState()
        bot = make_bot(telegram=telegram, state=state)

        result = await bot._ask_with_buttons(
            CHAT_ID,
            "Подтверди действие",
            kind="approval:command",
            keyboard={"inline_keyboard": []},
            timeout=0.001,
            timeout_message="Ожидание подтверждения истекло.",
        )

        self.assertIsNone(result)
        self.assertEqual(telegram.cleared, [(CHAT_ID, 1)])
        self.assertEqual(len(state.outbox), 1)
        self.assertEqual(state.outbox[0].text, "Ожидание подтверждения истекло.")
        self.assertFalse(state.outbox[0].formatted)

    async def test_request_user_input_returns_selected_label(self) -> None:
        bot = make_bot()
        task = asyncio.create_task(
            bot._handle_codex_request(
                "item/tool/requestUserInput",
                {
                    "threadId": THREAD_ID,
                    "turnId": TURN_ID,
                    "itemId": "item-1",
                    "questions": [
                        {
                            "id": "environment",
                            "header": "Среда",
                            "question": "Где продолжить?",
                            "options": [
                                {"label": "Локально", "description": "На Mac"},
                                {"label": "Codenv", "description": "На VM"},
                            ],
                        }
                    ],
                },
            )
        )
        await asyncio.sleep(0)
        token = next(iter(bot._pending_callbacks))

        await bot._handle_callback(
            {
                "id": "callback-2",
                "from": {"id": USER_ID},
                "message": {"message_id": 1, "chat": {"id": CHAT_ID}},
                "data": f"cb:{token}:1",
            }
        )

        self.assertEqual(
            await task,
            {"answers": {"environment": {"answers": ["Codenv"]}}},
        )

    async def test_status_reconciles_stale_active_turn(self) -> None:
        telegram = FakeTelegram()
        app_server = FakeAppServer()
        app_server.responses["thread/read"] = {
            "thread": {
                "id": THREAD_ID,
                "status": {"type": "idle"},
                "turns": [
                    {
                        "id": TURN_ID,
                        "status": "interrupted",
                        "items": [],
                        "durationMs": 1500,
                    }
                ],
            }
        }
        bot = make_bot(telegram=telegram, app_server=app_server)
        bot._register_active(ActiveTurn(CHAT_ID, THREAD_ID, TURN_ID))

        await bot._send_status(CHAT_ID)

        self.assertNotIn(CHAT_ID, bot._active_by_chat)
        self.assertIn("последняя задача прервана", telegram.sent[-1][1])
        self.assertIn("Проверка живого статуса: получено", telegram.sent[-1][1])

    async def test_new_list_switch_and_result_commands(self) -> None:
        telegram = FakeTelegram()
        state = FakeState()
        bot = make_bot(telegram=telegram, state=state)

        await bot._new_thread(CHAT_ID, USER_ID, "Второе ревью")
        await bot._list_tasks(CHAT_ID)
        await bot._switch_task(CHAT_ID, "1")
        state.complete_task(1, "completed", "Замечаний нет.", formatted=True)
        await bot._send_result(CHAT_ID, "1")

        self.assertIn("Создана задача #2", telegram.sent[0][1])
        self.assertIn("#1 — Review", telegram.sent[1][1])
        self.assertIn("→ ⚪️ #2 — Второе ревью", telegram.sent[1][1])
        self.assertIn("Выбрана задача #1", telegram.sent[2][1])
        self.assertEqual(
            telegram.sent[3][1], "Результат задачи #1:\n\nЗамечаний нет."
        )
        self.assertTrue(telegram.formatted[3])

    async def test_threads_lists_unattached_codex_threads_for_current_cwd(self) -> None:
        telegram = FakeTelegram()
        app_server = FakeAppServer()
        app_server.responses["thread/list"] = {
            "data": [
                {
                    "id": THREAD_ID,
                    "name": "Already attached",
                    "status": {"type": "notLoaded"},
                },
                {
                    "id": "external-thread",
                    "name": "Ревью из Desktop",
                    "status": {"type": "idle"},
                },
            ],
            "nextCursor": None,
        }
        bot = make_bot(telegram=telegram, app_server=app_server)

        await bot._list_available_threads(CHAT_ID)

        self.assertNotIn("Already attached", telegram.sent[-1][1])
        self.assertIn("1. 🔵 Ревью из Desktop", telegram.sent[-1][1])
        method, params = app_server.requests[-1]
        self.assertEqual(method, "thread/list")
        self.assertEqual(params["cwd"], "/tmp")
        self.assertIn("appServer", params["sourceKinds"])

    async def test_attach_selects_thread_and_shows_last_exchange(self) -> None:
        telegram = FakeTelegram()
        state = FakeState()
        app_server = FakeAppServer()
        app_server.responses.update(
            {
                "thread/list": {
                    "data": [
                        {
                            "id": "external-thread",
                            "name": "Ревью из Desktop",
                            "status": {"type": "notLoaded"},
                        }
                    ],
                    "nextCursor": None,
                },
                "thread/read": {
                    "thread": {
                        "id": "external-thread",
                        "name": "Ревью из Desktop",
                        "status": {"type": "notLoaded"},
                        "turns": [
                            {
                                "id": "external-turn",
                                "status": "completed",
                                "items": [
                                    {
                                        "id": "user-1",
                                        "type": "userMessage",
                                        "content": [
                                            {
                                                "type": "text",
                                                "text": "Проверь scooters-core",
                                            }
                                        ],
                                    },
                                    {
                                        "id": "commentary-1",
                                        "type": "agentMessage",
                                        "phase": "commentary",
                                        "text": "Читаю код.",
                                    },
                                    {
                                        "id": "answer-1",
                                        "type": "agentMessage",
                                        "phase": "final_answer",
                                        "text": "Нашла одно замечание.",
                                    },
                                ],
                            }
                        ],
                    }
                },
            }
        )
        bot = make_bot(telegram=telegram, app_server=app_server, state=state)

        await bot._list_available_threads(CHAT_ID)
        await bot._attach_available_thread(CHAT_ID, USER_ID, "1")

        attached = state.get_task(CHAT_ID, 2)
        self.assertIsNotNone(attached)
        assert attached is not None
        self.assertEqual(attached.thread_id, "external-thread")
        self.assertEqual(attached.status, "completed")
        self.assertEqual(attached.result_text, "Нашла одно замечание.")
        self.assertEqual(state.get_selected_task(CHAT_ID), attached)
        self.assertIn("Тред подключён как задача #2", telegram.sent[-1][1])
        self.assertIn("Проверь scooters-core", telegram.sent[-1][1])
        self.assertIn("Нашла одно замечание", telegram.sent[-1][1])
        self.assertNotIn("Читаю код", telegram.sent[-1][1])
        self.assertTrue(telegram.formatted[-1])

    async def test_history_shows_requested_number_of_latest_messages(self) -> None:
        telegram = FakeTelegram()
        app_server = FakeAppServer()
        app_server.responses["thread/read"] = {
            "thread": {
                "id": THREAD_ID,
                "status": {"type": "notLoaded"},
                "turns": [
                    {
                        "id": "turn-old",
                        "status": "completed",
                        "items": [
                            {
                                "type": "userMessage",
                                "content": [{"type": "text", "text": "Старый вопрос"}],
                            },
                            {
                                "type": "agentMessage",
                                "phase": "final_answer",
                                "text": "Старый ответ",
                            },
                        ],
                    },
                    {
                        "id": "turn-new",
                        "status": "completed",
                        "items": [
                            {
                                "type": "userMessage",
                                "content": [{"type": "text", "text": "Новый вопрос"}],
                            },
                            {
                                "type": "agentMessage",
                                "phase": "final_answer",
                                "text": "Новый ответ",
                            },
                        ],
                    },
                ],
            }
        }
        bot = make_bot(telegram=telegram, app_server=app_server)

        await bot._send_history(CHAT_ID, "1 2")

        self.assertNotIn("Старый вопрос", telegram.sent[-1][1])
        self.assertNotIn("Старый ответ", telegram.sent[-1][1])
        self.assertIn("Новый вопрос", telegram.sent[-1][1])
        self.assertIn("Новый ответ", telegram.sent[-1][1])
        self.assertTrue(telegram.formatted[-1])

    async def test_task_can_be_renamed(self) -> None:
        telegram = FakeTelegram()
        state = FakeState()
        app_server = FakeAppServer()
        app_server.responses["thread/name/set"] = {}
        bot = make_bot(telegram=telegram, state=state, app_server=app_server)

        await bot._rename_task(CHAT_ID, "1   Ревью новой ручки ")

        self.assertEqual(state.get_task(CHAT_ID, 1).title, "Ревью новой ручки")
        self.assertEqual(
            telegram.sent[-1][1], "Задача #1 переименована: Ревью новой ручки"
        )
        self.assertEqual(
            app_server.requests[-1],
            (
                "thread/name/set",
                {
                    "threadId": THREAD_ID,
                    "name": "[Telegram] Ревью новой ручки",
                },
            ),
        )

    async def test_list_filters_tasks_by_live_status(self) -> None:
        telegram = FakeTelegram()
        state = FakeState()
        completed = state.create_task(CHAT_ID, USER_ID, "Готовая задача")
        state.complete_task(completed.id, "completed", "Готово", formatted=True)
        bot = make_bot(telegram=telegram, state=state)
        bot._register_active(ActiveTurn(CHAT_ID, THREAD_ID, TURN_ID))
        bot._thread_statuses[THREAD_ID] = {
            "type": "active",
            "activeFlags": ["waitingOnUserInput"],
        }

        await bot._list_tasks(CHAT_ID, "waiting")
        await bot._list_tasks(CHAT_ID, "completed")

        self.assertIn("🟠 #1 — Review", telegram.sent[0][1])
        self.assertNotIn("Готовая задача", telegram.sent[0][1])
        self.assertIn("🟢 #2 — Готовая задача", telegram.sent[1][1])
        self.assertNotIn("Review", telegram.sent[1][1])

    async def test_stalled_task_reminder_is_sent_once_per_activity_period(self) -> None:
        state = FakeState()
        bot = make_bot(state=state)
        active = ActiveTurn(
            CHAT_ID,
            THREAD_ID,
            TURN_ID,
            task_id=1,
            task_number=1,
            last_activity_at=100.0,
        )
        bot._register_active(active)

        reminder_time = 100.0 + STALLED_TASK_REMINDER_SECONDS
        bot._enqueue_stalled_task_reminders(now=reminder_time)
        bot._enqueue_stalled_task_reminders(now=reminder_time + 60)

        self.assertEqual(len(state.outbox), 1)
        self.assertIn("задача #1", state.outbox[0].text)
        self.assertIn("/status 1", state.outbox[0].text)

        active.touch()
        bot._enqueue_stalled_task_reminders(
            now=active.last_activity_at + STALLED_TASK_REMINDER_SECONDS
        )
        self.assertEqual(len(state.outbox), 2)

    async def test_two_tasks_can_have_concurrent_turns(self) -> None:
        telegram = FakeTelegram()
        state = FakeState()
        app_server = FakeAppServer()
        bot = make_bot(telegram=telegram, app_server=app_server, state=state)

        second = state.create_task(CHAT_ID, USER_ID, "Второе ревью")
        app_server.responses.update(
            {
                "thread/start": {
                    "thread": {
                        "id": "thread-2",
                        "name": "[Telegram] Второе ревью",
                        "status": {"type": "idle"},
                    }
                },
                "turn/start": {"turn": {"id": "turn-2"}},
            }
        )
        await bot._submit_prompt(CHAT_ID, USER_ID, 501, "Проверь второй PR")

        state.select_task(CHAT_ID, 1)
        app_server.responses.update(
            {
                "thread/resume": {
                    "thread": {
                        "id": THREAD_ID,
                        "name": "[Telegram] Review",
                        "status": {"type": "idle"},
                    }
                },
                "turn/start": {"turn": {"id": TURN_ID}},
            }
        )
        await bot._submit_prompt(CHAT_ID, USER_ID, 502, "Проверь первый PR")

        self.assertEqual(set(bot._active_by_task), {1, second.id})
        self.assertEqual(bot._active_by_task[1].turn_id, TURN_ID)
        self.assertEqual(bot._active_by_task[second.id].turn_id, "turn-2")

    async def test_text_replies_are_routed_to_the_matching_parallel_task(self) -> None:
        telegram = FakeTelegram()
        state = FakeState()
        second = state.create_task(CHAT_ID, USER_ID, "Вторая")
        bot = make_bot(telegram=telegram, state=state)

        first_waiter = asyncio.create_task(
            bot._ask_for_text(
                CHAT_ID, "Вопрос первой задачи", kind="user-input", timeout=10, task_id=1
            )
        )
        second_waiter = asyncio.create_task(
            bot._ask_for_text(
                CHAT_ID,
                "Вопрос второй задачи",
                kind="user-input",
                timeout=10,
                task_id=second.id,
            )
        )
        await asyncio.sleep(0)

        await bot._handle_update(
            {
                "message": {
                    "chat": {"id": CHAT_ID, "type": "private"},
                    "from": {"id": USER_ID},
                    "message_id": 602,
                    "reply_to_message": {"message_id": 2},
                    "text": "Ответ второй",
                }
            }
        )
        await bot._handle_update(
            {
                "message": {
                    "chat": {"id": CHAT_ID, "type": "private"},
                    "from": {"id": USER_ID},
                    "message_id": 601,
                    "reply_to_message": {"message_id": 1},
                    "text": "Ответ первой",
                }
            }
        )

        self.assertEqual(await first_waiter, "Ответ первой")
        self.assertEqual(await second_waiter, "Ответ второй")
        self.assertEqual(
            telegram.sent[0][2], {"force_reply": True, "selective": True}
        )

    async def test_archive_hides_task_and_unarchive_restores_it(self) -> None:
        telegram = FakeTelegram()
        state = FakeState()
        second = state.create_task(CHAT_ID, USER_ID, "Готовое ревью")
        state.complete_task(second.id, "completed", "Готово", formatted=True)
        bot = make_bot(telegram=telegram, state=state)

        await bot._archive_task(CHAT_ID, str(second.number))
        await bot._list_tasks(CHAT_ID)
        await bot._list_tasks(CHAT_ID, "archived")
        await bot._unarchive_task(CHAT_ID, str(second.number))

        self.assertIn("перемещена в архив", telegram.sent[0][1])
        self.assertNotIn("Готовое ревью", telegram.sent[1][1])
        self.assertIn("📦 #2 — Готовое ревью", telegram.sent[2][1])
        self.assertIn("восстановлена", telegram.sent[3][1])

    async def test_running_task_cannot_be_archived(self) -> None:
        telegram = FakeTelegram()
        state = FakeState()
        bot = make_bot(telegram=telegram, state=state)
        bot._register_active(ActiveTurn(CHAT_ID, THREAD_ID, TURN_ID))

        await bot._archive_task(CHAT_ID, "1")

        self.assertIsNone(state.get_task(CHAT_ID, 1).archived_at)
        self.assertIn("ещё выполняется", telegram.sent[-1][1])


if __name__ == "__main__":
    unittest.main()
