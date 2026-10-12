import pytest
import aiosqlite
from pathlib import Path

from src.storage import repository

pytestmark = pytest.mark.asyncio


async def test_chat_history_order_with_identical_timestamps(mock_db_path):
    """
    Test that messages with identical created_at timestamps maintain strict
    insertion order (user -> assistant) using SQLite rowid as tie-breaker,
    even when the assistant's id is lexicographically smaller than the user's id.
    """
    async with aiosqlite.connect(mock_db_path) as db:
        # Clear pre-seeded dummy messages to isolate test data
        await db.execute("DELETE FROM chat_history;")
        await db.commit()

    # Simulate turn 1 and turn 2 with explicit identical timestamps and conflicting IDs
    # User ID starts with 'z' (lexicographically large), Assistant ID starts with 'a' (small)
    sample_turns = [
        # Turn 1: 08:20:22
        {"id": "msg_100_zzzz", "role": "user", "content": "Halo apa kabar?", "created_at": "2026-10-03 08:20:22"},
        {"id": "msg_100_aaaa", "role": "assistant", "content": "Halo! Saya baik.", "created_at": "2026-10-03 08:20:22"},
        # Turn 2: 08:23:20
        {"id": "msg_200_yyyy", "role": "user", "content": "Tolong ingatkan besok.", "created_at": "2026-10-03 08:23:20"},
        {"id": "msg_200_bbbb", "role": "assistant", "content": "Siap, sudah dicatat.", "created_at": "2026-10-03 08:23:20"},
    ]

    async with aiosqlite.connect(mock_db_path) as db:
        for item in sample_turns:
            await db.execute(
                """
                INSERT INTO chat_history (id, role, content, created_at)
                VALUES (?, ?, ?, ?);
                """,
                (item["id"], item["role"], item["content"], item["created_at"]),
            )
        await db.commit()

    # Retrieve history
    history = await repository.get_recent_chat_history(limit=4, db_path=mock_db_path)

    assert len(history) == 4
    roles = [m["role"] for m in history]
    contents = [m["content"] for m in history]

    # Verify strict alternating order: user -> assistant -> user -> assistant
    assert roles == ["user", "assistant", "user", "assistant"], f"Roles out of order: {roles}"
    assert contents == [
        "Halo apa kabar?",
        "Halo! Saya baik.",
        "Tolong ingatkan besok.",
        "Siap, sudah dicatat.",
    ]


async def test_save_chat_message_rapid_fire_order(mock_db_path):
    """
    Test rapid sequential calls to save_chat_message within the same second,
    verifying that get_recent_chat_history returns them in exact insertion order.
    """
    async with aiosqlite.connect(mock_db_path) as db:
        await db.execute("DELETE FROM chat_history;")
        await db.commit()

    # Insert 6 messages alternating user/assistant rapidly
    messages_to_insert = [
        ("user", "Pesan 1"),
        ("assistant", "Balasan 1"),
        ("user", "Pesan 2"),
        ("assistant", "Balasan 2"),
        ("user", "Pesan 3"),
        ("assistant", "Balasan 3"),
    ]

    for role, content in messages_to_insert:
        await repository.save_chat_message(role=role, content=content, db_path=mock_db_path)

    # Retrieve all 6
    history = await repository.get_recent_chat_history(limit=6, db_path=mock_db_path)

    assert len(history) == 6
    roles = [m["role"] for m in history]
    contents = [m["content"] for m in history]

    assert roles == ["user", "assistant", "user", "assistant", "user", "assistant"]
    assert contents == [
        "Pesan 1",
        "Balasan 1",
        "Pesan 2",
        "Balasan 2",
        "Pesan 3",
        "Balasan 3",
    ]


async def test_prune_chat_history_with_identical_timestamps(mock_db_path):
    """
    Test that prune_chat_history keeps the newest messages according to (created_at, rowid)
    even when timestamps are identical.
    """
    async with aiosqlite.connect(mock_db_path) as db:
        await db.execute("DELETE FROM chat_history;")
        await db.commit()

    # Insert 4 messages with the exact same timestamp
    timestamp = "2026-10-03 08:00:00"
    entries = [
        ("msg_1", "user", "Old 1"),
        ("msg_2", "assistant", "Old 2"),
        ("msg_3", "user", "New 1"),
        ("msg_4", "assistant", "New 2"),
    ]

    async with aiosqlite.connect(mock_db_path) as db:
        for msg_id, role, content in entries:
            await db.execute(
                """
                INSERT INTO chat_history (id, role, content, created_at)
                VALUES (?, ?, ?, ?);
                """,
                (msg_id, role, content, timestamp),
            )
        await db.commit()

    # Prune to keep only the latest 2 entries
    pruned = await repository.prune_chat_history(keep_latest=2, db_path=mock_db_path)
    assert pruned == 2

    # Remaining should be the newest by rowid: msg_3 (user) and msg_4 (assistant)
    remaining = await repository.get_recent_chat_history(limit=10, db_path=mock_db_path)
    assert len(remaining) == 2
    assert [m["content"] for m in remaining] == ["New 1", "New 2"]
    assert [m["role"] for m in remaining] == ["user", "assistant"]
