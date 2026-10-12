"""Asynchronous SQLite database operations for vigil."""

from contextlib import asynccontextmanager
import os
from pathlib import Path
import time
from typing import AsyncGenerator
import uuid
import aiosqlite

from src.config import config


def generate_id(prefix: str = "id") -> str:
    """Generate a unique prefixed identifier using timestamp and random hex.

    Example outputs: 'tsk_1774501234_ab12cd34', 'dos_1774501234_ef56gh78'
    """
    clean_prefix = prefix.rstrip("_")
    timestamp = int(time.time())
    random_suffix = uuid.uuid4().hex[:8]
    return f"{clean_prefix}_{timestamp}_{random_suffix}"


def _resolve_db_path(db_path: str | Path | None = None) -> Path:
    """Resolve and ensure the parent directory of the database exists."""
    target_path = Path(db_path or config.DATABASE_PATH)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    return target_path


@asynccontextmanager
async def get_connection(
    db_path: str | Path | None = None,
) -> AsyncGenerator[aiosqlite.Connection, None]:
    """Async context manager providing an aiosqlite database connection.

    NOTE: Currently opens a new connection per call (~6-7 per user request).
    This is acceptable for single-user load where LLM latency dominates.

    TODO: Consider a persistent singleton connection when user count grows.
    Key considerations:
      - PRAGMA foreign_keys must still be set per-connection (SQLite requirement)
      - WAL mode is already persistent (set once in init_db)
      - Singleton needs reconnect strategy for sqlite3.OperationalError
      - Must not conflict with concurrent writes from heartbeat background task
    """
    resolved_path = _resolve_db_path(db_path)
    async with aiosqlite.connect(str(resolved_path)) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON;")
        yield conn


async def init_db(db_path: str | Path | None = None) -> None:
    """Initialize database tables and set recommended SQLite PRAGMAs."""
    resolved_path = _resolve_db_path(db_path)
    async with aiosqlite.connect(str(resolved_path)) as conn:
        # Apply performance and integrity PRAGMAs
        await conn.execute("PRAGMA journal_mode = WAL;")
        await conn.execute("PRAGMA synchronous = NORMAL;")
        await conn.execute("PRAGMA foreign_keys = ON;")

        # Create tasks table
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                due_time TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        # Create user_dossier table
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_dossier (
                id TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                fact TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        # Create lab_notes table
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS lab_notes (
                id TEXT PRIMARY KEY,
                topic TEXT NOT NULL,
                content TEXT NOT NULL,
                tags TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        # Create system_state table
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS system_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        # Create chat_history table for short-term conversational memory
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_history (
                id TEXT PRIMARY KEY,
                role TEXT CHECK(role IN ('user', 'assistant')) NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        await conn.commit()
