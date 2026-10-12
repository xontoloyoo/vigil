"""Agent Evaluation Logger for Vigil.

Produces token-dense, structured Markdown trace logs specifically optimized
for LLM analysis and evaluation, stripping infrastructure and transport noise.
"""

from datetime import datetime
import logging
from pathlib import Path
import re
from typing import Any

from src.config import config
from src.core.utils import safe_truncate

logger = logging.getLogger(__name__)


def _extract_guidance_text(raw_guidance: str) -> str:
    """Extract quota usage and directive portion from follow-up message, stripping results block."""
    if not raw_guidance:
        return ""

    if "[TOOL EXECUTION RESULTS]" not in raw_guidance:
        return raw_guidance.strip()

    # Split into double-newline separated blocks
    blocks = [b.strip() for b in raw_guidance.split("\n\n") if b.strip()]
    guidance_blocks: list[str] = []

    for b in blocks:
        # Skip results header block and pure result listings
        if b.startswith("[TOOL EXECUTION RESULTS]"):
            continue
        if any(b.startswith(prefix) for prefix in (
            "Lab Notes Search Results",
            "Chat History Search Results",
            "Task Query Results",
            "User Dossier Profile",
            "No lab notes found",
            "No chat history found",
            "No tasks found",
            "No user dossier records found",
        )):
            continue
        guidance_blocks.append(b)

    if guidance_blocks:
        return "\n".join(guidance_blocks)

    # Fallback to the last block if no specific rule block separated
    return blocks[-1] if blocks else raw_guidance.strip()


