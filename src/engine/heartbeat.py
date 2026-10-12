"""Autonomous heartbeat runner module for vigil.

Monitors pending tasks periodically, identifies due or overdue items,
evaluates them through TrafficController, and invokes dispatch callbacks.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.config import config
from src.core.traffic import TrafficController, traffic_controller
from src.storage import memory, repository

logger = logging.getLogger(__name__)


def _parse_due_time(due_str: str | None) -> datetime | None:
    """Defensively parse a due_time string into a UTC datetime."""
    if not due_str or not due_str.strip():
        return None

    clean_str = due_str.strip()

    # 1. Attempt standard ISO format
    try:
        dt = datetime.fromisoformat(clean_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(config.TIMEZONE))
        result = dt.astimezone(timezone.utc)
        logger.debug(
            "[DEBUG] due_time parsed (ISO): '%s' -> %s (UTC)",
            clean_str,
            result.isoformat(),
        )
        return result
    except (ValueError, TypeError):
        pass

    # 2. Attempt common datetime and date/time formats
    datetime_formats = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%H:%M:%S",
        "%H:%M",
    )
    for fmt in datetime_formats:
        try:
            parsed = datetime.strptime(clean_str, fmt)
            if fmt in ("%H:%M:%S", "%H:%M"):
                tz = ZoneInfo(config.TIMEZONE)
                now = datetime.now(tz)
                parsed = now.replace(
                    hour=parsed.hour,
                    minute=parsed.minute,
                    second=parsed.second if fmt == "%H:%M:%S" else 0,
                    microsecond=0,
                )
            else:
                # For full dates that were naive, assume they are in the configured timezone
                parsed = parsed.replace(tzinfo=ZoneInfo(config.TIMEZONE))
            
            return parsed.astimezone(timezone.utc)
        except ValueError:
            continue

    logger.debug("[DEBUG] due_time: '%s' -> result UTC: %s", clean_str, parsed.astimezone(timezone.utc) if 'parsed' in dir() else None)

    logger.warning("Unrecognized due_time format: '%s'", due_str)
    return None


class HeartbeatRunner:
    """Background periodic checker for due tasks and proactive notifications."""

    def __init__(
        self,
        check_interval_seconds: int = 60,
        nudge_cooldown_minutes: int = 30,
        traffic_ctrl: TrafficController | None = None,
        db_path: str | Path | None = None,
    ) -> None:
        self.check_interval_seconds = check_interval_seconds
        self.nudge_cooldown_minutes = nudge_cooldown_minutes
        self.traffic_ctrl = traffic_ctrl or traffic_controller
        self.db_path = db_path

        self._dispatched_cache: dict[str, datetime] = {}
        self._is_running: bool = False
        self._task: asyncio.Task[None] | None = None
        self._dispatch_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._nightly_eval_callback: Callable[[], Awaitable[None]] | None = None
        self._dossier_reconcile_callback: Callable[[], Awaitable[None]] | None = None

    def register_dispatch_callback(
        self, callback: Callable[[dict[str, Any]], Awaitable[None]]
    ) -> None:
        """Register the asynchronous callback for dispatching due notifications."""
        self._dispatch_callback = callback

    def register_nightly_eval_callback(
        self, callback: Callable[[], Awaitable[None]]
    ) -> None:
        """Register the asynchronous callback for performing nightly evaluation."""
        self._nightly_eval_callback = callback

    def register_dossier_reconcile_callback(
        self, callback: Callable[[], Awaitable[None]]
    ) -> None:
        """Register the asynchronous callback for performing weekly/threshold dossier reconciliation."""
        self._dossier_reconcile_callback = callback

    def mark_dispatched(
        self, task_id: str, timestamp: datetime | None = None
    ) -> None:
        """Mark a task as dispatched or merged to activate the nudge cooldown timer."""
        self._dispatched_cache[task_id] = timestamp or datetime.now(timezone.utc)
        logger.debug("Task %s marked as dispatched; cooldown active.", task_id)

    async def check_due_tasks(self) -> list[dict[str, Any]]:
        """Scan pending tasks, evaluate due items, and trigger dispatch callbacks."""
        processed_tickets: list[dict[str, Any]] = []

        try:
            pending_tasks = await repository.get_pending_tasks(db_path=self.db_path)
        except Exception as exc:
            logger.error("Failed to query pending tasks in heartbeat: %s", exc, exc_info=True)
            return []

        now_utc = datetime.now(timezone.utc)
        logger.debug(
            "[DEBUG] Heartbeat cycle: checking %d pending tasks at %s UTC",
            len(pending_tasks),
            now_utc.isoformat(),
        )

        for task in pending_tasks:
            task_id = task.get("id")
            due_time_str = task.get("due_time")

            if not task_id or not due_time_str:
                continue

            # Check cooldown against dispatched cache
            last_dispatched = self._dispatched_cache.get(task_id)
            if last_dispatched:
                elapsed_minutes = (now_utc - last_dispatched).total_seconds() / 60.0
                if elapsed_minutes < self.nudge_cooldown_minutes:
                    continue

            # Parse due_time defensively
            due_dt = _parse_due_time(due_time_str)
            if due_dt is None:
                continue

            # Check if task is due or overdue
            if now_utc >= due_dt:
                ticket = {
                    "id": task_id,
                    "title": task.get("title", ""),
                    "due_time": due_time_str,
                    "type": "due_task",
                }

                try:
                    status, _ = await self.traffic_ctrl.evaluate_nudge(ticket)
                    processed_tickets.append({"ticket": ticket, "status": status})

                    if status == "DISPATCH_NOW":
                        self._dispatched_cache[task_id] = now_utc
                        if self._dispatch_callback:
                            try:
                                await self._dispatch_callback(ticket)
                            except Exception as cb_exc:
                                logger.error(
                                    "Error in heartbeat dispatch callback for task %s: %s",
                                    task_id,
                                    cb_exc,
                                    exc_info=True,
                                )
                except Exception as eval_exc:
                    logger.error(
                        "Error evaluating nudge for task %s: %s", task_id, eval_exc, exc_info=True
                    )

        return processed_tickets

    async def check_nightly_evaluation(self) -> bool:
        """Check if it's time for nightly evaluation (>= 21:00) and execute once per day."""
        tz = ZoneInfo(config.TIMEZONE)
        now_local = datetime.now(tz)
        today_str = now_local.strftime("%Y-%m-%d")

        # Nightly evaluation window: 21:00 to 23:59 local time
        if now_local.hour < 21:
            return False

        try:
            last_eval = await repository.get_state(
                "last_nightly_eval_date", db_path=self.db_path
            )
            if last_eval == today_str:
                return False

            logger.info(
                "Triggering nightly performance evaluation for date: %s (local time: %s)",
                today_str,
                now_local.strftime("%H:%M:%S"),
            )
            # Lock state immediately to prevent duplicate triggers
            await repository.set_state(
                "last_nightly_eval_date", today_str, db_path=self.db_path
            )

            if self._nightly_eval_callback:
                try:
                    await self._nightly_eval_callback()
                except Exception as cb_exc:
                    logger.error(
                        "Error in nightly evaluation callback: %s",
                        cb_exc,
                        exc_info=True,
                    )
            return True
        except Exception as exc:
            logger.error("Error checking nightly evaluation state: %s", exc, exc_info=True)
            return False

    async def check_dossier_reconciliation(
        self, user_md_path: Path | str | None = None
    ) -> bool:
        """Check if user dossier reconciliation is needed (Sunday >= 23:00 OR bullet items >= 20)."""
        tz = ZoneInfo(config.TIMEZONE)
        now_local = datetime.now(tz)
        today_str = now_local.strftime("%Y-%m-%d")

        target_path = Path(user_md_path or memory.DEFAULT_USER_MD)
        content = memory.read_markdown(target_path)
        bullet_lines = [
            line for line in content.splitlines()
            if line.strip().startswith("-") or (line.strip() and not line.strip().startswith("#"))
        ]
        line_count = len(bullet_lines)

        # Condition A: Capacity threshold >= 20 bullet items
        capacity_triggered = line_count >= 20

        # Condition B: Sunday night >= 23:00 (weekday 6 is Sunday) with at least 2 items to reconcile
        time_triggered = (now_local.weekday() == 6) and (now_local.hour >= 23) and (line_count >= 2)

        if not (capacity_triggered or time_triggered):
            return False

        try:
            last_reconciled = await repository.get_state(
                "last_dossier_reconcile_date", db_path=self.db_path
            )
            if last_reconciled == today_str:
                return False

            trigger_reason = (
                f"capacity threshold reached ({line_count} items)"
                if capacity_triggered
                else "scheduled Sunday 23:00 reconciliation"
            )
            logger.info(
                "Triggering user dossier reconciliation (%s) for date: %s",
                trigger_reason,
                today_str,
            )

            # Lock state immediately to prevent duplicate triggers
            await repository.set_state(
                "last_dossier_reconcile_date", today_str, db_path=self.db_path
            )

            if self._dossier_reconcile_callback:
                try:
                    try:
                        await self._dossier_reconcile_callback(user_md_path)
                    except TypeError:
                        await self._dossier_reconcile_callback()
                except Exception as cb_exc:
                    logger.error(
                        "Error in dossier reconciliation callback: %s",
                        cb_exc,
                        exc_info=True,
                    )
            return True
        except Exception as exc:
            logger.error("Error checking dossier reconciliation state: %s", exc, exc_info=True)
            return False

    async def _run_loop(self) -> None:
        """Continuous execution loop running at check_interval_seconds."""
        logger.info("Heartbeat loop started (interval: %ds).", self.check_interval_seconds)
        while self._is_running:
            try:
                await self.check_due_tasks()
            except Exception as exc:
                logger.error("Unexpected error in heartbeat cycle: %s", exc, exc_info=True)

            try:
                await self.check_nightly_evaluation()
            except Exception as exc:
                logger.error("Unexpected error in nightly evaluation cycle: %s", exc, exc_info=True)

            try:
                await self.check_dossier_reconciliation()
            except Exception as exc:
                logger.error("Unexpected error in dossier reconciliation cycle: %s", exc, exc_info=True)

            try:
                await asyncio.sleep(self.check_interval_seconds)
            except asyncio.CancelledError:
                break
        logger.info("Heartbeat loop stopped.")

    def start(self) -> None:
        """Start the background heartbeat runner."""
        if self._is_running:
            return
        self._is_running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Stop the background heartbeat runner cleanly."""
        if not self._is_running:
            return
        self._is_running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
