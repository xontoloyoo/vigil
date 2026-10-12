"""Unit tests for TelegramInterface typing indicator loop and lifecycle management."""

import asyncio
from unittest.mock import AsyncMock
import pytest
from telegram import Update, User, Chat, Message
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ContextTypes

from src.interfaces.telegram import TelegramInterface
from src.core.traffic import TrafficController

pytestmark = pytest.mark.asyncio


async def test_keep_typing_periodic_and_cancellation():
    """Verify _keep_typing dispatches ChatAction.TYPING repeatedly and exits cleanly when cancelled."""
    bot = AsyncMock()
    task = asyncio.create_task(TelegramInterface._keep_typing(bot, chat_id=123, interval=0.03))

    # Allow at least 2 ticks
    await asyncio.sleep(0.08)
    task.cancel()
    await task

    assert bot.send_chat_action.call_count >= 2
    bot.send_chat_action.assert_called_with(chat_id=123, action=ChatAction.TYPING)


async def test_handle_message_typing_loop_cancels_on_success(mocker):
    """Verify typing loop is active during processing and cancelled upon normal completion."""
    traffic_ctrl = TrafficController()
    interface = TelegramInterface(allowed_user_id=100, traffic_ctrl=traffic_ctrl)

    update = mocker.Mock(spec=Update)
    update.effective_user = mocker.Mock(spec=User)
    update.effective_user.id = 100
    update.effective_chat = mocker.Mock(spec=Chat)
    update.effective_chat.id = 555
    update.message = mocker.AsyncMock(spec=Message)
    update.message.text = "Halo Vigil"

    context = mocker.AsyncMock(spec=ContextTypes.DEFAULT_TYPE)

    async def slow_handler(text):
        await asyncio.sleep(0.05)
        return "Jawaban selesai"

    interface.register_message_handler(slow_handler)

    await interface._handle_message(update, context)

    context.bot.send_chat_action.assert_called_with(chat_id=555, action=ChatAction.TYPING)
    update.message.reply_text.assert_called_once_with("Jawaban selesai", parse_mode=ParseMode.HTML)


async def test_handle_message_typing_loop_cancels_on_error(mocker):
    """Verify typing loop is cancelled cleanly even if the handler throws an exception."""
    traffic_ctrl = TrafficController()
    interface = TelegramInterface(allowed_user_id=100, traffic_ctrl=traffic_ctrl)

    update = mocker.Mock(spec=Update)
    update.effective_user = mocker.Mock(spec=User)
    update.effective_user.id = 100
    update.effective_chat = mocker.Mock(spec=Chat)
    update.effective_chat.id = 555
    update.message = mocker.AsyncMock(spec=Message)
    update.message.text = "Crash test"

    context = mocker.AsyncMock(spec=ContextTypes.DEFAULT_TYPE)

    async def failing_handler(text):
        await asyncio.sleep(0.02)
        raise RuntimeError("Handler crash simulated")

    interface.register_message_handler(failing_handler)

    await interface._handle_message(update, context)

    context.bot.send_chat_action.assert_called_with(chat_id=555, action=ChatAction.TYPING)
    update.message.reply_text.assert_called_once_with(
        "⚠️ Terjadi kesalahan internal saat memproses pesan Anda. Ketik /retry untuk mencoba lagi.",
        parse_mode=ParseMode.HTML,
    )


