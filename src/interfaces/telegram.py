"""Telegram Bot interface for vigil.

Handles incoming messages, enforces whitelist authorization, records user activity,
dispatches typing indicators, and sends proactive heartbeat reminders.
"""

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
import inspect
import logging
from pathlib import Path
from typing import Any

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.config import config
from src.core.traffic import TrafficController, traffic_controller
from src.core.utils import markdown_to_telegram_html
from src.interfaces.commands import register_commands

logger = logging.getLogger(__name__)

# Fallback token for testing environments where .env is not yet configured
_DUMMY_FALLBACK_TOKEN = "0000000000:dummy_fallback_token_for_testing"


class TelegramInterface:
    """Asynchronous Telegram Bot adapter and notification dispatcher."""

    def __init__(
        self,
        bot_token: str | None = None,
        allowed_user_id: int | str | None = None,
        traffic_ctrl: TrafficController | None = None,
        enable_tracing: bool | None = None,
    ) -> None:
        self.bot_token = bot_token or config.TELEGRAM_BOT_TOKEN or ""

        # Normalize allowed_user_id to int if feasible
        raw_allowed = (
            allowed_user_id
            if allowed_user_id is not None
            else config.TELEGRAM_ALLOWED_USER_ID
        )
        self.allowed_user_id: int | None = None
        if raw_allowed is not None and str(raw_allowed).strip():
            try:
                self.allowed_user_id = int(str(raw_allowed).strip())
            except ValueError:
                logger.warning("Invalid TELEGRAM_ALLOWED_USER_ID: %s", raw_allowed)
                self.allowed_user_id = None

        self.traffic_ctrl = traffic_ctrl or traffic_controller
        self.enable_tracing = (
            enable_tracing
            if enable_tracing is not None
            else config.ENABLE_PROCESS_TRACING
        )
        self._message_handler: Callable[..., Awaitable[str]] | None = None
        self._application: Application | None = None
        self._is_running: bool = False

        # Task cancellation and retry tracking
        self._active_chat_task: asyncio.Task[Any] | None = None
        self._last_user_message: str | None = None
        self._last_failed_message: str | None = None

    def register_message_handler(
        self, handler: Callable[..., Awaitable[str]]
    ) -> None:
        """Register the decoupled async message handling callback (e.g. LLM pipeline)."""
        self._message_handler = handler

    def _is_user_authorized(self, user_id: int | None) -> bool:
        """Check if the sender matches the whitelisted Telegram user ID."""
        if self.allowed_user_id is None:
            # If no user whitelist configured, deny by default for security
            return False
        return user_id == self.allowed_user_id

    async def _handle_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle the /start command."""
        user = update.effective_user
        if not user or not self._is_user_authorized(user.id):
            logger.warning("Unauthorized /start attempt from user_id: %s", getattr(user, "id", None))
            return

        await self.traffic_ctrl.record_interaction()
        if update.message:
            await update.message.reply_text("Vigil aktif dan siap melayani.")

    async def _handle_trace_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle the /trace and /tracing commands to toggle live process tracing."""
        user = update.effective_user
        if not user or not self._is_user_authorized(user.id):
            logger.warning("Unauthorized /trace attempt from user_id: %s", getattr(user, "id", None))
            return

        await self.traffic_ctrl.record_interaction()
        if not update.message:
            return

        args = context.args or []
        subcmd = args[0].lower() if args else ""

        if subcmd in ("on", "1", "true", "enable", "aktif"):
            self.enable_tracing = True
            await update.message.reply_text(
                "🔍 Mode tracing diaktifkan. Anda akan melihat progres eksekusi aksi secara langsung."
            )
        elif subcmd in ("off", "0", "false", "disable", "nonaktif"):
            self.enable_tracing = False
            await update.message.reply_text(
                "🔇 Mode tracing dinonaktifkan. Vigil hanya akan mengirimkan pesan balasan akhir."
            )
        else:
            status_str = "AKTIF" if self.enable_tracing else "NONAKTIF"
            await update.message.reply_text(
                f"Status mode tracing saat ini: {status_str}.\n"
                "Gunakan '/trace on' untuk mengaktifkan atau '/trace off' untuk menonaktifkan."
            )

    async def _handle_stop(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle the /stop and /batal commands to cancel ongoing LLM generation."""
        user = update.effective_user
        if not user or not self._is_user_authorized(user.id):
            logger.warning("Unauthorized /stop attempt from user_id: %s", getattr(user, "id", None))
            return

        await self.traffic_ctrl.record_interaction()
        if not update.message:
            return

        current_task = asyncio.current_task()
        if (
            self._active_chat_task
            and not self._active_chat_task.done()
            and self._active_chat_task is not current_task
        ):
            task = self._active_chat_task
            task.cancel()
            # Wait for task to finish unwinding to eliminate any race condition with subsequent commands
            with contextlib.suppress(asyncio.CancelledError):
                await task

            logger.info("Cancelled active chat task for user_id=%s", getattr(user, "id", None))
            await update.message.reply_text(
                "⏹️ Respon LLM berhasil dihentikan. Ketik /retry untuk mencoba lagi."
            )
        else:
            await update.message.reply_text(
                "ℹ️ Tidak ada proses LLM yang sedang berjalan."
            )

    async def _handle_retry(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /retry and /ulang commands to re-run the last failed or cancelled message."""
        user = update.effective_user
        if not user or not self._is_user_authorized(user.id):
            logger.warning("Unauthorized /retry attempt from user_id: %s", getattr(user, "id", None))
            return

        await self.traffic_ctrl.record_interaction()
        if not update.message:
            return

        if self._active_chat_task and not self._active_chat_task.done():
            await update.message.reply_text(
                "⏳ Masih ada proses yang berjalan. Ketik /stop untuk membatalkan."
            )
            return

        retry_text = self._last_failed_message or self._last_user_message
        if not retry_text or not retry_text.strip():
            await update.message.reply_text(
                "ℹ️ Tidak ada pesan sebelumnya yang dapat diulang."
            )
            return

        logger.info("Retrying message for user_id=%s: %s", getattr(user, "id", None), retry_text[:80])
        # Re-run the retried message through _handle_message
        await self._handle_message(update, context, override_text=retry_text)

    @staticmethod
    async def _keep_typing(bot: Any, chat_id: int, interval: float = 4.0) -> None:
        """Periodically dispatch typing chat action until cancelled."""
        try:
            while True:
                try:
                    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                except Exception as exc:
                    logger.debug("Failed to send typing chat action: %s", exc)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass

    async def _handle_message(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        override_text: str | None = None,
    ) -> None:
        """Process incoming text messages, verify whitelist, record activity, and reply."""
        user = update.effective_user
        if not user or not self._is_user_authorized(user.id):
            logger.warning(
                "Unauthorized message dropped from user_id: %s (whitelist: %s)",
                getattr(user, "id", None),
                self.allowed_user_id,
            )
            return

        current_task = asyncio.current_task()
        # Protect against overlapping execution from a different concurrent task
        if (
            self._active_chat_task
            and not self._active_chat_task.done()
            and self._active_chat_task is not current_task
        ):
            if update.message:
                await update.message.reply_text(
                    "⏳ Vigil sedang memproses pesan sebelumnya. Ketik /stop jika ingin membatalkan."
                )
            return

        # Record interaction into traffic controller
        await self.traffic_ctrl.record_interaction()

        user_text = (
            override_text
            if override_text is not None
            else (update.message.text if update.message and update.message.text else "")
        )
        if not user_text or not user_text.strip():
            logger.debug("Ignoring empty or whitespace-only message from user_id: %s", getattr(user, "id", None))
            return

        logger.debug(
            "[DEBUG] Incoming message: user_id=%s chat_id=%s text_length=%d\n  text: %s",
            getattr(user, "id", None),
            update.effective_chat.id if update.effective_chat else None,
            len(user_text),
            user_text,
        )

        self._active_chat_task = current_task
        self._last_user_message = user_text

        # Start continuous typing action loop in background
        typing_task = None
        if update.effective_chat and getattr(context, "bot", None):
            typing_task = asyncio.create_task(
                self._keep_typing(context.bot, update.effective_chat.id)
            )

        # Setup live process tracing callback if enabled
        trace_msg_id: int | None = None
        trace_lines: list[str] = []

        async def _trace_callback(text: str) -> None:
            nonlocal trace_msg_id
            if not update.effective_chat or not getattr(context, "bot", None):
                return
            clean_text = text.strip()
            if not clean_text or clean_text in trace_lines:
                return
            trace_lines.append(clean_text)
            combined_text = "\n".join(trace_lines)
            try:
                if trace_msg_id is None:
                    sent = await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=combined_text,
                    )
                    trace_msg_id = getattr(sent, "message_id", None)
                else:
                    await context.bot.edit_message_text(
                        chat_id=update.effective_chat.id,
                        message_id=trace_msg_id,
                        text=combined_text,
                    )
            except Exception as exc:
                logger.debug("Failed to send/edit trace message: %s", exc)

        on_trace = _trace_callback if self.enable_tracing else None

        # Execute registered external handler or fallback
        cancelled = False
        reply_text: str | None = None
        try:
            if self._message_handler:
                try:
                    sig = inspect.signature(self._message_handler)
                    if "on_trace" in sig.parameters or len(sig.parameters) >= 2:
                        reply_text = await self._message_handler(user_text, on_trace=on_trace)
                    else:
                        reply_text = await self._message_handler(user_text)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error("Error in external message handler: %s", exc, exc_info=True)
                    reply_text = "⚠️ Terjadi kesalahan internal saat memproses pesan Anda. Ketik /retry untuk mencoba lagi."
            else:
                reply_text = "Pesan diterima. Belum ada handler percakapan yang terdaftar."
        except asyncio.CancelledError:
            logger.info("Message processing cancelled for user message: %s", user_text)
            self._last_failed_message = user_text
            cancelled = True
        finally:
            # 1. Reset active task first and synchronously to prevent lock if async cleanup is cancelled
            if self._active_chat_task is current_task:
                self._active_chat_task = None

            # 2. Cancel and clean up background typing loop
            if typing_task:
                typing_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await typing_task

            # 3. Clean up live tracing status bubble safely with shield
            if trace_msg_id and update.effective_chat and getattr(context, "bot", None):
                try:
                    await asyncio.shield(
                        context.bot.delete_message(
                            chat_id=update.effective_chat.id,
                            message_id=trace_msg_id,
                        )
                    )
                except (Exception, asyncio.CancelledError) as exc:
                    logger.debug("Failed to delete trace message during cleanup: %s", exc)

        # Send response back to user if not cancelled
        if not cancelled and update.message and reply_text:
            if reply_text.startswith("⚠️") or reply_text.startswith("Maaf"):
                self._last_failed_message = user_text
            else:
                self._last_failed_message = None

            formatted_reply = markdown_to_telegram_html(reply_text)
            try:
                await update.message.reply_text(
                    formatted_reply,
                    parse_mode=ParseMode.HTML,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to send HTML formatted reply, falling back to plain text: %s",
                    exc,
                )
                try:
                    await update.message.reply_text(reply_text)
                except Exception as fallback_exc:
                    logger.error(
                        "Failed to send reply to Telegram chat: %s",
                        fallback_exc,
                        exc_info=True,
                    )

    def build_application(self) -> Application:
        """Build and configure the python-telegram-bot Application instance."""
        if not self._application:
            effective_token = self.bot_token or _DUMMY_FALLBACK_TOKEN
            builder = ApplicationBuilder().token(effective_token)
            self._application = builder.build()

            # Register handlers
            self._application.add_handler(CommandHandler("start", self._handle_start))
            self._application.add_handler(
                CommandHandler(["trace", "tracing"], self._handle_trace_command)
            )
            self._application.add_handler(
                CommandHandler(["stop", "batal"], self._handle_stop)
            )
            self._application.add_handler(
                CommandHandler(["retry", "ulang"], self._handle_retry)
            )

            # Register interactive menu commands and callback queries (/task, /profil, /notes, /memory)
            register_commands(self._application, self.allowed_user_id)

            self._application.add_handler(
                MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
            )

        return self._application

    async def send_proactive_nudge(self, ticket: dict[str, Any] | str) -> bool:
        """Send a proactive reminder notification to the whitelisted user.

        Accepts either a raw message string or a ticket dict.
        Suitable as a callback for HeartbeatRunner.register_dispatch_callback.
        """
        if self.allowed_user_id is None:
            logger.warning("Cannot send proactive nudge: allowed_user_id is not configured.")
            return False

        app = self.build_application()
        if not app.bot:
            logger.warning("Cannot send proactive nudge: bot instance is unavailable.")
            return False

        if isinstance(ticket, str):
            message_text = ticket
        else:
            custom_msg = ticket.get("message") or ticket.get("text")
            if custom_msg:
                message_text = custom_msg
            else:
                title = ticket.get("title", "Tugas Tanpa Judul")
                due_time = ticket.get("due_time")
                due_info = f" (Jatuh tempo: {due_time})" if due_time else ""

                message_text = (
                    f"🔔 Pengingat Tugas\n\n"
                    f"Tugas: {title}{due_info}\n\n"
                    f"Bagaimana progresnya? Apakah sudah diselesaikan?"
                )

        formatted_message = markdown_to_telegram_html(message_text)
        try:
            await app.bot.send_message(
                chat_id=self.allowed_user_id,
                text=formatted_message,
                parse_mode=ParseMode.HTML,
            )
            return True
        except Exception as exc:
            logger.warning(
                "Failed to send HTML proactive nudge, falling back to plain text: %s",
                exc,
            )
            try:
                await app.bot.send_message(
                    chat_id=self.allowed_user_id,
                    text=message_text,
                )
                return True
            except Exception as fallback_exc:
                logger.error(
                    "Failed to send proactive nudge: %s",
                    fallback_exc,
                    exc_info=True,
                )
                return False

    async def start(self) -> None:
        """Initialize and start the application in an async event loop."""
        if self._is_running:
            return
        app = self.build_application()
        await app.initialize()
        await app.start()
        if app.updater and self.bot_token and self.bot_token != _DUMMY_FALLBACK_TOKEN:
            await app.updater.start_polling()
        else:
            logger.warning("Telegram live polling skipped: token is dummy or unconfigured.")
        self._is_running = True
        logger.info("TelegramInterface started.")

    async def stop(self) -> None:
        """Stop the application and shutdown background polling cleanly."""
        if not self._is_running or not self._application:
            return
        if self._application.updater and self._application.updater.running:
            await self._application.updater.stop()
        if self._application.running:
            await self._application.stop()
        await self._application.shutdown()
        self._is_running = False
        logger.info("TelegramInterface stopped.")

    def run_polling(self) -> None:
        """Start standard blocking polling loop."""
        app = self.build_application()
        app.run_polling()


# Default ready-to-use instance
telegram_interface = TelegramInterface()
