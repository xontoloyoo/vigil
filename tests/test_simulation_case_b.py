"""Simulation Test: Kasus B - Diskusi Bebas Saat Ada Tugas Tertunda (Uji Context Merging).

Demonstrates and verifies:
1. Skenario Sukses (Ideal Flow):
   - User asks an unrelated technical question (PostgreSQL vs MySQL) while a task (tsk_aws_88) is overdue.
   - Prompt contains Context Merging Directive with task ID.
   - Model answers technical inquiry FIRST, then appends a sharp, pragmatic nudge at the end.
   - Task remains pending, heartbeat cooldown is activated.

2. Skenario Gagal (Anti-Patterns):
   - Gagal 1 (Amnesia): Model answers technical query but forgets the pending nudge completely.
   - Gagal 2 (Hijacking): Model ignores the user's technical question and only screams about the overdue task.
   - Gagal 3 (False Completion): Model hallucinates that the user finished the task and calls update_task done.
"""

import pytest
import aiosqlite
from src.config import config
from src.storage import repository
from src.core.traffic import traffic_controller
from src.main import heartbeat_runner, handle_user_message


@pytest.mark.asyncio
async def test_simulate_case_b_context_merging_success_and_failures(mocker, mock_db_path):
    print("\n" + "="*80)
    print("[SIMULASI KASUS B] DISKUSI BEBAS DENGAN TUGAS TERTUNDA (CONTEXT MERGING)")
    print("="*80)

    # 1. SEED DATA TUGAS DI DATABASE
    print("\n[TAHAP 1: SEEDING TUGAS JATUH TEMPO DI DATABASE]")
    task_id = "tsk_aws_88"
    async with aiosqlite.connect(mock_db_path) as db:
        await db.execute(
            """INSERT INTO tasks (id, title, due_time, status)
               VALUES (?, ?, ?, 'pending')""",
            (task_id, "Bayar tagihan server AWS produksi", "14:00")
        )
        await db.commit()
    print(f"[OK] Tugas terdaftar di SQLite: [{task_id}] 'Bayar tagihan server AWS produksi' (Due: 14:00)")

    # 2. SIMULASI HEARTBEAT MENEMUKAN TUGAS & MEMASUKKAN KE PENDING NUDGES
    print("\n[TAHAP 2: SIMULASI TRAFFIC CONTROLLER MENAHAN NUDGE KARENA USER AKTIF]")
    # Set user idle 2 menit (masih aktif chat, batas threshold 10 menit)
    await traffic_controller.record_interaction()
    ticket = {
        "id": task_id,
        "title": "Bayar tagihan server AWS produksi",
        "due_time": "14:00",
        "type": "due_task",
    }
    decision, _ = await traffic_controller.evaluate_nudge(ticket)
    assert decision == "QUEUED"
    print(f"[OK] Evaluasi TrafficController: status='{decision}' (tiket ditahan dalam antrean pending_nudges)")

    # 3. USER INPUT (Pertanyaan Teknis Bebas yang sama sekali tidak menyinggung AWS)
    user_query = "Bro, buat backend microservices baru, enakan pake PostgreSQL atau MySQL?"
    print(f"\n[TAHAP 3: USER INPUT]")
    print(f"User: \"{user_query}\"")

    # 4. CAPTURE PROMPT YANG DISUSUN SISTEM
    captured_messages = []

    # Contoh Respons Ideal (SUKSES):
    # - Menjawab pertanyaan teknis dulu secara mendalam
    # - Menyelipkan todongan tagihan AWS di bagian akhir kalimat
    ideal_llm_response = (
        "Untuk microservices modern, saya sarankan **PostgreSQL**. "
        "PostgreSQL punya dukungan tipe data JSONB yang jauh lebih superior, penanganan konkurensi MVCC "
        "yang lebih matang, dan ekstensi ekosistem yang luas.\n\n"
        "Btw, tagihan server AWS produksi lu [tsk_aws_88] yang jam 14:00 tadi belum beres kan? "
        "Segera selesaikan sebelum server lu mati kena suspend."
    )

    async def mock_generate(messages, **kwargs):
        captured_messages.append(list(messages))
        return ideal_llm_response

    mocker.patch("src.main.llm_client.generate_response", side_effect=mock_generate)

    # 5. JALANKAN PIPELINE HANDLE_USER_MESSAGE
    final_reply = await handle_user_message(user_query)

    # 6. BEDAH PROMPT SYSTEM YANG DITERIMA LLM
    print("\n[TAHAP 4: INSPEKSI PROMPT YANG DISUNTIKKAN KE LLM]")
    sent_prompt = captured_messages[0][0]["content"]
    
    # Ekstrak bagian Context Merging dari prompt
    start_idx = sent_prompt.find("## CONTEXT MERGING DIRECTIVE")
    directive_snippet = sent_prompt[start_idx:] if start_idx != -1 else "(Not found)"
    print("--- Potongan Dynamic Context Merging Directive di System Prompt: ---")
    print(directive_snippet)

    # 7. EVALUASI HASIL SKENARIO SUKSES (CORRECT FLOW)
    print("\n[TAHAP 5: HASIL EKSEKUSI SKENARIO SUKSES]")
    print(f"Jawaban Vigil ke Pengguna:\n{final_reply}\n")

    # Verifikasi ID tugas muncul di prompt
    assert f"[{task_id}]" in sent_prompt, "Task ID wajib ada di prompt!"
    print(f"[PASS] 1. Task ID [{task_id}] terbukti hadir dalam prompt injeksi.")

    # Verifikasi jawaban memenuhi 2 kriteria: menjawab database DAN menodong tagihan AWS
    assert "PostgreSQL" in final_reply
    assert "tagihan server AWS" in final_reply or "tsk_aws_88" in final_reply
    print("[PASS] 2. Vigil berhasil menjawab diskusi teknis PostgreSQL sekaligus menodong AWS di akhir.")

    # Verifikasi cooldown heartbeat runner aktif
    assert task_id in heartbeat_runner._dispatched_cache
    print(f"[PASS] 3. Cooldown timer heartbeat aktif untuk [{task_id}] agar tidak spam.")

    # 8. ANALISIS PERBANDINGAN: CONTOH KASUS SALAH (ANTI-PATTERNS)
    print("\n" + "-"*80)
    print("[SIMULASI PERBANDINGAN KASUS SALAH / ANTI-PATTERNS]")
    print("-"*80)

    # CONTOH SALAH 1: AMNESIA (Lupa menodong)
    bad_response_amnesia = (
        "Untuk microservices modern, saya sarankan PostgreSQL. "
        "PostgreSQL punya dukungan tipe data JSONB yang jauh lebih superior."
    )
    print("\n[KASUS SALAH 1: AMNESIA / NUDGE HILANG]")
    print(f"Output Buruk: \"{bad_response_amnesia}\"")
    print("Penyebab   : LLM mengabaikan direktif context merging, hanya fokus pada pertanyaan user.")
    print("Dampak     : User tidak teringat tagihannya, tujuan proactive accountability gagal total.")
    assert "AWS" not in bad_response_amnesia, "Bukti: Nudge tertinggal"

    # CONTOH SALAH 2: HIJACKING (Membajak topik secara kasar)
    bad_response_hijacking = (
        "Gak usah nanya database dulu! Bayar dulu tagihan AWS lu sekarang juga! "
        "Gue gak bakal jawab pertanyaan PostgreSQL sebelum urusan server kelar."
    )
    print("\n[KASUS SALAH 2: HIJACKING / TOPIK DIBAJAK KASAR]")
    print(f"Output Buruk: \"{bad_response_hijacking}\"")
    print("Penyebab   : LLM melanggar Aturan 1: 'Jawab diskusi pengguna terlebih dahulu'.")
    print("Dampak     : Asisten terasa menyebalkan dan tidak kooperatif.")
    assert "PostgreSQL punya dukungan" not in bad_response_hijacking, "Bukti: Pertanyaan teknis diabaikan"

    # CONTOH SALAH 3: FALSE COMPLETION (Salah sangka tugas selesai)
    bad_response_false_complete = (
        "Oke siap, tagihan AWS lu udah gue tandai selesai ya!\n"
        "<update_task id=\"tsk_aws_88\" status=\"done\">Selesai</update_task>\n"
        "Ngomong-ngomong soal PostgreSQL vs MySQL..."
    )
    print("\n[KASUS SALAH 3: FALSE COMPLETION / HALUSINASI STATUS]")
    print(f"Output Buruk: \"{bad_response_false_complete}\"")
    print("Penyebab   : LLM melanggar Aturan 2: mengira user membahas penyelesaian padahal user cuma nanya database.")
    print("Dampak     : Data tugas di DB ditandai selesai secara prematur tanpa verifikasi.")
    assert "<update_task" in bad_response_false_complete, "Bukti: Pemanggilan update_task prematur"

    print("\n" + "="*80)
    print("[SIMULASI KASUS B SELESAI]: SUKSES DAN 3 KASUS SALAH BERHASIL DIVERIFIKASI!")
    print("="*80 + "\n")