async def test_trace_command_toggle(mocker):
    """Verify /trace on, /trace off, and /trace status toggle behavior."""
    traffic_ctrl = TrafficController()
    interface = TelegramInterface(allowed_user_id=100, traffic_ctrl=traffic_ctrl)
    assert interface.enable_tracing is False

    update = mocker.Mock(spec=Update)
    update.effective_user = mocker.Mock(spec=User)
    update.effective_user.id = 100
    update.message = mocker.AsyncMock(spec=Message)

    context = mocker.AsyncMock(spec=ContextTypes.DEFAULT_TYPE)

    # 1. Turn on
    context.args = ["on"]
    await interface._handle_trace_command(update, context)
    assert interface.enable_tracing is True
    assert "diaktifkan" in update.message.reply_text.call_args[0][0]

    # 2. Status query
    context.args = []
    await interface._handle_trace_command(update, context)
    assert "AKTIF" in update.message.reply_text.call_args[0][0]

    # 3. Turn off
    context.args = ["off"]
    await interface._handle_trace_command(update, context)
    assert interface.enable_tracing is False
    assert "dinonaktifkan" in update.message.reply_text.call_args[0][0]

    # 4. Unauthorized dropped
    update.message.reply_text.reset_mock()
    update.effective_user.id = 999
    await interface._handle_trace_command(update, context)
    update.message.reply_text.assert_not_called()


async def test_live_tracing_in_place_edit_and_auto_delete(mocker):
    """Verify live in-place edit (send then edit) and auto-deletion of status bubble."""
    traffic_ctrl = TrafficController()
    interface = TelegramInterface(allowed_user_id=100, traffic_ctrl=traffic_ctrl, enable_tracing=True)

    update = mocker.Mock(spec=Update)
    update.effective_user = mocker.Mock(spec=User)
    update.effective_user.id = 100
    update.effective_chat = mocker.Mock(spec=Chat)
    update.effective_chat.id = 777
    update.message = mocker.AsyncMock(spec=Message)
    update.message.text = "Tolong cek catatan"

    sent_status_msg = mocker.Mock(spec=Message)
    sent_status_msg.message_id = 12345

    context = mocker.AsyncMock(spec=ContextTypes.DEFAULT_TYPE)
    context.bot.send_message = mocker.AsyncMock(return_value=sent_status_msg)
    context.bot.edit_message_text = mocker.AsyncMock()
    context.bot.delete_message = mocker.AsyncMock()

    async def multi_step_handler(text, on_trace=None):
        if on_trace:
            await on_trace("🔍 Mencari catatan: 'optimasi'")
            # Duplicate text should be ignored
            await on_trace("🔍 Mencari catatan: 'optimasi'")
            await on_trace("⚡ Menyintesis jawaban dari data temuan...")
        return "Ini jawaban akhir"

    interface.register_message_handler(multi_step_handler)

    await interface._handle_message(update, context)

    # 1. Initial status sent
    context.bot.send_message.assert_called_once_with(
        chat_id=777,
        text="🔍 Mencari catatan: 'optimasi'",
    )

    # 2. In-place edit for step 2 (ignoring duplicate step 1, accumulating multi-line status)
    context.bot.edit_message_text.assert_called_once_with(
        chat_id=777,
        message_id=12345,
        text="🔍 Mencari catatan: 'optimasi'\n⚡ Menyintesis jawaban dari data temuan...",
    )

    # 3. Status bubble deleted before final reply
    context.bot.delete_message.assert_called_once_with(
        chat_id=777,
        message_id=12345,
    )

    # 4. Final reply sent
    update.message.reply_text.assert_called_once_with("Ini jawaban akhir", parse_mode=ParseMode.HTML)


async def test_telegram_interface_hybrid_tracing_config(mocker):
    """Verify hybrid tracing: default from config, explicit override, and live runtime toggle."""
    from src.config import config

    orig_val = config.enable_process_tracing
    try:
        # 1. Default fallback to config (False)
        object.__setattr__(config, "enable_process_tracing", False)
        itf_default_false = TelegramInterface()
        assert itf_default_false.enable_tracing is False

        # 2. Config set to True -> new instance defaults to True
        object.__setattr__(config, "enable_process_tracing", True)
        itf_default_true = TelegramInterface()
        assert itf_default_true.enable_tracing is True

        # 3. Explicit argument overrides config
        itf_explicit_false = TelegramInterface(enable_tracing=False)
        assert itf_explicit_false.enable_tracing is False

        # 4. Live runtime toggle overrides initial config state
        update = mocker.Mock(spec=Update)
        update.effective_user = mocker.Mock(spec=User)
        update.effective_user.id = 100
        update.message = mocker.AsyncMock(spec=Message)
        context = mocker.AsyncMock(spec=ContextTypes.DEFAULT_TYPE)

        itf_runtime = TelegramInterface(allowed_user_id=100)
        assert itf_runtime.enable_tracing is True  # From config=True

        context.args = ["off"]
        await itf_runtime._handle_trace_command(update, context)
        assert itf_runtime.enable_tracing is False

        context.args = ["on"]
        await itf_runtime._handle_trace_command(update, context)
        assert itf_runtime.enable_tracing is True
    finally:
        object.__setattr__(config, "enable_process_tracing", orig_val)


