"""Unit tests for interactive Telegram commands (/task, /profil, /notes, /memory) and callbacks."""

import pytest
from unittest.mock import AsyncMock, Mock
from telegram import Update, User, Chat, Message, CallbackQuery
from telegram.ext import ContextTypes

from src.interfaces.commands import (
    _make_task_cmd,
    _make_task_callback,
    _make_profile_cmd,
    _make_profile_callback,
    _make_notes_cmd,
    _make_notes_callback,
    _make_memory_cmd,
    _make_memory_callback,
)
from src.storage import repository, memory

pytestmark = pytest.mark.asyncio


async def test_task_menu_view_and_delete(mocker, mock_db_path):
    """Verify /task command renders active tasks, mode switch, and numbered deletion."""
    t1 = await repository.create_task("Beli domain", due_time="14:00", db_path=mock_db_path)
    t2 = await repository.create_task("Setup server", due_time="16:00", db_path=mock_db_path)

    # 1. Test /task invocation by authorized user
    update = mocker.Mock(spec=Update)
    update.effective_user = mocker.Mock(spec=User)
    update.effective_user.id = 123456
    update.message = mocker.AsyncMock(spec=Message)
    update.callback_query = None

    context = mocker.AsyncMock(spec=ContextTypes.DEFAULT_TYPE)

    task_cmd = _make_task_cmd(allowed_user_id=123456)
    await task_cmd(update, context)

    update.message.reply_text.assert_called_once()
    reply_args = update.message.reply_text.call_args
    assert "Daftar Tugas (Pending)" in reply_args.kwargs["text"]
    assert "Beli domain" in reply_args.kwargs["text"]
    assert "Setup server" in reply_args.kwargs["text"]
    assert reply_args.kwargs["reply_markup"] is not None

    # 2. Test /task invocation dropped for unauthorized user
    unauth_update = mocker.Mock(spec=Update)
    unauth_update.effective_user = mocker.Mock(spec=User)
    unauth_update.effective_user.id = 999999
    unauth_update.message = mocker.AsyncMock(spec=Message)
    unauth_update.callback_query = None

    await task_cmd(unauth_update, context)
    unauth_update.message.reply_text.assert_not_called()

    # 3. Test callback query: switch to delete mode (t:m:del:pending)
    cb_update = mocker.Mock(spec=Update)
    cb_update.message = None
    cb_update.callback_query = mocker.AsyncMock(spec=CallbackQuery)
    cb_update.callback_query.from_user = mocker.Mock(spec=User)
    cb_update.callback_query.from_user.id = 123456
    cb_update.callback_query.message = mocker.AsyncMock(spec=Message)
    cb_update.callback_query.data = "t:m:del:pending"

    task_callback = _make_task_callback(allowed_user_id=123456)
    await task_callback(cb_update, context)

    cb_update.callback_query.edit_message_text.assert_called_once()
    edit_kwargs = cb_update.callback_query.edit_message_text.call_args.kwargs
    assert "Mode Hapus Tugas (Pending)" in edit_kwargs["text"]
    # Check numbered buttons in markup
    markup = edit_kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].text == "[ 1 ]"
    assert markup.inline_keyboard[0][1].text == "[ 2 ]"

    # 4. Test callback query: execute delete button 1 (t:act:del:pending:<t1>)
    del_update = mocker.Mock(spec=Update)
    del_update.message = None
    del_update.callback_query = mocker.AsyncMock(spec=CallbackQuery)
    del_update.callback_query.from_user = mocker.Mock(spec=User)
    del_update.callback_query.from_user.id = 123456
    del_update.callback_query.message = mocker.AsyncMock(spec=Message)
    del_update.callback_query.data = f"t:act:del:pending:{t1}"

    await task_callback(del_update, context)

    del_update.callback_query.answer.assert_called_with("Tugas berhasil dihapus!", show_alert=False)

    # Verify t1 was deleted from database, t2 remains
    remaining = await repository.get_tasks_by_status("pending", db_path=mock_db_path)
    assert len(remaining) == 1
    assert remaining[0]["id"] == t2

    # 5. Test callback query: close menu (t:close)
    close_update = mocker.Mock(spec=Update)
    close_update.message = None
    close_update.callback_query = mocker.AsyncMock(spec=CallbackQuery)
    close_update.callback_query.from_user = mocker.Mock(spec=User)
    close_update.callback_query.from_user.id = 123456
    close_update.callback_query.message = mocker.AsyncMock(spec=Message)
    close_update.callback_query.data = "t:close"

    await task_callback(close_update, context)
    close_update.callback_query.message.delete.assert_called_once()


