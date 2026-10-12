"""Automated test suite verifying Orchestrator-enforced limits for search_chat and search_notes.

Covers:
1. search_chat 4-step call: exactly 3 executions, 4th refused, limit notice delivered, final answer without read tools.
2. search_notes 4-step call: exactly 3 executions, 4th refused, limit notice delivered, final answer without read tools.
3. Independent quotas: both tools run up to 3 times (total 6 runs) in 1 user message, 4th attempt refused for each.
4. Edge cases:
   - Batch tags containing a 4th tag in a single turn.
   - Empty queries consume budget properly to prevent infinite loops.
   - Read tool execution attempts during final answer step are completely suppressed.
   - Bounded loop guarantee: model cannot loop infinitely even if it constantly emits tool tags.
   - Malformed XML / empty responses are handled gracefully without breaking counters or crashing.
"""

import aiosqlite
import pytest

import src.tools.parser as parser_module
from src.main import (
    MAX_SEARCH_CHAT_CALLS,
    MAX_SEARCH_NOTES_CALLS,
    handle_user_message,
)
from src.storage.database import generate_id

pytestmark = pytest.mark.asyncio

CHAT_LIMIT_HEADER = "[search_chat NOT EXECUTED]"
CHAT_LIMIT_TEXT = "The search_chat usage limit has been reached (3/3)."
NOTES_LIMIT_HEADER = "[search_notes NOT EXECUTED]"
NOTES_LIMIT_TEXT = "The search_notes usage limit has been reached (3/3)."


async def _seed_data(db_path: str) -> None:
    """Seed both chat history and lab notes into database for realistic search hits."""
    async with aiosqlite.connect(db_path) as db:
        # Seed chat history (old rows so they aren't filtered out by recent-8 dedup)
        for i in range(5):
            await db.execute(
                "INSERT INTO chat_history (id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (
                    generate_id(f"old_chat_{i}"),
                    "user" if i % 2 == 0 else "assistant",
                    f"Topik diskusi chat tentang arsitektur sistem bagian {i}",
                    "2020-01-01 10:00:00",
                ),
            )
        # Seed lab notes
        for i in range(5):
            await db.execute(
                "INSERT INTO lab_notes (id, topic, content, tags) VALUES (?, ?, ?, ?)",
                (
                    generate_id(f"note_{i}"),
                    f"Riset performa database bab {i}",
                    f"Detail optimasi dan indexing SQLite serta PostgreSQL bab {i}",
                    "database, performance",
                ),
            )
        await db.commit()


def _setup_mock_llm(mocker, responses: list[str]):
    """Set up deterministic mock LLM and spies for read tools."""
    captured_messages: list[list[dict]] = []
    resp_iter = iter(responses)

    async def _mock_generate(messages, **kwargs):
        captured_messages.append(list(messages))
        try:
            return next(resp_iter)
        except StopIteration:
            return "Fallback answer when responses run out."

    mocker.patch("src.main.llm_client.generate_response", side_effect=_mock_generate)
    spy_chat = mocker.spy(parser_module, "search_chat_history")
    spy_notes = mocker.spy(parser_module, "search_lab_notes")
    spy_tasks = mocker.spy(parser_module.repository, "get_tasks_by_status")

    return captured_messages, spy_chat, spy_notes, spy_tasks


