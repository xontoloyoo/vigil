"""Unit tests for context formatters and prompt templates."""

from src.core.context_formatters import (
    format_query_tasks_result,
    format_search_notes_result,
    format_get_dossier_result,
    format_search_chat_result,
    format_nudges_for_prompt,
    format_follow_up_user_message,
)
from src.core.prompt_templates import (
    DEFAULT_SOUL_FALLBACK,
    DEFAULT_USER_FALLBACK,
    TEMPLATE_SYSTEM_CONTEXT_TIME,
    TEMPLATE_USER_DOSSIER,
    TEMPLATE_WORKING_MEMORY,
    TEMPLATE_ACTIVE_TASKS_HEADER,
    TEMPLATE_ACTIVE_TASK_ITEM,
    TEMPLATE_ACTIVE_TASK_DUE,
    TEMPLATE_ACTIVE_TASK_NO_DUE,
    TEMPLATE_EXTRA_CONTEXT,
    TEMPLATE_CONTEXT_MERGING_DIRECTIVE,
    TEMPLATE_NUDGE_SYSTEM,
    TEMPLATE_NUDGE_USER,
    TEMPLATE_NIGHTLY_EVAL_SYSTEM,
    TEMPLATE_NIGHTLY_EVAL_USER,
    TEMPLATE_NIGHTLY_NOTES_SECTION,
    TEMPLATE_RECONCILE_DOSSIER_SYSTEM,
    TEMPLATE_RECONCILE_DOSSIER_USER,
)


def test_format_query_tasks_result_empty():
    res = format_query_tasks_result([], "pending")
    assert "No tasks found" in res
    assert "pending" in res


def test_format_query_tasks_result_with_items():
    tasks = [
        {"id": "tsk_1", "title": "Beli kopi", "status": "pending", "due_time": "10:00"},
        {"id": "tsk_2", "title": "Bikin modul", "status": "done", "due_time": None},
    ]
    res = format_query_tasks_result(tasks, "all")
    assert "Task Query Results" in res
    assert "Beli kopi" in res
    assert "Due: 10:00" in res
    assert "Bikin modul" in res
    assert "Total: 2 tasks found." in res


def test_format_search_notes_result():
    assert "No lab notes found" in format_search_notes_result([], "database")
    
    notes = [
        {"topic": "DB Tuning", "content": "Gunakan indexing B-tree", "tags": "db,perf", "score": 0.95}
    ]
    res = format_search_notes_result(notes, "database")
    assert "Lab Notes Search Results" in res
    assert "DB Tuning" in res
    assert "Gunakan indexing B-tree" in res
    assert "tags: db,perf" in res


def test_format_get_dossier_result():
    assert "No user dossier records found." in format_get_dossier_result([], 5)

    entries = [
        {"category": "habit", "fact": "Suka kopi tanpa gula"}
    ]
    res = format_get_dossier_result(entries, 5, "habit")
    assert "User Dossier Profile" in res
    assert "HABIT" in res
    assert "Suka kopi tanpa gula" in res


def test_format_search_chat_result():
    assert "No chat history found" in format_search_chat_result([], "halo")

    messages = [
        {"role": "user", "content": "Halo apa kabar?", "created_at": "2026-09-30 20:00", "score": 0.8}
    ]
    res = format_search_chat_result(messages, "halo")
    assert "Chat History Search Results" in res
    assert "USER" in res
    assert "Halo apa kabar?" in res


def test_format_nudges_for_prompt():
    assert format_nudges_for_prompt([]) == ""

    nudges = [
        {"id": "tsk_host1", "title": "Bayar hosting", "due_time": "23:59"}
    ]
    res = format_nudges_for_prompt(nudges)
    assert "[PENDING OVERDUE TASKS / NUDGES:" in res
    assert "[tsk_host1]" in res
    assert "Bayar hosting" in res
    assert "Due: 23:59" in res


def test_format_follow_up_user_message():
    res = format_follow_up_user_message("DATA DUMMY")
    assert "[TOOL EXECUTION RESULTS]" in res
    assert "DATA DUMMY" in res
    assert "DO NOT emit any read query tool tags." in res


