from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.state import StateStore


class StateStoreOutboxTests(unittest.TestCase):
    def test_outbox_survives_store_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            state = StateStore(path)
            message_id = state.enqueue_outbox(101, "**Готово**", formatted=True)
            state.close()

            reopened = StateStore(path)
            messages = reopened.get_outbox()
            reopened.close()

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].id, message_id)
        self.assertEqual(messages[0].chat_id, 101)
        self.assertEqual(messages[0].text, "**Готово**")
        self.assertTrue(messages[0].formatted)
        self.assertEqual(messages[0].attempts, 0)

    def test_attempt_and_delete_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite3")
            message_id = state.enqueue_outbox(101, "Ответ", formatted=False)

            state.mark_outbox_attempt(message_id)
            self.assertEqual(state.get_outbox()[0].attempts, 1)

            state.delete_outbox(message_id)
            self.assertEqual(state.get_outbox(), [])
            state.close()


class StateStoreTaskTests(unittest.TestCase):
    def test_tasks_are_numbered_selected_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            state = StateStore(path)
            first = state.create_task(101, 202, "Первое ревью")
            state.attach_thread(first.id, "thread-1")
            state.set_task_running(first.id, "turn-1")
            second = state.create_task(101, 202, "Второе ревью")

            self.assertEqual((first.number, second.number), (1, 2))
            self.assertEqual(state.get_selected_task(101), second)

            selected = state.select_task(101, 1)
            self.assertIsNotNone(selected)
            assert selected is not None
            self.assertEqual(selected.thread_id, "thread-1")
            state.complete_task(
                first.id,
                "completed",
                "Готово",
                formatted=True,
                latest_diff="diff --git",
            )
            state.close()

            reopened = StateStore(path)
            tasks = reopened.list_tasks(101)
            selected = reopened.get_selected_task(101)
            reopened.close()

        self.assertEqual(len(tasks), 2)
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.number, 1)
        self.assertEqual(tasks[0].status, "completed")
        self.assertEqual(tasks[0].result_text, "Готово")
        self.assertEqual(tasks[0].latest_diff, "diff --git")

    def test_archiving_hides_task_and_selects_another_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite3")
            first = state.create_task(101, 202, "Первая")
            second = state.create_task(101, 202, "Вторая")

            archived = state.archive_task(101, second.number)

            self.assertIsNotNone(archived)
            assert archived is not None
            self.assertIsNotNone(archived.archived_at)
            self.assertEqual(state.list_tasks(101), [first])
            self.assertEqual(state.list_tasks(101, archived=True), [archived])
            self.assertEqual(state.get_selected_task(101), first)

            restored = state.unarchive_task(101, second.number)
            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertIsNone(restored.archived_at)
            self.assertEqual(len(state.list_tasks(101)), 2)
            state.close()

    def test_attached_thread_is_persisted_and_selected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            state = StateStore(path)
            state.create_task(101, 202, "Telegram task")

            attached = state.create_attached_task(
                101,
                202,
                "desktop-thread",
                "Desktop task",
                status="completed",
                result_text="Готово",
                result_formatted=True,
            )
            state.close()

            reopened = StateStore(path)
            persisted = reopened.get_task_by_thread_id("desktop-thread")
            selected = reopened.get_selected_task(101)
            reopened.close()

        self.assertEqual(persisted, attached)
        self.assertEqual(selected, attached)
        self.assertEqual(attached.number, 2)
        self.assertEqual(attached.result_text, "Готово")
        self.assertIsNotNone(attached.completed_at)

    def test_existing_tasks_schema_gets_archived_column(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    task_number INTEGER NOT NULL,
                    thread_id TEXT UNIQUE,
                    title TEXT,
                    status TEXT NOT NULL DEFAULT 'new',
                    current_turn_id TEXT,
                    result_text TEXT,
                    result_formatted INTEGER NOT NULL DEFAULT 1,
                    latest_diff TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at TEXT,
                    UNIQUE(chat_id, task_number)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO tasks (chat_id, user_id, task_number, title)
                VALUES (101, 202, 1, 'Старая задача')
                """
            )
            connection.commit()
            connection.close()

            state = StateStore(path)
            task = state.get_task(101, 1)
            state.close()

        self.assertIsNotNone(task)
        assert task is not None
        self.assertIsNone(task.archived_at)

    def test_legacy_chat_is_migrated_to_first_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE chats (
                    chat_id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    thread_id TEXT NOT NULL UNIQUE,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                "INSERT INTO chats (chat_id, user_id, thread_id) VALUES (101, 202, 'old')"
            )
            connection.commit()
            connection.close()

            state = StateStore(path)
            task = state.get_selected_task(101)
            state.close()

        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.number, 1)
        self.assertEqual(task.thread_id, "old")


if __name__ == "__main__":
    unittest.main()
