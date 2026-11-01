"""Full-text search (FTS5) operations for lab notes in vigil."""

from pathlib import Path
import re
from typing import Any

from src.storage.database import get_connection
from src.core.utils import to_local_timestamp_str


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
    exclude_ids: list[str] | None = None,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Search lab notes using SQLite FTS5 BM25 ranking.

    Returns a list of matching notes with their BM25 score (rank).
    """
    safe_query = sanitize_fts_query(query)
    if not safe_query:
        return []

    exclude_set = set(exclude_ids or ())
    fetch_limit = limit + len(exclude_set) * 2

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
            (safe_query, fetch_limit),
        )
        rows = await cursor.fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            if row["id"] in exclude_set:
                continue
            item = dict(row)
            item["created_at"] = to_local_timestamp_str(item.get("created_at"))
            item["_tz_converted"] = True
            item["score"] = round(abs(row["rank"]), 4)
            results.append(item)
            if len(results) >= limit:
                break
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


async def _hydrate_and_pair(
    conn: Any,
    raw_rows: list[Any],
    exclude_set: set[str],
    limit: int,
) -> list[dict[str, Any]]:
    """Filter exclude_set, attach paired turn messages, deduplicate pairs, and enforce limit."""
    if not raw_rows:
        return []

    # Filter out rows whose ID is already in exclude_set
    candidate_rows = [r for r in raw_rows if r["id"] not in exclude_set]
    if not candidate_rows:
        return []

    # Determine surrounding rowids needed for turn pairing:
    # - user row -> rowid + 1
    # - assistant row -> rowid - 1
    needed_rowids: set[int] = set()
    for r in candidate_rows:
        if r["role"] == "user":
            needed_rowids.add(r["rowid"] + 1)
        elif r["role"] == "assistant":
            needed_rowids.add(r["rowid"] - 1)

    surrounding_by_rowid: dict[int, dict[str, Any]] = {}
    if needed_rowids:
        placeholders = ",".join("?" for _ in needed_rowids)
        cur = await conn.execute(
            f"SELECT rowid, id, role, content, created_at FROM chat_history WHERE rowid IN ({placeholders});",
            tuple(needed_rowids),
        )
        s_rows = await cur.fetchall()
        for sr in s_rows:
            surrounding_by_rowid[sr["rowid"]] = dict(sr)

    # Build turn pairs and deduplicate
    results: list[dict[str, Any]] = []
    seen_pair_keys: set[tuple[int, ...]] = set()

    for r in candidate_rows:
        paired_row: dict[str, Any] | None = None
        if r["role"] == "user":
            next_row = surrounding_by_rowid.get(r["rowid"] + 1)
            if next_row and next_row["role"] == "assistant":
                if next_row["id"] not in exclude_set:
                    paired_row = next_row
        elif r["role"] == "assistant":
            prev_row = surrounding_by_rowid.get(r["rowid"] - 1)
            if prev_row and prev_row["role"] == "user":
                if prev_row["id"] not in exclude_set:
                    paired_row = prev_row

        if paired_row is not None:
            pair_key = (min(r["rowid"], paired_row["rowid"]), max(r["rowid"], paired_row["rowid"]))
        else:
            pair_key = (r["rowid"],)

        if pair_key in seen_pair_keys:
            continue
        seen_pair_keys.add(pair_key)

        has_rank = "rank" in r.keys()
        score = round(abs(r["rank"]), 4) if has_rank else 0.0

        r_dict = dict(r)
        r_created = to_local_timestamp_str(r_dict.get("created_at"))
        paired_created = to_local_timestamp_str(paired_row.get("created_at")) if paired_row else None

        item: dict[str, Any] = {
            "id": r_dict["id"],
            "role": r_dict["role"],
            "content": r_dict["content"],
            "created_at": r_created,
            "_tz_converted": True,
            "score": score,
            "paired_message": {
                "id": paired_row["id"],
                "role": paired_row["role"],
                "content": paired_row["content"],
                "created_at": paired_created,
                "_tz_converted": True,
            } if paired_row else None,
        }
        results.append(item)
        if len(results) >= limit:
            break

    return results


async def search_chat_history(
    query: str = "",
    limit: int = 5,
    date_str: str | None = None,
    days_ago: int | None = None,
    exclude_ids: set[str] | list[str] | None = None,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Search chat history using FTS5 BM25 ranking or temporal filtering with Turn-Pairing.

    Supports 3 calling shapes:
    1. Shape 1 (Keyword Only): query provided, no date_str/days_ago -> FTS5 MATCH + turn pairing
    2. Shape 2 (Temporal + Keyword): query and (date_str or days_ago) -> FTS5 MATCH filtered by UTC date range + turn pairing
    3. Shape 3 (Temporal / Browse Only): query empty/whitespace -> date range filter or recent rows before exclude_ids + turn pairing
    """
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    from src.config import config

    safe_query = sanitize_fts_query(query) if query else ""
    exclude_set = set(exclude_ids or ())

    # Calculate temporal UTC range if days_ago or date_str is provided
    tz = ZoneInfo(config.TIMEZONE)
    now_local = datetime.now(tz)
    target_date = None
    if days_ago is not None:
        target_date = (now_local - timedelta(days=days_ago)).date()
    elif date_str:
        try:
            target_date = datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
        except ValueError:
            target_date = None

    time_filter = None
    if target_date is not None:
        start_of_day_local = datetime(
            target_date.year, target_date.month, target_date.day, 0, 0, 0, tzinfo=tz
        )
        end_of_day_local = start_of_day_local + timedelta(days=1)
        start_utc = start_of_day_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        end_utc = end_of_day_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        time_filter = (start_utc, end_utc)

    # If neither query nor time filter is provided and exclude_set is empty,
    # or query is empty and time filter is not given, we fetch the latest rows before exclude_set
    fetch_limit = max(limit * 3 + len(exclude_set), 15)

    async with get_connection(db_path) as conn:
        if safe_query:
            if time_filter:
                # Shape 2: Temporal + Keyword
                cursor = await conn.execute(
                    """
                    SELECT
                        ch.rowid,
                        ch.id,
                        ch.role,
                        ch.content,
                        ch.created_at,
                        bm25(chat_history_fts) AS rank
                    FROM chat_history_fts
                    JOIN chat_history ch ON ch.rowid = chat_history_fts.rowid
                    WHERE chat_history_fts MATCH ?
                      AND (replace(ch.created_at, 'T', ' ') >= ? AND replace(ch.created_at, 'T', ' ') < ?)
                    ORDER BY rank ASC
                    LIMIT ?;
                    """,
                    (safe_query, time_filter[0], time_filter[1], fetch_limit),
                )
                raw_rows = await cursor.fetchall()
                results = await _hydrate_and_pair(conn, raw_rows, exclude_set, limit)
                if not results:
                    # Auto-Fallback Shape 2 -> Shape 1 (Keyword Only, without time_filter)
                    cursor = await conn.execute(
                        """
                        SELECT
                            ch.rowid,
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
                        (safe_query, fetch_limit),
                    )
                    raw_rows = await cursor.fetchall()
                    results = await _hydrate_and_pair(conn, raw_rows, exclude_set, limit)
                return results
            else:
                # Shape 1: Keyword Only
                cursor = await conn.execute(
                    """
                    SELECT
                        ch.rowid,
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
                    (safe_query, fetch_limit),
                )
                raw_rows = await cursor.fetchall()
                return await _hydrate_and_pair(conn, raw_rows, exclude_set, limit)
        else:
            # Shape 3: Browse Only (no keyword)
            if time_filter:
                cursor = await conn.execute(
                    """
                    SELECT
                        ch.rowid,
                        ch.id,
                        ch.role,
                        ch.content,
                        ch.created_at
                    FROM chat_history ch
                    WHERE (replace(ch.created_at, 'T', ' ') >= ? AND replace(ch.created_at, 'T', ' ') < ?)
                    ORDER BY ch.created_at ASC, ch.rowid ASC
                    LIMIT ?;
                    """,
                    (time_filter[0], time_filter[1], fetch_limit),
                )
                raw_rows = await cursor.fetchall()
                results = await _hydrate_and_pair(conn, raw_rows, exclude_set, limit)
                if not results:
                    # Auto-Fallback: fetch recent messages before start of today (local TIMEZONE in UTC)
                    tz = ZoneInfo(config.TIMEZONE)
                    now_local = datetime.now(tz)
                    start_of_today_local = datetime(
                        now_local.year, now_local.month, now_local.day, 0, 0, 0, tzinfo=tz
                    )
                    start_of_today_utc = start_of_today_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    cursor = await conn.execute(
                        """
                        SELECT
                            ch.rowid,
                            ch.id,
                            ch.role,
                            ch.content,
                            ch.created_at
                        FROM chat_history ch
                        WHERE replace(ch.created_at, 'T', ' ') < ?
                        ORDER BY ch.created_at DESC, ch.rowid DESC
                        LIMIT ?;
                        """,
                        (start_of_today_utc, fetch_limit),
                    )
                    raw_rows = await cursor.fetchall()
                    results = await _hydrate_and_pair(conn, raw_rows, exclude_set, limit)
                return results
            else:
                cursor = await conn.execute(
                    """
                    SELECT
                        ch.rowid,
                        ch.id,
                        ch.role,
                        ch.content,
                        ch.created_at
                    FROM chat_history ch
                    ORDER BY ch.created_at DESC, ch.rowid DESC
                    LIMIT ?;
                    """,
                    (fetch_limit,),
                )
                raw_rows = await cursor.fetchall()
                return await _hydrate_and_pair(conn, raw_rows, exclude_set, limit)