def test_templates_formatting_integrity():
    # Verify all templates format without KeyError
    s1 = TEMPLATE_SYSTEM_CONTEXT_TIME.format(time_str="2026-09-30 21:00:00 WIB")
    assert "2026-09-30 21:00:00 WIB" in s1

    s2 = TEMPLATE_USER_DOSSIER.format(content="User profile data")
    assert "User profile data" in s2

    s3 = TEMPLATE_WORKING_MEMORY.format(content="Memory data")
    assert "Memory data" in s3

    s4 = TEMPLATE_ACTIVE_TASK_ITEM.format(status="PENDING", id="tsk_1", title="Task A", due_str=" (Due: now)")
    assert "Task A" in s4
    assert "[tsk_1]" in s4

    s5 = TEMPLATE_EXTRA_CONTEXT.format(extra_context="Extra research")
    assert "Extra research" in s5

    s6 = TEMPLATE_CONTEXT_MERGING_DIRECTIVE.format(pending_nudge="[Nudge content]")
    assert "[Nudge content]" in s6

    s7 = TEMPLATE_NUDGE_SYSTEM.format(soul="Vigil soul", time_str="Now", ticket_str="Task 1", habits_note="")
    assert "Vigil soul" in s7

    s8 = TEMPLATE_NIGHTLY_EVAL_SYSTEM.format(
        soul="Vigil soul",
        time_str="Now",
        completed_count=1,
        completed_lines="- Task done",
        pending_count=0,
        pending_lines="- None",
        notes_section="",
        current_memory="- Prioritas Utama: Task A",
    )
    assert "Vigil soul" in s8
    assert "Prioritas Utama: Task A" in s8

    s9 = TEMPLATE_RECONCILE_DOSSIER_SYSTEM.format(
        soul="Vigil soul",
        time_str="Now",
        raw_dossier="- [PREFERENCE] Suka Python",
    )
    assert "Vigil soul" in s9
    assert "- [PREFERENCE] Suka Python" in s9
    assert "<user_dossier>" in s9


def test_format_search_follow_up_instructions():
    from src.core.context_formatters import (
        format_search_chat_follow_up_message,
        format_search_notes_follow_up_message,
        format_multi_search_follow_up_message,
    )
    # 1. Under limit: prompts model to evaluate sufficiency first
    chat_msg = format_search_chat_follow_up_message("RES", used=1, max_calls=3)
    assert "search_chat usage: 1/3." in chat_msg
    assert "1. If the results answer the question, answer using the evidence." in chat_msg
    assert "call <search_chat> again with a different query while quota remains." in chat_msg
    assert "3. If evidence remains insufficient, say so. Never invent project facts." in chat_msg
    assert "Do not reveal internal reasoning." in chat_msg

    notes_msg = format_search_notes_follow_up_message("RES", used=2, max_calls=3)
    assert "search_notes usage: 2/3." in notes_msg
    assert "1. If the results answer the question, answer using the evidence." in notes_msg
    assert "call <search_notes> again with a different query while quota remains." in notes_msg

    multi_msg = format_multi_search_follow_up_message("RES", chat_used=1, chat_max=3, notes_used=2, notes_max=3)
    assert "search_chat usage: 1/3." in multi_msg
    assert "search_notes usage: 2/3." in multi_msg
    assert "call <search_chat> or <search_notes> again with a different query while quota remains." in multi_msg

    # 2. At limit: directs honest admission without fabrication
    terminal_chat = format_search_chat_follow_up_message("RES", used=3, max_calls=3)
    assert "No more searches permitted. DO NOT call any read tools." in terminal_chat
    assert "Answer the user in natural Indonesian using the available information." in terminal_chat
    assert "honestly state so" in terminal_chat
    assert "do not fabricate details" in terminal_chat
    assert "Never present general knowledge as stored project records." in terminal_chat


def test_format_search_limit_notices():
    from src.core.context_formatters import (
        format_search_chat_limit_message,
        format_search_notes_limit_message,
        format_multi_search_limit_message,
    )
    c_lim = format_search_chat_limit_message("RES", max_calls=3)
    assert "[search_chat NOT EXECUTED]" in c_lim
    assert "The search_chat usage limit has been reached (3/3)." in c_lim
    assert "DO NOT call any read tools." in c_lim
    assert "state this honestly" in c_lim
    assert "do not fabricate or speculate" in c_lim

    n_lim = format_search_notes_limit_message("RES", max_calls=3)
    assert "[search_notes NOT EXECUTED]" in n_lim
    assert "The search_notes usage limit has been reached (3/3)." in n_lim
    assert "DO NOT call any read tools." in n_lim
    assert "state this honestly" in n_lim
    assert "do not fabricate or speculate" in n_lim

    m_lim = format_multi_search_limit_message("RES", chat_limit_reached=True, chat_max=3, notes_limit_reached=True, notes_max=3)
    assert "[search_chat NOT EXECUTED]" in m_lim
    assert "[search_notes NOT EXECUTED]" in m_lim
    assert "DO NOT call any read tools." in m_lim
    assert "do not fabricate or speculate" in m_lim