async def test_handle_message_html_fallback_to_plain_text(mocker):
    """Verify fallback to plain text if HTML reply_text raises a parsing exception."""
    traffic_ctrl = TrafficController()
    interface = TelegramInterface(allowed_user_id=100, traffic_ctrl=traffic_ctrl)

    update = mocker.Mock(spec=Update)
    update.effective_user = mocker.Mock(spec=User)
    update.effective_user.id = 100
    update.effective_chat = mocker.Mock(spec=Chat)
    update.effective_chat.id = 555
    update.message = mocker.AsyncMock(spec=Message)
    update.message.text = "Tes HTML error"

    # Simulate first HTML call failing with BadRequest, second plain-text call succeeding
    update.message.reply_text.side_effect = [
        Exception("BadRequest: Can't parse entities"),
        None,
    ]

    context = mocker.AsyncMock(spec=ContextTypes.DEFAULT_TYPE)
    interface.register_message_handler(lambda text: "Respon **rusak**")

    await interface._handle_message(update, context)

    assert update.message.reply_text.call_count == 2
    # First call had parse_mode=ParseMode.HTML
    assert update.message.reply_text.call_args_list[0].kwargs.get("parse_mode") == ParseMode.HTML
    # Second fallback call should not have parse_mode or parse_mode=None
    second_kwargs = update.message.reply_text.call_args_list[1].kwargs
    assert "parse_mode" not in second_kwargs or second_kwargs.get("parse_mode") is None


async def test_send_proactive_nudge_html_and_fallback(mocker):
    """Verify send_proactive_nudge sends HTML and falls back to plain text on failure."""
    interface = TelegramInterface(bot_token="test:dummy", allowed_user_id=100)
    mock_app = mocker.Mock()
    mock_app.bot = mocker.AsyncMock()
    interface._application = mock_app

    # 1. Normal successful send with HTML
    mock_app.bot.send_message.return_value = True
    success = await interface.send_proactive_nudge("Halo **prioritas**")
    assert success is True
    mock_app.bot.send_message.assert_called_once_with(
        chat_id=100,
        text="Halo <b>prioritas</b>",
        parse_mode=ParseMode.HTML,
    )

    # 2. Fallback to plain text on error
    mock_app.bot.send_message.reset_mock()
    mock_app.bot.send_message.side_effect = [
        Exception("Can't parse entities"),
        True,
    ]
    success_fallback = await interface.send_proactive_nudge("Pesan **gagal**")
    assert success_fallback is True
    assert mock_app.bot.send_message.call_count == 2
    # Second call should be plain text (no parse_mode)
    second_call_kwargs = mock_app.bot.send_message.call_args_list[1].kwargs
    assert "parse_mode" not in second_call_kwargs or second_call_kwargs.get("parse_mode") is None


async def test_stop_command_when_idle(mocker):
    """Verify /stop when no task is running replies with informational status."""
    traffic_ctrl = TrafficController()
    interface = TelegramInterface(allowed_user_id=100, traffic_ctrl=traffic_ctrl)

    update = mocker.Mock(spec=Update)
    update.effective_user = mocker.Mock(spec=User)
    update.effective_user.id = 100
    update.message = mocker.AsyncMock(spec=Message)

    context = mocker.AsyncMock(spec=ContextTypes.DEFAULT_TYPE)

    await interface._handle_stop(update, context)
    update.message.reply_text.assert_called_once_with(
        "ℹ️ Tidak ada proses LLM yang sedang berjalan."
    )


