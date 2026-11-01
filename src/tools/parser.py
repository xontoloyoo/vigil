"""Tool call parser and executor for vigil.

Extracts XML action tags from LLM responses, executes their corresponding
storage operations asynchronously, and returns cleaned user-facing text.
"""

from collections.abc import Awaitable, Callable
import logging
from pathlib import Path
import re
from typing import Any

from src.core.utils import safe_truncate
from src.storage import memory, repository
from src.storage.search import search_lab_notes, search_chat_history
from src.core.context_formatters import (
    format_query_tasks_result,
    format_search_notes_result,
    format_get_dossier_result,
    format_search_chat_result,
)

logger = logging.getLogger(__name__)

# Pattern to capture supported XML tool tags and their contents
_TOOL_TAG_PATTERN = re.compile(
    r"<(?P<tag>record_dossier|save_lab_note|create_task|update_task|delete_task|update_working_memory"
    r"|query_tasks|search_notes|get_dossier|search_chat)"
    r"(?P<attrs>[^>]*)>"
    r"(?P<content>.*?)"
    r"</(?P=tag)>",
    flags=re.DOTALL | re.IGNORECASE,
)

# Pattern to extract key="value" or key='value' attributes from a tag
_ATTR_PATTERN = re.compile(r'([a-zA-Z0-9_-]+)=["\']([^"\']*)["\']')

# Pattern to capture <no_memory_extracted/> (self-closing or paired)
_NO_MEMORY_EXTRACTED_PATTERN = re.compile(
    r"<no_memory_extracted\s*(?:/>|>(?:.*?)</no_memory_extracted>)",
    flags=re.DOTALL | re.IGNORECASE,
)


def _parse_attributes(attrs_str: str) -> dict[str, str]:
    """Extract key-value attributes from raw attribute substring."""
    return dict(_ATTR_PATTERN.findall(attrs_str))


def _clean_response_text(raw_text: str) -> str:
    """Remove tool XML tags and normalize whitespace."""
    if not raw_text or not isinstance(raw_text, str):
        return ""
    # Strip tool tags
    text_without_tags = _TOOL_TAG_PATTERN.sub("", raw_text)
    text_without_tags = _NO_MEMORY_EXTRACTED_PATTERN.sub("", text_without_tags)

    # Trim trailing whitespace on each line
    lines = [line.rstrip() for line in text_without_tags.splitlines()]
    joined_text = "\n".join(lines)

    # Collapse excessive blank lines (3+ into 2) and strip outer whitespace
    cleaned = re.sub(r"\n{3,}", "\n\n", joined_text).strip()
    return cleaned


def _normalize_task_status(raw_status: str) -> str:
    """Normalize task status string to standard internal enums.

    Follows OpenAI Function Calling & Anthropic Tool Use defensive coercion practices:
    - Tolerates hyphenated/spaced variations: 'in-progress', 'in progress', 'started', 'doing', 'working' -> 'in_progress'
    - Tolerates synonyms: 'completed', 'finish', 'finished', 'complete' -> 'done'
    - Tolerates cancellations: 'canceled', 'drop', 'dropped', 'abort', 'aborted' -> 'cancelled'
    - Tolerates pauses/reverts: 'pause', 'paused', 'backlog', 'queue', 'queued', 'todo' -> 'pending'
    """
    s = raw_status.strip().lower()
    if s in ("in_progress", "in-progress", "inprogress", "in progress", "doing", "working", "started"):
        return "in_progress"
    if s in ("done", "completed", "finish", "finished", "complete"):
        return "done"
    if s in ("cancelled", "canceled", "drop", "dropped", "abort", "aborted"):
        return "cancelled"
    if s in ("pending", "pause", "paused", "backlog", "queue", "queued", "todo"):
        return "pending"
    return s


