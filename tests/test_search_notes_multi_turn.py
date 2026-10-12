"""Multi-turn test suite for search_notes:
Bounded multi-call support (maximum 3 executions per user message).

Scenarios tested:
1. Normal (1 search -> final answer)
2. Follow-up search (2 searches -> final answer)
3. Reaching 3-call limit (3 searches -> final answer)
4. 4th call refused (stubborn model: 3 searches + 4th refused -> limit notice -> answer)
5. Stubborn twice (emits search_notes again in last chance turn -> loop ends, fallback)
6. Batch tags in single response (4 tags at once -> 3 executed, 1 limit reached notice)
7. Cross-tool interaction with search_chat (independent counters)
8. Interaction with legacy read tools (query_tasks / get_dossier -> legacy synthesis lock)
"""

import aiosqlite
import pytest

import src.tools.parser as parser_module
from src.main import MAX_SEARCH_NOTES_CALLS, handle_user_message
from src.storage.database import generate_id

pytestmark = pytest.mark.asyncio

LIMIT_HEADER = "[search_notes NOT EXECUTED]"
LIMIT_TEXT = "The search_notes usage limit has been reached (3/3)."


async def _seed_lab_notes(db_path: str) -> None:
    """Seed test lab notes into SQLite FTS5 table."""
    notes = [
        ("Architecture Patterns", "Clean architecture and domain driven design patterns", "architecture, ddd"),
        ("PostgreSQL Indexing", "B-Tree for range queries, GIN for full text search, BRIN for time series", "postgres, indexing"),
        ("Asyncio Event Loop", "Guidelines on running asyncio event loops on Windows and Unix", "python, asyncio"),
        ("Caching Strategies", "Redis cache aside and write through patterns", "caching, redis"),
    ]
    async with aiosqlite.connect(db_path) as db:
        for topic, content, tags in notes:
            await db.execute(
                "INSERT INTO lab_notes (id, topic, content, tags) VALUES (?, ?, ?, ?)",
                (generate_id("note"), topic, content, tags),
            )
        await db.commit()


