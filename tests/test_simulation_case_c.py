"""Simulation Test: Kasus C - Pemanggilan Read Tool & Turn Loop Follow-Up.

Demonstrates and prints step-by-step runtime execution of:
1. Turn 1: User asks question -> LLM emits <search_notes>
2. Interception: System executes FTS5 query on lab_notes and formats via format_search_notes_result
3. Turn 2: System injects follow-up message via format_follow_up_user_message
4. Final: Clean response delivered to user, intermediate raw XML stripped.
"""

import pytest
import aiosqlite
from src.config import config
from src.storage import repository
from src.storage.database import generate_id
from src.core.context_formatters import format_search_notes_result, format_follow_up_user_message
from src.main import handle_user_message


@pytest.mark.asyncio
async def test_simulate_case_c_read_tool_and_turn_loop(mocker, mock_db_path):
    print("\n" + "="*80)
    print("[SIMULASI KASUS C] PEMANGGILAN READ TOOL & TURN LOOP FOLLOW-UP")
    print("="*80)

    # 1. SETUP DATA SEED: Catatan Riset / Lab Note di Database FTS5
    print("\n[TAHAP 1: SEEDING DATABASE]")
    note_id = generate_id("note_pg")
    async with aiosqlite.connect(mock_db_path) as db:
        await db.execute(
            """INSERT INTO lab_notes (id, topic, tags, content) 
               VALUES (?, ?, ?, ?)""",
            (
                note_id,
                "Optimasi Query & Indexing PostgreSQL",
                "database, postgresql, performance, indexing",
                "Gunakan B-Tree untuk equality/range query standar, GIN untuk full text search & array, dan BRIN untuk tabel time-series besar. Selalu cek EXPLAIN ANALYZE."
            )
        )
        await db.commit()
    print("[OK] Berhasil menambahkan Lab Note ke SQLite & FTS5 Index:")
    print("     Topic  : Optimasi Query & Indexing PostgreSQL")
    print("     Tags   : database, postgresql, performance, indexing")
    print("     Content: Gunakan B-Tree untuk equality/range...")

    # 2. USER INPUT
    user_query = "Dulu kita pernah catet soal optimasi query database gak?"
    print(f"\n[TAHAP 2: USER INPUT]")
    print(f"User: \"{user_query}\"")

    # 3. MOCK LLM RESPONSES (TURN 1 dan TURN 2)
    # Turn 1: LLM mendeteksi perlunya Read Tool dan mengeluarkan tag XML murni
    turn1_llm_response = '<search_notes limit="3">optimasi query database</search_notes>'
    
    # Turn 2: LLM menerima hasil data DB dan menyusun jawaban komprehensif
    turn2_llm_response = (
        "Ya, ada di catatan riset kita! Untuk optimasi query PostgreSQL:\n"
        "- Gunakan **B-Tree** untuk query range & equality standar.\n"
        "- Gunakan **GIN** untuk full-text search dan tipe data array.\n"
        "- Gunakan **BRIN** untuk tabel log/time-series berukuran masif.\n"
        "Rekomendasi teknis: selalu evaluasi rencana query dengan `EXPLAIN ANALYZE`."
    )

    captured_prompts = []

    async def mock_generate(messages, **kwargs):
        captured_prompts.append(list(messages))
        turn_num = len(captured_prompts)
        if turn_num == 1:
            print(f"\n[TAHAP 3: LLM TURN 1 - EVALUASI AWAL]")
            print(f"LLM mengeluarkan aksi Read Tool:")
            print(f"   {turn1_llm_response}")
            return turn1_llm_response
        elif turn_num == 2:
            print(f"\n[TAHAP 5: LLM TURN 2 - JAWABAN AKHIR DARI DATA FTS5]")
            print(f"LLM menerima data dan menghasilkan respons final.")
            return turn2_llm_response
        return "Fallback"

    mocker.patch("src.main.llm_client.generate_response", side_effect=mock_generate)

    # 4. RUN HANDLE_USER_MESSAGE PIPELINE
    final_reply = await handle_user_message(user_query)

    # 5. INSPECT TURN 2 INJECTION PROMPT
    assert len(captured_prompts) == 2, "Harus terjadi 2 putaran panggilan LLM (Turn 1 & Turn 2)"
    turn2_messages = captured_prompts[1]

    # Ambil 2 pesan terakhir pada turn 2 (intersepsi asisten & follow-up user)
    injected_assistant_msg = turn2_messages[-2]
    injected_user_follow_up = turn2_messages[-1]

    print(f"\n[TAHAP 4: INTERSEPSI SISTEM & INJEKSI DATA (TURN 2 PROMPT)]")
    print(f"--- Pesan Asisten (Turn 1 yang ditahan): ---")
    print(f"Role   : {injected_assistant_msg['role']}")
    print(f"Content: {injected_assistant_msg['content']}")
    
    print(f"\n--- Pesan Follow-Up Dinamis (Hasil Format Context Formatter): ---")
    print(f"Role   : {injected_user_follow_up['role']}")
    print(f"Content:\n{injected_user_follow_up['content']}")

    print(f"\n[TAHAP 6: FINAL USER-FACING OUTPUT]")
    print(f"Jawaban Bersih yang Diterima Pengguna di Telegram:\n{final_reply}")

    # 6. VERIFIKASI KEAMANAN & INTEGRITAS
    print(f"\n[TAHAP 7: VERIFIKASI ASSERTIONS]")
    # Verifikasi data dari DB FTS5 berhasil masuk ke prompt turn 2
    assert "Optimasi Query & Indexing PostgreSQL" in injected_user_follow_up['content']
    assert "Gunakan B-Tree" in injected_user_follow_up['content']
    print("[PASS] 1. Data catatan FTS5 SQLite berhasil di-query dan dimasukkan ke follow-up prompt.")

    # Verifikasi instruksi kuota multi-call search_notes aktif
    assert "search_notes usage: 1/3." in injected_user_follow_up['content']
    assert "DO NOT emit any read query tool tags." not in injected_user_follow_up['content']
    print("[PASS] 2. Direktif kuota multi-call 'search_notes usage: 1/3.' aktif.")

    # Verifikasi jawaban akhir bersih dari XML tag
    assert "<search_notes>" not in final_reply
    assert "</search_notes>" not in final_reply
    print("[PASS] 3. Jawaban akhir ke pengguna 100% bersih dari tag XML mentah.")

    # Verifikasi riwayat chat tersimpan dengan benar di SQLite
    async with aiosqlite.connect(mock_db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT role, content FROM chat_history WHERE role = 'assistant' ORDER BY rowid DESC LIMIT 1"
        )
        latest_row = await cursor.fetchone()
        assert latest_row is not None
        assert "B-Tree" in latest_row["content"] or "PostgreSQL" in latest_row["content"]
        assert "<search_notes>" not in latest_row["content"]
    print("[PASS] 4. chat_history SQLite hanya menyimpan jawaban akhir, bukan raw XML tool.")

    print("\n" + "="*80)
    print("[SIMULASI KASUS C SELESAI]: 100% BERHASIL SESUAI ARSITEKTUR!")
    print("="*80 + "\n")