# =========================================================================
# 1. TEST BATAS search_chat (Stepwise 4 calls)
# =========================================================================
async def test_search_chat_stepwise_limit_enforcement(mocker, mock_db_path):
    """LLM requests search_chat 4 times stepwise: 3 executed, 4th refused, answered without read tools."""
    await _seed_data(mock_db_path)
    captured, spy_chat, spy_notes, _ = _setup_mock_llm(
        mocker,
        [
            "<search_chat>arsitektur 0</search_chat>",  # Call 1 -> executes (1/3)
            "<search_chat>arsitektur 1</search_chat>",  # Call 2 -> executes (2/3)
            "<search_chat>arsitektur 2</search_chat>",  # Call 3 -> executes (3/3)
            "<search_chat>arsitektur 3</search_chat>",  # Call 4 -> REFUSED!
            "Ini adalah kesimpulan akhir berdasarkan data yang ada.",  # Call 5 -> Final answer
        ],
    )

    reply = await handle_user_message("Cari riwayat arsitektur kita")

    # 1. Total LLM calls: 5 (1 initial + 3 follow-ups + 1 final answer after limit)
    assert len(captured) == 5
    # 2. Exactly 3 executions of search_chat_history
    assert len(spy_chat.call_args_list) == 3
    assert [c.kwargs["query"] for c in spy_chat.call_args_list] == [
        "arsitektur 0",
        "arsitektur 1",
        "arsitektur 2",
    ]
    # 3. search_notes was never called
    assert len(spy_notes.call_args_list) == 0
    # 4. Turn 4 prompt informed the model that search_chat was not executed and limit reached
    turn4_last_user_prompt = captured[4][-1]["content"]
    assert CHAT_LIMIT_HEADER in turn4_last_user_prompt
    assert CHAT_LIMIT_TEXT in turn4_last_user_prompt
    # 5. Clean final answer
    assert reply == "Ini adalah kesimpulan akhir berdasarkan data yang ada."
    assert "<search_chat" not in reply


# =========================================================================
# 2. TEST BATAS search_notes (Stepwise 4 calls)
# =========================================================================
async def test_search_notes_stepwise_limit_enforcement(mocker, mock_db_path):
    """LLM requests search_notes 4 times stepwise: 3 executed, 4th refused, answered without read tools."""
    await _seed_data(mock_db_path)
    captured, spy_chat, spy_notes, _ = _setup_mock_llm(
        mocker,
        [
            "<search_notes>database 0</search_notes>",  # Call 1 -> executes (1/3)
            "<search_notes>database 1</search_notes>",  # Call 2 -> executes (2/3)
            "<search_notes>database 2</search_notes>",  # Call 3 -> executes (3/3)
            "<search_notes>database 3</search_notes>",  # Call 4 -> REFUSED!
            "Ini adalah rangkuman catatan riset database.",  # Call 5 -> Final answer
        ],
    )

    reply = await handle_user_message("Cari catatan riset database")

    # 1. Total LLM calls: 5 (1 initial + 3 follow-ups + 1 final answer after limit)
    assert len(captured) == 5
    # 2. Exactly 3 executions of search_lab_notes
    assert len(spy_notes.call_args_list) == 3
    assert [c.kwargs["query"] for c in spy_notes.call_args_list] == [
        "database 0",
        "database 1",
        "database 2",
    ]
    # 3. search_chat was never called
    assert len(spy_chat.call_args_list) == 0
    # 4. Turn 4 prompt informed the model that search_notes was not executed and limit reached
    turn4_last_user_prompt = captured[4][-1]["content"]
    assert NOTES_LIMIT_HEADER in turn4_last_user_prompt
    assert NOTES_LIMIT_TEXT in turn4_last_user_prompt
    # 5. Clean final answer
    assert reply == "Ini adalah rangkuman catatan riset database."
    assert "<search_notes" not in reply