class EvalTraceSession:
    """Records semantic events of a single user-agent interaction session."""

    def __init__(self, user_query: str, log_file_path: Path | str | None = None):
        self.user_query = user_query
        self.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.log_file_path = Path(log_file_path or config.EVAL_LOG_PATH)
        self.turns: list[dict[str, Any]] = []
        self.final_answer: str = ""
        self.total_search_chat: int = 0
        self.total_search_notes: int = 0
        self.total_writes: int = 0

    def record_turn(
        self,
        turn_num: int,
        raw_llm_response: str,
        executed_actions: list[dict[str, Any]],
        guidance: str | None = None,
    ) -> None:
        """Record details of a single conversational turn."""
        turn_data: dict[str, Any] = {
            "turn_num": turn_num,
            "guidance": guidance,
            "actions": [],
            "emitted_tools": [],
        }

        # Track tool calls
        if executed_actions:
            for act in executed_actions:
                tool_name = act.get("tool", "unknown")
                status = act.get("status", "unknown")
                tool_type = act.get("type", "write")

                if tool_name == "search_chat":
                    self.total_search_chat += 1
                elif tool_name == "search_notes":
                    self.total_search_notes += 1
                elif tool_type != "read":
                    self.total_writes += 1

                action_summary: dict[str, Any] = {
                    "tool": tool_name,
                    "status": status,
                    "type": tool_type,
                }

                # If read tool with read_result, extract clean snippets
                if act.get("read_result"):
                    raw_result = str(act["read_result"]).strip()
                    # Truncate overly long read results to maintain token compactness
                    action_summary["result_snippet"] = safe_truncate(raw_result, max_chars=400)
                elif act.get("id"):
                    action_summary["id"] = act["id"]
                elif act.get("error"):
                    action_summary["error"] = act["error"]

                turn_data["actions"].append(action_summary)

        # Detect raw tool tags emitted in response
        tags_found = re.findall(r"<([a-zA-Z_]+)(?:[^>]*)>(.*?)</\1>", raw_llm_response, flags=re.DOTALL)
        if tags_found:
            for tag_name, tag_content in tags_found:
                clean_content = safe_truncate(tag_content.strip(), max_chars=120)
                turn_data["emitted_tools"].append(f"<{tag_name}>{clean_content}</{tag_name}>")

        self.turns.append(turn_data)

    def finish(self, final_reply: str) -> None:
        """Assemble session markdown and safely append to the evaluation log file."""
        self.final_answer = final_reply.strip()
        markdown_block = self._render_markdown()
        self._write_to_disk(markdown_block)

    def _render_markdown(self) -> str:
        """Render the recorded session into clean, LLM-friendly Markdown."""
        lines: list[str] = [
            f"=== INTERACTION START: [{self.timestamp}] ===",
            f'[USER QUERY]: "{self.user_query}"',
            "",
        ]

        for turn in self.turns:
            t_num = turn["turn_num"]
            lines.append(f"--- TURN {t_num} ---")

            if turn.get("guidance"):
                clean_guidance = _extract_guidance_text(turn["guidance"])
                truncated_guidance = safe_truncate(clean_guidance, max_chars=350)
                g_lines = [l.strip() for l in truncated_guidance.splitlines() if l.strip()]
                if len(g_lines) == 1:
                    lines.append(f"[INJECTED GUIDANCE]: {g_lines[0]}")
                elif g_lines:
                    lines.append("[INJECTED GUIDANCE]:")
                    for gl in g_lines:
                        lines.append(f"  {gl}")

            emitted = turn.get("emitted_tools", [])
            if emitted:
                lines.append(f"[AGENT DECISION]: Call tool(s): {', '.join(emitted)}")
            else:
                lines.append("[AGENT DECISION]: Final Response (No tools emitted)")

            actions = turn.get("actions", [])
            if actions:
                for a in actions:
                    tool_name = a["tool"]
                    status = a["status"]
                    snippet = a.get("result_snippet")
                    if snippet:
                        lines.append(f"[TOOL RESULT]: {tool_name} ({status})")
                        # Indent snippet for clean hierarchical presentation
                        for s_line in snippet.splitlines():
                            lines.append(f"  {s_line}")
                    else:
                        details = f"id={a['id']}" if "id" in a else f"status={status}"
                        lines.append(f"[TOOL ACTION]: {tool_name} [{details}]")
            lines.append("")

        lines.extend([
            f"[FINAL ANSWER]:",
            f'"{self.final_answer}"',
            "",
            "[EVALUATION METRICS]:",
            f"- Total Turns: {len(self.turns)}",
            f"- Total Searches: {self.total_search_chat + self.total_search_notes} (notes: {self.total_search_notes}, chat: {self.total_search_chat})",
            f"- Total State Writes: {self.total_writes}",
            f"=== INTERACTION END ===",
            "\n",
        ])

        return "\n".join(lines)

    def _write_to_disk(self, content: str) -> None:
        """Safely write content to the target file without throwing exceptions."""
        try:
            self.log_file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_file_path, "a", encoding="utf-8") as f:
                f.write(content)
        except Exception as exc:
            logger.warning("Failed to write agent evaluation log to %s: %s", self.log_file_path, exc)


class NoOpEvalTraceSession:
    """Null-object pattern session when evaluation logging is disabled."""

    def record_turn(
        self,
        turn_num: int,
        raw_llm_response: str,
        executed_actions: list[dict[str, Any]],
        guidance: str | None = None,
    ) -> None:
        pass

    def finish(self, final_reply: str) -> None:
        pass


class AgentEvalTracer:
    """Orchestrator for managing evaluation trace sessions."""

    def __init__(self, enabled: bool | None = None, log_file_path: Path | str | None = None):
        self._enabled = enabled
        self._log_file_path = log_file_path

    @property
    def is_enabled(self) -> bool:
        if self._enabled is not None:
            return self._enabled
        return config.ENABLE_EVAL_LOG

    def start_session(self, user_query: str) -> EvalTraceSession | NoOpEvalTraceSession:
        """Initiate a new tracing session for an incoming user query."""
        if not self.is_enabled:
            return NoOpEvalTraceSession()
        return EvalTraceSession(user_query, log_file_path=self._log_file_path)


# Default global tracer instance
eval_tracer = AgentEvalTracer()
