"""Tests for nightly evaluation and working memory management."""

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo
import pytest

from src.config import config
from src.core.prompts import build_nightly_eval_prompt
from src.engine.heartbeat import HeartbeatRunner
from src.storage import memory, repository
from src.storage.database import init_db
from src.tools.parser import parse_and_execute_tools
from src.main import handle_nightly_evaluation


@pytest.mark.asyncio
async def test_build_nightly_eval_prompt_with_custom_memory(tmp_path: Path):
    """Verify build_nightly_eval_prompt injects provided current_memory."""
    completed = [{"title": "Setup PostgreSQL", "id": "tsk_1"}]
    pending = [{"title": "Deploy K8s", "id": "tsk_2", "due_time": "2026-10-01 18:00:00"}]
    notes = [{"topic": "Postgres Tuning", "content": "Shared buffers set to 2GB"}]
    custom_memory = "# Active Sprint\n- Target: K8s cluster ready"

    messages = build_nightly_eval_prompt(
        completed_tasks=completed,
        pending_tasks=pending,
        daily_notes=notes,
        current_memory=custom_memory,
    )

    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"

    system_content = messages[0]["content"]
    assert "Setup PostgreSQL" in system_content
    assert "Deploy K8s" in system_content
    assert "Postgres Tuning" in system_content
    assert "# Active Sprint" in system_content
    assert "<working_memory>" in system_content


@pytest.mark.asyncio
async def test_update_working_memory_tool(tmp_path: Path):
    """Verify <update_working_memory> writes markdown and cleans response text."""
    target_mem = tmp_path / "MEMORY.md"
    raw_response = (
        "Baik, prioritas kerja kita perbarui sekarang.\n\n"
        "<update_working_memory>\n"
        "# Working Memory\n"
        "- Prioritas Utama: Refactoring Auth\n"
        "- Target Aktif: Selesaikan migrasi token\n"
        "</update_working_memory>\n\n"
        "Saya sudah catat di papan kerja harian."
    )

    cleaned_text, actions = await parse_and_execute_tools(
        raw_response,
        memory_md_path=target_mem,
    )

    assert "<update_working_memory>" not in cleaned_text
    assert "Baik, prioritas kerja kita perbarui sekarang." in cleaned_text
    assert "Saya sudah catat di papan kerja harian." in cleaned_text

    assert len(actions) == 1
    assert actions[0]["tool"] == "update_working_memory"
    assert actions[0]["status"] == "success"

    written = target_mem.read_text(encoding="utf-8")
    assert "# Working Memory" in written
    assert "Prioritas Utama: Refactoring Auth" in written


@pytest.mark.asyncio
async def test_update_working_memory_empty_content(tmp_path: Path):
    """Verify empty <update_working_memory> reports error and does not corrupt file."""
    target_mem = tmp_path / "MEMORY.md"
    target_mem.write_text("# Initial Memory", encoding="utf-8")

    raw_response = "<update_working_memory>   </update_working_memory>"
    cleaned_text, actions = await parse_and_execute_tools(
        raw_response,
        memory_md_path=target_mem,
    )

    assert len(actions) == 1
    assert actions[0]["tool"] == "update_working_memory"
    assert actions[0]["status"] == "error"

    # Original content preserved
    assert target_mem.read_text(encoding="utf-8") == "# Initial Memory"


@pytest.mark.asyncio
async def test_heartbeat_nightly_eval_idempotency_and_hour_check(tmp_path: Path):
    """Verify nightly evaluation triggers once per day when hour >= 21."""
    db_file = tmp_path / "test_hb.db"
    await init_db(db_file)

    runner = HeartbeatRunner(db_path=db_file)
    callback_mock = AsyncMock()
    runner.register_nightly_eval_callback(callback_mock)

    # 1. Hour < 21 (e.g. 14:00) -> should NOT trigger
    dt_1400 = datetime(2026, 10, 1, 14, 0, 0, tzinfo=ZoneInfo(config.TIMEZONE))
    with patch("src.engine.heartbeat.datetime") as mock_dt:
        mock_dt.now.return_value = dt_1400
        result = await runner.check_nightly_evaluation()
        assert result is False
        callback_mock.assert_not_called()

    # 2. Hour == 21 (e.g. 21:05) -> SHOULD trigger and record state
    dt_2105 = datetime(2026, 10, 1, 21, 5, 0, tzinfo=ZoneInfo(config.TIMEZONE))
    with patch("src.engine.heartbeat.datetime") as mock_dt:
        mock_dt.now.return_value = dt_2105
        result = await runner.check_nightly_evaluation()
        assert result is True
        callback_mock.assert_awaited_once()

    # Verify state saved
    state = await repository.get_state("last_nightly_eval_date", db_path=db_file)
    assert state == "2026-10-01"

    # 3. Second call at 21:10 (same day) -> should be IDEMPOTENT (skipped)
    dt_2110 = datetime(2026, 10, 1, 21, 10, 0, tzinfo=ZoneInfo(config.TIMEZONE))
    with patch("src.engine.heartbeat.datetime") as mock_dt:
        mock_dt.now.return_value = dt_2110
        result = await runner.check_nightly_evaluation()
        assert result is False
        # Callback still only called once
        assert callback_mock.await_count == 1