async def test_profile_menu_view_delete_sync_user_md(mocker, mock_db_path, tmp_path):
    """Verify /profil renders facts, deletes from SQLite, and syncs removal to USER.md."""
    fact_text = "Lebih suka FastAPI dibanding Django"
    d1 = await repository.add_dossier_entry("preference", fact_text, db_path=mock_db_path)
    d2 = await repository.add_dossier_entry("habit", "Minum kopi espresso pagi", db_path=mock_db_path)

    # Setup isolated USER.md
    test_user_md = tmp_path / "USER.md"
    initial_user_md = (
        "# User Profile\n"
        f"- [PREFERENCE] {fact_text}\n"
        "- [HABIT] Minum kopi espresso pagi\n"
    )
    test_user_md.write_text(initial_user_md, encoding="utf-8")
    mocker.patch.object(memory, "DEFAULT_USER_MD", test_user_md)

    context = mocker.AsyncMock(spec=ContextTypes.DEFAULT_TYPE)

    # 1. Test /profil invocation
    update = mocker.Mock(spec=Update)
    update.effective_user = mocker.Mock(spec=User)
    update.effective_user.id = 123456
    update.message = mocker.AsyncMock(spec=Message)
    update.callback_query = None

    profile_cmd = _make_profile_cmd(allowed_user_id=123456)
    await profile_cmd(update, context)

    update.message.reply_text.assert_called_once()
    reply_text = update.message.reply_text.call_args.kwargs["text"]
    assert "Fakta Profil Pengguna (Semua)" in reply_text
    assert fact_text in reply_text

    # 2. Test callback query: delete fact d1 (p:act:del:all:<d1>)
    del_update = mocker.Mock(spec=Update)
    del_update.message = None
    del_update.callback_query = mocker.AsyncMock(spec=CallbackQuery)
    del_update.callback_query.from_user = mocker.Mock(spec=User)
    del_update.callback_query.from_user.id = 123456
    del_update.callback_query.message = mocker.AsyncMock(spec=Message)
    del_update.callback_query.data = f"p:act:del:all:{d1}"

    profile_callback = _make_profile_callback(allowed_user_id=123456)
    await profile_callback(del_update, context)

    del_update.callback_query.answer.assert_called_with("Fakta profil berhasil dihapus!", show_alert=False)

    # Verify SQLite deletion
    remaining_dossier = await repository.get_recent_dossier(db_path=mock_db_path)
    assert len(remaining_dossier) == 1
    assert remaining_dossier[0]["id"] == d2

    # Verify USER.md synchronization (fact 1 removed, fact 2 preserved)
    updated_user_md = test_user_md.read_text(encoding="utf-8")
    assert fact_text not in updated_user_md
    assert "Minum kopi espresso pagi" in updated_user_md


async def test_notes_menu_view_and_delete(mocker, mock_db_path):
    """Verify /notes renders lab notes and deletes them with numbered buttons."""
    n1 = await repository.add_lab_note(
        topic="Optimasi DB", content="Gunakan composite index", tags="db,perf", db_path=mock_db_path
    )

    context = mocker.AsyncMock(spec=ContextTypes.DEFAULT_TYPE)

    # 1. Test /notes invocation
    update = mocker.Mock(spec=Update)
    update.effective_user = mocker.Mock(spec=User)
    update.effective_user.id = 123456
    update.message = mocker.AsyncMock(spec=Message)
    update.callback_query = None

    notes_cmd = _make_notes_cmd(allowed_user_id=123456)
    await notes_cmd(update, context)

    update.message.reply_text.assert_called_once()
    reply_text = update.message.reply_text.call_args.kwargs["text"]
    assert "Optimasi DB" in reply_text

    # 2. Test callback query: delete note n1 (n:act:del:<n1>)
    del_update = mocker.Mock(spec=Update)
    del_update.message = None
    del_update.callback_query = mocker.AsyncMock(spec=CallbackQuery)
    del_update.callback_query.from_user = mocker.Mock(spec=User)
    del_update.callback_query.from_user.id = 123456
    del_update.callback_query.message = mocker.AsyncMock(spec=Message)
    del_update.callback_query.data = f"n:act:del:{n1}"

    notes_callback = _make_notes_callback(allowed_user_id=123456)
    await notes_callback(del_update, context)

    del_update.callback_query.answer.assert_called_with("Catatan riset berhasil dihapus!", show_alert=False)

    # Verify SQLite deletion
    notes_left = await repository.get_recent_lab_notes(db_path=mock_db_path)
    # The fixture pre-seeds 5 notes, n1 was added as 6th; after deleting n1, count is 5
    assert all(n["id"] != n1 for n in notes_left)


