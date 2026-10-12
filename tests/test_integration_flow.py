import pytest
import aiosqlite
from unittest.mock import AsyncMock

from src.main import handle_user_message
from src.interfaces.telegram import telegram_interface
from src.storage import repository

pytestmark = pytest.mark.asyncio

async def test_happy_path_no_tool(mocker):
    """Test 1: Happy path — no tool call"""
    # Patch LLM Call
    mock_llm = mocker.patch("src.main.llm_client.generate_response", return_value="Halo, ada yang bisa dibantu?")
    
    # Spies
    import src.main
    spy_record = mocker.spy(telegram_interface.traffic_ctrl, "record_interaction")
    spy_get_history = mocker.spy(src.main, "get_recent_chat_history")
    
    spy_save_chat = mocker.spy(src.main, "save_chat_message")
    spy_prune = mocker.spy(src.main, "prune_chat_history")
    spy_parse = mocker.spy(src.main, "parse_and_execute_tools")

    result = await handle_user_message("Halo")

    assert result == "Halo, ada yang bisa dibantu?"
    
    # Asserts
    spy_record.assert_not_called()
    spy_get_history.assert_called_once_with(limit=8)
    
    # FTS5 search NO LONGER called passively in main.py (now On-Demand via tool calling)
    mock_llm.assert_called_once()
    spy_parse.assert_called_once()
    
    assert spy_save_chat.call_count == 2
    spy_save_chat.assert_any_call("user", "Halo")
    spy_save_chat.assert_any_call("assistant", "Halo, ada yang bisa dibantu?")
    
    spy_prune.assert_called_once_with(keep_latest=500)


async def test_write_tool_only(mocker, mock_db_path):
    """Test 2: Write tool saja (create_task)"""
    mock_llm = mocker.patch(
        "src.main.llm_client.generate_response", 
        return_value='Oke saya catat. <create_task due="2026-10-10T10:00:00">Task Baru</create_task>'
    )
    import src.main
    spy_save_chat = mocker.spy(src.main, "save_chat_message")
    spy_parse = mocker.spy(src.main, "parse_and_execute_tools")
    
    result = await handle_user_message("Tolong catat task baru")
    
    assert mock_llm.call_count == 1
    assert spy_parse.call_count == 1
    assert result == "Oke saya catat."
    spy_save_chat.assert_any_call("assistant", "Oke saya catat.")
    
    async with aiosqlite.connect(mock_db_path) as db:
        cursor = await db.execute("SELECT title FROM tasks WHERE title = 'Task Baru'")
        row = await cursor.fetchone()
        assert row is not None


async def test_read_tool_triggers_call_2(mocker, mock_db_path):
    """Test 3: Read tool -> trigger Call #2"""
    mock_llm = mocker.patch(
        "src.main.llm_client.generate_response", 
        side_effect=[
            'Sebentar saya cek. <query_tasks status="all" limit="10"></query_tasks>',
            'Ada 2 task aktif di sistem.'
        ]
    )
    import src.main
    spy_parse = mocker.spy(src.main, "parse_and_execute_tools")
    spy_save_chat = mocker.spy(src.main, "save_chat_message")
    
    result = await handle_user_message("Cek task")
    
    assert mock_llm.call_count == 2
    assert spy_parse.call_count == 2
    
    call2_args = spy_parse.call_args_list[1]
    assert call2_args.kwargs.get("allow_read_tools") is False
    
    assert result == "Ada 2 task aktif di sistem."
    spy_save_chat.assert_any_call("assistant", "Ada 2 task aktif di sistem.")
    
    assert "<query_tasks" not in result