async def test_stop_command_cancels_active_task(mocker):
    """Verify /stop cancels the running message task cleanly and records failed message."""
    traffic_ctrl = TrafficController()
    interface = TelegramInterface(allowed_user_id=100, traffic_ctrl=traffic_ctrl)

    # 1. Setup mock for running message
    update_msg = mocker.Mock(spec=Update)
    update_msg.effective_user = mocker.Mock(spec=User)
    update_msg.effective_user.id = 100
    update_msg.effective_chat = mocker.Mock(spec=Chat)
    update_msg.effective_chat.id = 555
    update_msg.message = mocker.AsyncMock(spec=Message)
    update_msg.message.text = "Prompt yang sangat panjang"

    context_msg = mocker.AsyncMock(spec=ContextTypes.DEFAULT_TYPE)

    started_event = asyncio.Event()

    async def slow_pipeline(text):
        started_event.set()
        await asyncio.sleep(5.0)
        return "Jawaban akhir"

    interface.register_message_handler(slow_pipeline)

    # Start message processing as background task
    msg_task = asyncio.create_task(interface._handle_message(update_msg, context_msg))
    await started_event.wait()

    assert interface._active_chat_task is not None
    assert not interface._active_chat_task.done()

    # 2. Invoke /stop command
    update_stop = mocker.Mock(spec=Update)
    update_stop.effective_user = mocker.Mock(spec=User)
    update_stop.effective_user.id = 100
    update_stop.message = mocker.AsyncMock(spec=Message)
    context_stop = mocker.AsyncMock(spec=ContextTypes.DEFAULT_TYPE)

    await interface._handle_stop(update_stop, context_stop)

    # Verify stop message replied
    update_stop.message.reply_text.assert_called_once_with(
        "⏹️ Respon LLM berhasil dihentikan. Ketik /retry untuk mencoba lagi."
    )

    # Wait for cancelled message task to finish unwinding
    await msg_task

    # Verify message processing task was cancelled and no final reply was sent from _handle_message
    update_msg.message.reply_text.assert_not_called()
    assert interface._active_chat_task is None
    assert interface._last_failed_message == "Prompt yang sangat panjang"


async def test_retry_command_when_no_previous_message(mocker):
    """Verify /retry when no message history exists replies with informational text."""
    traffic_ctrl = TrafficController()
    interface = TelegramInterface(allowed_user_id=100, traffic_ctrl=traffic_ctrl)

    update = mocker.Mock(spec=Update)
    update.effective_user = mocker.Mock(spec=User)
    update.effective_user.id = 100
    update.message = mocker.AsyncMock(spec=Message)
    context = mocker.AsyncMock(spec=ContextTypes.DEFAULT_TYPE)

    await interface._handle_retry(update, context)
    update.message.reply_text.assert_called_once_with(
        "ℹ️ Tidak ada pesan sebelumnya yang dapat diulang."
    )


async def test_retry_command_re_runs_failed_message(mocker):
    """Verify /retry re-runs the last failed message and delivers reply."""
    traffic_ctrl = TrafficController()
    interface = TelegramInterface(allowed_user_id=100, traffic_ctrl=traffic_ctrl)
    interface._last_failed_message = "Tolong rangkum artikel ini"

    update = mocker.Mock(spec=Update)
    update.effective_user = mocker.Mock(spec=User)
    update.effective_user.id = 100
    update.effective_chat = mocker.Mock(spec=Chat)
    update.effective_chat.id = 555
    update.message = mocker.AsyncMock(spec=Message)
    update.message.text = "/retry"
    context = mocker.AsyncMock(spec=ContextTypes.DEFAULT_TYPE)

    received_texts = []

    async def echo_handler(text):
        received_texts.append(text)
        return f"Hasil ringkasan untuk: {text}"

    interface.register_message_handler(echo_handler)

    await interface._handle_retry(update, context)

    assert received_texts == ["Tolong rangkum artikel ini"]
    update.message.reply_text.assert_called_once_with(
        "Hasil ringkasan untuk: Tolong rangkum artikel ini",
        parse_mode=ParseMode.HTML,
    )
    # Failed message should now be cleared
    assert interface._last_failed_message is None


