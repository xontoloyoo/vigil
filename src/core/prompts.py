"""Prompt factory and template assembler for vigil.

Builds static cacheable system prompts and dynamic user context blocks.
Implements separation of concerns between character persona (SOUL.md)
and XML tool specifications, with Context Merging for deferred nudges.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.tools.registry import get_tools_instruction
from src.config import config
from src.core.prompt_templates import (
    DEFAULT_SOUL_FALLBACK,
    DEFAULT_USER_FALLBACK,
    TEMPLATE_SYSTEM_CONTEXT_TIME,
    TEMPLATE_USER_DOSSIER,
    TEMPLATE_WORKING_MEMORY,
    TEMPLATE_ACTIVE_TASKS_HEADER,
    TEMPLATE_ACTIVE_TASK_ITEM,
    TEMPLATE_ACTIVE_TASK_DUE,
    TEMPLATE_ACTIVE_TASK_NO_DUE,
    TEMPLATE_EXTRA_CONTEXT,
    TEMPLATE_CONTEXT_MERGING_DIRECTIVE,
    TEMPLATE_NUDGE_SYSTEM,
    TEMPLATE_NUDGE_USER,
    TEMPLATE_NIGHTLY_EVAL_SYSTEM,
    TEMPLATE_NIGHTLY_EVAL_USER,
    TEMPLATE_NIGHTLY_NOTES_SECTION,
    TEMPLATE_RECONCILE_DOSSIER_SYSTEM,
    TEMPLATE_RECONCILE_DOSSIER_USER,
)

# Project root path resolution
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = _PROJECT_ROOT / "data"
SOUL_PATH = DATA_DIR / "SOUL.md"
USER_PATH = DATA_DIR / "USER.md"
MEMORY_PATH = DATA_DIR / "MEMORY.md"


def _get_current_time_str() -> str:
    """Return formatted current time string using the configured timezone."""
    tz = ZoneInfo(config.TIMEZONE)
    now = datetime.now(tz)
    tz_name = now.tzname() or "Local"
    return now.strftime(f"%Y-%m-%d %H:%M:%S {tz_name} (%A)")


def load_soul_prompt() -> str:
    """Read persona from data/SOUL.md with defensive fallback."""
    if SOUL_PATH.is_file():
        try:
            content = SOUL_PATH.read_text(encoding="utf-8").strip()
            if content:
                return content
        except Exception:
            pass
    return DEFAULT_SOUL_FALLBACK


def load_user_profile() -> str:
    """Read user facts and working memory from data/*.md with defensive fallback."""
    parts: list[str] = []
    if USER_PATH.is_file():
        try:
            content = USER_PATH.read_text(encoding="utf-8").strip()
            if content:
                parts.append(TEMPLATE_USER_DOSSIER.format(content=content))
        except Exception:
            pass

    if MEMORY_PATH.is_file():
        try:
            content = MEMORY_PATH.read_text(encoding="utf-8").strip()
            if content:
                parts.append(TEMPLATE_WORKING_MEMORY.format(content=content))
        except Exception:
            pass

    if parts:
        return "\n\n".join(parts)
    return DEFAULT_USER_FALLBACK


def build_static_system_prefix() -> str:
    """Construct deterministic static prefix (Persona + XML Tool Spec) for Prompt Caching."""
    soul = load_soul_prompt()
    tools = get_tools_instruction()
    return f"{soul}\n\n{tools}".strip()


def build_chat_prompt(
    user_message: str,
    current_tasks: list[dict[str, Any]] | None = None,
    pending_nudge: str | None = None,
    extra_context: str | None = None,
    history: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    """Assemble standard chat completions prompt with Prompt Caching layout and Context Merging.

    Layout:
    1. Static Prefix (Cacheable): Persona + XML Tools Spec
    2. Dynamic Context: Current Time, User Profile, Tasks Snapshot, Retrieval Context, Context Merging Directive
    3. Chat History (previous turns)
    4. User Message (current turn)
    """
    static_prefix = build_static_system_prefix()

    dynamic_sections: list[str] = [
        TEMPLATE_SYSTEM_CONTEXT_TIME.format(time_str=_get_current_time_str()),
        load_user_profile(),
    ]

    if current_tasks:
        task_lines = [TEMPLATE_ACTIVE_TASKS_HEADER]
        for task in current_tasks:
            title = task.get("title", "Untitled Task")
            due = task.get("due_time")
            due_str = TEMPLATE_ACTIVE_TASK_DUE.format(due=due) if due else TEMPLATE_ACTIVE_TASK_NO_DUE
            status = task.get("status", "pending")
            task_id = task.get("id", "?")
            task_lines.append(
                TEMPLATE_ACTIVE_TASK_ITEM.format(
                    status=status.upper(),
                    id=task_id,
                    title=title,
                    due_str=due_str,
                )
            )
        dynamic_sections.append("\n".join(task_lines))

    if extra_context:
        dynamic_sections.append(TEMPLATE_EXTRA_CONTEXT.format(extra_context=extra_context))

    if pending_nudge:
        dynamic_sections.append(TEMPLATE_CONTEXT_MERGING_DIRECTIVE.format(pending_nudge=pending_nudge))

    dynamic_block = "\n\n".join(dynamic_sections)
    system_prompt = f"{static_prefix}\n\n---\n\n{dynamic_block}".strip()

    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]

    if history:
        validated = [
            msg
            for msg in history
            if isinstance(msg, dict)
            and msg.get("role") in ("user", "assistant")
            and isinstance(msg.get("content"), str)
            and msg["content"].strip()
        ]
        messages.extend(validated)

    messages.append({"role": "user", "content": user_message})

    return messages


def build_nudge_prompt(
    ticket: dict[str, Any] | str,
    user_habits: str | None = None,
) -> list[dict[str, str]]:
    """Assemble single-shot proactive nudge prompt for Heartbeat Runner."""
    soul = load_soul_prompt()
    time_str = _get_current_time_str()

    if isinstance(ticket, dict):
        title = ticket.get("title", "Untitled Task")
        due = ticket.get("due_time", "Overdue")
        ticket_str = f"'{title}' (Due: {due})"
    else:
        ticket_str = str(ticket)

    habits_note = f"\nUser Context: {user_habits}" if user_habits else ""

    system_content = TEMPLATE_NUDGE_SYSTEM.format(
        soul=soul,
        time_str=time_str,
        ticket_str=ticket_str,
        habits_note=habits_note,
    )

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": TEMPLATE_NUDGE_USER},
    ]


def build_nightly_eval_prompt(
    completed_tasks: list[dict[str, Any]],
    pending_tasks: list[dict[str, Any]],
    daily_notes: list[dict[str, Any]] | None = None,
    current_memory: str | None = None,
) -> list[dict[str, str]]:
    """Assemble nightly performance evaluation and recap prompt."""
    soul = load_soul_prompt()
    time_str = _get_current_time_str()

    completed_lines = (
        [f"- {t.get('title', 'Untitled')}" for t in completed_tasks]
        if completed_tasks
        else ["- No tasks completed today."]
    )

    pending_lines = (
        [
            f"- {t.get('title', 'Untitled')} (Due: {t.get('due_time', 'No deadline')})"
            for t in pending_tasks
        ]
        if pending_tasks
        else ["- No pending tasks remaining."]
    )

    notes_section = ""
    if daily_notes:
        note_items = "\n".join(
            f"- [{n.get('topic', 'Note')}] {n.get('content', '')}"
            for n in daily_notes
        )
        notes_section = TEMPLATE_NIGHTLY_NOTES_SECTION.format(note_items=note_items)

    if current_memory is None:
        if MEMORY_PATH.is_file():
            try:
                mem_content = MEMORY_PATH.read_text(encoding="utf-8").strip()
                current_memory_str = mem_content if mem_content else "(No prior working memory recorded)"
            except Exception:
                current_memory_str = "(Failed to read working memory)"
        else:
            current_memory_str = "(No prior working memory recorded)"
    else:
        current_memory_str = current_memory.strip() if current_memory.strip() else "(No prior working memory recorded)"

    system_content = TEMPLATE_NIGHTLY_EVAL_SYSTEM.format(
        soul=soul,
        time_str=time_str,
        completed_count=len(completed_tasks),
        completed_lines="\n".join(completed_lines),
        pending_count=len(pending_tasks),
        pending_lines="\n".join(pending_lines),
        notes_section=notes_section,
        current_memory=current_memory_str,
    )

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": TEMPLATE_NIGHTLY_EVAL_USER},
    ]


def build_dossier_reconciliation_prompt(
    raw_dossier: str,
) -> list[dict[str, str]]:
    """Assemble user dossier reconciliation and deduplication prompt."""
    soul = load_soul_prompt()
    time_str = _get_current_time_str()

    clean_raw = raw_dossier.strip() if raw_dossier.strip() else "(No user dossier records)"

    system_content = TEMPLATE_RECONCILE_DOSSIER_SYSTEM.format(
        soul=soul,
        time_str=time_str,
        raw_dossier=clean_raw,
    )

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": TEMPLATE_RECONCILE_DOSSIER_USER},
    ]