async def test_memory_menu_view_and_reset(mocker, tmp_path):
    """Verify /memory renders MEMORY.md and executes reset with confirmation dialog."""
    test_memory_md = tmp_path / "MEMORY.md"
    test_memory_md.write_text("## Focus\n- Selesaikan testing bot", encoding="utf-8")
    mocker.patch.object(memory, "DEFAULT_MEMORY_MD", test_memory_md)

    context = mocker.AsyncMock(spec=ContextTypes.DEFAULT_TYPE)

    # 1. Test /memory invocation
    update = mocker.Mock(spec=Update)
    update.effective_user = mocker.Mock(spec=User)
    update.effective_user.id = 123456
    update.message = mocker.AsyncMock(spec=Message)
    update.callback_query = None

    memory_cmd = _make_memory_cmd(allowed_user_id=123456)
    await memory_cmd(update, context)

    update.message.reply_text.assert_called_once()
    reply_text = update.message.reply_text.call_args.kwargs["text"]
    assert "Selesaikan testing bot" in reply_text

    # 2. Test callback query: ask reset (m:reset:ask)
    ask_update = mocker.Mock(spec=Update)
    ask_update.message = None
    ask_update.callback_query = mocker.AsyncMock(spec=CallbackQuery)
    ask_update.callback_query.from_user = mocker.Mock(spec=User)
    ask_update.callback_query.from_user.id = 123456
    ask_update.callback_query.message = mocker.AsyncMock(spec=Message)
    ask_update.callback_query.data = "m:reset:ask"

    memory_callback = _make_memory_callback(allowed_user_id=123456)
    await memory_callback(ask_update, context)

    ask_update.callback_query.edit_message_text.assert_called_once()
    assert "Konfirmasi Pengosongan Working Memory" in ask_update.callback_query.edit_message_text.call_args.kwargs["text"]

    # 3. Test callback query: confirm reset (m:reset:confirm)
    confirm_update = mocker.Mock(spec=Update)
    confirm_update.message = None
    confirm_update.callback_query = mocker.AsyncMock(spec=CallbackQuery)
    confirm_update.callback_query.from_user = mocker.Mock(spec=User)
    confirm_update.callback_query.from_user.id = 123456
    confirm_update.callback_query.message = mocker.AsyncMock(spec=Message)
    confirm_update.callback_query.data = "m:reset:confirm"

    await memory_callback(confirm_update, context)

    confirm_update.callback_query.answer.assert_called_with("Working memory berhasil dikosongkan!", show_alert=False)
    assert test_memory_md.read_text(encoding="utf-8") == ""


async def test_unauthorized_callback_rejected(mocker):
    """Verify unauthorized callback query is rejected with alert toast."""
    context = mocker.AsyncMock(spec=ContextTypes.DEFAULT_TYPE)

    cb_update = mocker.Mock(spec=Update)
    cb_update.message = None
    cb_update.callback_query = mocker.AsyncMock(spec=CallbackQuery)
    cb_update.callback_query.from_user = mocker.Mock(spec=User)
    cb_update.callback_query.from_user.id = 999999
    cb_update.callback_query.data = "t:f:all"

    task_callback = _make_task_callback(allowed_user_id=123456)
    await task_callback(cb_update, context)

    cb_update.callback_query.answer.assert_called_once_with("Akses ditolak.", show_alert=True)
    cb_update.callback_query.edit_message_text.assert_not_called()


async def test_safe_edit_or_reply_html_mode_and_fallback(mocker):
    """Verify _safe_edit_or_reply uses ParseMode.HTML and falls back gracefully to plain text."""
    from telegram.constants import ParseMode
    from src.interfaces.commands import _safe_edit_or_reply

    # 1. Successful callback edit with HTML parse_mode
    cb_update = mocker.Mock(spec=Update)
    cb_update.message = None
    cb_update.callback_query = mocker.AsyncMock(spec=CallbackQuery)
    cb_update.callback_query.message = mocker.AsyncMock(spec=Message)

    await _safe_edit_or_reply(cb_update, "<b>Test HTML</b>")
    cb_update.callback_query.edit_message_text.assert_called_once_with(
        text="<b>Test HTML</b>",
        reply_markup=None,
        parse_mode=ParseMode.HTML,
    )

    # 2. Callback edit failure triggers fallback to plain text
    cb_update_fail = mocker.Mock(spec=Update)
    cb_update_fail.message = None
    cb_update_fail.callback_query = mocker.AsyncMock(spec=CallbackQuery)
    cb_update_fail.callback_query.message = mocker.AsyncMock(spec=Message)
    cb_update_fail.callback_query.edit_message_text.side_effect = [
        Exception("Can't parse entities"),
        None,  # Fallback succeeds
    ]

    await _safe_edit_or_reply(cb_update_fail, "<malformed>Text")
    assert cb_update_fail.callback_query.edit_message_text.call_count == 2
    # Second call should not have parse_mode=ParseMode.HTML
    second_call_kwargs = cb_update_fail.callback_query.edit_message_text.call_args_list[1].kwargs
    assert "parse_mode" not in second_call_kwargs or second_call_kwargs.get("parse_mode") is None

    # 3. Identical content error (Message is not modified) must NOT trigger plain text fallback
    cb_update_identical = mocker.Mock(spec=Update)
    cb_update_identical.message = None
    cb_update_identical.callback_query = mocker.AsyncMock(spec=CallbackQuery)
    cb_update_identical.callback_query.message = mocker.AsyncMock(spec=Message)
    cb_update_identical.callback_query.edit_message_text.side_effect = Exception(
        "BadRequest: Message is not modified: specified new message content and reply markup are exactly the same"
    )

    await _safe_edit_or_reply(cb_update_identical, "<b>Identical Content</b>")
    assert cb_update_identical.callback_query.edit_message_text.call_count == 1

