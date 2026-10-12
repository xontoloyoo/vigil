"""Full-text search (FTS5) operations for lab notes in vigil."""

from pathlib import Path
import re
from typing import Any

from src.storage.database import get_connection


def sanitize_fts_query(query: str) -> str:
    """Sanitize user input query into safe FTS5 prefix tokens.

    Extracts clean word tokens and joins them with OR so BM25 ranking
    elevates documents matching the greatest number of terms while
    preventing syntax errors from special characters.
    """
    tokens = re.findall(r"\w+", query)
    if not tokens:
        return ""
    # Deduplicate tokens while preserving order
    unique_tokens = list(dict.fromkeys(tokens))
    return " OR ".join(f'"{token}"*' for token in unique_tokens)


async def init_search_index(db_path: str | Path | None = None) -> None:
    """Initialize FTS5 virtual table and synchronization triggers for lab_notes."""
    async with get_connection(db_path) as conn:
        # Create external content virtual table
        await conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS lab_notes_fts USING fts5(
                topic,
                content,
                tags,
                content='lab_notes',
                content_rowid='rowid'
            );
            """
        )

        # Triggers for automated synchronization
        await conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS lab_notes_ai AFTER INSERT ON lab_notes BEGIN
                INSERT INTO lab_notes_fts(rowid, topic, content, tags)
                VALUES (new.rowid, new.topic, new.content, new.tags);
            END;
            """
        )

        await conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS lab_notes_ad AFTER DELETE ON lab_notes BEGIN
                INSERT INTO lab_notes_fts(lab_notes_fts, rowid, topic, content, tags)
                VALUES('delete', old.rowid, old.topic, old.content, old.tags);
            END;
            """
        )

        await conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS lab_notes_au AFTER UPDATE ON lab_notes BEGIN
                INSERT INTO lab_notes_fts(lab_notes_fts, rowid, topic, content, tags)
                VALUES('delete', old.rowid, old.topic, old.content, old.tags);
                INSERT INTO lab_notes_fts(rowid, topic, content, tags)
                VALUES (new.rowid, new.topic, new.content, new.tags);
            END;
            """
        )

        # Rebuild index in case records existed prior to FTS initialization
        await conn.execute("INSERT INTO lab_notes_fts(lab_notes_fts) VALUES('rebuild');")
        await conn.commit()


async def search_lab_notes(
    query: str,
    limit: int = 5,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Search lab notes using SQLite FTS5 BM25 ranking.

    Returns a list of matching notes with their BM25 score (rank).
    """
    safe_query = sanitize_fts_query(query)
    if not safe_query:
        return []

    async with get_connection(db_path) as conn:
        cursor = await conn.execute(
            """
            SELECT
                n.id,
                n.topic,
                n.content,
                n.tags,
                n.created_at,
                bm25(lab_notes_fts) AS rank
            FROM lab_notes_fts
            JOIN lab_notes n ON n.rowid = lab_notes_fts.rowid
            WHERE lab_notes_fts MATCH ?
            ORDER BY rank ASC
            LIMIT ?;
            """,
            (safe_query, limit),
        )
        rows = await cursor.fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["score"] = round(abs(row["rank"]), 4)
            results.append(item)
        return results


async def init_chat_history_search_index(db_path: str | Path | None = None) -> None:
    """Initialize FTS5 virtual table and synchronization triggers for chat_history."""
    async with get_connection(db_path) as conn:
        # Create external content virtual table for chat_history
        await conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS chat_history_fts USING fts5(
                content,
                content='chat_history',
                content_rowid='rowid'
            );
            """
        )

        # Auto-insert trigger
        await conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS chat_history_ai AFTER INSERT ON chat_history BEGIN
                INSERT INTO chat_history_fts(rowid, content)
                VALUES (new.rowid, new.content);
            END;
            """
        )

        # Auto-delete trigger (also fires during pruning)
        await conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS chat_history_ad AFTER DELETE ON chat_history BEGIN
                INSERT INTO chat_history_fts(chat_history_fts, rowid, content)
                VALUES('delete', old.rowid, old.content);
            END;
            """
        )

        # Rebuild index from existing data
        await conn.execute(
            "INSERT INTO chat_history_fts(chat_history_fts) VALUES('rebuild');"
        )
        await conn.commit()


async def search_chat_history(
    query: str,
    limit: int = 5,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Search chat history using FTS5 BM25 ranking.

    Returns a list of matching messages with their BM25 score.
    """
    safe_query = sanitize_fts_query(query)
    if not safe_query:
        return []

    async with get_connection(db_path) as conn:
        cursor = await conn.execute(
            """
            SELECT
                ch.id,
                ch.role,
                ch.content,
                ch.created_at,
                bm25(chat_history_fts) AS rank
            FROM chat_history_fts
            JOIN chat_history ch ON ch.rowid = chat_history_fts.rowid
            WHERE chat_history_fts MATCH ?
            ORDER BY rank ASC
            LIMIT ?;
            """,
            (safe_query, limit),
        )
        rows = await cursor.fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["score"] = round(abs(row["rank"]), 4)
            results.append(item)
        return results
