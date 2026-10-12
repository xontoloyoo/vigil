"""Traffic controller for regulating proactive notifications and user interaction state.

Tracks user inactivity, evaluates whether reminder tickets can be dispatched
immediately or must be queued, and formats queued nudges for prompt merging.
"""

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any

from src.storage import repository

logger = logging.getLogger(__name__)

KEY_LAST_INTERACTION = "last_interaction_time"
KEY_PENDING_NUDGES = "pending_nudges"
DEFAULT_SILENCE_THRESHOLD_MINUTES = 10.0


class TrafficController:
    """Manages notification rhythm and deferred reminder queues."""

    def __init__(
        self,
        silence_threshold_minutes: float = DEFAULT_SILENCE_THRESHOLD_MINUTES,
        db_path: str | Path | None = None,
    ) -> None:
        self.silence_threshold_minutes = silence_threshold_minutes
        self.db_path = db_path
        self._last_interaction_time: str | None = None
        self._pending_nudges: list[dict[str, Any]] = []

    async def _load_pending_nudges(self) -> list[dict[str, Any]]:
        """Load pending nudges from system_state with defensive JSON fallback."""
        raw = await repository.get_state(
            KEY_PENDING_NUDGES, default="[]", db_path=self.db_path
        )
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
            logger.warning("Decoded pending_nudges is not a list, returning empty list.")
            return []
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("Failed to decode pending_nudges JSON: %s. Using [].", exc)
            return []

    async def _save_pending_nudges(self, nudges: list[dict[str, Any]]) -> None:
        """Serialize and persist pending nudges list to system_state."""
        try:
            payload = json.dumps(nudges, ensure_ascii=False)
            await repository.set_state(
                KEY_PENDING_NUDGES, payload, db_path=self.db_path
            )
        except Exception as exc:
            logger.error("Failed to persist pending_nudges to system_state: %s", exc)

    async def record_interaction(self) -> None:
        """Record the current timestamp as the user's latest interaction."""
        now_iso = datetime.now(timezone.utc).isoformat()
        self._last_interaction_time = now_iso
        await repository.set_state(
            KEY_LAST_INTERACTION, now_iso, db_path=self.db_path
        )

    async def get_idle_minutes(self) -> float:
        """Calculate elapsed minutes since the user's last recorded interaction.

        Returns 999.0 if no interaction has been recorded yet.
        """
        raw = await repository.get_state(
            KEY_LAST_INTERACTION, default=None, db_path=self.db_path
        )
        if not raw:
            return 999.0

        try:
            dt = datetime.fromisoformat(raw)
            now = datetime.now(timezone.utc)
            delta_seconds = (now - dt).total_seconds()
            return max(0.0, delta_seconds / 60.0)
        except (ValueError, TypeError) as exc:
            logger.warning("Failed to parse last_interaction_time '%s': %s", raw, exc)
            return 999.0

    async def evaluate_nudge(
        self, ticket: dict[str, Any]
    ) -> tuple[str, dict[str, Any] | None]:
        """Evaluate if a reminder ticket can be dispatched now or must be queued.

        Returns:
            ('DISPATCH_NOW', ticket) if idle_minutes >= silence_threshold_minutes
            ('QUEUED', None) if idle_minutes < silence_threshold_minutes
        """
        idle_minutes = await self.get_idle_minutes()

        if idle_minutes >= self.silence_threshold_minutes:
            return ("DISPATCH_NOW", ticket)

        # User is actively chatting / recently active; hold ticket in queue
        current_nudges = await self._load_pending_nudges()
        ticket_id = ticket.get("id")
        if ticket_id and any(n.get("id") == ticket_id for n in current_nudges):
            logger.debug(
                "Ticket %s already in pending_nudges, skipping duplicate append.",
                ticket_id,
            )
            return ("QUEUED", None)

        current_nudges.append(ticket)
        self._pending_nudges = current_nudges
        await self._save_pending_nudges(current_nudges)
        return ("QUEUED", None)

    async def consume_pending_nudges(self) -> list[dict[str, Any]]:
        """Retrieve and clear all queued tickets from memory and system_state."""
        nudges = await self._load_pending_nudges()
        self._pending_nudges = []
        await self._save_pending_nudges([])
        return nudges

    def format_nudges_for_prompt(self, nudges: list[dict[str, Any]]) -> str:
        """Format pending reminder tickets (backward-compatible delegate to context_formatters)."""
        from src.core.context_formatters import format_nudges_for_prompt
        return format_nudges_for_prompt(nudges)





# Default ready-to-use instance
traffic_controller = TrafficController()
