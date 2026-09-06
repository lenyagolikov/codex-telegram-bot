from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    id: int
    chat_id: int
    text: str
    formatted: bool
    attempts: int


@dataclass(frozen=True, slots=True)
class TaskRecord:
    id: int
    chat_id: int
    user_id: int
    number: int
    thread_id: str | None
    title: str | None
    status: str
    current_turn_id: str | None
    result_text: str | None
    result_formatted: bool
    latest_diff: str | None
    created_at: str
    updated_at: str
    completed_at: str | None
    archived_at: str | None


class StateStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                thread_id TEXT NOT NULL UNIQUE,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                formatted INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_attempt_at TEXT
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
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
                archived_at TEXT,
                UNIQUE(chat_id, task_number)
            )
            """
        )
        self._ensure_task_columns()
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS task_selections (
                chat_id INTEGER PRIMARY KEY,
                task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE
            )
            """
        )
        self._migrate_legacy_chats()
        self._connection.commit()

    def _ensure_task_columns(self) -> None:
        columns = {
            str(row[1])
            for row in self._connection.execute("PRAGMA table_info(tasks)").fetchall()
        }
        if "archived_at" not in columns:
            self._connection.execute("ALTER TABLE tasks ADD COLUMN archived_at TEXT")

    def _migrate_legacy_chats(self) -> None:
        rows = self._connection.execute(
            "SELECT chat_id, user_id, thread_id FROM chats"
        ).fetchall()
        for chat_id, user_id, thread_id in rows:
            existing = self._connection.execute(
                "SELECT id FROM tasks WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            if existing is None:
                next_number_row = self._connection.execute(
                    """
                    SELECT COALESCE(MAX(task_number), 0) + 1
                    FROM tasks
                    WHERE chat_id = ?
                    """,
                    (chat_id,),
                ).fetchone()
                next_number = int(next_number_row[0])
                cursor = self._connection.execute(
                    """
                    INSERT INTO tasks (
                        chat_id, user_id, task_number, thread_id, title, status
                    )
                    VALUES (?, ?, ?, ?, 'Существующий диалог', 'idle')
                    """,
                    (chat_id, user_id, next_number, thread_id),
                )
                task_id = int(cursor.lastrowid)
            else:
                task_id = int(existing[0])
            self._connection.execute(
                """
                INSERT INTO task_selections (chat_id, task_id) VALUES (?, ?)
                ON CONFLICT(chat_id) DO NOTHING
                """,
                (chat_id, task_id),
            )

    def close(self) -> None:
        self._connection.close()

    def create_task(
        self, chat_id: int, user_id: int, title: str | None = None
    ) -> TaskRecord:
        row = self._connection.execute(
            "SELECT COALESCE(MAX(task_number), 0) + 1 FROM tasks WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        number = int(row[0])
        cursor = self._connection.execute(
            """
            INSERT INTO tasks (chat_id, user_id, task_number, title)
            VALUES (?, ?, ?, ?)
            """,
            (chat_id, user_id, number, title),
        )
        task_id = int(cursor.lastrowid)
        self._connection.execute(
            """
            INSERT INTO task_selections (chat_id, task_id) VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET task_id = excluded.task_id
            """,
            (chat_id, task_id),
        )
        self._connection.commit()
        task = self.get_task_by_id(task_id)
        assert task is not None
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
        row = self._connection.execute(
            "SELECT COALESCE(MAX(task_number), 0) + 1 FROM tasks WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        number = int(row[0])
        cursor = self._connection.execute(
            """
            INSERT INTO tasks (
                chat_id, user_id, task_number, thread_id, title, status,
                result_text, result_formatted,
                completed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?,
                    CASE WHEN ? IN ('completed', 'failed', 'interrupted')
                         THEN CURRENT_TIMESTAMP ELSE NULL END)
            """,
            (
                chat_id,
                user_id,
                number,
                thread_id,
                title,
                status,
                result_text,
                int(result_formatted),
                status,
            ),
        )
        task_id = int(cursor.lastrowid)
        self._connection.execute(
            """
            INSERT INTO task_selections (chat_id, task_id) VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET task_id = excluded.task_id
            """,
            (chat_id, task_id),
        )
        self._connection.commit()
        task = self.get_task_by_id(task_id)
        assert task is not None
        return task

    def list_tasks(
        self, chat_id: int, *, archived: bool | None = False
    ) -> list[TaskRecord]:
        archive_filter = ""
        if archived is True:
            archive_filter = " AND archived_at IS NOT NULL"
        elif archived is False:
            archive_filter = " AND archived_at IS NULL"
        rows = self._connection.execute(
            f"SELECT * FROM tasks WHERE chat_id = ?{archive_filter} ORDER BY task_number",
            (chat_id,),
        ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def get_task(self, chat_id: int, number: int) -> TaskRecord | None:
        row = self._connection.execute(
            "SELECT * FROM tasks WHERE chat_id = ? AND task_number = ?",
            (chat_id, number),
        ).fetchone()
        return self._task_from_row(row) if row else None

    def get_task_by_id(self, task_id: int) -> TaskRecord | None:
        row = self._connection.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return self._task_from_row(row) if row else None

    def get_task_by_thread_id(self, thread_id: str) -> TaskRecord | None:
        row = self._connection.execute(
            "SELECT * FROM tasks WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return self._task_from_row(row) if row else None

    def get_selected_task(self, chat_id: int) -> TaskRecord | None:
        row = self._connection.execute(
            """
            SELECT tasks.*
            FROM task_selections
            JOIN tasks ON tasks.id = task_selections.task_id
            WHERE task_selections.chat_id = ? AND tasks.archived_at IS NULL
            """,
            (chat_id,),
        ).fetchone()
        return self._task_from_row(row) if row else None

    def select_task(self, chat_id: int, number: int) -> TaskRecord | None:
        task = self.get_task(chat_id, number)
        if task is None or task.archived_at is not None:
            return None
        self._connection.execute(
            """
            INSERT INTO task_selections (chat_id, task_id) VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET task_id = excluded.task_id
            """,
            (chat_id, task.id),
        )
        self._connection.commit()
        return task

    def archive_task(self, chat_id: int, number: int) -> TaskRecord | None:
        task = self.get_task(chat_id, number)
        if task is None:
            return None
        self._connection.execute(
            """
            UPDATE tasks
            SET archived_at = COALESCE(archived_at, CURRENT_TIMESTAMP),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (task.id,),
        )
        selected = self._connection.execute(
            "SELECT task_id FROM task_selections WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if selected is not None and int(selected[0]) == task.id:
            replacement = self._connection.execute(
                """
                SELECT id FROM tasks
                WHERE chat_id = ? AND archived_at IS NULL
                ORDER BY task_number DESC LIMIT 1
                """,
                (chat_id,),
            ).fetchone()
            if replacement is None:
                self._connection.execute(
                    "DELETE FROM task_selections WHERE chat_id = ?", (chat_id,)
                )
            else:
                self._connection.execute(
                    "UPDATE task_selections SET task_id = ? WHERE chat_id = ?",
                    (int(replacement[0]), chat_id),
                )
        self._connection.commit()
        return self.get_task_by_id(task.id)

    def unarchive_task(self, chat_id: int, number: int) -> TaskRecord | None:
        task = self.get_task(chat_id, number)
        if task is None:
            return None
        self._connection.execute(
            """
            UPDATE tasks
            SET archived_at = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (task.id,),
        )
        self._connection.commit()
        return self.get_task_by_id(task.id)

    def attach_thread(self, task_id: int, thread_id: str) -> None:
        self._connection.execute(
            """
            UPDATE tasks SET thread_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?
            """,
            (thread_id, task_id),
        )
        self._connection.commit()

    def set_task_title(self, task_id: int, title: str) -> None:
        self._connection.execute(
            "UPDATE tasks SET title = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (title, task_id),
        )
        self._connection.commit()

    def set_task_running(self, task_id: int, turn_id: str) -> None:
        self._connection.execute(
            """
            UPDATE tasks
            SET status = 'running', current_turn_id = ?, result_text = NULL,
                completed_at = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (turn_id, task_id),
        )
        self._connection.commit()

    def complete_task(
        self,
        task_id: int,
        status: str,
        result_text: str,
        *,
        formatted: bool,
        latest_diff: str | None = None,
    ) -> None:
        self._connection.execute(
            """
            UPDATE tasks
            SET status = ?, current_turn_id = NULL, result_text = ?,
                result_formatted = ?, latest_diff = COALESCE(?, latest_diff),
                completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, result_text, int(formatted), latest_diff, task_id),
        )
        self._connection.commit()

    def set_task_diff(self, task_id: int, diff: str) -> None:
        self._connection.execute(
            "UPDATE tasks SET latest_diff = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (diff, task_id),
        )
        self._connection.commit()

    @staticmethod
    def _task_from_row(row: sqlite3.Row | tuple[object, ...]) -> TaskRecord:
        return TaskRecord(
            id=int(row[0]),
            chat_id=int(row[1]),
            user_id=int(row[2]),
            number=int(row[3]),
            thread_id=str(row[4]) if row[4] is not None else None,
            title=str(row[5]) if row[5] is not None else None,
            status=str(row[6]),
            current_turn_id=str(row[7]) if row[7] is not None else None,
            result_text=str(row[8]) if row[8] is not None else None,
            result_formatted=bool(row[9]),
            latest_diff=str(row[10]) if row[10] is not None else None,
            created_at=str(row[11]),
            updated_at=str(row[12]),
            completed_at=str(row[13]) if row[13] is not None else None,
            archived_at=str(row[14]) if row[14] is not None else None,
        )

    def get_thread_id(self, chat_id: int) -> str | None:
        task = self.get_selected_task(chat_id)
        return task.thread_id if task else None

    def get_chat_id(self, thread_id: str) -> int | None:
        task = self.get_task_by_thread_id(thread_id)
        return task.chat_id if task else None

    def set_thread_id(self, chat_id: int, user_id: int, thread_id: str) -> None:
        task = self.get_selected_task(chat_id) or self.create_task(chat_id, user_id)
        self.attach_thread(task.id, thread_id)

    def clear_thread(self, chat_id: int) -> None:
        task = self.get_selected_task(chat_id)
        if task is None:
            return
        self._connection.execute(
            """
            UPDATE tasks
            SET thread_id = NULL, status = 'new', current_turn_id = NULL,
                result_text = NULL, latest_diff = NULL, completed_at = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (task.id,),
        )
        self._connection.commit()

    def get_update_offset(self) -> int | None:
        row = self._connection.execute(
            "SELECT value FROM metadata WHERE key = 'telegram_update_offset'"
        ).fetchone()
        return int(row[0]) if row else None

    def set_update_offset(self, offset: int) -> None:
        self._connection.execute(
            """
            INSERT INTO metadata (key, value) VALUES ('telegram_update_offset', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(offset),),
        )
        self._connection.commit()

    def enqueue_outbox(self, chat_id: int, text: str, *, formatted: bool) -> int:
        cursor = self._connection.execute(
            "INSERT INTO outbox (chat_id, text, formatted) VALUES (?, ?, ?)",
            (chat_id, text, int(formatted)),
        )
        self._connection.commit()
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)

    def get_outbox(self, limit: int = 20) -> list[OutboxMessage]:
        rows = self._connection.execute(
            """
            SELECT id, chat_id, text, formatted, attempts
            FROM outbox
            ORDER BY id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            OutboxMessage(
                id=int(row[0]),
                chat_id=int(row[1]),
                text=str(row[2]),
                formatted=bool(row[3]),
                attempts=int(row[4]),
            )
            for row in rows
        ]

    def mark_outbox_attempt(self, message_id: int) -> None:
        self._connection.execute(
            """
            UPDATE outbox
            SET attempts = attempts + 1, last_attempt_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (message_id,),
        )
        self._connection.commit()

    def delete_outbox(self, message_id: int) -> None:
        self._connection.execute("DELETE FROM outbox WHERE id = ?", (message_id,))
        self._connection.commit()