@pytest.mark.asyncio
async def test_handle_nightly_evaluation_end_to_end(tmp_path: Path):
    """Verify handle_nightly_evaluation updates MEMORY.md and sends Telegram review."""
    db_file = tmp_path / "test_main.db"
    mem_file = tmp_path / "MEMORY.md"
    mem_file.write_text("# Old Memory", encoding="utf-8")

    await init_db(db_file)
    await repository.create_task("Task 1", db_path=db_file)
    await repository.create_task("Task 2", db_path=db_file)

    llm_output = (
        "Berikut evaluasi performa Anda hari ini:\n\n"
        "Eksekusi tugas hari ini cukup baik namun masih ada tugas yang tertunda. "
        "Pertahankan fokus untuk besok pagi.\n\n"
        "<working_memory>\n"
        "# Active Working Memory\n"
        "- Prioritas Utama Besok: Tuntaskan Task 2\n"
        "- Target Aktif: Menyiapkan deployment pipeline\n"
        "</working_memory>"
    )

    with (
        patch("src.main.repository.get_tasks_by_status", new_callable=AsyncMock) as mock_get_done,
        patch("src.main.repository.get_pending_tasks", new_callable=AsyncMock) as mock_get_pending,
        patch("src.main.repository.get_recent_lab_notes", new_callable=AsyncMock) as mock_get_notes,
        patch("src.main.memory.DEFAULT_MEMORY_MD", mem_file),
        patch("src.main.llm_client.generate_response", new_callable=AsyncMock) as mock_llm,
        patch("src.main.telegram_interface.send_proactive_nudge", new_callable=AsyncMock) as mock_tg,
        patch("src.main.save_chat_message", new_callable=AsyncMock) as mock_save_msg,
    ):
        mock_get_done.return_value = [{"title": "Task 1", "id": "tsk_1"}]
        mock_get_pending.return_value = [{"title": "Task 2", "id": "tsk_2"}]
        mock_get_notes.return_value = []
        mock_llm.return_value = llm_output
        mock_tg.return_value = True

        await handle_nightly_evaluation()

        # Check LLM called
        mock_llm.assert_awaited_once()

        # Check MEMORY.md updated
        updated_mem = mem_file.read_text(encoding="utf-8")
        assert "Prioritas Utama Besok: Tuntaskan Task 2" in updated_mem
        assert "# Active Working Memory" in updated_mem

        # Check Telegram called with clean review (without <working_memory> tag)
        mock_tg.assert_awaited_once()
        delivered_msg = mock_tg.call_args[0][0]
        assert "<working_memory>" not in delivered_msg
        assert "Eksekusi tugas hari ini cukup baik" in delivered_msg

        # Check saved to chat_history
        mock_save_msg.assert_awaited_once_with("assistant", delivered_msg)


@pytest.mark.asyncio
async def test_handle_nightly_evaluation_missing_tag_fallback(tmp_path: Path):
    """Verify missing <working_memory> tag retains existing MEMORY.md safely."""
    mem_file = tmp_path / "MEMORY.md"
    mem_file.write_text("# Existing Untouched Memory", encoding="utf-8")

    llm_output = (
        "Hari ini Anda bekerja dengan sangat disiplin! "
        "Semua tugas tuntas tanpa ada sisa."
    )

    with (
        patch("src.main.repository.get_tasks_by_status", new_callable=AsyncMock) as mock_get_done,
        patch("src.main.repository.get_pending_tasks", new_callable=AsyncMock) as mock_get_pending,
        patch("src.main.repository.get_recent_lab_notes", new_callable=AsyncMock) as mock_get_notes,
        patch("src.main.memory.DEFAULT_MEMORY_MD", mem_file),
        patch("src.main.llm_client.generate_response", new_callable=AsyncMock) as mock_llm,
        patch("src.main.telegram_interface.send_proactive_nudge", new_callable=AsyncMock) as mock_tg,
        patch("src.main.save_chat_message", new_callable=AsyncMock) as mock_save_msg,
    ):
        mock_get_done.return_value = [{"title": "Task 1", "id": "tsk_1"}]
        mock_get_pending.return_value = []
        mock_get_notes.return_value = []
        mock_llm.return_value = llm_output
        mock_tg.return_value = True

        await handle_nightly_evaluation()

        # Existing memory file is NOT wiped or overwritten
        assert mem_file.read_text(encoding="utf-8") == "# Existing Untouched Memory"

        # Telegram message still sent
        mock_tg.assert_awaited_once_with(llm_output)
        mock_save_msg.assert_awaited_once_with("assistant", llm_output)