async def parse_and_execute_tools(
    raw_response: str,
    db_path: str | Path | None = None,
    user_md_path: str | Path | None = None,
    memory_md_path: str | Path | None = None,
    allow_read_tools: bool = True,
    recent_history_ids: set[str] | None = None,
    exclude_ids: list[str] | set[str] | None = None,
    exclude_note_ids: list[str] | set[str] | None = None,
    on_trace: Callable[[str], Awaitable[None]] | None = None,
    search_chat_budget: int | None = None,
    search_notes_budget: int | None = None,
    allowed_tools: set[str] | list[str] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Parse XML action tags from raw model response, execute them asynchronously,

    and return cleaned user-facing text along with execution logs.

    search_chat_budget / search_notes_budget: when None, tools follow allow_read_tools (legacy).
    When an int, the tool runs at most that many times in this call regardless
    of allow_read_tools; extra attempts are recorded with status="limit_reached".
    allowed_tools: optional whitelist of permitted tool names. If provided, any tool not in
    this set is blocked from execution and recorded with status="blocked".
    """
    if not raw_response or not isinstance(raw_response, str):
        return "", []

    executed_actions: list[dict[str, Any]] = []
    target_user_md = Path(user_md_path or memory.DEFAULT_USER_MD)
    target_memory_md = Path(memory_md_path or memory.DEFAULT_MEMORY_MD)
    search_chat_remaining = search_chat_budget
    search_notes_remaining = search_notes_budget
    effective_exclude_ids = set(exclude_ids or ()) | set(recent_history_ids or ())
    whitelist = set(allowed_tools) if allowed_tools is not None else None

    # Check for <no_memory_extracted/> tags
    for _ in _NO_MEMORY_EXTRACTED_PATTERN.finditer(raw_response):
        if whitelist is not None and "no_memory_extracted" not in whitelist:
            logger.warning("[Security] Unauthorized tool <no_memory_extracted> blocked by allowed_tools filter.")
            executed_actions.append(
                {
                    "tool": "no_memory_extracted",
                    "status": "blocked",
                    "error": "Tool 'no_memory_extracted' is not permitted in this execution context",
                }
            )
            continue
        executed_actions.append(
            {
                "tool": "no_memory_extracted",
                "status": "success",
            }
        )
    if on_trace and any(a.get("tool") == "no_memory_extracted" and a.get("status") == "success" for a in executed_actions):
        await on_trace("💤 Tidak ada memori baru yang diekstrak")

    # Find all tool tag matches
    matches = list(_TOOL_TAG_PATTERN.finditer(raw_response))

    for match in matches:
        tag_name = match.group("tag").lower()
        raw_attrs = match.group("attrs")
        content = match.group("content").strip()
        attrs = _parse_attributes(raw_attrs)
        actions_before = len(executed_actions)

        logger.debug(
            "[DEBUG] Tool tag detected: <%s> attrs=%s content_length=%d\n  content: %s",
            tag_name,
            attrs,
            len(content),
            content,
        )

        if whitelist is not None and tag_name not in whitelist:
            logger.warning("[Security] Unauthorized tool <%s> blocked by allowed_tools filter.", tag_name)
            executed_actions.append(
                {
                    "tool": tag_name,
                    "status": "blocked",
                    "error": f"Tool '{tag_name}' is not permitted in this execution context",
                }
            )
            continue

        try:
            if tag_name == "record_dossier":
                category = attrs.get("category", "general").strip()
                fact = content
                if not fact:
                    executed_actions.append(
                        {
                            "tool": "record_dossier",
                            "status": "error",
                            "error": "Empty dossier fact content",
                        }
                    )
                    continue

                dossier_id = await repository.add_dossier_entry(
                    category=category, fact=fact, db_path=db_path
                )
                # Append concise bullet to USER.md with verbatim duplicate check
                new_bullet = f"- [{category.upper()}] {fact}"
                existing_user_md = memory.read_markdown(target_user_md)
                existing_lines = {l.strip().lower() for l in existing_user_md.splitlines()}
                if new_bullet.strip().lower() not in existing_lines:
                    memory.append_markdown(
                        target_user_md,
                        new_bullet,
                    )
                executed_actions.append(
                    {
                        "tool": "record_dossier",
                        "status": "success",
                        "id": dossier_id,
                        "category": category,
                        "fact": fact,
                    }
                )
                if on_trace:
                    await on_trace(f'👤 Mencatat profil: "{safe_truncate(fact, 35)}"')

            elif tag_name == "save_lab_note":
                topic = attrs.get("topic", "General Note").strip()
                tags = attrs.get("tags", "").strip()
                note_content = content
                if not note_content:
                    executed_actions.append(
                        {
                            "tool": "save_lab_note",
                            "status": "error",
                            "error": "Empty lab note content",
                        }
                    )
                    continue

                note_id = await repository.add_lab_note(
                    topic=topic, content=note_content, tags=tags, db_path=db_path
                )
                executed_actions.append(
                    {
                        "tool": "save_lab_note",
                        "status": "success",
                        "id": note_id,
                        "topic": topic,
                        "tags": tags,
                    }
                )
                if on_trace:
                    await on_trace(f'💾 Menyimpan catatan: "{safe_truncate(topic, 35)}"')

            elif tag_name == "create_task":
                title = content
                due_time = attrs.get("due")
                if not title:
                    executed_actions.append(
                        {
                            "tool": "create_task",
                            "status": "error",
                            "error": "Empty task title",
                        }
                    )
                    continue

                task_id = await repository.create_task(
                    title=title, due_time=due_time, db_path=db_path
                )
                executed_actions.append(
                    {
                        "tool": "create_task",
                        "status": "success",
                        "id": task_id,
                        "title": title,
                        "due_time": due_time,
                    }
                )
                due_info = f" (Due: {due_time})" if due_time else ""
                if on_trace:
                    await on_trace(f'📝 Menambahkan tugas: "{safe_truncate(title, 35)}"{due_info}')

            elif tag_name == "update_task":
                task_id = attrs.get("id", "").strip()
                raw_status = attrs.get("status", "done").strip()
                new_status = _normalize_task_status(raw_status)
                reason = content

                if not task_id:
                    executed_actions.append(
                        {
                            "tool": "update_task",
                            "status": "error",
                            "error": "Missing task id attribute",
                        }
                    )
                    continue

                success = await repository.update_task_status(
                    task_id=task_id, status=new_status, db_path=db_path
                )
                executed_actions.append(
                    {
                        "tool": "update_task",
                        "status": "success" if success else "failed",
                        "id": task_id,
                        "new_status": new_status,
                        "updated": success,
                        "reason": reason,
                    }
                )
                if on_trace and success:
                    if new_status == "in_progress":
                        await on_trace(f"⏳ Menandai tugas [{task_id}] sedang dikerjakan")
                    elif new_status == "done":
                        await on_trace(f"✅ Menyelesaikan tugas [{task_id}]")
                    elif new_status == "cancelled":
                        await on_trace(f"❌ Membatalkan tugas [{task_id}]")
                    elif new_status == "pending":
                        await on_trace(f"⏸️ Mengembalikan tugas [{task_id}] ke antrean")
                    else:
                        await on_trace(f"📋 Memperbarui tugas [{task_id}] -> {new_status}")

            elif tag_name == "delete_task":
                task_id = attrs.get("id", "").strip() or content.strip()
                if not task_id:
                    executed_actions.append(
                        {
                            "tool": "delete_task",
                            "status": "error",
                            "error": "Missing task ID for deletion",
                        }
                    )
                    continue
                deleted = await repository.delete_task(task_id, db_path=db_path)
                executed_actions.append(
                    {
                        "tool": "delete_task",
                        "status": "success" if deleted else "not_found",
                        "id": task_id,
                    }
                )
                if on_trace and deleted:
                    await on_trace(f"🗑️ Menghapus tugas [{task_id}]")

            elif tag_name == "update_working_memory":
                if not content:
                    executed_actions.append(
                        {
                            "tool": "update_working_memory",
                            "status": "error",
                            "error": "Empty working memory content",
                        }
                    )
                    continue

                memory.overwrite_markdown(target_memory_md, content)
                executed_actions.append(
                    {
                        "tool": "update_working_memory",
                        "status": "success",
                        "content": content,
                    }
                )
                if on_trace:
                    await on_trace('🧠 Memperbarui working memory')

            # ==========================================
            # Read Tools (query data, return results)
            # ==========================================

            elif tag_name == "query_tasks" and allow_read_tools:
                raw_status = attrs.get("status", "all").strip().lower()
                status_filter = _normalize_task_status(raw_status) if raw_status != "all" else "all"
                try:
                    limit_val = int(attrs.get("limit", "10"))
                except ValueError:
                    limit_val = 10

                tasks = await repository.get_tasks_by_status(
                    status=status_filter, limit=limit_val, db_path=db_path
                )
                result_text = format_query_tasks_result(tasks, status_filter)

                executed_actions.append(
                    {
                        "tool": "query_tasks",
                        "type": "read",
                        "status": "success",
                        "read_result": result_text,
                        "count": len(tasks),
                    }
                )
                if on_trace:
                    await on_trace(f"📋 Mengecek daftar tugas ({status_filter})")

            elif tag_name == "search_notes" and (
                allow_read_tools if search_notes_remaining is None else search_notes_remaining > 0
            ):
                if search_notes_remaining is not None:
                    search_notes_remaining -= 1
                search_query = content.strip()
                is_browse = not search_query or search_query == "*"
                effective_exclude_note_ids = list(exclude_note_ids or ())

                if is_browse:
                    if "limit" in attrs:
                        try:
                            parsed_lim = int(attrs["limit"])
                        except ValueError:
                            parsed_lim = 10
                        limit_val = min(max(parsed_lim, 10), 15)
                    else:
                        limit_val = 10

                    results = await repository.get_recent_lab_notes(
                        limit=limit_val,
                        exclude_ids=effective_exclude_note_ids,
                        db_path=db_path,
                    )
                else:
                    try:
                        limit_val = int(attrs.get("limit", "3"))
                    except ValueError:
                        limit_val = 3
                    limit_val = min(max(1, limit_val), 10)

                    results = await search_lab_notes(
                        query=search_query,
                        limit=limit_val,
                        exclude_ids=effective_exclude_note_ids,
                        db_path=db_path,
                    )

                result_text = format_search_notes_result(results, search_query)
                returned_ids = [str(r["id"]) for r in results if r.get("id")]

                executed_actions.append(
                    {
                        "tool": "search_notes",
                        "type": "read",
                        "status": "success",
                        "read_result": result_text,
                        "count": len(results),
                        "returned_ids": returned_ids,
                    }
                )
                if on_trace:
                    if search_query and search_query != "*":
                        await on_trace(f'🔍 Mencari catatan: "{safe_truncate(search_query, 35)}"')
                    else:
                        await on_trace("🔍 Menjelajahi katalog catatan teknis")

            elif tag_name == "search_notes" and search_notes_remaining is not None:
                # Budget exhausted: not executed; reported so the orchestrator can inform the LLM.
                executed_actions.append(
                    {
                        "tool": "search_notes",
                        "status": "limit_reached",
                        "query": content.strip(),
                    }
                )

            elif tag_name == "get_dossier" and allow_read_tools:
                category_filter = content.strip().lower() if content.strip() else None
                try:
                    limit_val = int(attrs.get("limit", "5"))
                except ValueError:
                    limit_val = 5

                entries = await repository.get_recent_dossier(
                    limit=limit_val, db_path=db_path
                )
                if category_filter:
                    entries = [
                        e for e in entries
                        if e.get("category", "").lower() == category_filter
                    ]

                result_text = format_get_dossier_result(entries, limit_val, category_filter)

                executed_actions.append(
                    {
                        "tool": "get_dossier",
                        "type": "read",
                        "status": "success",
                        "read_result": result_text,
                        "count": len(entries),
                    }
                )
                filter_info = f" ({category_filter})" if category_filter else ""
                if on_trace:
                    await on_trace(f'👤 Membaca profil preferensi{filter_info}')

            elif tag_name == "search_chat" and (
                allow_read_tools if search_chat_remaining is None else search_chat_remaining > 0
            ):
                if search_chat_remaining is not None:
                    search_chat_remaining -= 1
                search_query = content.strip()

                date_val = attrs.get("date")
                if date_val:
                    date_val = date_val.strip()

                raw_days_ago = attrs.get("days_ago") or attrs.get("days-ago")
                days_ago_val: int | None = None
                if raw_days_ago is not None:
                    try:
                        days_ago_val = int(raw_days_ago.strip())
                    except ValueError:
                        days_ago_val = None

                try:
                    limit_val = int(attrs.get("limit", "3"))
                except ValueError:
                    limit_val = 3
                limit_val = min(max(1, limit_val), 10)

                # Bounded over-fetch: request target limit + recent history size to prevent premature truncation
                fetch_limit = limit_val + (len(effective_exclude_ids) if effective_exclude_ids else 0)
                results = await search_chat_history(
                    query=search_query,
                    limit=fetch_limit,
                    date_str=date_val,
                    days_ago=days_ago_val,
                    exclude_ids=effective_exclude_ids,
                    db_path=db_path,
                )

                # Dedup safety net: filter out messages already in exclude_ids
                if effective_exclude_ids and results:
                    pre_dedup = len(results)
                    results = [
                        r for r in results
                        if r.get("id") not in effective_exclude_ids
                    ]
                    dedup_removed = pre_dedup - len(results)
                    if dedup_removed > 0:
                        logger.info(
                            "search_chat dedup: %d → %d (%d already in excluded history)",
                            pre_dedup,
                            len(results),
                            dedup_removed,
                        )

                # Slice to the desired final limit
                results = results[:limit_val]

                result_text = format_search_chat_result(results, search_query)

                returned_ids: list[str] = []
                for r in results:
                    if r.get("id"):
                        returned_ids.append(r["id"])
                    if r.get("paired_message") and r["paired_message"].get("id"):
                        returned_ids.append(r["paired_message"]["id"])

                executed_actions.append(
                    {
                        "tool": "search_chat",
                        "type": "read",
                        "status": "success",
                        "read_result": result_text,
                        "count": len(results),
                        "returned_ids": returned_ids,
                    }
                )
                if on_trace:
                    if search_query:
                        await on_trace(f'💬 Mencari riwayat obrolan: "{safe_truncate(search_query, 35)}"')
                    elif days_ago_val is not None:
                        await on_trace(f'💬 Menelusuri riwayat obrolan ({days_ago_val} hari lalu)')
                    elif date_val:
                        await on_trace(f'💬 Menelusuri riwayat obrolan tanggal {date_val}')
                    else:
                        await on_trace('💬 Menelusuri riwayat obrolan terdahulu')

            elif tag_name == "search_chat" and search_chat_remaining is not None:
                # Budget exhausted: not executed; reported so the orchestrator can inform the LLM.
                executed_actions.append(
                    {
                        "tool": "search_chat",
                        "status": "limit_reached",
                        "query": content.strip(),
                    }
                )

        except Exception as exc:
            logger.error("Failed to execute tool tag %s: %s", tag_name, exc, exc_info=True)
            executed_actions.append(
                {
                    "tool": tag_name,
                    "status": "error",
                    "error": str(exc),
                    "content": content,
                }
            )
        finally:
            # Attach the verbatim XML tag to executed read actions so the orchestrator
            # can replay it in the ephemeral multi-turn loop (never persisted).
            # Runs on `continue` paths too; write / limit_reached actions are skipped.
            for action in executed_actions[actions_before:]:
                if action.get("type") == "read":
                    action["raw_tag"] = match.group(0)

    cleaned_text = _clean_response_text(raw_response)
    return cleaned_text, executed_actions
