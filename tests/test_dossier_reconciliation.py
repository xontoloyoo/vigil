"""Tests for User Dossier reconciliation and deduplication routine."""

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo
import pytest

from src.config import config
from src.core.prompts import build_dossier_reconciliation_prompt
from src.engine.heartbeat import HeartbeatRunner
from src.storage import repository
from src.storage.database import init_db
from src.main import handle_dossier_reconciliation


def test_build_dossier_reconciliation_prompt():
    """Verify build_dossier_reconciliation_prompt constructs structured prompts."""
    raw = (
        "- [PREFERENCE] Suka kopi hitam\n"
        "- [PREFERENCE] Minum kopi tanpa gula\n"
        "- [HABIT] Tidur jam 23:00\n"
    )
    messages = build_dossier_reconciliation_prompt(raw)

    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"

    system_content = messages[0]["content"]
    assert "Suka kopi hitam" in system_content
    assert "<user_dossier>" in system_content
    assert "ZERO HALLUCINATION" in system_content


@pytest.mark.asyncio
async def test_heartbeat_dossier_reconcile_capacity_trigger(tmp_path: Path):
    """Verify dossier reconciliation triggers when bullet count reaches 20."""
    db_file = tmp_path / "test_reconcile.db"
    user_md = tmp_path / "USER.md"
    await init_db(db_file)

    # Create 20 bullet lines
    lines = [f"- [HABIT] Kebiasaan nomor {i}" for i in range(1, 21)]
    user_md.write_text("\n".join(lines), encoding="utf-8")

    runner = HeartbeatRunner(db_path=db_file)
    callback_mock = AsyncMock()
    runner.register_dossier_reconcile_callback(callback_mock)

    # Non-Sunday time (e.g. Wednesday 10:00)
    dt_wed = datetime(2026, 9, 30, 10, 0, 0, tzinfo=ZoneInfo(config.TIMEZONE))
    with patch("src.engine.heartbeat.datetime") as mock_dt:
        mock_dt.now.return_value = dt_wed
        triggered = await runner.check_dossier_reconciliation(user_md_path=user_md)

        assert triggered is True
        callback_mock.assert_awaited_once()

    # Second call should be skipped due to date lock
    with patch("src.engine.heartbeat.datetime") as mock_dt:
        mock_dt.now.return_value = dt_wed
        triggered_second = await runner.check_dossier_reconciliation(user_md_path=user_md)
        assert triggered_second is False
        assert callback_mock.await_count == 1


