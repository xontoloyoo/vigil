"""Unit tests for task 'in_progress' status, defensive normalization, and prompt rendering."""

import pytest
from unittest.mock import AsyncMock

from src.storage import repository
from src.tools.parser import _normalize_task_status, parse_and_execute_tools
from src.core.prompts import build_chat_prompt

pytestmark = pytest.mark.asyncio


async def test_normalize_task_status_variations():
    """Verify defensive normalization coerces common LLM variations into standard enums."""
    # in_progress variations
    assert _normalize_task_status("in_progress") == "in_progress"
    assert _normalize_task_status("in-progress") == "in_progress"
    assert _normalize_task_status("inprogress") == "in_progress"
    assert _normalize_task_status("in progress") == "in_progress"
    assert _normalize_task_status("working") == "in_progress"
    assert _normalize_task_status("started") == "in_progress"
    assert _normalize_task_status("doing") == "in_progress"

    # done variations
    assert _normalize_task_status("done") == "done"
    assert _normalize_task_status("completed") == "done"
    assert _normalize_task_status("finish") == "done"
    assert _normalize_task_status("finished") == "done"
    assert _normalize_task_status("complete") == "done"

    # cancelled variations
    assert _normalize_task_status("cancelled") == "cancelled"
    assert _normalize_task_status("canceled") == "cancelled"
    assert _normalize_task_status("drop") == "cancelled"
    assert _normalize_task_status("dropped") == "cancelled"

    # pending variations
    assert _normalize_task_status("pending") == "pending"
    assert _normalize_task_status("pause") == "pending"
    assert _normalize_task_status("paused") == "pending"
    assert _normalize_task_status("backlog") == "pending"
    assert _normalize_task_status("queue") == "pending"

    # Unknown untouched
    assert _normalize_task_status("unknown_status") == "unknown_status"


async def test_repository_task_status_lifecycle(mock_db_path):
    """Verify repository stores in_progress, validates allowed enums, and rejects invalid ones."""
    # 1. Create a task (defaults to pending)
    t1 = await repository.create_task("Tugas Riset", due_time="15:00", db_path=mock_db_path)
    t2 = await repository.create_task("Tugas Desain", due_time="17:00", db_path=mock_db_path)

    # 2. Update t1 to in_progress
    ok_prog = await repository.update_task_status(t1, "in_progress", db_path=mock_db_path)
    assert ok_prog is True

    # 3. Reject invalid status
    bad_ok = await repository.update_task_status(t1, "invalid_status", db_path=mock_db_path)
    assert bad_ok is False

    # 4. Active tasks snapshot returns both pending and in_progress, prioritizing in_progress
    active_tasks = await repository.get_pending_tasks(db_path=mock_db_path)
    assert len(active_tasks) == 2
    assert active_tasks[0]["id"] == t1
    assert active_tasks[0]["status"] == "in_progress"
    assert active_tasks[1]["id"] == t2
    assert active_tasks[1]["status"] == "pending"

    # 5. Filter by in_progress specifically
    in_prog_tasks = await repository.get_tasks_by_status("in_progress", db_path=mock_db_path)
    assert len(in_prog_tasks) == 1
    assert in_prog_tasks[0]["id"] == t1

    # 6. Complete t1
    ok_done = await repository.update_task_status(t1, "done", db_path=mock_db_path)
    assert ok_done is True

    # Now only t2 is active
    active_after = await repository.get_pending_tasks(db_path=mock_db_path)
    assert len(active_after) == 1
    assert active_after[0]["id"] == t2


async def test_parser_update_task_in_progress_and_trace(mock_db_path):
    """Verify parser parses in_progress with tolerant variation and dispatches proper trace text."""
    task_id = await repository.create_task("Tugas Backend", db_path=mock_db_path)

    trace_calls = []

    async def mock_trace(msg: str):
        trace_calls.append(msg)

    # Model outputs hyphenated in-progress
    llm_output = f'<update_task id="{task_id}" status="in-progress">Mulai mengerjakan backend</update_task>Siap!'

    cleaned, actions = await parse_and_execute_tools(
        llm_output,
        db_path=mock_db_path,
        on_trace=mock_trace,
    )

    assert cleaned == "Siap!"
    assert len(actions) == 1
    assert actions[0]["tool"] == "update_task"
    assert actions[0]["status"] == "success"
    assert actions[0]["new_status"] == "in_progress"

    # Verify Indonesian trace text
    assert len(trace_calls) == 1
    assert "⏳ Menandai tugas" in trace_calls[0]
    assert "sedang dikerjakan" in trace_calls[0]

    # Verify task in DB is indeed in_progress
    active = await repository.get_pending_tasks(db_path=mock_db_path)
    assert active[0]["status"] == "in_progress"


async def test_prompt_rendering_with_in_progress_task():
    """Verify build_chat_prompt correctly renders [IN_PROGRESS] status in Active Tasks Snapshot."""
    tasks = [
        {"id": "tsk_01", "title": "Bikin Database", "due_time": "14:00", "status": "in_progress"},
        {"id": "tsk_02", "title": "Setup Nginx", "due_time": None, "status": "pending"},
    ]

    messages = build_chat_prompt(
        user_message="Halo Vigil",
        current_tasks=tasks,
    )

    system_prompt = messages[0]["content"]
    assert "## Active Tasks Snapshot (Pending & In-Progress):" in system_prompt
    assert "- [IN_PROGRESS] [tsk_01] Bikin Database (Due: 14:00)" in system_prompt
    assert "- [PENDING] [tsk_02] Setup Nginx (No deadline)" in system_prompt
