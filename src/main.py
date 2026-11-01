"""Main system orchestrator for vigil.

Initializes database and search indexes, registers callbacks, wires
the Telegram interface with prompt construction, tool execution,
and autonomous heartbeat reminders, and handles graceful shutdown.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
import logging
from pathlib import Path
import re
import signal
from typing import Any
from zoneinfo import ZoneInfo

from src.config import config
from src.core.eval_logger import eval_tracer
from src.core.llm import LLMError, llm_client
from src.core.prompts import (
    build_chat_prompt,
    build_dossier_reconciliation_prompt,
    build_nightly_eval_prompt,
    build_nudge_prompt,
    build_silent_memory_extraction_prompt,
)
from src.core.prompt_templates import (
    SYSTEM_PROMPT_MEMORY_EXTRACTION,
    TEMPLATE_SILENT_MEMORY_EXTRACTION,
)
from src.core.traffic import traffic_controller
from src.engine.heartbeat import HeartbeatRunner
from src.interfaces.telegram import telegram_interface
from src.storage import memory, repository
from src.storage.database import init_db
from src.storage.repository import (
    get_recent_chat_history,
    get_chat_history_today,
    prune_chat_history,
    save_chat_message,
)
from src.storage.search import (
    init_chat_history_search_index,
    init_search_index,
)
from src.core.utils import safe_truncate
from src.tools.parser import parse_and_execute_tools
from src.core.context_formatters import (
    format_nudges_for_prompt,
    format_follow_up_user_message,
    format_search_chat_follow_up_message,
    format_search_chat_limit_message,
    format_search_notes_follow_up_message,
    format_search_notes_limit_message,
    format_multi_search_follow_up_message,
    format_multi_search_limit_message,
    format_history_with_tool_context,
    format_daily_transcript_for_extraction,
    format_existing_dossier_for_extraction,
    format_existing_notes_index_for_extraction,
)

# Configure structured logging
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("vigil.main")

# Max real search_chat executions per incoming user message (counted by the orchestrator).
MAX_SEARCH_CHAT_CALLS = 3
# Max real search_notes executions per incoming user message (counted by the orchestrator).
MAX_SEARCH_NOTES_CALLS = 3


def _count_search_chat_runs(actions: list[dict[str, Any]]) -> int:
    """Count search_chat tags that were processed (consumed budget) in a parse result."""
    return sum(
        1 for a in actions if a.get("tool") == "search_chat" and a.get("type") == "read"
    )


def _count_search_notes_runs(actions: list[dict[str, Any]]) -> int:
    """Count search_notes tags that were processed (consumed budget) in a parse result."""
    return sum(
        1 for a in actions if a.get("tool") == "search_notes" and a.get("type") == "read"
    )


def _build_ephemeral_assistant_turn(actions: list[dict[str, Any]], fallback_text: str) -> str:
    """Build the in-loop (ephemeral) assistant message for a read-tool turn.

    Replays only the verbatim XML of read tools executed in that turn so the model
    keeps its tool-calling pattern on the next iteration. Write tags are excluded
    (they already ran; replaying them invites duplicate writes) and so is any intro
    text. This message lives only in the loop `conversation`; it is never persisted
    to chat_history nor sent to Telegram.
    """
    read_tags = [
        a["raw_tag"] for a in actions if a.get("type") == "read" and a.get("raw_tag")
    ]
    if read_tags:
        return "\n".join(read_tags)
    # No read tag executed this turn (e.g. limit-only notice): keep legacy fallback.
    return fallback_text or "(Memproses query data...)"

# Instantiate default heartbeat runner
heartbeat_runner = HeartbeatRunner(
    check_interval_seconds=int(config.HEARTBEAT_INTERVAL_MINUTES * 60)
    if config.HEARTBEAT_INTERVAL_MINUTES
    else 60,
    nudge_cooldown_minutes=30,
    traffic_ctrl=traffic_controller,
)


async def handle_heartbeat_dispatch(ticket: dict[str, Any]) -> None:
    """Proactive dispatch callback invoked by HeartbeatRunner when user is idle."""
    logger.info("Heartbeat proactive dispatch triggered for task: %s", ticket.get("id"))
    try:
        messages = build_nudge_prompt(ticket)
        raw_nudge = await llm_client.generate_response(
            messages, temperature=0.7, max_tokens=150
        )
        cleaned_nudge = raw_nudge.strip()
        logger.info("Generated proactive reminder message: %s", cleaned_nudge)
        delivered = await telegram_interface.send_proactive_nudge(cleaned_nudge)
        if delivered:
            # Fix 1: Persist proactive nudge to chat_history so context is not lost
            await save_chat_message("assistant", cleaned_nudge)
            logger.info("Proactive nudge saved to chat_history.")
        else:
            logger.warning(
                "Failed to deliver proactive reminder to Telegram for task: %s",
                ticket.get("id"),
            )
    except Exception as exc:
        logger.error("Error in heartbeat proactive dispatch: %s", exc, exc_info=True)


async def handle_nightly_evaluation() -> None:
    """Nightly performance evaluation and memory consolidation callback."""
    logger.info("Executing nightly performance evaluation & memory consolidation pipeline.")
    try:
        # 1. Fetch performance data
        completed_tasks = await repository.get_completed_tasks_today(limit=20)
        pending_tasks = await repository.get_pending_tasks()

        # Guard Clause: Silent Skip if zero task activity today and no pending tasks
        if not completed_tasks and not pending_tasks:
            logger.info(
                "Nightly evaluation skipped: no tasks completed today and no active/pending tasks."
            )
            return

        daily_notes = await repository.get_recent_lab_notes(limit=10)
        current_memory = memory.read_markdown(memory.DEFAULT_MEMORY_MD)

        # 2. Assemble nightly eval prompt
        messages = build_nightly_eval_prompt(
            completed_tasks=completed_tasks,
            pending_tasks=pending_tasks,
            daily_notes=daily_notes,
            current_memory=current_memory,
        )

        # 3. Call LLM
        raw_eval = await llm_client.generate_response(
            messages, temperature=0.7, max_tokens=1000
        )
        logger.debug(
            "[DEBUG] Raw nightly evaluation LLM response (%d chars):\n%s",
            len(raw_eval),
            raw_eval,
        )

        # 4. Extract <working_memory>...</working_memory> or <update_working_memory>...</update_working_memory>
        mem_match = re.search(
            r"<(?:update_)?working_memory>(.*?)</(?:update_)?working_memory>",
            raw_eval,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if mem_match:
            new_memory = mem_match.group(1).strip()
            if new_memory:
                memory.overwrite_markdown(memory.DEFAULT_MEMORY_MD, new_memory)
                logger.info(
                    "Updated data/MEMORY.md via nightly evaluation consolidation (%d chars).",
                    len(new_memory),
                )
        else:
            logger.warning(
                "No <working_memory> tag found in LLM nightly evaluation; preserving existing MEMORY.md."
            )

        # 5. Extract clean user review text for Telegram
        clean_eval = re.sub(
            r"<(?:update_)?working_memory>.*?</(?:update_)?working_memory>",
            "",
            raw_eval,
            flags=re.DOTALL | re.IGNORECASE,
        ).strip()

        if clean_eval:
            delivered = await telegram_interface.send_proactive_nudge(clean_eval)
            if delivered:
                await save_chat_message("assistant", clean_eval)
                logger.info(
                    "Nightly evaluation review delivered to Telegram and saved to chat_history."
                )
            else:
                logger.warning("Failed to deliver nightly evaluation review to Telegram.")
    except Exception as exc:
        logger.error("Error executing nightly evaluation pipeline: %s", exc, exc_info=True)


async def handle_dossier_reconciliation(user_md_path: Path | str | None = None) -> None:
    """Consolidate, deduplicate, and resolve contradictions in user dossier."""
    target_path = Path(user_md_path or memory.DEFAULT_USER_MD)
    logger.info("Executing user dossier reconciliation pipeline for: %s", target_path)
    try:
        raw_content = memory.read_markdown(target_path)
        bullet_lines = [
            line for line in raw_content.splitlines()
            if line.strip().startswith("-") or (line.strip() and not line.strip().startswith("#"))
        ]
        if len(bullet_lines) < 2:
            logger.info(
                "Dossier has fewer than 2 items (%d found); skipping reconciliation.",
                len(bullet_lines),
            )
            return

        messages = build_dossier_reconciliation_prompt(raw_content)
        raw_response = await llm_client.generate_response(
            messages, temperature=0.3, max_tokens=1000
        )
        logger.debug(
            "[DEBUG] Raw dossier reconciliation LLM response (%d chars):\n%s",
            len(raw_response),
            raw_response,
        )

        # Extract <user_dossier>...</user_dossier>
        match = re.search(
            r"<user_dossier>(.*?)</user_dossier>", raw_response, flags=re.DOTALL | re.IGNORECASE
        )
        if not match:
            logger.warning(
                "No <user_dossier> tag found in LLM response; aborting overwrite to protect USER.md."
            )
            return

        reconciled_text = match.group(1).strip()
        # Anti-wipeout guard: ensure result has meaningful content
        if len(reconciled_text) < 20:
            logger.warning(
                "Reconciled dossier output is too short (%d chars); aborting overwrite to protect USER.md.",
                len(reconciled_text),
            )
            return

        memory.overwrite_markdown(target_path, reconciled_text)
        logger.info(
            "User dossier successfully reconciled and saved to %s (pre: %d items, post length: %d chars).",
            target_path,
            len(bullet_lines),
            len(reconciled_text),
        )
    except Exception as exc:
        logger.error("Error in dossier reconciliation pipeline: %s", exc, exc_info=True)


async def run_silent_memory_extraction(
    db_path: str | Path | None = None,
    user_md_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Silent background memory extraction pipeline executed at 23:30 local time.

    1. Watermark-based Retrieval:
       - Uses 'last_extracted_chat_rowid' watermark if present.
       - Guarantees zero dropped messages during late-night chats (23:30-23:59).
    2. Pre-Flight Gate:
       - Checks unextracted/today's chat history.
       - Skips without calling LLM if no chat history or < 2 user messages / < 40 chars.
       - Never locks 'last_memory_extraction_date' on Pre-Flight skip, allowing later night chats.
    3. LLM Extraction Pass (1-Turn Pass):
       - Feeds unextracted transcript with noise filtering and read-only recall notes.
       - Injects existing dossier and existing lab notes index for upsert-by-topic.
    4. Strict Whitelist & Isolation:
       - Only allows 'save_lab_note', 'record_dossier', and 'no_memory_extracted'.
       - Blocks any hallucinated task/system actions.
    5. 100% Silent Guarantee:
       - Never sends Telegram messages.
       - Never saves to chat_history table.
    6. State Persistence upon Success:
       - Advances 'last_extracted_chat_rowid' watermark to max_rowid.
       - Locks 'last_memory_extraction_date' to today_str.
    """
    logger.info("Executing silent daily memory extraction pipeline.")

    # 1. Watermark & Unextracted Messages Retrieval
    watermark_str = await repository.get_state(
        "last_extracted_chat_rowid", db_path=db_path
    )
    watermark = int(watermark_str) if watermark_str and watermark_str.isdigit() else 0

    if watermark > 0:
        messages_today = await repository.get_chat_history_since_rowid(
            since_rowid=watermark, db_path=db_path
        )
    else:
        messages_today = await repository.get_chat_history_today(db_path=db_path)

    # 2. Pre-Flight Gate
    if not messages_today:
        logger.info("[Silent Memory Extraction] Skipped: No unextracted chat history today.")
        return []

    user_messages = [m for m in messages_today if m.get("role") == "user"]
    total_user_chars = sum(len((m.get("content") or "").strip()) for m in user_messages)

    if len(user_messages) < 2 or total_user_chars < 40:
        logger.info(
            "[Silent Memory Extraction] Skipped by Pre-Flight Gate: Insufficient substantive user messages (%d user messages, %d chars).",
            len(user_messages),
            total_user_chars,
        )
        return []

    # 3. Prepare Context
    raw_dossier = await repository.get_recent_dossier(limit=30, db_path=db_path)
    raw_notes = await repository.get_recent_lab_notes(limit=30, db_path=db_path)

    existing_dossier_str = format_existing_dossier_for_extraction(raw_dossier)
    existing_notes_str = format_existing_notes_index_for_extraction(raw_notes)
    daily_transcript_str = format_daily_transcript_for_extraction(messages_today)

    # 4. Assemble 100% Isolated Extraction Prompt (No SOUL.md, No live tools, No MEMORY.md, No active_tasks)
    messages = build_silent_memory_extraction_prompt(
        existing_dossier=existing_dossier_str,
        existing_lab_notes=existing_notes_str,
        daily_transcript=daily_transcript_str,
    )

    # 5. Call LLM (1-turn pass)
    try:
        raw_response = await llm_client.generate_response(
            messages, temperature=0.2, max_tokens=1500
        )
    except Exception as llm_exc:
        logger.error(
            "LLM API failure during silent memory extraction: %s",
            llm_exc,
            exc_info=True,
        )
        raise

    if not raw_response or not isinstance(raw_response, str) or not raw_response.strip():
        logger.warning(
            "[Silent Memory Extraction] LLM returned empty response. Extraction pass aborted without locking state."
        )
        return []

    logger.debug(
        "[DEBUG] Raw silent memory extraction LLM response (%d chars):\n%s",
        len(raw_response),
        raw_response,
    )

    # 6. Parse and execute write tools with strict whitelist
    cleaned_text, executed_actions = await parse_and_execute_tools(
        raw_response,
        db_path=db_path,
        user_md_path=user_md_path,
        allow_read_tools=False,
        allowed_tools={"save_lab_note", "record_dossier", "no_memory_extracted"},
    )

    # 7. Lock state & update watermark on successful pass
    tz = ZoneInfo(config.TIMEZONE)
    today_str = datetime.now(tz).strftime("%Y-%m-%d")
    await repository.set_state("last_memory_extraction_date", today_str, db_path=db_path)

    max_rowid = max((m.get("rowid") or 0) for m in messages_today)
    if max_rowid > 0:
        await repository.set_state("last_extracted_chat_rowid", str(max_rowid), db_path=db_path)

    now_utc_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await repository.set_state("last_memory_extraction_timestamp", now_utc_str, db_path=db_path)

    logger.info(
        "[Silent Memory Extraction] Completed successfully. Executed %d memory actions: %s (watermark rowid: %d)",
        len(executed_actions),
        [a.get("tool") for a in executed_actions],
        max_rowid,
    )

    # 8. Optional PROCESS_TRACING log notification to Telegram
    # Only sent if config.PROCESS_TRACING is True and user was not AFK (passed Pre-Flight Gate).
    # Never saved to chat_history.
    is_tracing = bool(
        getattr(config, "PROCESS_TRACING", getattr(config, "ENABLE_PROCESS_TRACING", config.enable_process_tracing))
    )
    if is_tracing:
        has_mutation = any(
            a.get("tool") in ("save_lab_note", "record_dossier") and a.get("status") == "success"
            for a in executed_actions
        )
        has_no_mem = any(
            a.get("tool") == "no_memory_extracted" and a.get("status") == "success"
            for a in executed_actions
        )
        if has_mutation or has_no_mem:
            trace_lines: list[str] = []
            for a in executed_actions:
                if a.get("status") != "success":
                    continue
                tool = a.get("tool")
                if tool == "save_lab_note":
                    topic = a.get("topic", "")
                    trace_lines.append(f'💾 Menyimpan catatan: "{safe_truncate(topic, 35)}"')
                elif tool == "record_dossier":
                    fact = a.get("fact", "")
                    trace_lines.append(f'👤 Mencatat profil: "{safe_truncate(fact, 35)}"')
                elif tool == "no_memory_extracted":
                    trace_lines.append("💤 Tidak ada memori baru yang diekstrak")

            if trace_lines:
                log_message = "🔍 [PROCESS_TRACING]\n" + "\n".join(trace_lines)
                try:
                    await telegram_interface.send_proactive_nudge(log_message)
                    logger.info(
                        "[Silent Memory Extraction] PROCESS_TRACING log delivered to Telegram (%d lines).",
                        len(trace_lines),
                    )
                except Exception as tg_exc:
                    logger.warning(
                        "[Silent Memory Extraction] Failed to deliver PROCESS_TRACING log to Telegram: %s",
                        tg_exc,
                    )

    return executed_actions


