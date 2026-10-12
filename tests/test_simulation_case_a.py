"""Simulation Test: Kasus A - Penyelesaian Tugas Jatuh Tempo & Uji Task ID.

Demonstrates and verifies:
1. Skenario Sukses:
   - User reports completing the overdue task: "Bro, gue barusan udah bayar tagihan server AWS kok, aman."
   - Prompt contains exact task ID: [tsk_aws_88] in both snapshot and pending nudge.
   - Model reads Directive Rule 2: does NOT nag, calls <update_task id="tsk_aws_88" status="done">.
   - Database status changes from 'pending' to 'done'.
   - Clean reply returned to user without raw XML.

2. Skenario Gagal Tanpa Task ID (The Old Bug):
   - Demonstrates what would happen if task ID was missing:
     a) Missing attribute id -> rejected by parser with "Missing task id attribute"
     b) Hallucinated id -> update_task fails because ID does not exist in DB.
"""

import pytest
import aiosqlite
from src.config import config
from src.storage import repository
from src.core.traffic import traffic_controller
from src.tools.parser import parse_and_execute_tools
from src.main import handle_user_message


@pytest.mark.asyncio
async def test_simulate_case_a_task_completion_and_id_verification(mocker, mock_db_path):
    print("\n" + "="*80)
    print("[SIMULASI KASUS A] PENYELESAIAN TUGAS JATUH TEMPO & UJI TASK ID")
    print("="*80)

    # 1. SETUP TUGAS DI DATABASE
    print("\n[TAHAP 1: SEEDING DATABASE DENGAN TUGAS PENDING]")
    task_id = "tsk_aws_88"
    async with aiosqlite.connect(mock_db_path) as db:
        await db.execute(
            """INSERT INTO tasks (id, title, due_time, status)
               VALUES (?, ?, ?, 'pending')""",
            (task_id, "Bayar tagihan server AWS produksi", "14:00")
        )
        await db.commit()
    print(f"[OK] Tugas terdaftar: [{task_id}] 'Bayar tagihan server AWS produksi' (Status: PENDING)")

    # 2. SIMULASI NUDGE MASUK KE QUEUE TRAFFIC CONTROLLER
    print("\n[TAHAP 2: SIMULASI NUDGE TERTAHAN DI QUEUE]")
    await traffic_controller.record_interaction()
    ticket = {
        "id": task_id,
        "title": "Bayar tagihan server AWS produksi",
        "due_time": "14:00",
        "type": "due_task",
    }
    await traffic_controller.evaluate_nudge(ticket)
    print(f"[OK] Tiket [{task_id}] tersimpan di pending_nudges queue.")

    # 3. USER INPUT: Pengguna melaporkan tugas sudah beres
    user_query = "Bro, gue barusan udah bayar tagihan server AWS kok, aman."
    print(f"\n[TAHAP 3: USER INPUT]")
    print(f"User: \"{user_query}\"")

    # 4. CAPTURE PROMPT & SIMULASIKAN RESPON MODEL DENGAN TOOL CALL YANG VALID
    captured_messages = []

    # Respon yang diharapkan dari model (memanfaatkan Task ID nyata dari prompt):
    valid_llm_response = (
        '<update_task id="tsk_aws_88" status="done">User konfirmasi sudah bayar tagihan server AWS</update_task>\n'
        "Baguslah kalau sudah dibereskan. Status tugas bayar tagihan AWS sudah gue update jadi selesai di sistem. "
        "Pertahankan disiplin eksekusi seperti ini."
    )

    async def mock_generate(messages, **kwargs):
        captured_messages.append(list(messages))
        return valid_llm_response

    mocker.patch("src.main.llm_client.generate_response", side_effect=mock_generate)

    # 5. JALANKAN PIPELINE HANDLE_USER_MESSAGE
    final_reply = await handle_user_message(user_query)

    # 6. INSPEKSI PROMPT YANG DIKIRIM KE MODEL
    print("\n[TAHAP 4: INSPEKSI TASK ID DI DALAM PROMPT]")
    sent_prompt = captured_messages[0][0]["content"]

    # Pastikan task ID muncul di Snapshot Tugas Aktif
    assert f"[{task_id}]" in sent_prompt
    print(f"[PASS] 1. Task ID [{task_id}] terbukti hadir di bagian Snapshot Tugas Aktif & Nudge:")
    for line in sent_prompt.splitlines():
        if task_id in line:
            print(f"       -> {line.strip()}")

    # 7. VERIFIKASI EKSEKUSI TOOL & PERUBAHAN STATUS DI DATABASE
    print("\n[TAHAP 5: VERIFIKASI STATUS TUGAS DI DATABASE SQLITE]")
    async with aiosqlite.connect(mock_db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT id, title, status FROM tasks WHERE id = ?", (task_id,))
        task_row = await cursor.fetchone()
        assert task_row is not None
        assert task_row["status"] == "done", f"Status tugas harus 'done', tapi bernilai '{task_row['status']}'"
    print(f"[PASS] 2. Status tugas [{task_id}] di SQLite BERHASIL BERUBAH menjadi 'DONE'.")

    # 8. VERIFIKASI OUTPUT BERSIH KE TELEGRAM PENGGUNA
    print("\n[TAHAP 6: VERIFIKASI OUTPUT USER]")
    print(f"Jawaban Bersih ke Telegram:\n{final_reply}")
    assert "<update_task" not in final_reply
    assert "</update_task>" not in final_reply
    assert "status tugas bayar tagihan aws sudah gue update" in final_reply.lower()
    print("[PASS] 3. Jawaban bersih dari tag XML mentah, dan memuat konfirmasi penyelesaian.")

    # 9. ANALISIS KASUS GAGAL TANPA TASK ID (THE OLD BUG)
    print("\n" + "-"*80)
    print("[SIMULASI PERBANDINGAN: APA JADINYA JIKA TASK ID HILANG? (THE OLD BUG)]")
    print("-"*80)

    # Kasus Gagal A: Model memanggil tanpa atribut id karena prompt tidak menyediakannya
    bad_call_no_id = '<update_task status="done">Selesai bayar AWS</update_task>Udah selesai ya bro!'
    cleaned_a, actions_a = await parse_and_execute_tools(bad_call_no_id, db_path=mock_db_path)
    print("\n[KASUS GAGAL A: UPDATE_TASK TANPA ID]")
    print(f"Tool Input: '{bad_call_no_id}'")
    print(f"Hasil Log : {actions_a[0]}")
    assert actions_a[0]["status"] == "error"
    assert "Missing task id attribute" in actions_a[0]["error"]
    print("[BUKTI] Parser menolak eksekusi karena id kosong: 'Missing task id attribute'.")

    # Kasus Gagal B: Model berhalusinasi mengarang ID (misal: tsk_aws_palsu)
    bad_call_fake_id = '<update_task id="tsk_aws_palsu" status="done">Selesai bayar AWS</update_task>Udah selesai ya bro!'
    cleaned_b, actions_b = await parse_and_execute_tools(bad_call_fake_id, db_path=mock_db_path)
    print("\n[KASUS GAGAL B: MODEL MENGARANG TASK ID PALSU]")
    print(f"Tool Input: '{bad_call_fake_id}'")
    print(f"Hasil Log : {actions_b[0]}")
    assert actions_b[0]["status"] == "failed"
    assert actions_b[0]["updated"] is False
    print("[BUKTI] Database gagal update karena ID 'tsk_aws_palsu' tidak ditemukan di tabel tasks.")

    print("\n" + "="*80)
    print("[SIMULASI KASUS A SELESAI]: KASUS SUKSES & SEMUA BENTUK KEGAGALAN TERBUKTI!")
    print("="*80 + "\n")