async def test_fallback_generic_only_xml(mocker, mock_db_path):
    """Test 4: Fallback generik — hanya tag XML"""
    mock_llm = mocker.patch(
        "src.main.llm_client.generate_response", 
        return_value='<create_task>Test Task 1</create_task>'
    )
    import src.main
    spy_save_chat = mocker.spy(src.main, "save_chat_message")
    
    result = await handle_user_message("Buatkan task dong")
    
    assert result == "\u2705 Aksi telah dieksekusi."
    
    for call in spy_save_chat.call_args_list:
        assert call.args[0] != "assistant"
        
    spy_save_chat.assert_any_call("user", "Buatkan task dong")
    
    async with aiosqlite.connect(mock_db_path) as db:
        cursor = await db.execute("SELECT title FROM tasks WHERE title = 'Test Task 1'")
        row = await cursor.fetchone()
        assert row is not None


async def test_fallback_generic_whitespace_xml(mocker, mock_db_path):
    """Test 5: Fallback generik — whitespace + tag"""
    mock_llm = mocker.patch(
        "src.main.llm_client.generate_response", 
        return_value='   \n<create_task>Test Task 2</create_task>\n   '
    )
    import src.main
    spy_save_chat = mocker.spy(src.main, "save_chat_message")
    
    result = await handle_user_message("Satu lagi task")
    
    assert result == "\u2705 Aksi telah dieksekusi."
    
    for call in spy_save_chat.call_args_list:
        assert call.args[0] != "assistant"


async def test_call_order(mocker):
    """Test 6: Urutan pemanggilan (call order)"""
    from src.core.traffic import traffic_controller
    import src.main
    
    manager = mocker.Mock()
    
    # Attach spies instead of full patches to verify the real order, but we can patch to make it simple
    manager.attach_mock(mocker.patch.object(traffic_controller, "consume_pending_nudges", new_callable=AsyncMock, return_value=[]), "consume_pending_nudges")
    manager.attach_mock(mocker.patch("src.main.repository.get_pending_tasks", new_callable=AsyncMock, return_value=[]), "get_pending_tasks")
    manager.attach_mock(mocker.patch("src.main.get_recent_chat_history", new_callable=AsyncMock, return_value=[]), "get_recent_chat_history")
    manager.attach_mock(mocker.patch("src.main.llm_client.generate_response", new_callable=AsyncMock, return_value="Teks"), "generate_response")
    manager.attach_mock(mocker.patch("src.main.parse_and_execute_tools", new_callable=AsyncMock, return_value=("Teks", [])), "parse_and_execute_tools")
    manager.attach_mock(mocker.patch("src.main.save_chat_message", new_callable=AsyncMock), "save_chat_message")
    manager.attach_mock(mocker.patch("src.main.prune_chat_history", new_callable=AsyncMock, return_value=0), "prune_chat_history")
    
    await handle_user_message("Test order")
    
    calls = manager.mock_calls
    call_names = [c[0] for c in calls] # c[0] is the name of the mock method called
    
    # No more passive FTS5 search in main pipeline — search is now On-Demand via tool calling
    expected_order = [
        "consume_pending_nudges",
        "get_pending_tasks",
        "get_recent_chat_history",
        "generate_response",
        "parse_and_execute_tools",
        "save_chat_message",  # user
        "save_chat_message",  # assistant
        "prune_chat_history"
    ]
    
    assert call_names == expected_order


async def test_prune_actually_works(mocker, mock_db_path):
    """Test 7: Prune benar-benar jalan"""
    mock_llm = mocker.patch("src.main.llm_client.generate_response", return_value="Hello")
    
    from src.storage.database import generate_id
    async with aiosqlite.connect(mock_db_path) as db:
        for i in range(550):
            await db.execute("INSERT INTO chat_history (id, role, content) VALUES (?, 'user', 'junk')", (generate_id(f"test_{i}"),))
        await db.commit()
    
    await handle_user_message("Test prune")
    
    async with aiosqlite.connect(mock_db_path) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM chat_history")
        count = (await cursor.fetchone())[0]
        
    assert count <= 502