handle_silent_memory_extraction = run_silent_memory_extraction


async def handle_user_message(
    user_text: str,
    on_trace: Callable[[str], Awaitable[None]] | None = None,
) -> str:
    """End-to-end incoming user message handling pipeline."""
    logger.info("Received user message (%d chars): %s", len(user_text), repr(user_text[:60]))
    session = eval_tracer.start_session(user_text)
    try:
        # 1. Check and consume deferred reminder nudges (Context Merging)
        # Fix 5: record_interaction() removed here — already called in telegram.py
        pending_nudges = await traffic_controller.consume_pending_nudges()
        pending_nudge_text = (
            format_nudges_for_prompt(pending_nudges)
            if pending_nudges
            else None
        )
        if pending_nudges:
            for item in pending_nudges:
                tid = item.get("id")
                if tid:
                    heartbeat_runner.mark_dispatched(tid)

        # 2. Retrieve active pending tasks snapshot
        current_tasks = await repository.get_pending_tasks()

        # 3. Retrieve short-term memory (recent chat history)
        history = await get_recent_chat_history(limit=8)

        # Build id set from recent history for dedup in tool-based search_chat
        recent_history_ids: set[str] = {m["id"] for m in history if m.get("id")}
        seen_chat_ids: list[str] = [m["id"] for m in history if m.get("id")]
        seen_note_ids: list[str] = []
        clean_tool_evidence: list[str] = []

        # 4. Format history with tool_context injected into user turns for the last 2 interactions
        history_for_prompt = (
            format_history_with_tool_context(history) if history else None
        )

        # 5. Assemble chat prompt with history (no passive retrieval;
        #    all long-term memory is now accessed on-demand via tool calling)
        messages = build_chat_prompt(
            user_message=user_text,
            current_tasks=current_tasks,
            pending_nudge=pending_nudge_text,
            history=history_for_prompt,
        )
        logger.debug(
            "[DEBUG] Full prompt sent to LLM (%d messages, system=%d chars):\n%s",
            len(messages),
            len(messages[0]["content"]) if messages else 0,
            "\n---\n".join(f"[{m['role'].upper()}]\n{m['content']}" for m in messages),
        )

        # 6. Obtain response from LLM
        raw_response = await llm_client.generate_response(messages)
        logger.debug("[DEBUG] Raw LLM response (%d chars):\n%s", len(raw_response), raw_response)

        # 7. Parse & execute XML tools, then strip tags
        cleaned_text, executed_actions = await parse_and_execute_tools(
            raw_response,
            recent_history_ids=recent_history_ids,
            exclude_ids=seen_chat_ids,
            exclude_note_ids=seen_note_ids,
            on_trace=on_trace,
            search_chat_budget=MAX_SEARCH_CHAT_CALLS,
            search_notes_budget=MAX_SEARCH_NOTES_CALLS,
        )
        for a in executed_actions:
            if a.get("tool") == "search_chat" and a.get("returned_ids"):
                for rid in a["returned_ids"]:
                    if rid not in seen_chat_ids:
                        seen_chat_ids.append(rid)
            elif a.get("tool") == "search_notes" and a.get("returned_ids"):
                for rid in a["returned_ids"]:
                    if rid not in seen_note_ids:
                        seen_note_ids.append(rid)
            if a.get("type") == "read" and a.get("status") == "success" and a.get("count", 0) > 0 and a.get("read_result"):
                if a["read_result"] not in clean_tool_evidence:
                    clean_tool_evidence.append(a["read_result"])

        session.record_turn(
            turn_num=1,
            raw_llm_response=raw_response,
            executed_actions=executed_actions,
        )
        if executed_actions:
            logger.info("Executed %d tool actions.", len(executed_actions))
            logger.debug(
                "[DEBUG] Tool actions executed:\n%s",
                "\n".join(
                    f"  [{a.get('tool')}] status={a.get('status')} "
                    + (f"id={a.get('id')} " if a.get('id') else "")
                    + (f"read_result={a.get('read_result', '')[:300]}" if a.get('read_result') else "")
                    for a in executed_actions
                ),
            )

        # 8. Read-tool follow-up loop.
        #    - query_tasks / get_dossier: one legacy synthesis turn (read tools locked).
        #    - search_chat / search_notes: up to MAX_SEARCH_*_CALLS real executions counted here; an
        #      over-limit request gets one final answer turn, then the loop ends.
        #    Bounded: at most MAX_SEARCH_CHAT_CALLS + MAX_SEARCH_NOTES_CALLS + 1 follow-up LLM calls.
        search_chat_used = _count_search_chat_runs(executed_actions)
        search_notes_used = _count_search_notes_runs(executed_actions)
        conversation = list(messages)
        turn_text = cleaned_text
        turn_actions = executed_actions
        turn_num = 2
        for _ in range(MAX_SEARCH_CHAT_CALLS + MAX_SEARCH_NOTES_CALLS + 1):
            read_results = [
                a["read_result"]
                for a in turn_actions
                if a.get("type") == "read" and a.get("read_result")
            ]
            chat_limit_reached = any(
                a.get("tool") == "search_chat" and a.get("status") == "limit_reached"
                for a in turn_actions
            )
            notes_limit_reached = any(
                a.get("tool") == "search_notes" and a.get("status") == "limit_reached"
                for a in turn_actions
            )
            limit_reached = chat_limit_reached or notes_limit_reached

            if not read_results and not limit_reached:
                break

            legacy_read = any(
                a.get("type") == "read" and a.get("tool") not in ("search_chat", "search_notes")
                for a in turn_actions
            )
            final_turn = legacy_read or limit_reached

            if on_trace:
                await on_trace("⚡ Menyintesis jawaban dari data temuan...")
            results_block = "\n\n".join(read_results)
            has_chat_action = any(a.get("tool") == "search_chat" for a in turn_actions)
            has_notes_action = any(a.get("tool") == "search_notes" for a in turn_actions)
            chat_has_results = any(
                a.get("tool") == "search_chat" and a.get("count", 0) > 0
                for a in turn_actions
            )
            notes_has_results = any(
                a.get("tool") == "search_notes" and a.get("count", 0) > 0
                for a in turn_actions
            )
            any_search_has_results = chat_has_results or notes_has_results
            can_search_notes = search_notes_used < MAX_SEARCH_NOTES_CALLS
            can_search_chat = search_chat_used < MAX_SEARCH_CHAT_CALLS

            if legacy_read:
                follow_up_content = format_follow_up_user_message(results_block)
            elif has_chat_action and has_notes_action:
                if limit_reached:
                    follow_up_content = format_multi_search_limit_message(
                        results_block,
                        chat_limit_reached=chat_limit_reached,
                        chat_max=MAX_SEARCH_CHAT_CALLS,
                        notes_limit_reached=notes_limit_reached,
                        notes_max=MAX_SEARCH_NOTES_CALLS,
                    )
                else:
                    follow_up_content = format_multi_search_follow_up_message(
                        results_block,
                        chat_used=search_chat_used,
                        chat_max=MAX_SEARCH_CHAT_CALLS,
                        notes_used=search_notes_used,
                        notes_max=MAX_SEARCH_NOTES_CALLS,
                        has_results=any_search_has_results,
                    )
            elif has_notes_action:
                if notes_limit_reached:
                    logger.info(
                        "search_notes limit reached (%d/%d); final answer turn.",
                        search_notes_used,
                        MAX_SEARCH_NOTES_CALLS,
                    )
                    follow_up_content = format_search_notes_limit_message(
                        results_block, MAX_SEARCH_NOTES_CALLS
                    )
                else:
                    follow_up_content = format_search_notes_follow_up_message(
                        results_block,
                        search_notes_used,
                        MAX_SEARCH_NOTES_CALLS,
                        has_results=notes_has_results,
                        can_search_chat=can_search_chat,
                    )
            else:
                if chat_limit_reached:
                    logger.info(
                        "search_chat limit reached (%d/%d); final answer turn.",
                        search_chat_used,
                        MAX_SEARCH_CHAT_CALLS,
                    )
                    follow_up_content = format_search_chat_limit_message(
                        results_block, MAX_SEARCH_CHAT_CALLS
                    )
                else:
                    follow_up_content = format_search_chat_follow_up_message(
                        results_block,
                        search_chat_used,
                        MAX_SEARCH_CHAT_CALLS,
                        has_results=chat_has_results,
                        can_search_notes=can_search_notes,
                    )

            logger.info("Read tools returned %d results, making follow-up LLM call.", len(read_results))
            conversation = conversation + [
                {
                    "role": "assistant",
                    "content": _build_ephemeral_assistant_turn(turn_actions, turn_text),
                },
                {
                    "role": "user",
                    "content": follow_up_content,
                },
            ]
            raw_response_2 = await llm_client.generate_response(conversation)
            logger.debug(
                "[DEBUG] Follow-up LLM raw response (%d chars):\n%s",
                len(raw_response_2),
                raw_response_2,
            )
            if final_turn:
                turn_text, turn_actions = await parse_and_execute_tools(
                    raw_response_2, allow_read_tools=False, on_trace=on_trace
                )
            else:
                turn_text, turn_actions = await parse_and_execute_tools(
                    raw_response_2,
                    allow_read_tools=False,
                    recent_history_ids=recent_history_ids,
                    exclude_ids=seen_chat_ids,
                    exclude_note_ids=seen_note_ids,
                    on_trace=on_trace,
                    search_chat_budget=MAX_SEARCH_CHAT_CALLS - search_chat_used,
                    search_notes_budget=MAX_SEARCH_NOTES_CALLS - search_notes_used,
                )
                search_chat_used += _count_search_chat_runs(turn_actions)
                search_notes_used += _count_search_notes_runs(turn_actions)
                for a in turn_actions:
                    if a.get("tool") == "search_chat" and a.get("returned_ids"):
                        for rid in a["returned_ids"]:
                            if rid not in seen_chat_ids:
                                seen_chat_ids.append(rid)
                    elif a.get("tool") == "search_notes" and a.get("returned_ids"):
                        for rid in a["returned_ids"]:
                            if rid not in seen_note_ids:
                                seen_note_ids.append(rid)
                    if a.get("type") == "read" and a.get("status") == "success" and a.get("count", 0) > 0 and a.get("read_result"):
                        if a["read_result"] not in clean_tool_evidence:
                            clean_tool_evidence.append(a["read_result"])
            if turn_text and turn_text.strip():
                cleaned_text = turn_text
                executed_actions.extend(turn_actions)

            session.record_turn(
                turn_num=turn_num,
                raw_llm_response=raw_response_2,
                executed_actions=turn_actions,
                guidance=follow_up_content,
            )
            turn_num += 1

            if final_turn:
                break

        # 9. Fix 3: Separate user-facing reply from what gets persisted
        has_meaningful_content = bool(cleaned_text and cleaned_text.strip())

        if has_meaningful_content:
            final_reply = cleaned_text.strip()
        elif executed_actions:
            final_reply = "\u2705 Aksi telah dieksekusi."
        else:
            final_reply = "Pesan telah diproses."

        # 10. Persist conversation to chat_history
        await save_chat_message("user", user_text)
        if has_meaningful_content:
            # Only save meaningful assistant responses, not generic fallbacks
            tool_ctx = "\n\n".join(clean_tool_evidence) if clean_tool_evidence else None
            if tool_ctx is not None:
                await save_chat_message("assistant", cleaned_text.strip(), tool_context=tool_ctx)
            else:
                await save_chat_message("assistant", cleaned_text.strip())

        # 11. Fix 2: Prune old chat history to prevent unbounded growth
        pruned = await prune_chat_history(keep_latest=500)
        if pruned > 0:
            logger.info("Pruned %d old chat_history rows.", pruned)

        # 12. Return clean user-facing reply and finalize evaluation trace
        session.finish(final_reply)
        return final_reply

    except LLMError as err:
        logger.error("LLM API error in user message pipeline: %s", err, exc_info=True)
        fallback: str
        if err.status_code:
            code_desc = {
                400: "Bad Request",
                401: "API Key Invalid",
                403: "Forbidden",
                404: "Model Not Found",
                429: "Rate Limit",
                500: "Internal Server Error",
                502: "Bad Gateway",
                503: "Service Unavailable",
                504: "Gateway Timeout",
            }.get(err.status_code, "API Error")
            fallback = f"⚠️ Gagal menghubungi LLM (HTTP {err.status_code}: {code_desc}). Ketik /retry untuk mencoba lagi."
        elif err.is_timeout:
            fallback = "⚠️ Gagal menghubungi LLM (Request Timeout). Ketik /retry untuk mencoba lagi."
        elif err.is_network_error:
            fallback = "⚠️ Gagal menghubungi LLM (Koneksi Terputus). Ketik /retry untuk mencoba lagi."
        else:
            fallback = f"⚠️ Gagal menghubungi LLM ({err}). Ketik /retry untuk mencoba lagi."
        session.finish(fallback)
        return fallback

    except Exception as exc:
        logger.error("Error in user message pipeline: %s", exc, exc_info=True)
        fallback = "⚠️ Terjadi kesalahan internal saat memproses pesan Anda. Ketik /retry untuk mencoba lagi."
        session.finish(fallback)
        return fallback


