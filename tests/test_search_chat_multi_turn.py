"""Experiment: search_chat may run up to 3 times per user message (counted by the orchestrator).

Scenarios:
1. Normal          : 1 search  -> final answer
2. Second search   : 2 searches -> final answer
3. Third search    : 3 searches -> final answer
4. Stubborn model  : 3 searches + 4th refused -> limit notice -> one last turn -> done
Extra:
5. Stubborn twice  : model still emits search_chat in the last turn -> no further LLM call
6. Batch in one reply: 4 tags in a single response -> only 3 executed, limit notice sent
"""

import aiosqlite
import pytest

import src.tools.parser as parser_module
from src.main import MAX_SEARCH_CHAT_CALLS, handle_user_message
from src.storage.database import generate_id

pytestmark = pytest.mark.asyncio

LIMIT_HEADER = "[search_chat NOT EXECUTED]"
LIMIT_TEXT = "The search_chat usage limit has been reached (3/3)."


async def _seed_chat(db_path: str) -> None:
    """Seed old chat rows (outside the recent-8 window) so FTS5 has matches."""
    rows = [
        ("user", "Kita pakai SQLite untuk storage lokal Vigil"),
        ("assistant", "SQLite FTS5 dipakai untuk full text search catatan"),
        ("user", "Index FTS5 database perlu rebuild saat startup"),
    ]
    async with aiosqlite.connect(db_path) as db:
        for role, content in rows:
            await db.execute(
                "INSERT INTO chat_history (id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (generate_id("old"), role, content, "2020-01-01 10:00:00"),
            )
        await db.commit()


def _setup(mocker, llm_outputs: list[str]):
    captured: list[list[dict]] = []
    outputs = iter(llm_outputs)

    async def fake_llm(messages, **kwargs):
        captured.append(list(messages))
        return next(outputs)

    mocker.patch("src.main.llm_client.generate_response", side_effect=fake_llm)
    spy_search = mocker.spy(parser_module, "search_chat_history")
    return captured, spy_search


def _queries(spy_search) -> list[str]:
    return [c.kwargs["query"] for c in spy_search.call_args_list]


def _last_user_msg(call_messages: list[dict]) -> str:
    return call_messages[-1]["content"]


async def test_scenario_1_normal(mocker, mock_db_path):
    await _seed_chat(mock_db_path)
    captured, spy = _setup(mocker, [
        '<search_chat limit="3">SQLite</search_chat>',
        "Kita memilih SQLite untuk storage lokal.",
    ])

    reply = await handle_user_message("Dulu kita pilih database apa?")

    assert reply == "Kita memilih SQLite untuk storage lokal."
    assert len(captured) == 2
    assert _queries(spy) == ["SQLite"]
    follow_up = _last_user_msg(captured[1])
    assert "[TOOL EXECUTION RESULTS]" in follow_up
    assert "SQLite" in follow_up
    assert "search_chat usage: 1/3." in follow_up
    assert "DO NOT emit any read query tool tags." not in follow_up


async def test_scenario_2_second_search(mocker, mock_db_path):
    await _seed_chat(mock_db_path)
    captured, spy = _setup(mocker, [
        "<search_chat>SQLite</search_chat>",
        "<search_chat>SQLite FTS5</search_chat>",
        "SQLite dengan FTS5 untuk pencarian.",
    ])

    reply = await handle_user_message("Detail database kita gimana?")

    assert reply == "SQLite dengan FTS5 untuk pencarian."
    assert len(captured) == 3
    assert _queries(spy) == ["SQLite", "SQLite FTS5"]
    assert "search_chat usage: 1/3." in _last_user_msg(captured[1])
    assert "search_chat usage: 2/3." in _last_user_msg(captured[2])
    # Conversation grows across turns: both result blocks are visible in the last call
    assert sum("[TOOL EXECUTION RESULTS]" in m["content"] for m in captured[2]) == 2


async def test_scenario_3_third_search(mocker, mock_db_path):
    await _seed_chat(mock_db_path)
    captured, spy = _setup(mocker, [
        "<search_chat>SQLite</search_chat>",
        "<search_chat>SQLite FTS5</search_chat>",
        "<search_chat>FTS5 database</search_chat>",
        "Ringkasan lengkap dari tiga pencarian.",
    ])

    reply = await handle_user_message("Ceritakan semua soal database kita")

    assert reply == "Ringkasan lengkap dari tiga pencarian."
    assert len(captured) == 4
    assert _queries(spy) == ["SQLite", "SQLite FTS5", "FTS5 database"]
    last = _last_user_msg(captured[3])
    assert "Answer the user in natural Indonesian using the available information." in last
    assert "you may call <search_chat> again" not in last
    assert LIMIT_HEADER not in last


async def test_scenario_4_stubborn_model(mocker, mock_db_path):
    await _seed_chat(mock_db_path)
    captured, spy = _setup(mocker, [
        "<search_chat>SQLite</search_chat>",
        "<search_chat>SQLite FTS5</search_chat>",
        "<search_chat>FTS5 database</search_chat>",
        "<search_chat>storage lokal</search_chat>",   # 4th -> refused
        "Berdasarkan hasil yang ada, kita pakai SQLite + FTS5.",
    ])

    reply = await handle_user_message("Cari terus sampai ketemu")

    assert reply == "Berdasarkan hasil yang ada, kita pakai SQLite + FTS5."
    assert len(captured) == 5
    assert _queries(spy) == ["SQLite", "SQLite FTS5", "FTS5 database"]  # 4th never executed
    last = _last_user_msg(captured[4])
    assert LIMIT_HEADER in last
    assert LIMIT_TEXT in last
    assert "TOOL_LIMIT_EXCEEDED" not in last


async def test_scenario_5_stubborn_in_last_chance(mocker, mock_db_path):
    await _seed_chat(mock_db_path)
    captured, spy = _setup(mocker, [
        "<search_chat>SQLite</search_chat>",
        "<search_chat>SQLite FTS5</search_chat>",
        "<search_chat>FTS5 database</search_chat>",
        "<search_chat>storage lokal</search_chat>",
        "<search_chat>coba lagi</search_chat>",       # still stubborn in last chance
        "THIS MUST NEVER BE REQUESTED",
    ])

    reply = await handle_user_message("Cari terus")

    assert len(captured) == 5                         # no 6th LLM call
    assert len(spy.call_args_list) == MAX_SEARCH_CHAT_CALLS
    assert "<search_chat" not in reply
    assert reply == "\u2705 Aksi telah dieksekusi."  # existing fallback (prior actions exist)


async def test_scenario_6_batch_tags_in_one_reply(mocker, mock_db_path):
    await _seed_chat(mock_db_path)
    captured, spy = _setup(mocker, [
        "<search_chat>SQLite</search_chat><search_chat>FTS5</search_chat>"
        "<search_chat>database</search_chat><search_chat>storage</search_chat>",
        "Jawaban dari tiga hasil.",
    ])

    reply = await handle_user_message("Cari banyak sekaligus")

    assert reply == "Jawaban dari tiga hasil."
    assert len(captured) == 2
    assert _queries(spy) == ["SQLite", "FTS5", "database"]
    last = _last_user_msg(captured[1])
    assert "[TOOL EXECUTION RESULTS]" in last
    assert LIMIT_TEXT in last