async def test_record_interaction_via_telegram(mocker):
    """Test 8: record_interaction via telegram.py"""
    from telegram import Update, User, Message, Chat
    from telegram.constants import ParseMode
    from telegram.ext import ContextTypes
    
    # Patch the allowed user ID on the existing singleton
    telegram_interface.allowed_user_id = 123456
    
    update = mocker.AsyncMock(spec=Update)
    update.effective_user = mocker.Mock(spec=User)
    update.effective_user.id = 123456
    update.effective_chat = mocker.Mock(spec=Chat)
    update.effective_chat.id = 999
    update.message = mocker.AsyncMock(spec=Message)
    update.message.text = "Hello via telegram"
    
    context = mocker.AsyncMock(spec=ContextTypes.DEFAULT_TYPE)
    
    spy_record = mocker.spy(telegram_interface.traffic_ctrl, "record_interaction")
    
    handler_mock = mocker.AsyncMock(return_value="Balasan LLM")
    telegram_interface.register_message_handler(handler_mock)
    
    await telegram_interface._handle_message(update, context)
    
    spy_record.assert_called_once()
    update.message.reply_text.assert_called_once_with("Balasan LLM", parse_mode=ParseMode.HTML)
    
    spy_record.reset_mock()
    update.message.reply_text.reset_mock()
    update.effective_user.id = 999999 # Unauthorized ID
    
    await telegram_interface._handle_message(update, context)
    
    spy_record.assert_not_called()
    update.message.reply_text.assert_not_called()


async def test_fts5_chat_dedup_removes_overlap(mocker, mock_db_path):
    """Test 9: search_chat tool dedup — messages already in recent 8 are filtered out.

    conftest seeds 10 chat_history rows: 'Pesan dummy ke-1' .. 'Pesan dummy ke-10'.
    We insert a unique message, then simulate search_chat tool calling with
    recent_history_ids. The tool should filter it out from results.
    """
    import aiosqlite
    from src.storage.database import generate_id
    from src.tools.parser import parse_and_execute_tools

    unique_text = "Jerapah Ungu Menari"
    unique_id = generate_id("uniq1")
    async with aiosqlite.connect(mock_db_path) as db:
        await db.execute(
            "INSERT INTO chat_history (id, role, content) VALUES (?, 'user', ?)",
            (unique_id, unique_text)
        )
        await db.commit()

    # Simulate LLM emitting a search_chat tool call
    raw_response = '<search_chat limit="3">Jerapah</search_chat>'

    # Case 1: WITHOUT dedup ids — should find the message
    cleaned_text, actions = await parse_and_execute_tools(
        raw_response, db_path=mock_db_path, recent_history_ids=None
    )
    read_results_no_dedup = [
        a["read_result"] for a in actions
        if a.get("type") == "read" and a.get("read_result")
    ]
    assert any(unique_text in r for r in read_results_no_dedup), (
        "Without dedup, search_chat should find the unique message"
    )

    # Case 2: WITH dedup ids containing the unique message — should be filtered out
    cleaned_text2, actions2 = await parse_and_execute_tools(
        raw_response, db_path=mock_db_path, recent_history_ids={unique_id}
    )
    read_results_with_dedup = [
        a["read_result"] for a in actions2
        if a.get("type") == "read" and a.get("read_result")
    ]
    # The unique message should NOT appear in deduped results
    for r in read_results_with_dedup:
        assert unique_text not in r, (
            "With dedup, search_chat should NOT include the message already in recent history"
        )


async def test_traffic_controller_nudge_queue_deduplication(mock_db_path):
    """Test 10: evaluate_nudge avoids duplicate tickets when called multiple times while user is active."""
    from src.core.traffic import TrafficController

    ctrl = TrafficController(silence_threshold_minutes=10.0, db_path=mock_db_path)
    # Simulate recent user activity (active chat)
    await ctrl.record_interaction()

    ticket = {"id": "tsk_dedup_1", "title": "Bayar tagihan listrik", "due_time": "14:00"}

    # Evaluate multiple times across multiple heartbeat ticks
    status1, _ = await ctrl.evaluate_nudge(ticket)
    status2, _ = await ctrl.evaluate_nudge(ticket)
    status3, _ = await ctrl.evaluate_nudge(ticket)

    assert status1 == "QUEUED"
    assert status2 == "QUEUED"
    assert status3 == "QUEUED"

    # Consume pending nudges: must have exactly 1 ticket, not 3
    consumed = await ctrl.consume_pending_nudges()
    assert len(consumed) == 1
    assert consumed[0]["id"] == "tsk_dedup_1"