async def main() -> None:
    """Initialize system components and start runtime loops."""
    logger.info("=== Initializing Vigil Autonomous System ===")

    # 1. Initialize SQLite Database & FTS5 Search Indexes
    await init_db()
    await init_search_index()
    await init_chat_history_search_index()
    logger.info("Database and search indexes initialized successfully.")

    # 2. Register callbacks and pipelines
    heartbeat_runner.register_dispatch_callback(handle_heartbeat_dispatch)
    heartbeat_runner.register_nightly_eval_callback(handle_nightly_evaluation)
    heartbeat_runner.register_dossier_reconcile_callback(handle_dossier_reconciliation)
    heartbeat_runner.register_memory_extraction_callback(run_silent_memory_extraction)
    telegram_interface.register_message_handler(handle_user_message)

    # 3. Setup graceful shutdown triggers
    stop_event = asyncio.Event()

    def _trigger_shutdown():
        logger.info("Shutdown signal received.")
        stop_event.set()

    loop = asyncio.get_running_loop()
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _trigger_shutdown)
    except (NotImplementedError, AttributeError):
        # Fallback for Windows ProactorEventLoop where add_signal_handler is unsupported
        def _win_handler(signum: int, frame: Any) -> None:
            logger.info("Windows signal %d received.", signum)
            loop.call_soon_threadsafe(stop_event.set)

        signal.signal(signal.SIGINT, _win_handler)
        signal.signal(signal.SIGTERM, _win_handler)

    # 4. Start background heartbeat loop & Telegram interface
    heartbeat_runner.start()
    await telegram_interface.start()
    logger.info("Vigil is fully operational. System running.")

    try:
        await stop_event.wait()
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        logger.info("Initiating graceful shutdown...")
        await heartbeat_runner.stop()
        await telegram_interface.stop()
        await asyncio.sleep(0.1)
        logger.info("=== Vigil Autonomous System Shut Down Cleanly ===")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