async def _seed_chat_history(db_path: str) -> None:
    """Seed test chat history into SQLite FTS5 table."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO chat_history (id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (generate_id("old"), "user", "Diskusi lama tentang Redis caching", "2020-01-01 10:00:00"),
        )
        await db.commit()


def _setup(mocker, llm_outputs: list[str]):
    captured: list[list[dict]] = []
    outputs = iter(llm_outputs)

    async def fake_llm(messages, **kwargs):
        captured.append(list(messages))
        return next(outputs)

    mocker.patch("src.main.llm_client.generate_response", side_effect=fake_llm)
    spy_search_notes = mocker.spy(parser_module, "search_lab_notes")
    spy_search_chat = mocker.spy(parser_module, "search_chat_history")
    return captured, spy_search_notes, spy_search_chat


def _note_queries(spy_search) -> list[str]:
    return [c.kwargs["query"] for c in spy_search.call_args_list]


def _last_user_msg(call_messages: list[dict]) -> str:
    return call_messages[-1]["content"]


async def test_scenario_1_normal(mocker, mock_db_path):
    """Test 1: Normal single search_notes call followed by direct answer."""
    await _seed_lab_notes(mock_db_path)
    captured, spy_notes, _ = _setup(mocker, [
        '<search_notes limit="3">PostgreSQL</search_notes>',
        "Berdasarkan catatan riset, gunakan B-Tree atau GIN untuk PostgreSQL.",
    ])

    reply = await handle_user_message("Bagaimana cara indexing di PostgreSQL?")

    assert reply == "Berdasarkan catatan riset, gunakan B-Tree atau GIN untuk PostgreSQL."
    assert len(captured) == 2
    assert _note_queries(spy_notes) == ["PostgreSQL"]
    follow_up = _last_user_msg(captured[1])
    assert "[TOOL EXECUTION RESULTS]" in follow_up
    assert "search_notes usage: 1/3." in follow_up
    assert "call <search_notes> again" in follow_up
    assert "DO NOT emit any read query tool tags." not in follow_up


async def test_scenario_2_second_search(mocker, mock_db_path):
    """Test 2: LLM performs a second search with different keywords before answering."""
    await _seed_lab_notes(mock_db_path)
    captured, spy_notes, _ = _setup(mocker, [
        "<search_notes>PostgreSQL</search_notes>",
        "<search_notes>Indexing</search_notes>",
        "Ringkasan dari dua pencarian indexing PostgreSQL.",
    ])

    reply = await handle_user_message("Tolong cari info indexing PostgreSQL lebih detail.")

    assert reply == "Ringkasan dari dua pencarian indexing PostgreSQL."
    assert len(captured) == 3
    assert _note_queries(spy_notes) == ["PostgreSQL", "Indexing"]
    assert "search_notes usage: 1/3." in _last_user_msg(captured[1])
    assert "search_notes usage: 2/3." in _last_user_msg(captured[2])
    assert sum("[TOOL EXECUTION RESULTS]" in m["content"] for m in captured[2]) == 2


async def test_scenario_3_third_search(mocker, mock_db_path):
    """Test 3: LLM uses all 3 search_notes calls, receives 3/3 notice, answers directly."""
    await _seed_lab_notes(mock_db_path)
    captured, spy_notes, _ = _setup(mocker, [
        "<search_notes>Architecture</search_notes>",
        "<search_notes>Patterns</search_notes>",
        "<search_notes>Clean</search_notes>",
        "Ringkasan arsitektur dari 3 kali pencarian.",
    ])

    reply = await handle_user_message("Bahas lengkap architecture notes kita.")

    assert reply == "Ringkasan arsitektur dari 3 kali pencarian."
    assert len(captured) == 4
    assert _note_queries(spy_notes) == ["Architecture", "Patterns", "Clean"]
    last_prompt = _last_user_msg(captured[3])
    assert "Answer the user in natural Indonesian using the available information." in last_prompt
    assert "you may call <search_notes> again" not in last_prompt
    assert LIMIT_HEADER not in last_prompt


async def test_scenario_4_stubborn_model_refuses_4th(mocker, mock_db_path):
    """Test 4: LLM tries a 4th search_notes call -> refused by Orchestrator with limit message."""
    await _seed_lab_notes(mock_db_path)
    captured, spy_notes, _ = _setup(mocker, [
        "<search_notes>Architecture</search_notes>",
        "<search_notes>Patterns</search_notes>",
        "<search_notes>Clean</search_notes>",
        "<search_notes>Event Loop</search_notes>",  # 4th -> refused!
        "Hasil setelah limit tercapai.",
    ])

    reply = await handle_user_message("Cari arsitektur sampai tuntas.")

    assert reply == "Hasil setelah limit tercapai."
    assert len(captured) == 5
    assert _note_queries(spy_notes) == ["Architecture", "Patterns", "Clean"]
    last_prompt = _last_user_msg(captured[4])
    assert LIMIT_HEADER in last_prompt
    assert LIMIT_TEXT in last_prompt


async def test_scenario_5_stubborn_twice(mocker, mock_db_path):
    """Test 5: LLM is still stubborn on final turn -> loop terminates, fallback returned."""
    await _seed_lab_notes(mock_db_path)
    captured, spy_notes, _ = _setup(mocker, [
        "<search_notes>Architecture</search_notes>",
        "<search_notes>Patterns</search_notes>",
        "<search_notes>Clean</search_notes>",
        "<search_notes>Event Loop</search_notes>",  # 4th -> refused
        "<search_notes>Caching</search_notes>",     # still emitting tool on last chance
        "SHOULD NOT BE REQUESTED",
    ])

    reply = await handle_user_message("Cari terus jangan berhenti.")

    assert len(captured) == 5
    assert len(spy_notes.call_args_list) == MAX_SEARCH_NOTES_CALLS
    assert "<search_notes" not in reply
    assert reply == "✅ Aksi telah dieksekusi."


async def test_scenario_6_batch_tags_in_one_reply(mocker, mock_db_path):
    """Test 6: LLM outputs 4 search_notes tags in a single message -> only 3 executed, 1 limit reached."""
    await _seed_lab_notes(mock_db_path)
    captured, spy_notes, _ = _setup(mocker, [
        "<search_notes>Architecture</search_notes><search_notes>PostgreSQL</search_notes>"
        "<search_notes>Asyncio</search_notes><search_notes>Caching</search_notes>",
        "Jawaban komprehensif dari hasil batch.",
    ])

    reply = await handle_user_message("Cari semua topik riset sekaligus.")

    assert reply == "Jawaban komprehensif dari hasil batch."
    assert len(captured) == 2
    assert _note_queries(spy_notes) == ["Architecture", "PostgreSQL", "Asyncio"]
    last_prompt = _last_user_msg(captured[1])
    assert "[TOOL EXECUTION RESULTS]" in last_prompt
    assert LIMIT_TEXT in last_prompt


async def test_scenario_7_interaction_with_search_chat(mocker, mock_db_path):
    """Test 7: search_notes and search_chat can be used in separate turns with independent counters."""
    await _seed_lab_notes(mock_db_path)
    await _seed_chat_history(mock_db_path)
    captured, spy_notes, spy_chat = _setup(mocker, [
        "<search_notes>Caching Strategies</search_notes>",
        "<search_chat>Redis</search_chat>",
        "Catatan menyebut Redis write-through, dan chat lama juga membahas Redis caching.",
    ])

    reply = await handle_user_message("Cari catatan dan chat tentang Redis.")

    assert reply == "Catatan menyebut Redis write-through, dan chat lama juga membahas Redis caching."
    assert len(captured) == 3
    assert len(spy_notes.call_args_list) == 1
    assert len(spy_chat.call_args_list) == 1
    assert "search_notes usage: 1/3." in _last_user_msg(captured[1])
    assert "search_chat usage: 1/3." in _last_user_msg(captured[2])


async def test_scenario_8_interaction_with_legacy_read_tool(mocker, mock_db_path):
    """Test 8: If a legacy read tool (e.g. query_tasks) is called alongside search_notes, legacy lock engages."""
    await _seed_lab_notes(mock_db_path)
    async with aiosqlite.connect(mock_db_path) as db:
        await db.execute(
            "INSERT INTO tasks (id, title, status, created_at) VALUES ('t1', 'Review PR caching', 'pending', '2026-10-09 10:00:00')"
        )
        await db.commit()

    captured, spy_notes, _ = _setup(mocker, [
        '<search_notes>Caching</search_notes><query_tasks status="pending"></query_tasks>',
        "Tugas pending adalah review PR caching, dan catatan riset menyarankan write-through.",
    ])

    reply = await handle_user_message("Cek tugas dan catatan terkait caching.")

    assert reply == "Tugas pending adalah review PR caching, dan catatan riset menyarankan write-through."
    assert len(captured) == 2
    last_prompt = _last_user_msg(captured[1])
    assert "DO NOT emit any read query tool tags." in last_prompt