async def test_heartbeat_runner_mark_dispatched():
    """Test 11: HeartbeatRunner.mark_dispatched properly registers task in cooldown cache."""
    from src.engine.heartbeat import HeartbeatRunner
    from datetime import datetime, timezone

    runner = HeartbeatRunner()
    runner.mark_dispatched("tsk_cooldown_1")

    assert "tsk_cooldown_1" in runner._dispatched_cache
    assert isinstance(runner._dispatched_cache["tsk_cooldown_1"], datetime)


async def test_context_merging_prompt_directive():
    """Test 12: build_chat_prompt formats smart conditional rules for Context Merging."""
    from src.core.prompts import build_chat_prompt

    nudge_block = "[PENDING OVERDUE TASKS / NUDGES: - Bayar tagihan]"
    messages = build_chat_prompt(
        user_message="Halo bot",
        pending_nudge=nudge_block,
    )

    system_content = messages[0]["content"]
    assert "## CONTEXT MERGING DIRECTIVE" in system_content
    assert "Mandatory Rules:" in system_content
    assert "DO NOT nag the user" in system_content
    assert nudge_block in system_content


async def test_main_marks_heartbeat_dispatched_on_merged_nudges(mocker):
    """Test 13: main.py marks dispatched in heartbeat_runner when pending nudges are consumed."""
    from src.main import handle_user_message, traffic_controller, heartbeat_runner

    mocker.patch("src.main.llm_client.generate_response", return_value="Jawaban bot")
    mocker.patch.object(
        traffic_controller,
        "consume_pending_nudges",
        new_callable=mocker.AsyncMock,
        return_value=[{"id": "tsk_merged_99", "title": "Tugas penting"}],
    )
    spy_mark = mocker.spy(heartbeat_runner, "mark_dispatched")

    await handle_user_message("Pertanyaan user")

    spy_mark.assert_called_once_with("tsk_merged_99")


async def test_integration_stop_flow_cancels_and_preserves_db(mocker, mock_db_path):
    """Test 14: End-to-end /stop command cancels active LLM pipeline and leaves DB clean."""
    import asyncio
    from telegram import Update, User, Chat, Message
    from telegram.ext import ContextTypes
    from src.interfaces.telegram import TelegramInterface
    from src.core.traffic import TrafficController
    from src.main import handle_user_message, llm_client

    traffic_ctrl = TrafficController()
    interface = TelegramInterface(allowed_user_id=100, traffic_ctrl=traffic_ctrl)
    interface.register_message_handler(handle_user_message)

    started_llm = asyncio.Event()

    async def slow_llm_call(*args, **kwargs):
        started_llm.set()
        await asyncio.sleep(10.0)
        return "Jawaban yang tidak akan pernah sampai"

    mocker.patch.object(llm_client, "generate_response", side_effect=slow_llm_call)

    # 1. User sends message
    update_msg = mocker.Mock(spec=Update)
    update_msg.effective_user = mocker.Mock(spec=User)
    update_msg.effective_user.id = 100
    update_msg.effective_chat = mocker.Mock(spec=Chat)
    update_msg.effective_chat.id = 555
    update_msg.message = mocker.AsyncMock(spec=Message)
    update_msg.message.text = "Analisis mendalam laporan keuangan Q3"
    context_msg = mocker.AsyncMock(spec=ContextTypes.DEFAULT_TYPE)

    msg_task = asyncio.create_task(interface._handle_message(update_msg, context_msg))
    await started_llm.wait()

    # 2. User invokes /stop
    update_stop = mocker.Mock(spec=Update)
    update_stop.effective_user = mocker.Mock(spec=User)
    update_stop.effective_user.id = 100
    update_stop.message = mocker.AsyncMock(spec=Message)
    context_stop = mocker.AsyncMock(spec=ContextTypes.DEFAULT_TYPE)

    await interface._handle_stop(update_stop, context_stop)

    # Verify stop reply
    update_stop.message.reply_text.assert_called_once_with(
        "⏹️ Respon LLM berhasil dihentikan. Ketik /retry untuk mencoba lagi."
    )

    # Wait for cancelled task to finish unwinding
    await msg_task

    # Verify original message received no answer
    update_msg.message.reply_text.assert_not_called()

    # Verify database was NOT polluted with partial prompt
    async with aiosqlite.connect(mock_db_path) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM chat_history WHERE content LIKE '%keuangan Q3%'"
        ) as cursor:
            count = (await cursor.fetchone())[0]
            assert count == 0


