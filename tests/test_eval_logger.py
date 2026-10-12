"""Unit and integration tests for Agent Evaluation Logger."""

import pytest
from pathlib import Path
from src.core.eval_logger import AgentEvalTracer, EvalTraceSession, NoOpEvalTraceSession
from src.main import handle_user_message


def test_noop_tracer_when_disabled(tmp_path: Path):
    target_log = tmp_path / "test_eval.md"
    tracer = AgentEvalTracer(enabled=False, log_file_path=target_log)
    assert tracer.is_enabled is False

    session = tracer.start_session("Kueri uji")
    assert isinstance(session, NoOpEvalTraceSession)

    session.record_turn(1, "raw response", [{"tool": "search_chat", "status": "success"}])
    session.finish("Jawaban final")

    assert not target_log.exists()


def test_active_tracer_produces_structured_markdown(tmp_path: Path):
    target_log = tmp_path / "test_eval.md"
    tracer = AgentEvalTracer(enabled=True, log_file_path=target_log)
    assert tracer.is_enabled is True

    session = tracer.start_session("Bandingkan config database staging dan prod")
    assert isinstance(session, EvalTraceSession)

    # Turn 1: tool call with result snippet
    session.record_turn(
        turn_num=1,
        raw_llm_response='<search_notes limit="3">database staging prod</search_notes>',
        executed_actions=[
            {
                "tool": "search_notes",
                "status": "success",
                "type": "read",
                "read_result": (
                    'Lab Notes Search Results (query: "database staging prod"):\n'
                    '- [DB Config] Staging host=db-stg, Prod host=db-prod [relevance: -0.92]\n'
                    'Total: 1 notes found.'
                ),
            }
        ],
    )

    # Turn 2: final answer after follow-up guidance
    session.record_turn(
        turn_num=2,
        raw_llm_response="Beda bro, staging pake db-stg sedangkan prod pake db-prod.",
        executed_actions=[],
        guidance="search_notes usage: 1/3. Evaluate whether the results above are sufficient.",
    )

    session.finish("Beda bro, staging pake db-stg sedangkan prod pake db-prod.")

    assert target_log.exists()
    content = target_log.read_text(encoding="utf-8")

    assert "=== INTERACTION START:" in content
    assert '[USER QUERY]: "Bandingkan config database staging dan prod"' in content
    assert "--- TURN 1 ---" in content
    assert "[AGENT DECISION]: Call tool(s): <search_notes>database staging prod</search_notes>" in content
    assert "[TOOL RESULT]: search_notes (success)" in content
    assert "Staging host=db-stg, Prod host=db-prod" in content
    assert "--- TURN 2 ---" in content
    assert "[INJECTED GUIDANCE]: search_notes usage: 1/3" in content
    assert "[AGENT DECISION]: Final Response (No tools emitted)" in content
    assert '[FINAL ANSWER]:\n"Beda bro, staging pake db-stg sedangkan prod pake db-prod."' in content
    assert "[EVALUATION METRICS]:" in content
    assert "- Total Turns: 2" in content
    assert "- Total Searches: 1 (notes: 1, chat: 0)" in content
    assert "=== INTERACTION END ===" in content


def test_tracer_snippet_truncation_guard(tmp_path: Path):
    target_log = tmp_path / "test_eval_trunc.md"
    tracer = AgentEvalTracer(enabled=True, log_file_path=target_log)

    session = tracer.start_session("Kueri data panjang")
    huge_result = "KONTEN_PENTING " * 100  # ~1500 chars

    session.record_turn(
        turn_num=1,
        raw_llm_response="<search_chat>query</search_chat>",
        executed_actions=[
            {
                "tool": "search_chat",
                "status": "success",
                "type": "read",
                "read_result": huge_result,
            }
        ],
    )
    session.finish("Selesai")

    content = target_log.read_text(encoding="utf-8")
    # Snippet must be truncated with safe indicator to conserve tokens
    assert "terpotong" in content or len(content) < 1500


@pytest.mark.asyncio
async def test_handle_user_message_with_eval_logger_integration(mocker, tmp_path: Path):
    """End-to-end integration test verifying handle_user_message writes eval trace when enabled."""
    target_log = tmp_path / "integration_eval.md"
    from src.config import Config
    from src.core.eval_logger import eval_tracer

    mocker.patch.object(Config, "ENABLE_EVAL_LOG", True)
    mocker.patch.object(Config, "EVAL_LOG_PATH", str(target_log))
    mocker.patch.object(eval_tracer, "_enabled", True)
    mocker.patch.object(eval_tracer, "_log_file_path", target_log)

    # Mock LLM response: direct simple message
    mocker.patch(
        "src.core.llm.llm_client.generate_response",
        mocker.AsyncMock(return_value="Halo, saya Vigil siap bantu."),
    )

    reply = await handle_user_message("Halo vigil!")
    assert reply == "Halo, saya Vigil siap bantu."

    assert target_log.exists()
    trace_text = target_log.read_text(encoding="utf-8")
    assert '[USER QUERY]: "Halo vigil!"' in trace_text
    assert "=== INTERACTION START:" in trace_text
    assert "=== INTERACTION END ===" in trace_text
    assert '"Halo, saya Vigil siap bantu."' in trace_text


def test_guidance_strips_tool_results_and_keeps_rules():
    from src.core.eval_logger import _extract_guidance_text

    raw = (
        "[TOOL EXECUTION RESULTS]\n"
        'Lab Notes Search Results (query: "Redis memory limit"):\n'
        "- [Redis] Memory limit 512MB\n"
        "Total: 1 notes found.\n\n"
        "search_notes usage: 1/3.\n"
        "1. If the results answer the question, answer using the evidence.\n"
        "2. If results are empty or insufficient, call <search_notes> again.\n"
        "3. If evidence remains insufficient, say so."
    )
    extracted = _extract_guidance_text(raw)
    assert "[TOOL EXECUTION RESULTS]" not in extracted
    assert "Lab Notes Search Results" not in extracted
    assert "search_notes usage: 1/3." in extracted
    assert "1. If the results answer the question" in extracted