async def test_concurrent_message_rejected_with_warning(mocker):
    """Verify sending a new message while a previous message is active warns user to wait or use /stop."""
    traffic_ctrl = TrafficController()
    interface = TelegramInterface(allowed_user_id=100, traffic_ctrl=traffic_ctrl)

    update1 = mocker.Mock(spec=Update)
    update1.effective_user = mocker.Mock(spec=User)
    update1.effective_user.id = 100
    update1.effective_chat = mocker.Mock(spec=Chat)
    update1.effective_chat.id = 555
    update1.message = mocker.AsyncMock(spec=Message)
    update1.message.text = "Pesan pertama"
    context1 = mocker.AsyncMock(spec=ContextTypes.DEFAULT_TYPE)

    started1 = asyncio.Event()

    async def slow_handler(text):
        started1.set()
        await asyncio.sleep(2.0)
        return "Selesai 1"

    interface.register_message_handler(slow_handler)

    task1 = asyncio.create_task(interface._handle_message(update1, context1))
    await started1.wait()

    # Second concurrent message
    update2 = mocker.Mock(spec=Update)
    update2.effective_user = mocker.Mock(spec=User)
    update2.effective_user.id = 100
    update2.effective_chat = mocker.Mock(spec=Chat)
    update2.effective_chat.id = 555
    update2.message = mocker.AsyncMock(spec=Message)
    update2.message.text = "Pesan kedua yang menyela"
    context2 = mocker.AsyncMock(spec=ContextTypes.DEFAULT_TYPE)

    await interface._handle_message(update2, context2)

    update2.message.reply_text.assert_called_once_with(
        "⏳ Vigil sedang memproses pesan sebelumnya. Ketik /stop jika ingin membatalkan."
    )

    # Cancel task 1 to finish cleanly
    task1.cancel()
    await task1


async def test_unauthorized_stop_and_retry_ignored(mocker):
    """Verify unauthorized user cannot execute /stop or /retry."""
    traffic_ctrl = TrafficController()
    interface = TelegramInterface(allowed_user_id=100, traffic_ctrl=traffic_ctrl)

    update = mocker.Mock(spec=Update)
    update.effective_user = mocker.Mock(spec=User)
    update.effective_user.id = 999  # Unauthorized
    update.message = mocker.AsyncMock(spec=Message)
    context = mocker.AsyncMock(spec=ContextTypes.DEFAULT_TYPE)

    await interface._handle_stop(update, context)
    update.message.reply_text.assert_not_called()

    await interface._handle_retry(update, context)
    update.message.reply_text.assert_not_called()


