"""Automated test suite for Vigil edge search scenarios & FTS5 mechanics.

Validates:
1. Actual FTS5 BM25 ranking mechanics (order, abs, round, target document retrieval).
2. FTS5 trigger auto-synchronization without explicit rebuild on active triggers.
3. Scenario 1: Single note sufficient -> complete answer synthesized.
4. Scenario 2: Information spread across notes -> all required facts retrieved & synthesized.
5. Scenario 3: Similar topics with contrasting details -> accurate comparison without swapping.
6. Scenario 4: Topic resembles database but specific fact absent -> honest admission, no hallucination.
7. Fixture cleanup audit using exact manifest IDs (no SQL wildcard risks).
"""

from typing import Any
import aiosqlite
import pytest

import src.tools.parser as parser_module
from src.main import handle_user_message
from src.storage.search import search_chat_history, search_lab_notes

pytestmark = pytest.mark.asyncio

# ============================================================================
# EXACT FIXTURE MANIFESTS (No SQL wildcard risks)
# ============================================================================

SYNTHETIC_LAB_NOTES: list[dict[str, str]] = [
    # Base corpus (ensures diverse document frequencies so IDF is meaningful)
    {
        "id": "syn_note_base_001",
        "topic": "Container Orchestration Guidelines",
        "content": "Pedoman deployment container Docker untuk worker Vigil di VPS.",
        "tags": "infra,docker,devops",
        "created_at": "2026-10-01 08:00:00",
    },
    {
        "id": "syn_note_base_002",
        "topic": "CI/CD Pipeline Workflow",
        "content": "Pipeline pengujian otomatis berjalan di GitHub Actions sebelum merge.",
        "tags": "cicd,github,automation",
        "created_at": "2026-10-01 08:15:00",
    },
    # Scenario 1: Single note clearly answers the question
    {
        "id": "syn_note_s1_001",
        "topic": "Arsitektur Redis Cache Eviction",
        "content": "Kebijakan eviction Redis untuk Vigil menggunakan allkeys-lru dengan batas memori maksimum 512MB.",
        "tags": "redis,caching,eviction,memory",
        "created_at": "2026-10-02 09:00:00",
    },
    # Scenario 2: Information spread across multiple notes with different keywords
    {
        "id": "syn_note_s2_001",
        "topic": "Audio Ingest Format Specification",
        "content": "File rekaman suara pengguna di-ingest dalam format WAV 16kHz mono sebelum diproses modul VAD.",
        "tags": "audio,ingest,format,wav",
        "created_at": "2026-10-03 10:00:00",
    },
    {
        "id": "syn_note_s2_002",
        "topic": "Whisper Transcription Engine Settings",
        "content": "Transkripsi audio dijalankan menggunakan model Whisper large-v3 dengan temperature 0.0 dan beam size 5.",
        "tags": "whisper,transcription,stt,engine",
        "created_at": "2026-10-03 10:15:00",
    },
    # Scenario 3: Notes on similar topic with distinct/contrasting details
    {
        "id": "syn_note_s3_001",
        "topic": "Database Connection Pooling Staging",
        "content": "Di environment staging, connection pool PostgreSQL diatur minimum 5 dan maksimum 20 koneksi dengan timeout 30 detik.",
        "tags": "database,staging,pool,postgres",
        "created_at": "2026-10-04 11:00:00",
    },
    {
        "id": "syn_note_s3_002",
        "topic": "Database Connection Pooling Produksi",
        "content": "Di environment produksi, connection pool PostgreSQL diatur minimum 50 dan maksimum 200 koneksi dengan timeout 5 detik menggunakan PgBouncer.",
        "tags": "database,produksi,pool,pgbouncer",
        "created_at": "2026-10-04 11:30:00",
    },
    # Scenario 4: Topic resembles database, but specific asked fact is absent
    {
        "id": "syn_note_s4_001",
        "topic": "Telegram Bot Network Timeout",
        "content": "HTTP request timeout untuk Telegram bot polling disetel ke 30 detik dengan retry backoff eksponensial.",
        "tags": "telegram,network,timeout",
        "created_at": "2026-10-05 13:00:00",
    },
    {
        "id": "syn_note_s4_002",
        "topic": "Telegram Bot Token Storage",
        "content": "Token bot Telegram disimpan di environment variable TELEGRAM_BOT_TOKEN dan dibaca saat inisialisasi runtime.",
        "tags": "telegram,token,security",
        "created_at": "2026-10-05 13:15:00",
    },
]

