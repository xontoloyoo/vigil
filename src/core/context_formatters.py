"""
Context formatters for vigil.
Functions to format data structures (like database results or queued nudges)
into user-facing strings or prompt contexts.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import re
from typing import Any
from src.config import config
from src.core.utils import safe_truncate, to_local_timestamp_str


def format_query_tasks_result(tasks: list[dict[str, Any]], status_filter: str) -> str:
    """Format tasks query result into a string."""
    if not tasks:
        return f"No tasks found (filter: {status_filter})."
    
    lines = [f"Task Query Results (filter: {status_filter}):"]
    for t in tasks:
        due_info = f" (Due: {t.get('due_time')})" if t.get("due_time") else ""
        lines.append(
            f"- [{t.get('status', '?').upper()}] {t.get('id')}: "
            f"\"{t.get('title', '')}\"{due_info}"
        )
    lines.append(f"Total: {len(tasks)} tasks found.")
    return "\n".join(lines)


def format_search_notes_result(results: list[dict[str, Any]], search_query: str = "") -> str:
    """Format notes search result into a string."""
    if not results:
        return f'No lab notes found for query: "{search_query}".' if search_query and search_query != "*" else "No lab notes found."

    header = (
        f'Lab Notes Search Results (query: "{search_query}"):'
        if search_query and search_query != "*"
        else "Lab Notes Catalog (Recent Notes):"
    )
    lines = [header]
    for r in results:
        tags_info = f" (tags: {r.get('tags')})" if r.get("tags") else ""
        raw_created = r.get("created_at")
        if raw_created:
            created_str = raw_created if r.get("_tz_converted") else to_local_timestamp_str(raw_created)
            time_info = f" ({created_str})" if created_str else ""
        else:
            time_info = ""
        score_val = r.get("score")
        score_info = f" [relevance: {score_val}]" if score_val is not None else ""
        lines.append(
            f"- [{r.get('topic', 'Note')}] {safe_truncate(r.get('content', ''))}"
            f"{tags_info}{time_info}{score_info}"
        )
    lines.append(f"Total: {len(results)} notes found.")
    return "\n".join(lines)


def format_get_dossier_result(entries: list[dict[str, Any]], limit: int, category_filter: str | None = None) -> str:
    """Format dossier entries into a string."""
    if not entries:
        return "No user dossier records found."

    filter_note = f", category: {category_filter}" if category_filter else ""
    lines = [f"User Dossier Profile (limit: {limit}{filter_note}):"]
    for e in entries:
        lines.append(f"- [{e.get('category', 'general').upper()}] {e.get('fact', '')}")
    lines.append(f"Total: {len(entries)} entries found.")
    return "\n".join(lines)


def format_search_chat_result(results: list[dict[str, Any]], search_query: str = "") -> str:
    """Format chat search result into a string, hydrating turn pairs if present."""
    if not results:
        return f'No chat history found for query: "{search_query}".' if search_query else "No chat history found."

    header = (
        f'Chat History Search Results (query: "{search_query}"):'
        if search_query
        else "Chat History Search Results:"
    )
    lines = [header]
    for r in results:
        paired = r.get("paired_message")
        raw_r_time = r.get("created_at", "")
        r_time = raw_r_time if r.get("_tz_converted") else to_local_timestamp_str(raw_r_time)
        r_time_str = f" ({r_time})" if r_time else ""

        if paired:
            raw_p_time = paired.get("created_at", "")
            p_time = raw_p_time if paired.get("_tz_converted") else to_local_timestamp_str(raw_p_time)
            p_time_str = f" ({p_time})" if p_time else ""

            if r.get("role") == "user":
                lines.append(
                    f"- [USER] {safe_truncate(r.get('content', ''))}{r_time_str} [relevance: {r.get('score', 0)}]\n"
                    f"  ↳ [{paired.get('role', 'assistant').upper()}] {safe_truncate(paired.get('content', ''))}"
                )
            else:
                lines.append(
                    f"- [{paired.get('role', 'user').upper()}] {safe_truncate(paired.get('content', ''))}{p_time_str}\n"
                    f"  ↳ [ASSISTANT] {safe_truncate(r.get('content', ''))}{r_time_str} [relevance: {r.get('score', 0)}]"
                )
        else:
            lines.append(
                f"- [{r.get('role', '?').upper()}] {safe_truncate(r.get('content', ''))}"
                f"{r_time_str}"
                f" [relevance: {r.get('score', 0)}]"
            )
    lines.append(f"Total: {len(results)} messages found.")
    return "\n".join(lines)


def format_nudges_for_prompt(nudges: list[dict[str, Any]]) -> str:
    """Format pending reminder tickets into a concise prompt data block.

    Returns an empty string if there are no pending nudges.
    """
    if not nudges:
        return ""

    lines = ["[PENDING OVERDUE TASKS / NUDGES:"]
    for item in nudges:
        task_id = item.get("id") or item.get("task_id", "")
        id_info = f"[{task_id}] " if task_id else ""
        title = (
            item.get("title")
            or item.get("text")
            or item.get("content")
            or item.get("message")
            or str(item)
        )
        due = item.get("due_time") or item.get("due")
        due_info = f" (Due: {due})" if due else ""
        lines.append(f"- {id_info}{title}{due_info}")

    lines.append("]")
    return "\n".join(lines)


def format_follow_up_user_message(results_block: str) -> str:
    """Format the internal runtime follow-up driver message with read results."""
    return (
        f"[TOOL EXECUTION RESULTS]\n"
        f"{results_block}\n\n"
        f"Synthesize the data above to directly answer the user inquiry in natural Indonesian. "
        f"DO NOT emit any read query tool tags."
    )


def format_search_chat_follow_up_message(
    results_block: str,
    used: int,
    max_calls: int,
    has_results: bool | None = None,
    can_search_notes: bool = True,
) -> str:
    """Format the follow-up message carrying search_chat results (no read-tool lock)."""
    header = f"[TOOL EXECUTION RESULTS]\n{results_block}" if results_block else "[TOOL EXECUTION RESULTS]"

    # Fallback backward-compatibility detection
    if has_results is None:
        has_results = bool(results_block) and not results_block.strip().startswith("No chat history found")

    if used < max_calls:
        if not has_results:
            cross_tool_hint = (
                " Alternatively, if this topic might be stored as a technical note or configuration, call <search_notes>."
                if can_search_notes
                else ""
            )
            return (
                f"{header}\n\n"
                f"search_chat usage: {used}/{max_calls} (0 results).\n"
                "MANDATORY RETRY: The previous query returned ZERO matches. You are FORBIDDEN from giving up or answering 'not found' while search quota remains.\n"
                "1. Immediately emit <search_chat> again using a DIFFERENT, broader, or single-noun keyword (e.g., use root topic or synonym, never repeat the failed query)."
                f"{cross_tool_hint}\n"
                "2. Output ONLY the raw XML tool tag with zero conversational text."
            )
        else:
            cross_tool_rule = (
                "\n3. NEGATIVE CLAIM GUARD: If the user asks for OTHER details beyond what is shown above (e.g., 'selain X', 'apa lagi'), you are FORBIDDEN from claiming nothing else exists until you have ALSO checked <search_notes> or called <search_chat> again (previous results are auto-excluded on retry)."
                if can_search_notes
                else "\n3. Note: Repeating <search_chat> will automatically exclude the messages shown above to reveal deeper results."
            )
            return (
                f"{header}\n\n"
                f"search_chat usage: {used}/{max_calls}.\n"
                "1. If the results above COMPLETELY answer the user's question, synthesize the answer directly in natural Indonesian (DO NOT emit any read tool tags).\n"
                f"2. If the results are incomplete or only cover part of what the user asked, call <search_chat>{' or <search_notes>' if can_search_notes else ''} with a different keyword while quota remains."
                f"{cross_tool_rule}\n"
                "4. Never invent project facts outside the provided evidence, and NEVER offer to break down or elaborate on extra points if the retrieved record only contains a brief summary.\n"
                "Do not reveal internal reasoning."
            )

    return (
        f"{header}\n\n"
        f"No more searches permitted. DO NOT call any read tools. "
        f"Answer the user in natural Indonesian using the available information. "
        f"If the requested information was not found or evidence is insufficient, honestly state so; "
        f"do not fabricate details, invent project configurations, or assume unverified facts. "
        f"Never present general knowledge as stored project records."
    )


def format_search_chat_limit_message(results_block: str, max_calls: int) -> str:
    """Format the notice sent when a search_chat request exceeds the per-message limit."""
    parts: list[str] = []
    if results_block:
        parts.append(f"[TOOL EXECUTION RESULTS]\n{results_block}")
    parts.append(
        "[search_chat NOT EXECUTED]\n"
        f"The search_chat usage limit has been reached ({max_calls}/{max_calls}). "
        "DO NOT call any read tools. "
        "Answer the user in natural Indonesian using the available information. "
        "If the requested information was not found or evidence is insufficient, state this honestly; "
        "do not fabricate or speculate on missing details."
    )
    return "\n\n".join(parts)


def format_search_notes_follow_up_message(
    results_block: str,
    used: int,
    max_calls: int,
    has_results: bool | None = None,
    can_search_chat: bool = True,
) -> str:
    """Format the follow-up message carrying search_notes results (no read-tool lock)."""
    header = f"[TOOL EXECUTION RESULTS]\n{results_block}" if results_block else "[TOOL EXECUTION RESULTS]"

    # Fallback backward-compatibility detection
    if has_results is None:
        has_results = bool(results_block) and not results_block.strip().startswith("No lab notes found")

    if used < max_calls:
        if not has_results:
            cross_tool_hint = (
                " Alternatively, if this was discussed casually in conversation rather than saved as a formal note, call <search_chat>."
                if can_search_chat
                else ""
            )
            return (
                f"{header}\n\n"
                f"search_notes usage: {used}/{max_calls} (0 results).\n"
                "MANDATORY RETRY: The previous query returned ZERO matches. You are FORBIDDEN from giving up or answering 'not found' while search quota remains.\n"
                "1. Immediately emit <search_notes> again using a DIFFERENT, broader, or single-noun keyword (never repeat the failed query)."
                f"{cross_tool_hint}\n"
                "2. Output ONLY the raw XML tool tag with zero conversational text."
            )
        else:
            cross_tool_rule = (
                "\n3. NEGATIVE CLAIM GUARD: If the user asks for OTHER details beyond what is shown above (e.g., 'selain X', 'apa lagi'), you are FORBIDDEN from claiming nothing else exists until you have ALSO checked <search_chat> or called <search_notes> again."
                if can_search_chat
                else "\n3. Note: Repeating <search_notes> will reveal deeper results."
            )
            return (
                f"{header}\n\n"
                f"search_notes usage: {used}/{max_calls}.\n"
                "1. If the results above COMPLETELY answer the user's question, synthesize the answer directly in natural Indonesian (DO NOT emit any read tool tags).\n"
                f"2. If the results are incomplete or only cover part of what the user asked, call <search_notes>{' or <search_chat>' if can_search_chat else ''} with a different keyword while quota remains."
                f"{cross_tool_rule}\n"
                "4. Never invent project facts outside the provided evidence, and NEVER offer to break down or elaborate on extra points if the retrieved record only contains a brief summary.\n"
                "Do not reveal internal reasoning."
            )

    return (
        f"{header}\n\n"
        f"No more searches permitted. DO NOT call any read tools. "
        f"Answer the user in natural Indonesian using the available information. "
        f"If the requested information was not found or evidence is insufficient, honestly state so; "
        f"do not fabricate details, invent project configurations, or assume unverified facts. "
        f"Never present general knowledge as stored project records."
    )


def format_search_notes_limit_message(results_block: str, max_calls: int) -> str:
    """Format the notice sent when a search_notes request exceeds the per-message limit."""
    parts: list[str] = []
    if results_block:
        parts.append(f"[TOOL EXECUTION RESULTS]\n{results_block}")
    parts.append(
        "[search_notes NOT EXECUTED]\n"
        f"The search_notes usage limit has been reached ({max_calls}/{max_calls}). "
        "DO NOT call any read tools. "
        "Answer the user in natural Indonesian using the available information. "
        "If the requested information was not found or evidence is insufficient, state this honestly; "
        "do not fabricate or speculate on missing details."
    )
    return "\n\n".join(parts)


def format_multi_search_follow_up_message(
    results_block: str,
    chat_used: int,
    chat_max: int,
    notes_used: int,
    notes_max: int,
    has_results: bool | None = None,
) -> str:
    """Format follow-up message when both search_chat and search_notes were executed in the same turn."""
    header = f"[TOOL EXECUTION RESULTS]\n{results_block}" if results_block else "[TOOL EXECUTION RESULTS]"
    can_chat = chat_used < chat_max
    can_notes = notes_used < notes_max

    if has_results is None:
        has_results = bool(results_block) and (
            "Total: " in results_block
            or (
                not results_block.strip().startswith("No chat history found")
                and not results_block.strip().startswith("No lab notes found")
            )
        )

    if can_chat or can_notes:
        allowed = []
        if can_chat:
            allowed.append("<search_chat>")
        if can_notes:
            allowed.append("<search_notes>")
        allowed_str = " or ".join(allowed)

        if not has_results:
            return (
                f"{header}\n\n"
                f"search_chat usage: {chat_used}/{chat_max}.\n"
                f"search_notes usage: {notes_used}/{notes_max}.\n"
                "MANDATORY RETRY: The previous queries returned ZERO matches. You are FORBIDDEN from giving up or answering 'not found' while search quota remains.\n"
                f"1. Immediately emit {allowed_str} again using a DIFFERENT, broader, or single-noun keyword (never repeat failed queries).\n"
                "2. Output ONLY the raw XML tool tag with zero conversational text."
            )

        return (
            f"{header}\n\n"
            f"search_chat usage: {chat_used}/{chat_max}.\n"
            f"search_notes usage: {notes_used}/{notes_max}.\n"
            "1. If the results above COMPLETELY answer the user's question, synthesize the answer directly in natural Indonesian (DO NOT emit any read tool tags).\n"
            f"2. If results are off-topic or incomplete, call {allowed_str} again with a different query while quota remains.\n"
            "3. NEGATIVE CLAIM GUARD: If the user asks for OTHER details beyond what is shown above (e.g., 'selain X', 'apa lagi'), you are FORBIDDEN from claiming nothing else exists while search quota remains.\n"
            "4. Never invent project facts outside the provided evidence, and NEVER offer to break down or elaborate on extra points if the retrieved record only contains a brief summary.\n"
            "Do not reveal internal reasoning."
        )

    return (
        f"{header}\n\n"
        f"No more searches permitted. DO NOT call any read tools. "
        f"Answer the user in natural Indonesian using the available information. "
        f"If the requested information was not found or evidence is insufficient, honestly state so; "
        f"do not fabricate details, invent project configurations, or assume unverified facts. "
        f"Never present general knowledge as stored project records."
    )


def format_multi_search_limit_message(
    results_block: str,
    chat_limit_reached: bool,
    chat_max: int,
    notes_limit_reached: bool,
    notes_max: int,
) -> str:
    """Format notice when search_chat and/or search_notes exceeded limits in a turn."""
    parts: list[str] = []
    if results_block:
        parts.append(f"[TOOL EXECUTION RESULTS]\n{results_block}")
    notices: list[str] = []
    if chat_limit_reached:
        notices.append(
            f"[search_chat NOT EXECUTED]\n"
            f"The search_chat usage limit has been reached ({chat_max}/{chat_max})."
        )
    if notes_limit_reached:
        notices.append(
            f"[search_notes NOT EXECUTED]\n"
            f"The search_notes usage limit has been reached ({notes_max}/{notes_max})."
        )
    notices.append(
        "DO NOT call any read tools. "
        "Answer the user in natural Indonesian using the available information. "
        "If the requested information was not found or evidence is insufficient, state this honestly; "
        "do not fabricate or speculate on missing details."
    )
    parts.append("\n\n".join(notices))
    return "\n\n".join(parts)


def _format_local_user_timestamp(created_at_val: Any) -> str | None:
    """Format created_at (assumed UTC if naive) into local 'YYYY-MM-DD HH:MM'.

    Returns None if created_at_val is missing, empty, or unparseable.
    """
    if not created_at_val:
        return None

    if isinstance(created_at_val, datetime):
        dt = created_at_val
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        tz = ZoneInfo(config.TIMEZONE)
        return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")

    s = str(created_at_val).strip()
    if not s:
        return None

    # Try ISO parsing first
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        tz = ZoneInfo(config.TIMEZONE)
        return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        pass

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            tz = ZoneInfo(config.TIMEZONE)
            return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            pass

    return None


def format_history_with_tool_context(
    history: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Format sliding-window chat history for LLM prompt injection.

    Injects local timestamp '[YYYY-MM-DD HH:MM]' into user turns. Injects
    persistent tool_context into the paired user message for at most the
    latest 2 interaction pairs with non-empty tool_context. Assistant bubbles
    remain 100% pure response text. Older interactions omit tool_context to
    preserve token budget.
    """
    if not history:
        return []

    # Identify consecutive (user, assistant) pairs
    pairs: list[tuple[int, int]] = []
    for i in range(len(history) - 1):
        if history[i].get("role") == "user" and history[i + 1].get("role") == "assistant":
            pairs.append((i, i + 1))

    # Select at most 2 newest interaction pairs that actually have non-empty tool_context
    injections: dict[int, str] = {}
    for u_idx, a_idx in reversed(pairs):
        a_msg = history[a_idx]
        tool_ctx = a_msg.get("tool_context")
        if tool_ctx and isinstance(tool_ctx, str) and tool_ctx.strip():
            injections[u_idx] = tool_ctx.strip()
            if len(injections) >= 2:
                break

    formatted: list[dict[str, str]] = []
    for idx, msg in enumerate(history):
        role = str(msg.get("role") or "user")
        content = str(msg.get("content") or "")
        if role == "user":
            if not re.match(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]", content):
                ts = _format_local_user_timestamp(msg.get("created_at"))
                if ts:
                    content = f"[{ts}]\n{content}" if content else f"[{ts}]"
        if idx in injections:
            content = f"{content}\n\n[SYSTEM: RETRIEVED DATABASE EVIDENCE FOR THIS TURN]\n{injections[idx]}"
        formatted.append({"role": role, "content": content})

    return formatted