# =========================================================================
# 3. TEST KUOTA TERPISAH (Both tools run to full 3/3 capacity)
# =========================================================================
async def test_independent_quotas_full_interleaved_usage(mocker, mock_db_path):
    """Both search_chat and search_notes are called 3 times each (total 6 executions) in 1 user message."""
    await _seed_data(mock_db_path)
    captured, spy_chat, spy_notes, _ = _setup_mock_llm(
        mocker,
        [
            "<search_chat>chat 1</search_chat>",   # Turn 1 -> chat executed (chat: 1/3, notes: 0/3)
            "<search_notes>note 1</search_notes>", # Turn 2 -> notes executed (chat: 1/3, notes: 1/3)
            "<search_chat>chat 2</search_chat>",   # Turn 3 -> chat executed (chat: 2/3, notes: 1/3)
            "<search_notes>note 2</search_notes>", # Turn 4 -> notes executed (chat: 2/3, notes: 2/3)
            "<search_chat>chat 3</search_chat>",   # Turn 5 -> chat executed (chat: 3/3, notes: 2/3)
            "<search_notes>note 3</search_notes>", # Turn 6 -> notes executed (chat: 3/3, notes: 3/3)
            # Turn 7: Model attempts 4th call for BOTH tools in the same turn -> BOTH REFUSED!
            "<search_chat>chat 4</search_chat><search_notes>note 4</search_notes>",
            # Turn 8: Final answer turn
            "Jawaban lengkap memadukan 3 temuan chat dan 3 temuan lab notes.",
        ],
    )

    reply = await handle_user_message("Bandingkan riwayat chat dan catatan riset")

    # 1. Total LLM calls: 8 (1 initial + 7 follow-ups)
    assert len(captured) == 8
    # 2. Both tools executed EXACTLY 3 times
    assert len(spy_chat.call_args_list) == 3
    assert len(spy_notes.call_args_list) == 3
    assert [c.kwargs["query"] for c in spy_chat.call_args_list] == ["chat 1", "chat 2", "chat 3"]
    assert [c.kwargs["query"] for c in spy_notes.call_args_list] == ["note 1", "note 2", "note 3"]

    # 3. Verify turn 7 prompt contains limit notices for BOTH tools
    turn7_prompt = captured[7][-1]["content"]
    assert CHAT_LIMIT_HEADER in turn7_prompt
    assert CHAT_LIMIT_TEXT in turn7_prompt
    assert NOTES_LIMIT_HEADER in turn7_prompt
    assert NOTES_LIMIT_TEXT in turn7_prompt

    # 4. Final reply is clean
    assert reply == "Jawaban lengkap memadukan 3 temuan chat dan 3 temuan lab notes."
    assert "<search_chat" not in reply
    assert "<search_notes" not in reply


# =========================================================================
# 4. TEST KASUS PINGGIR (Edge Cases)
# =========================================================================
async def test_edge_case_batch_tags_with_4th_tag(mocker, mock_db_path):
    """LLM emits 4 search_notes tags at once in Turn 1: exactly 3 executed, 1 marked limit reached."""
    await _seed_data(mock_db_path)
    captured, spy_chat, spy_notes, _ = _setup_mock_llm(
        mocker,
        [
            (
                "<search_notes>tag 1</search_notes>"
                "<search_notes>tag 2</search_notes>"
                "<search_notes>tag 3</search_notes>"
                "<search_notes>tag 4</search_notes>"
            ),
            "Jawaban dari pemanggilan batch 3 tag pertama.",
        ],
    )

    reply = await handle_user_message("Cari 4 topik sekaligus")

    assert len(captured) == 2
    assert len(spy_notes.call_args_list) == 3
    assert [c.kwargs["query"] for c in spy_notes.call_args_list] == ["tag 1", "tag 2", "tag 3"]
    follow_up_prompt = captured[1][-1]["content"]
    assert "[TOOL EXECUTION RESULTS]" in follow_up_prompt
    assert NOTES_LIMIT_TEXT in follow_up_prompt
    assert reply == "Jawaban dari pemanggilan batch 3 tag pertama."