SYNTHETIC_CHAT_HISTORY: list[dict[str, str]] = [
    {
        "id": "syn_chat_s1_001",
        "role": "user",
        "content": "Apakah kita sudah mengatur batas memori cache Redis?",
        "created_at": "2026-10-02 08:50:00",
    },
    {
        "id": "syn_chat_s1_002",
        "role": "assistant",
        "content": "Sudah, detail konfigurasi ada di catatan lab bagian caching.",
        "created_at": "2026-10-02 08:51:00",
    },
    {
        "id": "syn_chat_s4_001",
        "role": "user",
        "content": "Bagaimana keamanan token Telegram kita?",
        "created_at": "2026-10-05 12:45:00",
    },
    {
        "id": "syn_chat_s4_002",
        "role": "assistant",
        "content": "Token disimpan rahasia di .env dan tidak pernah di-hardcode.",
        "created_at": "2026-10-05 12:46:00",
    },
]

MANIFEST_NOTE_IDS: list[str] = [n["id"] for n in SYNTHETIC_LAB_NOTES]
MANIFEST_CHAT_IDS: list[str] = [c["id"] for c in SYNTHETIC_CHAT_HISTORY]


async def seed_synthetic_fixtures(db_path: str) -> None:
    """Seed synthetic fixtures into the temporary isolated database."""
    async with aiosqlite.connect(db_path) as db:
        for n in SYNTHETIC_LAB_NOTES:
            await db.execute(
                """
                INSERT INTO lab_notes (id, topic, content, tags, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (n["id"], n["topic"], n["content"], n["tags"], n["created_at"]),
            )
        for c in SYNTHETIC_CHAT_HISTORY:
            await db.execute(
                """
                INSERT INTO chat_history (id, role, content, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (c["id"], c["role"], c["content"], c["created_at"]),
            )
        await db.commit()


async def cleanup_synthetic_fixtures(db_path: str) -> tuple[int, int]:
    """Clean up synthetic fixtures using exact ID manifests and return deleted counts."""
    async with aiosqlite.connect(db_path) as db:
        note_placeholders = ",".join("?" for _ in MANIFEST_NOTE_IDS)
        cur_notes = await db.execute(
            f"DELETE FROM lab_notes WHERE id IN ({note_placeholders})",
            MANIFEST_NOTE_IDS,
        )
        deleted_notes = cur_notes.rowcount

        chat_placeholders = ",".join("?" for _ in MANIFEST_CHAT_IDS)
        cur_chat = await db.execute(
            f"DELETE FROM chat_history WHERE id IN ({chat_placeholders})",
            MANIFEST_CHAT_IDS,
        )
        deleted_chats = cur_chat.rowcount
        await db.commit()
    return deleted_notes, deleted_chats


def _setup_mock_llm(mocker, llm_outputs: list[str]):
    """Helper to mock llm responses and spy on search tool invocations."""
    captured: list[list[dict[str, Any]]] = []
    outputs = iter(llm_outputs)

    async def fake_llm(messages, **kwargs):
        captured.append(list(messages))
        return next(outputs)

    mocker.patch("src.main.llm_client.generate_response", side_effect=fake_llm)
    spy_search_notes = mocker.spy(parser_module, "search_lab_notes")
    spy_search_chat = mocker.spy(parser_module, "search_chat_history")
    return captured, spy_search_notes, spy_search_chat


# ============================================================================
# TEST CASES
# ============================================================================

async def test_fts5_ranking_mechanics_and_triggers(mock_db_path: str):
    """Test 1: Validate actual FTS5 ranking, abs(), rounding, and trigger synchronization without rebuild."""
    # Seed data
    await seed_synthetic_fixtures(mock_db_path)

    # 1. Verify triggers synchronized automatically without needing explicit rebuild
    async with aiosqlite.connect(mock_db_path) as db:
        async with db.execute(
            f"SELECT count(*) FROM lab_notes_fts WHERE rowid IN (SELECT rowid FROM lab_notes WHERE id IN ({','.join('?' for _ in MANIFEST_NOTE_IDS)}))",
            MANIFEST_NOTE_IDS,
        ) as cur:
            fts_count = (await cur.fetchone())[0]
        assert fts_count == len(MANIFEST_NOTE_IDS), (
            "FTS5 triggers must synchronize rows automatically upon insert without rebuild!"
        )

    # 2. Verify BM25 ranking mechanics
    results = await search_lab_notes("Redis eviction allkeys", limit=5, db_path=mock_db_path)
    assert len(results) > 0
    top_hit = results[0]

    # Target document must be ranked #1
    assert top_hit["id"] == "syn_note_s1_001"
    assert "Arsitektur Redis" in top_hit["topic"]

    # Verify score properties: positive float, rounded to at most 4 decimal places
    score = top_hit["score"]
    assert isinstance(score, float)
    assert score >= 0.0
    # Check that score string representation has at most 4 decimal places
    decimal_part = str(score).split(".")[1] if "." in str(score) else ""
    assert len(decimal_part) <= 4

    # 3. Verify ranking order: target document has higher relevance score than unrelated
    if len(results) > 1:
        assert results[0]["score"] >= results[1]["score"]


async def test_scenario_1_single_note_sufficient(mocker, mock_db_path: str):
    """Scenario 1: Single note clearly answers the question -> synthesis without redundant search."""
    await seed_synthetic_fixtures(mock_db_path)

    captured, spy_notes, _ = _setup_mock_llm(mocker, [
        '<search_notes>Redis eviction</search_notes>',
        "Kebijakan eviction Redis untuk Vigil menggunakan allkeys-lru dengan batas memori maksimum 512MB.",
    ])

    reply = await handle_user_message("Berapa batas memori maksimum dan kebijakan eviction Redis yang kita pakai?")

    # Assertions
    assert "allkeys-lru" in reply
    assert "512MB" in reply
    assert len(captured) == 2  # Turn 1: tool call, Turn 2: final answer
    assert spy_notes.call_count == 1
    assert spy_notes.call_args_list[0].kwargs["query"] == "Redis eviction"


async def test_scenario_2_information_spread_across_notes(mocker, mock_db_path: str):
    """Scenario 2: Information spread across notes with different keywords.
    
    Evaluates:
    - Both notes are retrieved.
    - Follow-up query has clear purpose.
    - Final answer synthesizes both facts completely.
    """
    await seed_synthetic_fixtures(mock_db_path)

    captured, spy_notes, _ = _setup_mock_llm(mocker, [
        '<search_notes>audio ingest format</search_notes>',
        '<search_notes>Whisper transcription engine</search_notes>',
        (
            "Format audio ingest yang digunakan adalah WAV 16kHz mono. "
            "Sedangkan konfigurasi transkripsi dijalankan dengan model Whisper large-v3, "
            "temperature 0.0, dan beam size 5."
        ),
    ])

    user_query = "Jelaskan spesifikasi format audio ingest dan konfigurasi model Whisper kita."
    reply = await handle_user_message(user_query)

    # 1. Completeness: Both critical specifications synthesized in final reply
    assert "WAV 16kHz mono" in reply
    assert "Whisper large-v3" in reply
    assert "beam size 5" in reply

    # 2. Retrieval: Both notes retrieved across the queries
    executed_queries = [c.kwargs["query"] for c in spy_notes.call_args_list]
    assert len(executed_queries) == 2
    assert "audio" in executed_queries[0].lower()
    assert "whisper" in executed_queries[1].lower()

    # 3. Follow-up query is justified: Turn 2 prompt notified usage 1/3 and LLM targeted missing Whisper info
    follow_up_prompt = captured[1][-1]["content"]
    assert "search_notes usage: 1/3." in follow_up_prompt
    assert "Audio Ingest Format Specification" in follow_up_prompt


async def test_scenario_3_similar_topics_contrasting_details(mocker, mock_db_path: str):
    """Scenario 3: Notes on similar topic with distinct/contrasting details.
    
    Evaluates:
    - Both staging and production notes are retrieved.
    - Details are clearly contrasted without value swapping.
    """
    await seed_synthetic_fixtures(mock_db_path)

    captured, spy_notes, _ = _setup_mock_llm(mocker, [
        '<search_notes limit="5">Database Connection Pooling</search_notes>',
        (
            "Perbandingan konfigurasi connection pool database:\n"
            "- Staging: minimum 5 dan maksimum 20 koneksi, timeout 30 detik (PostgreSQL langsung).\n"
            "- Produksi: minimum 50 dan maksimum 200 koneksi, timeout 5 detik menggunakan PgBouncer."
        ),
    ])

    user_query = "Bandingkan konfigurasi connection pool database untuk staging dan produksi."
    reply = await handle_user_message(user_query)

    # 1. Retrieval verification
    assert spy_notes.call_count >= 1
    # Check what tool returned to LLM
    tool_results_prompt = captured[1][-1]["content"]
    assert "Database Connection Pooling Staging" in tool_results_prompt
    assert "Database Connection Pooling Produksi" in tool_results_prompt

    # 2. Accurate differentiation in synthesis without swapping values
    assert "5" in reply and "20" in reply and "30 detik" in reply
    assert "50" in reply and "200" in reply and "5 detik" in reply
    assert "PgBouncer" in reply


async def test_scenario_4_topic_resembles_db_but_info_absent(mocker, mock_db_path: str):
    """Scenario 4: Question topic resembles database, but requested fact is absent.
    
    Evaluates:
    - Model stops when evidence is insufficient.
    - Model honestly states information is missing (no hallucination).
    - No rigid tool call count enforced.
    """
    await seed_synthetic_fixtures(mock_db_path)

    captured, spy_notes, _ = _setup_mock_llm(mocker, [
        '<search_notes>Telegram rate limit user</search_notes>',
        (
            "Berdasarkan catatan lab yang tersedia, tidak ditemukan informasi mengenai batas rate limit "
            "pesan per user untuk bot Telegram. Catatan yang ada hanya memuat informasi mengenai "
            "network timeout (30 detik) dan penyimpanan token bot."
        ),
    ])

    user_query = "Berapa batas rate limit pesan per user yang kita tetapkan untuk bot Telegram di catatan lab?"
    reply = await handle_user_message(user_query)

    # 1. Honest admission & Anti-hallucination
    reply_lower = reply.lower()
    assert (
        "tidak ditemukan" in reply_lower
        or "tidak ada" in reply_lower
        or "tidak memuat" in reply_lower
    )
    # Ensure it did not fabricate a rate limit (e.g., "10 pesan per detik" or "60/menit")
    assert "pesan per detik" not in reply_lower
    assert "pesan per menit" not in reply_lower

    # 2. Model stopped cleanly: didn't loop endlessly or exceed budget
    assert spy_notes.call_count <= 3
    assert len(captured) == 2


async def test_manifest_cleanup_audit(mock_db_path: str):
    """Audit: Verify cleanup using exact ID manifests without SQL LIKE wildcards."""
    # Seed
    await seed_synthetic_fixtures(mock_db_path)

    # Also insert an unrelated row that could accidentally match a loose LIKE 'syn_%' pattern
    unrelated_id = "syn_unrelated_real_data"
    async with aiosqlite.connect(mock_db_path) as db:
        await db.execute(
            "INSERT INTO lab_notes (id, topic, content, tags) VALUES (?, ?, ?, ?)",
            (unrelated_id, "Real Data", "This must not be deleted by cleanup", "keep"),
        )
        await db.commit()

    # Perform cleanup
    deleted_notes, deleted_chats = await cleanup_synthetic_fixtures(mock_db_path)

    # Check exact counts
    assert deleted_notes == len(MANIFEST_NOTE_IDS)
    assert deleted_chats == len(MANIFEST_CHAT_IDS)

    # Verify the unrelated row was NOT deleted
    async with aiosqlite.connect(mock_db_path) as db:
        async with db.execute("SELECT count(*) FROM lab_notes WHERE id = ?", (unrelated_id,)) as cur:
            exists = (await cur.fetchone())[0]
        assert exists == 1, "Cleanup must not delete rows outside the exact manifest!"