@pytest.mark.asyncio
async def test_heartbeat_dossier_reconcile_sunday_time_trigger(tmp_path: Path):
    """Verify dossier reconciliation triggers on Sunday at >= 23:00 even with few lines."""
    db_file = tmp_path / "test_reconcile_time.db"
    user_md = tmp_path / "USER.md"
    await init_db(db_file)

    # 5 bullet lines (below 20 threshold, but >= 2 items)
    lines = [f"- [PREFERENCE] Preferensi {i}" for i in range(1, 6)]
    user_md.write_text("\n".join(lines), encoding="utf-8")

    runner = HeartbeatRunner(db_path=db_file)
    callback_mock = AsyncMock()
    runner.register_dossier_reconcile_callback(callback_mock)

    # Sunday 2026-10-04 14:00 (hour < 23) -> should NOT trigger
    dt_sun_early = datetime(2026, 10, 4, 14, 0, 0, tzinfo=ZoneInfo(config.TIMEZONE))
    with patch("src.engine.heartbeat.datetime") as mock_dt:
        mock_dt.now.return_value = dt_sun_early
        assert await runner.check_dossier_reconciliation(user_md_path=user_md) is False
        callback_mock.assert_not_called()

    # Sunday 2026-10-04 23:15 (hour >= 23) -> SHOULD trigger
    dt_sun_night = datetime(2026, 10, 4, 23, 15, 0, tzinfo=ZoneInfo(config.TIMEZONE))
    with patch("src.engine.heartbeat.datetime") as mock_dt:
        mock_dt.now.return_value = dt_sun_night
        assert await runner.check_dossier_reconciliation(user_md_path=user_md) is True
        callback_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_dossier_reconciliation_success(tmp_path: Path):
    """Verify handle_dossier_reconciliation overwrites USER.md with consolidated content."""
    user_md = tmp_path / "USER.md"
    raw_lines = (
        "- [PREFERENCE] Suka dipanggil bro\n"
        "- [PREFERENCE] Jangan panggil mas, panggil bro saja\n"
        "- [HABIT] Tidur jam 22:00 malam\n"
    )
    user_md.write_text(raw_lines, encoding="utf-8")

    reconciled_output = (
        "Berikut hasil pembersihan dossier:\n\n"
        "<user_dossier>\n"
        "# User Profile & Dossier\n"
        "- [PREFERENCE] Lebih suka disapa 'bro', hindari sapaan 'mas'.\n"
        "- [HABIT] Memiliki jadwal tidur teratur jam 22:00 malam.\n"
        "</user_dossier>"
    )

    with patch("src.main.llm_client.generate_response", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = reconciled_output

        await handle_dossier_reconciliation(user_md_path=user_md)

        mock_llm.assert_awaited_once()
        saved_content = user_md.read_text(encoding="utf-8")
        assert "# User Profile & Dossier" in saved_content
        assert "- [PREFERENCE] Lebih suka disapa 'bro'" in saved_content
        assert "<user_dossier>" not in saved_content


@pytest.mark.asyncio
async def test_handle_dossier_reconciliation_anti_wipeout_guard(tmp_path: Path):
    """Verify invalid or empty LLM output aborts overwrite to protect USER.md."""
    user_md = tmp_path / "USER.md"
    original_content = (
        "- [PREFERENCE] Suka ngoding backend\n"
        "- [IDENTITY] Software Architect\n"
    )
    user_md.write_text(original_content, encoding="utf-8")

    # Case 1: Missing <user_dossier> tag
    with patch("src.main.llm_client.generate_response", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = "Saya sudah merapikan profilnya tanpa tag."
        await handle_dossier_reconciliation(user_md_path=user_md)
        assert user_md.read_text(encoding="utf-8") == original_content

    # Case 2: Output too short (< 20 chars)
    with patch("src.main.llm_client.generate_response", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = "<user_dossier>- [H] OK</user_dossier>"
        await handle_dossier_reconciliation(user_md_path=user_md)
        assert user_md.read_text(encoding="utf-8") == original_content


@pytest.mark.asyncio
async def test_record_dossier_verbatim_deduplication(tmp_path: Path):
    """Verify parse_and_execute_tools ignores verbatim duplicate bullet in USER.md."""
    from src.tools.parser import parse_and_execute_tools

    db_file = tmp_path / "test_dedup.db"
    user_md = tmp_path / "USER.md"
    await init_db(db_file)

    user_md.write_text("- [PREFERENCE] Suka kopi tubruk\n", encoding="utf-8")

    # Attempt to record identical bullet
    raw_response = '<record_dossier category="preference">Suka kopi tubruk</record_dossier>'
    _, actions = await parse_and_execute_tools(
        raw_response, db_path=db_file, user_md_path=user_md
    )

    assert len(actions) == 1
    assert actions[0]["tool"] == "record_dossier"

    # Verify USER.md still only has 1 line (not duplicated)
    lines = [l for l in user_md.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1
    assert lines[0] == "- [PREFERENCE] Suka kopi tubruk"


@pytest.mark.asyncio
async def test_repository_dossier_upsert_verbatim(tmp_path: Path):
    """Verify add_dossier_entry updates timestamp and does not create duplicate rows in SQLite."""
    db_file = tmp_path / "test_upsert_db.db"
    await init_db(db_file)

    # 1. First insert
    id1 = await repository.add_dossier_entry(
        category="preference", fact="Suka kopi tubruk", db_path=db_file
    )
    assert id1.startswith("dos_")

    entries_1 = await repository.get_recent_dossier(limit=10, db_path=db_file)
    assert len(entries_1) == 1
    assert entries_1[0]["id"] == id1
    assert entries_1[0]["fact"] == "Suka kopi tubruk"

    # 2. Second insert with identical category & fact (different casing)
    id2 = await repository.add_dossier_entry(
        category="PREFERENCE", fact="suka kopi tubruk", db_path=db_file
    )
    assert id2 == id1

    # Verify SQLite still only has 1 row total
    entries_2 = await repository.get_recent_dossier(limit=10, db_path=db_file)
    assert len(entries_2) == 1
    assert entries_2[0]["id"] == id1


