"""
Context formatters for vigil.
Functions to format data structures (like database results or queued nudges)
into user-facing strings or prompt contexts.
"""

from typing import Any
from src.core.utils import safe_truncate


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


def format_search_notes_result(results: list[dict[str, Any]], search_query: str) -> str:
    """Format notes search result into a string."""
    if not results:
        return f'No lab notes found for query: "{search_query}".'

    lines = [f'Lab Notes Search Results (query: "{search_query}"):']
    for r in results:
        tags_info = f" (tags: {r.get('tags')})" if r.get("tags") else ""
        lines.append(
            f"- [{r.get('topic', 'Note')}] {safe_truncate(r.get('content', ''))}"
            f"{tags_info} [relevance: {r.get('score', 0)}]"
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


def format_search_chat_result(results: list[dict[str, Any]], search_query: str) -> str:
    """Format chat search result into a string."""
    if not results:
        return f'No chat history found for query: "{search_query}".'

    lines = [f'Chat History Search Results (query: "{search_query}"):']
    for r in results:
        lines.append(
            f"- [{r.get('role', '?').upper()}] {safe_truncate(r.get('content', ''))}"
            f" ({r.get('created_at', '')})"
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


def format_search_chat_follow_up_message(results_block: str, used: int, max_calls: int) -> str:
    """Format the follow-up message carrying search_chat results (no read-tool lock)."""
    header = f"[TOOL EXECUTION RESULTS]\n{results_block}" if results_block else "[TOOL EXECUTION RESULTS]"
    if used < max_calls:
        return (
            f"{header}\n\n"
            f"search_chat usage: {used}/{max_calls}.\n"
            "1. If the results answer the question, answer using the evidence.\n"
            "2. If results are empty or insufficient, call <search_chat> "
            "again with a different query while quota remains.\n"
            "3. If evidence remains insufficient, say so. Never invent "
            "project facts.\n"
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


def format_search_notes_follow_up_message(results_block: str, used: int, max_calls: int) -> str:
    """Format the follow-up message carrying search_notes results (no read-tool lock)."""
    header = f"[TOOL EXECUTION RESULTS]\n{results_block}" if results_block else "[TOOL EXECUTION RESULTS]"
    if used < max_calls:
        return (
            f"{header}\n\n"
            f"search_notes usage: {used}/{max_calls}.\n"
            "1. If the results answer the question, answer using the evidence.\n"
            "2. If results are empty or insufficient, call <search_notes> "
            "again with a different query while quota remains.\n"
            "3. If evidence remains insufficient, say so. Never invent "
            "project facts.\n"
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
) -> str:
    """Format follow-up message when both search_chat and search_notes were executed in the same turn."""
    header = f"[TOOL EXECUTION RESULTS]\n{results_block}" if results_block else "[TOOL EXECUTION RESULTS]"
    can_chat = chat_used < chat_max
    can_notes = notes_used < notes_max
    if can_chat or can_notes:
        allowed = []
        if can_chat:
            allowed.append("<search_chat>")
        if can_notes:
            allowed.append("<search_notes>")
        allowed_str = " or ".join(allowed)

        return (
            f"{header}\n\n"
            f"search_chat usage: {chat_used}/{chat_max}.\n"
            f"search_notes usage: {notes_used}/{notes_max}.\n"
            "1. If the results answer the question, answer using the evidence.\n"
            f"2. If results are empty or insufficient, call {allowed_str} "
            "again with a different query while quota remains.\n"
            "3. If evidence remains insufficient, say so. Never invent "
            "project facts.\n"
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
