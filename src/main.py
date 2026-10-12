"""Main system orchestrator for vigil.

Initializes database and search indexes, registers callbacks, wires
the Telegram interface with prompt construction, tool execution,
and autonomous heartbeat reminders, and handles graceful shutdown.
"""

import asyncio
from collections.abc import Awaitable, Callable
import logging
from pathlib import Path
import re
import signal
from typing import Any

from src.config import config
from src.core.eval_logger import eval_tracer
from src.core.llm import LLMError, llm_client
from src.core.prompts import (
    build_chat_prompt,
    build_dossier_reconciliation_prompt,
    build_nightly_eval_prompt,
    build_nudge_prompt,
)
from src.core.traffic import traffic_controller
from src.engine.heartbeat import HeartbeatRunner
from src.interfaces.telegram import telegram_interface
from src.storage import memory, repository
from src.storage.database import init_db
from src.storage.repository import (
    get_recent_chat_history,
    prune_chat_history,
    save_chat_message,
)
from src.storage.search import (
    init_chat_history_search_index,
    init_search_index,
)
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
        completed_tasks = await repository.get_tasks_by_status(status="done", limit=20)
        pending_tasks = await repository.get_pending_tasks()
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

        # 4. Extract <working_memory>...</working_memory>
        mem_match = re.search(
            r"<working_memory>(.*?)</working_memory>", raw_eval, flags=re.DOTALL | re.IGNORECASE
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
            r"<working_memory>.*?</working_memory>", "", raw_eval, flags=re.DOTALL | re.IGNORECASE
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

        # 4. Strip 'id' from history before injecting into LLM messages array
        history_for_prompt = [
            {"role": m["role"], "content": m["content"]} for m in history
        ] if history else None

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
            on_trace=on_trace,
            search_chat_budget=MAX_SEARCH_CHAT_CALLS,
            search_notes_budget=MAX_SEARCH_NOTES_CALLS,
        )
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
                        results_block, search_notes_used, MAX_SEARCH_NOTES_CALLS
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
                        results_block, search_chat_used, MAX_SEARCH_CHAT_CALLS
                    )

            logger.info("Read tools returned %d results, making follow-up LLM call.", len(read_results))
            conversation = conversation + [
                {
                    "role": "assistant",
                    "content": turn_text or "(Memproses query data...)",
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
                    on_trace=on_trace,
                    search_chat_budget=MAX_SEARCH_CHAT_CALLS - search_chat_used,
                    search_notes_budget=MAX_SEARCH_NOTES_CALLS - search_notes_used,
                )
                search_chat_used += _count_search_chat_runs(turn_actions)
                search_notes_used += _count_search_notes_runs(turn_actions)
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