def _format_transcript_time(created_at_val: Any) -> str:
    """Extract HH:MM from created_at in local config.TIMEZONE."""
    if not created_at_val:
        return "00:00"
    s = str(created_at_val).strip()
    if re.fullmatch(r"\d{2}:\d{2}", s):
        return s

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S.%f",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            tz = ZoneInfo(config.TIMEZONE)
            return dt.astimezone(tz).strftime("%H:%M")
        except ValueError:
            pass

    m = re.search(r"\b(\d{2}:\d{2})\b", s)
    if m:
        return m.group(1)
    return "00:00"


def format_daily_transcript_for_extraction(messages: list[dict[str, Any]]) -> str:
    """Format daily chat transcript into chronological lines for memory extraction.

    Format: [{HH:MM}] {ROLE}: {content}
    If assistant turn has non-empty tool_context, appends:
    [Note: Read-Only Recall from DB]
    """
    if not messages:
        return "(No messages recorded today)"

    lines: list[str] = []
    for msg in messages:
        role = str(msg.get("role", "user")).upper()
        content = (msg.get("content") or "").strip()
        time_str = _format_transcript_time(msg.get("created_at"))

        recall_note = ""
        if role == "ASSISTANT":
            tool_ctx = msg.get("tool_context")
            if tool_ctx and isinstance(tool_ctx, str) and tool_ctx.strip():
                recall_note = " [Note: Read-Only Recall from DB]"

        lines.append(f"[{time_str}] {role}: {content}{recall_note}")

    return "\n".join(lines)


def format_existing_dossier_for_extraction(entries: list[dict[str, Any]]) -> str:
    """Format existing user dossier entries for silent memory extraction prompt."""
    if not entries:
        return "(No existing user dossier records)"
    lines = []
    for e in entries:
        cat = str(e.get("category", "general")).upper()
        fact = str(e.get("fact", "")).strip()
        lines.append(f"- [{cat}] {fact}")
    return "\n".join(lines)


def format_existing_notes_index_for_extraction(notes: list[dict[str, Any]]) -> str:
    """Format existing lab notes index for silent memory extraction prompt."""
    if not notes:
        return "(No existing lab notes recorded)"
    lines = []
    for n in notes:
        topic = str(n.get("topic", "")).strip()
        tags = str(n.get("tags", "")).strip()
        tags_str = f" (tags: {tags})" if tags else ""
        content = safe_truncate(str(n.get("content", "")).strip(), 120)
        lines.append(f'- Topic: "{topic}"{tags_str} -> {content}')
    return "\n".join(lines)

