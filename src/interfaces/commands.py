"""Interactive Telegram commands and inline keyboard callbacks for vigil.

Provides direct, deterministic administrative management bypassing the LLM:
- /task  : Filter active tasks and delete tasks with numbered buttons.
- /profil: Filter profile facts and delete facts with numbered buttons (synced to USER.md).
- /notes : View recent 5 technical lab notes and delete them with numbered buttons.
- /memory: Inspect working memory (MEMORY.md) with refresh and reset confirmation.
"""

from collections.abc import Callable
import html
import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from src.storage import memory, repository

logger = logging.getLogger(__name__)


def _is_authorized(user_id: int | None, allowed_user_id: int | None) -> bool:
    """Check if the requesting user matches the whitelisted Telegram user ID."""
    if allowed_user_id is None or user_id is None:
        return False
    return user_id == allowed_user_id


async def _safe_edit_or_reply(
    update: Update,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Edit current message in-place for callback queries, or reply for text commands.

    Uses ParseMode.HTML for rich formatting, with an automatic defensive fallback
    to plain text only if Telegram rejects HTML entity syntax. Identical content
    (Message is not modified) is safely ignored without triggering plain text fallback.
    """
    if update.callback_query and update.callback_query.message:
        try:
            await update.callback_query.edit_message_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            err_msg = str(exc).lower()
            if "message is not modified" in err_msg:
                # Content has not changed (e.g. user clicked refresh on identical data).
                # Do NOT trigger plain text fallback!
                logger.debug("Message content is identical, edit skipped.")
                return

            logger.warning("HTML edit failed, attempting plain text fallback: %s", exc)
            try:
                await update.callback_query.edit_message_text(
                    text=text,
                    reply_markup=reply_markup,
                )
            except Exception as fallback_exc:
                if "message is not modified" not in str(fallback_exc).lower():
                    logger.debug("Edit message fallback failed: %s", fallback_exc)
    elif update.message:
        try:
            await update.message.reply_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.warning("Reply with HTML failed, falling back to plain text: %s", exc)
            await update.message.reply_text(
                text=text,
                reply_markup=reply_markup,
            )


def _build_number_grid(
    item_ids: list[str],
    action_prefix: str,
    back_callback: str,
    items_per_row: int = 5,
) -> InlineKeyboardMarkup:
    """Build a numbered inline keyboard grid [ 1 ] [ 2 ] ... mapped to item IDs."""
    buttons: list[list[InlineKeyboardButton]] = []
    current_row: list[InlineKeyboardButton] = []

    for index, item_id in enumerate(item_ids, start=1):
        btn = InlineKeyboardButton(
            text=f"[ {index} ]",
            callback_data=f"{action_prefix}:{item_id}",
        )
        current_row.append(btn)
        if len(current_row) >= items_per_row:
            buttons.append(current_row)
            current_row = []

    if current_row:
        buttons.append(current_row)

    # Back button on the last row
    buttons.append([InlineKeyboardButton(text="⬅️ Kembali", callback_data=back_callback)])
    return InlineKeyboardMarkup(buttons)


# ============================================================================
# 1. TASK MENU (/task, /tasks)
# ============================================================================

def _format_task_status_badge(status: str) -> str:
    """Format status badge for tasks."""
    s = status.lower()
    if s == "in_progress":
        return "▶️ Progress"
    if s == "done":
        return "✅ Selesai"
    if s == "cancelled":
        return "❌ Dibatalkan"
    return "⏳ Pending"


async def _render_task_view(
    update: Update,
    status_filter: str = "pending",
    mode: str = "view",
) -> None:
    """Render the interactive task dashboard (view mode or delete mode)."""
    filter_map = {
        "pending": "Pending",
        "in_prog": "In-Progress",
        "done": "Selesai",
        "all": "Semua",
    }
    repo_filter = "in_progress" if status_filter == "in_prog" else status_filter
    tasks = await repository.get_tasks_by_status(status=repo_filter, limit=10)
    display_title = filter_map.get(status_filter, "Pending")

    if not tasks:
        text = f"📋 <b>Daftar Tugas ({display_title})</b>\n\nTidak ada tugas dalam status ini."
        buttons = [
            [
                InlineKeyboardButton(
                    ("✅ Pending" if status_filter == "pending" else "Pending"),
                    callback_data="t:f:pending",
                ),
                InlineKeyboardButton(
                    ("✅ Progress" if status_filter == "in_prog" else "Progress"),
                    callback_data="t:f:in_prog",
                ),
                InlineKeyboardButton(
                    ("✅ Selesai" if status_filter == "done" else "Selesai"),
                    callback_data="t:f:done",
                ),
                InlineKeyboardButton(
                    ("✅ Semua" if status_filter == "all" else "Semua"),
                    callback_data="t:f:all",
                ),
            ],
            [InlineKeyboardButton("❌ Tutup", callback_data="t:close")],
        ]
        await _safe_edit_or_reply(update, text, InlineKeyboardMarkup(buttons))
        return

    # If in delete mode, show numbered prompt
    if mode == "del":
        lines = [
            f"🗑️ <b>Mode Hapus Tugas ({display_title})</b>",
            "Sentuh nomor tugas di bawah untuk menghapusnya:\n",
        ]
        task_ids = []
        for idx, t in enumerate(tasks, start=1):
            due_str = f" <i>(Due: {html.escape(str(t['due_time']))})</i>" if t.get("due_time") else ""
            badge = _format_task_status_badge(t.get("status", "pending"))
            lines.append(f"<b>{idx}.</b> [{badge}] {html.escape(str(t['title']))}{due_str}")
            task_ids.append(t["id"])

        text = "\n".join(lines)
        markup = _build_number_grid(
            item_ids=task_ids,
            action_prefix=f"t:act:del:{status_filter}",
            back_callback=f"t:f:{status_filter}",
        )
        await _safe_edit_or_reply(update, text, markup)
        return

    # Normal View Mode
    lines = [
        f"📋 <b>Daftar Tugas ({display_title})</b>",
        f"<i>Total: {len(tasks)} tugas</i>\n",
    ]
    for idx, t in enumerate(tasks, start=1):
        due_str = f" <i>(Due: {html.escape(str(t['due_time']))})</i>" if t.get("due_time") else ""
        badge = _format_task_status_badge(t.get("status", "pending"))
        lines.append(f"<b>{idx}.</b> [{badge}] {html.escape(str(t['title']))}{due_str}")

    text = "\n".join(lines)
    buttons = [
        # Row 1: Filter tabs
        [
            InlineKeyboardButton(
                ("✅ Pending" if status_filter == "pending" else "Pending"),
                callback_data="t:f:pending",
            ),
            InlineKeyboardButton(
                ("✅ Progress" if status_filter == "in_prog" else "Progress"),
                callback_data="t:f:in_prog",
            ),
            InlineKeyboardButton(
                ("✅ Selesai" if status_filter == "done" else "Selesai"),
                callback_data="t:f:done",
            ),
            InlineKeyboardButton(
                ("✅ Semua" if status_filter == "all" else "Semua"),
                callback_data="t:f:all",
            ),
        ],
        # Row 2: Action buttons
        [
            InlineKeyboardButton("🗑️ Hapus", callback_data=f"t:m:del:{status_filter}"),
            InlineKeyboardButton("❌ Tutup", callback_data="t:close"),
        ],
    ]
    await _safe_edit_or_reply(update, text, InlineKeyboardMarkup(buttons))


def _make_task_cmd(allowed_user_id: int | None) -> Callable[..., Any]:
    """Create CommandHandler callback for /task."""
    async def _cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not _is_authorized(user.id, allowed_user_id):
            return
        await _render_task_view(update, status_filter="pending", mode="view")
    return _cmd


def _make_task_callback(allowed_user_id: int | None) -> Callable[..., Any]:
    """Create CallbackQueryHandler for task menu (prefix t:)."""
    async def _callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not _is_authorized(query.from_user.id, allowed_user_id):
            if query:
                await query.answer("Akses ditolak.", show_alert=True)
            return

        data = query.data or ""
        parts = data.split(":")

        if data == "t:close":
            await query.answer()
            if query.message:
                await query.message.delete()
            return

        # t:f:<filter> (change filter)
        if len(parts) == 3 and parts[1] == "f":
            status_filter = parts[2]
            await query.answer()
            await _render_task_view(update, status_filter=status_filter, mode="view")
            return

        # t:m:del:<filter> (switch to delete mode)
        if len(parts) == 4 and parts[1] == "m" and parts[2] == "del":
            status_filter = parts[3]
            await query.answer()
            await _render_task_view(update, status_filter=status_filter, mode="del")
            return

        # t:act:del:<filter>:<task_id> (execute task delete)
        if len(parts) == 5 and parts[1] == "act" and parts[2] == "del":
            status_filter = parts[3]
            task_id = parts[4]
            success = await repository.delete_task(task_id)
            if success:
                await query.answer("Tugas berhasil dihapus!", show_alert=False)
            else:
                await query.answer("Tugas tidak ditemukan atau sudah dihapus.", show_alert=False)
            await _render_task_view(update, status_filter=status_filter, mode="del")
            return

        await query.answer()
    return _callback


# ============================================================================
# 2. PROFILE MENU (/profil, /profile, /fakta)
# ============================================================================

async def _render_profile_view(
    update: Update,
    category_filter: str = "all",
    mode: str = "view",
) -> None:
    """Render the interactive profile facts dashboard (view mode or delete mode)."""
    cat_map = {
        "all": "Semua",
        "pref": "Preferensi",
        "habit": "Habit",
        "ident": "Identitas",
        "comm": "Komitmen",
    }
    repo_cat = {
        "pref": "preference",
        "habit": "habit",
        "ident": "identity",
        "comm": "commitment",
        "all": "all",
    }.get(category_filter, "all")

    entries = await repository.get_dossier_by_category(category=repo_cat, limit=10)
    display_title = cat_map.get(category_filter, "Semua")

    if not entries:
        text = f"🧠 <b>Fakta Profil Pengguna ({display_title})</b>\n\nBelum ada fakta yang tercatat."
        buttons = [
            [
                InlineKeyboardButton(
                    ("✅ Semua" if category_filter == "all" else "Semua"),
                    callback_data="p:f:all",
                ),
                InlineKeyboardButton(
                    ("✅ Pref" if category_filter == "pref" else "Pref"),
                    callback_data="p:f:pref",
                ),
                InlineKeyboardButton(
                    ("✅ Habit" if category_filter == "habit" else "Habit"),
                    callback_data="p:f:habit",
                ),
                InlineKeyboardButton(
                    ("✅ Identitas" if category_filter == "ident" else "Identitas"),
                    callback_data="p:f:ident",
                ),
            ],
            [InlineKeyboardButton("❌ Tutup", callback_data="p:close")],
        ]
        await _safe_edit_or_reply(update, text, InlineKeyboardMarkup(buttons))
        return

    # Delete Mode
    if mode == "del":
        lines = [
            f"🗑️ <b>Hapus Fakta Profil ({display_title})</b>",
            "Sentuh nomor fakta di bawah untuk menghapusnya dari memori:\n",
        ]
        entry_ids = []
        for idx, e in enumerate(entries, start=1):
            cat_badge = html.escape(str(e.get("category", "general")).upper())
            lines.append(f"<b>{idx}.</b> [{cat_badge}] {html.escape(str(e['fact']))}")
            entry_ids.append(e["id"])

        text = "\n".join(lines)
        markup = _build_number_grid(
            item_ids=entry_ids,
            action_prefix=f"p:act:del:{category_filter}",
            back_callback=f"p:f:{category_filter}",
        )
        await _safe_edit_or_reply(update, text, markup)
        return

    # Normal View Mode
    lines = [
        f"🧠 <b>Fakta Profil Pengguna ({display_title})</b>",
        f"<i>Total: {len(entries)} fakta tercatat</i>\n",
    ]
    for idx, e in enumerate(entries, start=1):
        cat_badge = html.escape(str(e.get("category", "general")).upper())
        lines.append(f"<b>{idx}.</b> [{cat_badge}] {html.escape(str(e['fact']))}")

    text = "\n".join(lines)
    buttons = [
        # Row 1: Category filter tabs
        [
            InlineKeyboardButton(
                ("✅ Semua" if category_filter == "all" else "Semua"),
                callback_data="p:f:all",
            ),
            InlineKeyboardButton(
                ("✅ Pref" if category_filter == "pref" else "Pref"),
                callback_data="p:f:pref",
            ),
            InlineKeyboardButton(
                ("✅ Habit" if category_filter == "habit" else "Habit"),
                callback_data="p:f:habit",
            ),
            InlineKeyboardButton(
                ("✅ Identitas" if category_filter == "ident" else "Identitas"),
                callback_data="p:f:ident",
            ),
        ],
        # Row 2: Action buttons
        [
            InlineKeyboardButton("🗑️ Hapus", callback_data=f"p:m:del:{category_filter}"),
            InlineKeyboardButton("❌ Tutup", callback_data="p:close"),
        ],
    ]
    await _safe_edit_or_reply(update, text, InlineKeyboardMarkup(buttons))


def _make_profile_cmd(allowed_user_id: int | None) -> Callable[..., Any]:
    """Create CommandHandler callback for /profil."""
    async def _cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not _is_authorized(user.id, allowed_user_id):
            return
        await _render_profile_view(update, category_filter="all", mode="view")
    return _cmd


def _make_profile_callback(allowed_user_id: int | None) -> Callable[..., Any]:
    """Create CallbackQueryHandler for profile menu (prefix p:)."""
    async def _callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not _is_authorized(query.from_user.id, allowed_user_id):
            if query:
                await query.answer("Akses ditolak.", show_alert=True)
            return

        data = query.data or ""
        parts = data.split(":")

        if data == "p:close":
            await query.answer()
            if query.message:
                await query.message.delete()
            return

        # p:f:<cat> (filter category)
        if len(parts) == 3 and parts[1] == "f":
            cat_filter = parts[2]
            await query.answer()
            await _render_profile_view(update, category_filter=cat_filter, mode="view")
            return

        # p:m:del:<cat> (switch to delete mode)
        if len(parts) == 4 and parts[1] == "m" and parts[2] == "del":
            cat_filter = parts[3]
            await query.answer()
            await _render_profile_view(update, category_filter=cat_filter, mode="del")
            return

        # p:act:del:<cat>:<entry_id> (execute dossier fact delete)
        if len(parts) == 5 and parts[1] == "act" and parts[2] == "del":
            cat_filter = parts[3]
            entry_id = parts[4]
            del_data = await repository.delete_dossier_entry(entry_id)
            if del_data:
                # Also synchronize removal from USER.md
                fact_text = del_data.get("fact", "")
                if fact_text:
                    memory.remove_markdown_bullet(memory.DEFAULT_USER_MD, fact_text)
                await query.answer("Fakta profil berhasil dihapus!", show_alert=False)
            else:
                await query.answer("Fakta tidak ditemukan atau sudah dihapus.", show_alert=False)
            await _render_profile_view(update, category_filter=cat_filter, mode="del")
            return

        await query.answer()
    return _callback


# ============================================================================
# 3. NOTES MENU (/notes, /note, /catatan)
# ============================================================================

async def _render_notes_view(update: Update, mode: str = "view") -> None:
    """Render the lab notes dashboard (view mode or delete mode)."""
    notes = await repository.get_recent_lab_notes(limit=5)

    if not notes:
        text = "🔬 <b>Catatan Riset Teknis</b>\n\nBelum ada catatan riset yang disimpan."
        buttons = [
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="n:refresh"),
                InlineKeyboardButton("❌ Tutup", callback_data="n:close"),
            ]
        ]
        await _safe_edit_or_reply(update, text, InlineKeyboardMarkup(buttons))
        return

    if mode == "del":
        lines = [
            "🗑️ <b>Hapus Catatan Riset</b>",
            "Sentuh nomor catatan yang ingin dihapus:\n",
        ]
        note_ids = []
        for idx, n in enumerate(notes, start=1):
            tags_str = f" <i>(Tags: {html.escape(str(n['tags']))})</i>" if n.get("tags") else ""
            lines.append(f"<b>{idx}.</b> <b>{html.escape(str(n['topic']))}</b>{tags_str}")
            note_ids.append(n["id"])

        text = "\n".join(lines)
        markup = _build_number_grid(
            item_ids=note_ids,
            action_prefix="n:act:del",
            back_callback="n:view",
        )
        await _safe_edit_or_reply(update, text, markup)
        return

    # Normal View Mode
    lines = [
        "🔬 <b>Catatan Riset Teknis (5 Terakhir)</b>\n",
    ]
    for idx, n in enumerate(notes, start=1):
        tags_str = f" <i>(Tags: {html.escape(str(n['tags']))})</i>" if n.get("tags") else ""
        raw_content = str(n.get("content", ""))
        content_snippet = (raw_content[:80] + "...") if len(raw_content) > 80 else raw_content
        lines.append(f"<b>{idx}.</b> <b>{html.escape(str(n['topic']))}</b>{tags_str}\n   {html.escape(content_snippet)}\n")

    text = "\n".join(lines)
    buttons = [
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="n:refresh"),
            InlineKeyboardButton("🗑️ Hapus", callback_data="n:m:del"),
            InlineKeyboardButton("❌ Tutup", callback_data="n:close"),
        ]
    ]
    await _safe_edit_or_reply(update, text, InlineKeyboardMarkup(buttons))


def _make_notes_cmd(allowed_user_id: int | None) -> Callable[..., Any]:
    """Create CommandHandler callback for /notes."""
    async def _cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not _is_authorized(user.id, allowed_user_id):
            return
        await _render_notes_view(update, mode="view")
    return _cmd


def _make_notes_callback(allowed_user_id: int | None) -> Callable[..., Any]:
    """Create CallbackQueryHandler for notes menu (prefix n:)."""
    async def _callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not _is_authorized(query.from_user.id, allowed_user_id):
            if query:
                await query.answer("Akses ditolak.", show_alert=True)
            return

        data = query.data or ""
        parts = data.split(":")

        if data == "n:close":
            await query.answer()
            if query.message:
                await query.message.delete()
            return

        if data in ("n:view", "n:refresh"):
            if data == "n:refresh":
                await query.answer("Catatan diperbarui.", show_alert=False)
            else:
                await query.answer()
            await _render_notes_view(update, mode="view")
            return

        if data == "n:m:del":
            await query.answer()
            await _render_notes_view(update, mode="del")
            return

        # n:act:del:<note_id> (execute note delete)
        if len(parts) == 4 and parts[1] == "act" and parts[2] == "del":
            note_id = parts[3]
            success = await repository.delete_lab_note(note_id)
            if success:
                await query.answer("Catatan riset berhasil dihapus!", show_alert=False)
            else:
                await query.answer("Catatan tidak ditemukan.", show_alert=False)
            await _render_notes_view(update, mode="del")
            return

        await query.answer()
    return _callback


# ============================================================================
# 4. MEMORY MENU (/memory, /fokus)
# ============================================================================

async def _render_memory_view(update: Update, ask_reset: bool = False) -> None:
    """Render active working memory inspection or reset confirmation dialog."""
    if ask_reset:
        text = (
            "⚠️ <b>Konfirmasi Pengosongan Working Memory</b>\n\n"
            "Apakah Anda yakin ingin mengosongkan file <code>data/MEMORY.md</code>?\n"
            "Tindakan ini akan menghapus seluruh fokus harian yang sedang aktif."
        )
        buttons = [
            [
                InlineKeyboardButton("⚠️ Yakin Kosongkan", callback_data="m:reset:confirm"),
                InlineKeyboardButton("⬅️ Batal", callback_data="m:view"),
            ]
        ]
        await _safe_edit_or_reply(update, text, InlineKeyboardMarkup(buttons))
        return

    content = memory.read_markdown(memory.DEFAULT_MEMORY_MD).strip()
    if not content:
        display_content = "<i>Working memory saat ini kosong.</i>"
    else:
        # Truncate preview if very long to prevent Telegram message overflow
        preview = content[:1500] if len(content) > 1500 else content
        display_content = f"<pre><code>{html.escape(preview)}</code></pre>"

    text = f"📝 <b>Working Memory Aktif (<code>data/MEMORY.md</code>)</b>\n\n{display_content}"
    buttons = [
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="m:view"),
            InlineKeyboardButton("🧹 Kosongkan", callback_data="m:reset:ask"),
            InlineKeyboardButton("❌ Tutup", callback_data="m:close"),
        ]
    ]
    await _safe_edit_or_reply(update, text, InlineKeyboardMarkup(buttons))


def _make_memory_cmd(allowed_user_id: int | None) -> Callable[..., Any]:
    """Create CommandHandler callback for /memory."""
    async def _cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not _is_authorized(user.id, allowed_user_id):
            return
        await _render_memory_view(update, ask_reset=False)
    return _cmd


def _make_memory_callback(allowed_user_id: int | None) -> Callable[..., Any]:
    """Create CallbackQueryHandler for memory menu (prefix m:)."""
    async def _callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not _is_authorized(query.from_user.id, allowed_user_id):
            if query:
                await query.answer("Akses ditolak.", show_alert=True)
            return

        data = query.data or ""

        if data == "m:close":
            await query.answer()
            if query.message:
                await query.message.delete()
            return

        if data == "m:view":
            await query.answer("Working memory diperbarui.", show_alert=False)
            await _render_memory_view(update, ask_reset=False)
            return

        if data == "m:reset:ask":
            await query.answer()
            await _render_memory_view(update, ask_reset=True)
            return

        if data == "m:reset:confirm":
            memory.overwrite_markdown(memory.DEFAULT_MEMORY_MD, "")
            await query.answer("Working memory berhasil dikosongkan!", show_alert=False)
            await _render_memory_view(update, ask_reset=False)
            return

        await query.answer()
    return _callback


# ============================================================================
# MASTER REGISTRATION
# ============================================================================

def register_commands(application: Application, allowed_user_id: int | None) -> None:
    """Register all interactive command and callback query handlers into Telegram Application."""
    # 1. Command Handlers
    application.add_handler(
        CommandHandler(["task", "tasks"], _make_task_cmd(allowed_user_id))
    )
    application.add_handler(
        CommandHandler(["profil", "profile", "fakta"], _make_profile_cmd(allowed_user_id))
    )
    application.add_handler(
        CommandHandler(["notes", "note", "catatan"], _make_notes_cmd(allowed_user_id))
    )
    application.add_handler(
        CommandHandler(["memory", "fokus"], _make_memory_cmd(allowed_user_id))
    )

    # 2. CallbackQuery Handlers (namespaced by prefix)
    application.add_handler(
        CallbackQueryHandler(_make_task_callback(allowed_user_id), pattern=r"^t:")
    )
    application.add_handler(
        CallbackQueryHandler(_make_profile_callback(allowed_user_id), pattern=r"^p:")
    )
    application.add_handler(
        CallbackQueryHandler(_make_notes_callback(allowed_user_id), pattern=r"^n:")
    )
    application.add_handler(
        CallbackQueryHandler(_make_memory_callback(allowed_user_id), pattern=r"^m:")
    )
    logger.info("Interactive Telegram commands and callback handlers registered successfully.")