async def test_edge_case_empty_queries_consume_budget(mocker, mock_db_path):
    """Empty queries consume budget to prevent infinite looping on invalid queries."""
    await _seed_data(mock_db_path)
    captured, spy_chat, spy_notes, _ = _setup_mock_llm(
        mocker,
        [
            "<search_notes></search_notes>",       # Turn 1: empty query -> consumes budget 1/3, returns error
            "<search_notes>   </search_notes>",    # Turn 2: whitespace -> consumes budget 2/3, returns error
            "<search_notes></search_notes>",       # Turn 3: empty -> consumes budget 3/3, returns error
            "<search_notes>query 4</search_notes>",# Turn 4: 4th attempt -> REFUSED as limit_reached!
            "Menjawab setelah berkali-kali query kosong.",
        ],
    )

    reply = await handle_user_message("Test query kosong")

    assert len(captured) == 5
    # Notice: spy_notes is called 0 times because empty queries fail validation before DB query
    assert len(spy_notes.call_args_list) == 0
    # But budget WAS consumed, so the 4th call was rejected with NOTES_LIMIT_HEADER
    turn4_prompt = captured[4][-1]["content"]
    assert NOTES_LIMIT_HEADER in turn4_prompt
    assert NOTES_LIMIT_TEXT in turn4_prompt
    assert reply == "Menjawab setelah berkali-kali query kosong."


async def test_edge_case_read_tools_strictly_suppressed_in_final_turn(mocker, mock_db_path):
    """Model tries to sneak in read tools (search_notes, search_chat, query_tasks) in final answer turn."""
    await _seed_data(mock_db_path)
    captured, spy_chat, spy_notes, spy_tasks = _setup_mock_llm(
        mocker,
        [
            "<search_notes>topic 1</search_notes>",
            "<search_notes>topic 2</search_notes>",
            "<search_notes>topic 3</search_notes>",
            "<search_notes>topic 4</search_notes>",  # 4th -> refused, triggers final_turn = True
            # In final turn, model sneakily emits multiple read tools:
            (
                "<search_notes>sneaky notes</search_notes>"
                "<search_chat>sneaky chat</search_chat>"
                '<query_tasks status="pending"></query_tasks>'
                "Ini jawaban final bersih tanpa ada tool yang dijalankan lagi."
            ),
        ],
    )

    reply = await handle_user_message("Uji coba penguncian turn final")

    # Final turn must NOT execute any sneaky read tools:
    assert len(spy_notes.call_args_list) == 3
    assert len(spy_chat.call_args_list) == 0
    assert len(spy_tasks.call_args_list) == 0
    # The tags must be completely stripped from the final reply:
    assert "<search_notes>" not in reply
    assert "<search_chat>" not in reply
    assert "<query_tasks" not in reply
    assert "Ini jawaban final bersih tanpa ada tool yang dijalankan lagi." in reply


async def test_edge_case_bounded_loop_prevents_infinite_llm_loops(mocker, mock_db_path):
    """An adversarial LLM continuously emitting tool calls is forcefully stopped after 7 follow-up turns."""
    await _seed_data(mock_db_path)
    # 20 responses that keep emitting search_notes
    adversarial_responses = [f"<search_notes>query {i}</search_notes>" for i in range(20)]
    captured, _, spy_notes, _ = _setup_mock_llm(mocker, adversarial_responses)

    reply = await handle_user_message("Tes loop abadi")

    # Total LLM calls MUST be exactly 5:
    # Turn 1: query 0 (executes, 1/3)
    # Turn 2: query 1 (executes, 2/3)
    # Turn 3: query 2 (executes, 3/3)
    # Turn 4: query 3 (refused -> final_turn = True)
    # Turn 5: query 4 (final turn -> allow_read_tools=False -> if final_turn: break!)
    assert len(captured) == 5
    assert len(spy_notes.call_args_list) == 3
    # Did not loop forever; gracefully produced fallback reply
    assert reply == "✅ Aksi telah dieksekusi."


async def test_edge_case_malformed_xml_and_unclosed_tags(mocker, mock_db_path):
    """Malformed XML or unclosed tags do not crash the orchestrator or corrupt counter state."""
    await _seed_data(mock_db_path)
    captured, spy_chat, spy_notes, _ = _setup_mock_llm(
        mocker,
        [
            "<search_notes unclosed tag query without closing bracket",
            "Jawaban normal setelah tag malformed.",
        ],
    )

    reply = await handle_user_message("Tes tag rusak")

    # Does not crash:
    assert len(captured) == 1
    assert len(spy_notes.call_args_list) == 0
    assert reply == "<search_notes unclosed tag query without closing bracket"
