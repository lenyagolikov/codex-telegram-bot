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
