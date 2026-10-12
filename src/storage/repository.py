"""Data repository functions for vigil.

Provides asynchronous CRUD helpers for tasks, user_dossier, lab_notes, and system_state.
"""

import logging
from pathlib import Path
from typing import Any

from src.storage.database import generate_id, get_connection

logger = logging.getLogger(__name__)

VALID_TASK_STATUSES = frozenset({"pending", "in_progress", "done", "cancelled"})


# ==========================================
# Tasks CRUD
# ==========================================

async def create_task(
    title: str,
    due_time: str | None = None,
    db_path: str | Path | None = None,
) -> str:
    """Create a new task with 'pending' status and return its generated ID."""
    task_id = generate_id("tsk")
    async with get_connection(db_path) as conn:
        await conn.execute(
            """
            INSERT INTO tasks (id, title, due_time, status)
            VALUES (?, ?, ?, 'pending');
            """,
            (task_id, title, due_time),
        )
        await conn.commit()
    return task_id


async def get_pending_tasks(db_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Retrieve all active tasks (pending and in_progress) ordered by priority and due_time ASC."""
    async with get_connection(db_path) as conn:
        cursor = await conn.execute(
            """
            SELECT id, title, due_time, status, created_at
            FROM tasks
            WHERE status IN ('pending', 'in_progress')
            ORDER BY
                CASE WHEN status = 'in_progress' THEN 0 ELSE 1 END,
                CASE WHEN due_time IS NULL THEN 1 ELSE 0 END,
                due_time ASC,
                created_at ASC;
            """
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def update_task_status(
    task_id: str,
    status: str,
    db_path: str | Path | None = None,
) -> bool:
    """Update the status of a task ('pending', 'in_progress', 'done', 'cancelled').

    Returns True if a row was updated, False otherwise.
    """
    normalized_status = status.strip().lower()
    if normalized_status not in VALID_TASK_STATUSES:
        logger.warning(
            "Rejecting invalid task status '%s' for task_id '%s'",
            status,
            task_id,
        )
        return False

    async with get_connection(db_path) as conn:
        cursor = await conn.execute(
            """
            UPDATE tasks
            SET status = ?
            WHERE id = ?;
            """,
            (normalized_status, task_id),
        )
        await conn.commit()
        return cursor.rowcount > 0


async def delete_task(
    task_id: str,
    db_path: str | Path | None = None,
) -> bool:
    """Delete a task permanently by its ID.

    Returns True if a row was deleted, False otherwise.
    """
    async with get_connection(db_path) as conn:
        cursor = await conn.execute(
            """
            DELETE FROM tasks
            WHERE id = ?;
            """,
            (task_id,),
        )
        await conn.commit()
        return cursor.rowcount > 0


# ==========================================
# User Dossier CRUD
# ==========================================

async def add_dossier_entry(
    category: str,
    fact: str,
    db_path: str | Path | None = None,
) -> str:
    """Insert a new user dossier fact or update timestamp if verbatim fact exists.

    Returns the generated or existing entry ID.
    """
    clean_cat = category.strip()
    clean_fact = fact.strip()

    async with get_connection(db_path) as conn:
        # Check if identical (case-insensitive) fact already exists
        cursor = await conn.execute(
            """
            SELECT id FROM user_dossier
            WHERE LOWER(category) = LOWER(?) AND LOWER(fact) = LOWER(?);
            """,
            (clean_cat, clean_fact),
        )
        row = await cursor.fetchone()
        if row is not None:
            existing_id = row["id"]
            await conn.execute(
                """
                UPDATE user_dossier
                SET created_at = CURRENT_TIMESTAMP
                WHERE id = ?;
                """,
                (existing_id,),
            )
            await conn.commit()
            return existing_id

        # Insert new entry
        dossier_id = generate_id("dos")
        await conn.execute(
            """
            INSERT INTO user_dossier (id, category, fact, created_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP);
            """,
            (dossier_id, clean_cat, clean_fact),
        )
        await conn.commit()
        return dossier_id


async def get_recent_dossier(
    limit: int = 10,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Retrieve the most recent user dossier entries."""
    async with get_connection(db_path) as conn:
        cursor = await conn.execute(
            """
            SELECT id, category, fact, created_at
            FROM user_dossier
            ORDER BY created_at DESC, rowid DESC
            LIMIT ?;
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_dossier_by_category(
    category: str = "all",
    limit: int = 10,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Retrieve user dossier entries optionally filtered by category ('habit', 'preference', etc., or 'all')."""
    clean_cat = category.strip().lower()
    async with get_connection(db_path) as conn:
        if clean_cat == "all":
            cursor = await conn.execute(
                """
                SELECT id, category, fact, created_at
                FROM user_dossier
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?;
                """,
                (limit,),
            )
        else:
            cursor = await conn.execute(
                """
                SELECT id, category, fact, created_at
                FROM user_dossier
                WHERE LOWER(category) = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?;
                """,
                (clean_cat, limit),
            )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def delete_dossier_entry(
    dossier_id: str,
    db_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Delete a user dossier entry by ID and return its deleted data (for USER.md sync).

    Returns a dict with entry info if deleted, None otherwise.
    """
    async with get_connection(db_path) as conn:
        cursor = await conn.execute(
            """
            SELECT id, category, fact FROM user_dossier WHERE id = ?;
            """,
            (dossier_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        data = dict(row)
        await conn.execute(
            """
            DELETE FROM user_dossier WHERE id = ?;
            """,
            (dossier_id,),
        )
        await conn.commit()
        return data


# ==========================================
# Lab Notes CRUD
# ==========================================

async def add_lab_note(
    topic: str,
    content: str,
    tags: str = "",
    db_path: str | Path | None = None,
) -> str:
    """Insert a new lab note and return its generated ID."""
    note_id = generate_id("not")
    async with get_connection(db_path) as conn:
        await conn.execute(
            """
            INSERT INTO lab_notes (id, topic, content, tags)
            VALUES (?, ?, ?, ?);
            """,
            (note_id, topic, content, tags),
        )
        await conn.commit()
    return note_id


async def get_recent_lab_notes(
    limit: int = 5,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Retrieve the most recent lab notes."""
    async with get_connection(db_path) as conn:
        cursor = await conn.execute(
            """
            SELECT id, topic, content, tags, created_at
            FROM lab_notes
            ORDER BY created_at DESC, rowid DESC
            LIMIT ?;
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def delete_lab_note(
    note_id: str,
    db_path: str | Path | None = None,
) -> bool:
    """Delete a lab note permanently by its ID.

    Returns True if a row was deleted, False otherwise.
    """
    async with get_connection(db_path) as conn:
        cursor = await conn.execute(
            """
            DELETE FROM lab_notes
            WHERE id = ?;
            """,
            (note_id,),
        )
        await conn.commit()
        return cursor.rowcount > 0


# ==========================================
# System State CRUD
# ==========================================

async def set_state(
    key: str,
    value: str,
    db_path: str | Path | None = None,
) -> None:
    """Set or update a system state key-value pair using UPSERT."""
    async with get_connection(db_path) as conn:
        await conn.execute(
            """
            INSERT INTO system_state (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP;
            """,
            (key, value),
        )
        await conn.commit()


async def get_state(
    key: str,
    default: str | None = None,
    db_path: str | Path | None = None,
) -> str | None:
    """Retrieve a system state value by key, returning default if not found."""
    async with get_connection(db_path) as conn:
        cursor = await conn.execute(
            """
            SELECT value
            FROM system_state
            WHERE key = ?;
            """,
            (key,),
        )
        row = await cursor.fetchone()
        if row is not None:
            return row["value"]
        return default


# ==========================================
# Chat History CRUD
# ==========================================

async def save_chat_message(
    role: str,
    content: str,
    db_path: str | Path | None = None,
) -> None:
    """Save a single chat message (user or assistant) to chat_history."""
    msg_id = generate_id("msg")
    async with get_connection(db_path) as conn:
        await conn.execute(
            """
            INSERT INTO chat_history (id, role, content)
            VALUES (?, ?, ?);
            """,
            (msg_id, role, content),
        )
        await conn.commit()


async def get_recent_chat_history(
    limit: int = 8,
    db_path: str | Path | None = None,
) -> list[dict[str, str]]:
    """Retrieve the most recent chat messages in chronological order (ASC).

    Returns a list of {"id": ..., "role": ..., "content": ...} dicts.
    The 'id' field enables deduplication against FTS5 search results;
    callers should strip it before injecting into an LLM messages array.
    """
    async with get_connection(db_path) as conn:
        cursor = await conn.execute(
            """
            SELECT id, role, content
            FROM (
                SELECT id, role, content, created_at, rowid
                FROM chat_history
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
            )
            ORDER BY created_at ASC, rowid ASC;
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        return [
            {"id": row["id"], "role": row["role"], "content": row["content"]}
            for row in rows
        ]


# ==========================================
# Chat History Maintenance
# ==========================================

async def prune_chat_history(
    keep_latest: int = 500,
    db_path: str | Path | None = None,
) -> int:
    """Delete old chat_history rows, keeping only the most recent `keep_latest` entries.

    Returns the number of deleted rows.
    """
    async with get_connection(db_path) as conn:
        cursor = await conn.execute(
            """
            DELETE FROM chat_history
            WHERE id NOT IN (
                SELECT id FROM chat_history
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
            );
            """,
            (keep_latest,),
        )
        await conn.commit()
        return cursor.rowcount


# ==========================================
# Extended Query Helpers (Read Tools)
# ==========================================

async def get_tasks_by_status(
    status: str = "all",
    limit: int = 10,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Retrieve tasks filtered by status ('pending', 'in_progress', 'done', 'cancelled', or 'all')."""
    async with get_connection(db_path) as conn:
        if status == "all":
            cursor = await conn.execute(
                """
                SELECT id, title, due_time, status, created_at
                FROM tasks
                ORDER BY created_at DESC
                LIMIT ?;
                """,
                (limit,),
            )
        else:
            cursor = await conn.execute(
                """
                SELECT id, title, due_time, status, created_at
                FROM tasks
                WHERE status = ?
                ORDER BY created_at DESC
                LIMIT ?;
                """,
                (status, limit),
            )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