async def test_stop_followed_immediately_by_retry_no_race_condition(mocker):
    """Verify calling /stop immediately followed by /retry does NOT produce a race condition."""
    traffic_ctrl = TrafficController()
    interface = TelegramInterface(allowed_user_id=100, traffic_ctrl=traffic_ctrl)

    started_event = asyncio.Event()
    attempts = 0

    async def mock_handler(text):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            started_event.set()
            await asyncio.sleep(5.0)
            return "Hasil tidak selesai"
        return f"Hasil sukses retry: {text}"

    interface.register_message_handler(mock_handler)

    # 1. Start message task
    update_msg = mocker.Mock(spec=Update)
    update_msg.effective_user = mocker.Mock(spec=User)
    update_msg.effective_user.id = 100
    update_msg.effective_chat = mocker.Mock(spec=Chat)
    update_msg.effective_chat.id = 555
    update_msg.message = mocker.AsyncMock(spec=Message)
    update_msg.message.text = "Prompt awal"
    context_msg = mocker.AsyncMock(spec=ContextTypes.DEFAULT_TYPE)

    asyncio.create_task(interface._handle_message(update_msg, context_msg))
    await started_event.wait()

    # 2. Call /stop
    update_stop = mocker.Mock(spec=Update)
    update_stop.effective_user = mocker.Mock(spec=User)
    update_stop.effective_user.id = 100
    update_stop.message = mocker.AsyncMock(spec=Message)
    context_stop = mocker.AsyncMock(spec=ContextTypes.DEFAULT_TYPE)

    await interface._handle_stop(update_stop, context_stop)
    assert interface._active_chat_task is None

    # 3. Call /retry IMMEDIATELY
    update_retry = mocker.Mock(spec=Update)
    update_retry.effective_user = mocker.Mock(spec=User)
    update_retry.effective_user.id = 100
    update_retry.effective_chat = mocker.Mock(spec=Chat)
    update_retry.effective_chat.id = 555
    update_retry.message = mocker.AsyncMock(spec=Message)
    update_retry.message.text = "/retry"
    context_retry = mocker.AsyncMock(spec=ContextTypes.DEFAULT_TYPE)

    await interface._handle_retry(update_retry, context_retry)

    # Verify retry ran successfully without being rejected as 'already running'
    update_retry.message.reply_text.assert_called_once_with(
        "Hasil sukses retry: Prompt awal",
        parse_mode=ParseMode.HTML,
    )


async def test_handle_message_resets_active_task_even_if_trace_cleanup_fails(mocker):
    """Verify _active_chat_task is always cleared even if trace message cleanup encounters an error."""
    traffic_ctrl = TrafficController()
    interface = TelegramInterface(allowed_user_id=100, traffic_ctrl=traffic_ctrl, enable_tracing=True)

    update = mocker.Mock(spec=Update)
    update.effective_user = mocker.Mock(spec=User)
    update.effective_user.id = 100
    update.effective_chat = mocker.Mock(spec=Chat)
    update.effective_chat.id = 555
    update.message = mocker.AsyncMock(spec=Message)
    update.message.text = "Tes error cleanup"

    sent_trace = mocker.Mock(spec=Message)
    sent_trace.message_id = 999

    context = mocker.AsyncMock(spec=ContextTypes.DEFAULT_TYPE)
    context.bot.send_message = mocker.AsyncMock(return_value=sent_trace)
    # Simulate delete_message failing with exception
    context.bot.delete_message = mocker.AsyncMock(side_effect=RuntimeError("Telegram API network drop"))

    async def tracing_handler(text, on_trace=None):
        if on_trace:
            await on_trace("Status trace")
        return "Jawaban akhir"

    interface.register_message_handler(tracing_handler)

    await interface._handle_message(update, context)

    # Task MUST be reset to None, not locked
    assert interface._active_chat_task is None
    update.message.reply_text.assert_called_once_with("Jawaban akhir", parse_mode=ParseMode.HTML)


async def test_handle_message_and_retry_ignore_whitespace_only(mocker):
    """Verify empty or whitespace-only messages do not invoke message handler or consume LLM."""
    traffic_ctrl = TrafficController()
    interface = TelegramInterface(allowed_user_id=100, traffic_ctrl=traffic_ctrl)

    mock_handler = mocker.AsyncMock()
    interface.register_message_handler(mock_handler)

    update = mocker.Mock(spec=Update)
    update.effective_user = mocker.Mock(spec=User)
    update.effective_user.id = 100
    update.effective_chat = mocker.Mock(spec=Chat)
    update.effective_chat.id = 555
    update.message = mocker.AsyncMock(spec=Message)
    update.message.text = "    "
    context = mocker.AsyncMock(spec=ContextTypes.DEFAULT_TYPE)

    await interface._handle_message(update, context)
    mock_handler.assert_not_called()
    assert interface._last_user_message is None