async def test_integration_retry_flow_after_api_error(mocker, mock_db_path):
    """Test 15: End-to-end /retry flow after LLMError 429 recovers cleanly and saves to DB."""
    from telegram import Update, User, Chat, Message
    from telegram.constants import ParseMode
    from telegram.ext import ContextTypes
    from src.interfaces.telegram import TelegramInterface
    from src.core.traffic import TrafficController
    from src.core.llm import LLMError
    from src.main import handle_user_message, llm_client

    traffic_ctrl = TrafficController()
    interface = TelegramInterface(allowed_user_id=100, traffic_ctrl=traffic_ctrl)
    interface.register_message_handler(handle_user_message)

    # First call fails with 429 Rate Limit; second call succeeds
    mocker.patch.object(
        llm_client,
        "generate_response",
        side_effect=[
            LLMError("Too Many Requests", status_code=429),
            "Ini adalah ringkasan yang berhasil diambil.",
        ],
    )

    # 1. User sends message -> fails with 429
    update_msg = mocker.Mock(spec=Update)
    update_msg.effective_user = mocker.Mock(spec=User)
    update_msg.effective_user.id = 100
    update_msg.effective_chat = mocker.Mock(spec=Chat)
    update_msg.effective_chat.id = 555
    update_msg.message = mocker.AsyncMock(spec=Message)
    update_msg.message.text = "Tolong rangkumkan data penting"
    context_msg = mocker.AsyncMock(spec=ContextTypes.DEFAULT_TYPE)

    await interface._handle_message(update_msg, context_msg)

    # Verify concise error message with HTTP status code and /retry prompt
    update_msg.message.reply_text.assert_called_once_with(
        "⚠️ Gagal menghubungi LLM (HTTP 429: Rate Limit). Ketik /retry untuk mencoba lagi.",
        parse_mode=ParseMode.HTML,
    )

    # Verify database has 0 rows for this failed query
    async with aiosqlite.connect(mock_db_path) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM chat_history WHERE content = 'Tolong rangkumkan data penting'"
        ) as cursor:
            count = (await cursor.fetchone())[0]
            assert count == 0

    # 2. User invokes /retry
    update_retry = mocker.Mock(spec=Update)
    update_retry.effective_user = mocker.Mock(spec=User)
    update_retry.effective_user.id = 100
    update_retry.effective_chat = mocker.Mock(spec=Chat)
    update_retry.effective_chat.id = 555
    update_retry.message = mocker.AsyncMock(spec=Message)
    update_retry.message.text = "/retry"
    context_retry = mocker.AsyncMock(spec=ContextTypes.DEFAULT_TYPE)

    await interface._handle_retry(update_retry, context_retry)

    # Verify successful retry response sent
    update_retry.message.reply_text.assert_called_once_with(
        "Ini adalah ringkasan yang berhasil diambil.",
        parse_mode=ParseMode.HTML,
    )

    # Verify database now stores both user and assistant messages
    async with aiosqlite.connect(mock_db_path) as db:
        async with db.execute(
            "SELECT role, content FROM chat_history WHERE content LIKE '%Tolong rangkumkan data penting%' OR content LIKE '%ringkasan yang berhasil%'"
        ) as cursor:
            rows = await cursor.fetchall()
            assert len(rows) == 2
            assert rows[0][0] == "user"
            assert rows[1][0] == "assistant"

