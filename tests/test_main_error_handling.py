"""Unit tests for error handling and concise LLM error messages in main.py."""

import pytest
import aiosqlite
from src.core.llm import LLMError, llm_client
from src.main import handle_user_message


@pytest.mark.asyncio
async def test_handle_user_message_llm_rate_limit(mocker, mock_db_path):
    """Verify HTTP 429 status code is formatted concisely and prompts /retry."""
    mocker.patch.object(
        llm_client,
        "generate_response",
        side_effect=LLMError("Too Many Requests", status_code=429),
    )

    reply = await handle_user_message("Halo vigil!")
    assert "⚠️ Gagal menghubungi LLM (HTTP 429: Rate Limit). Ketik /retry untuk mencoba lagi." == reply

    # Verify message was NOT saved to chat_history
    async with aiosqlite.connect(mock_db_path) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM chat_history WHERE content = 'Halo vigil!'"
        ) as cursor:
            count = (await cursor.fetchone())[0]
            assert count == 0


@pytest.mark.asyncio
async def test_handle_user_message_llm_server_error(mocker):
    """Verify HTTP 500 status code is formatted concisely and prompts /retry."""
    mocker.patch.object(
        llm_client,
        "generate_response",
        side_effect=LLMError("Internal Server Error", status_code=500),
    )

    reply = await handle_user_message("Halo vigil!")
    assert "⚠️ Gagal menghubungi LLM (HTTP 500: Internal Server Error). Ketik /retry untuk mencoba lagi." == reply


@pytest.mark.asyncio
async def test_handle_user_message_llm_timeout(mocker):
    """Verify request timeout is formatted concisely and prompts /retry."""
    mocker.patch.object(
        llm_client,
        "generate_response",
        side_effect=LLMError("Request timed out after 60s", is_timeout=True),
    )

    reply = await handle_user_message("Halo vigil!")
    assert "⚠️ Gagal menghubungi LLM (Request Timeout). Ketik /retry untuk mencoba lagi." == reply


@pytest.mark.asyncio
async def test_handle_user_message_llm_network_error(mocker):
    """Verify network drop is formatted concisely and prompts /retry."""
    mocker.patch.object(
        llm_client,
        "generate_response",
        side_effect=LLMError("Connection reset by peer", is_network_error=True),
    )

    reply = await handle_user_message("Halo vigil!")
    assert "⚠️ Gagal menghubungi LLM (Koneksi Terputus). Ketik /retry untuk mencoba lagi." == reply


@pytest.mark.asyncio
async def test_handle_user_message_llm_other_error(mocker):
    """Verify unclassified LLM error is formatted with its message and prompts /retry."""
    mocker.patch.object(
        llm_client,
        "generate_response",
        side_effect=LLMError("Custom LLM API failure"),
    )

    reply = await handle_user_message("Halo vigil!")
    assert "⚠️ Gagal menghubungi LLM (Custom LLM API failure). Ketik /retry untuk mencoba lagi." == reply


@pytest.mark.asyncio
async def test_handle_user_message_generic_exception(mocker):
    """Verify unexpected general exception returns internal error with /retry."""
    mocker.patch.object(
        llm_client,
        "generate_response",
        side_effect=ValueError("Unexpected crash in pipeline"),
    )

    reply = await handle_user_message("Halo vigil!")
    assert "⚠️ Terjadi kesalahan internal saat memproses pesan Anda. Ketik /retry untuk mencoba lagi." == reply
