"""Asynchronous LLM client for vigil.

Provides a lightweight, resource-efficient HTTP client using pure httpx,
compatible with OpenAI Chat Completions and OpenRouter APIs. Tracks token metrics
and prompt caching details.
"""

import asyncio
from dataclasses import dataclass
import json
import logging
import time
from typing import Any
import httpx

from src.config import config

logger = logging.getLogger(__name__)


@dataclass
class TokenUsage:
    """Usage metrics and timing for an LLM generation call."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    duration_seconds: float = 0.0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0


class LLMError(RuntimeError):
    """Structured exception for LLM API errors, timeouts, and network issues."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        is_timeout: bool = False,
        is_network_error: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.is_timeout = is_timeout
        self.is_network_error = is_network_error


_MISSING = object()


def _get_nested_value(data: Any, path: str) -> Any:
    """Retrieve value from a nested dict using dot-separated keys."""
    if not isinstance(data, dict):
        return _MISSING
    current = data
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return _MISSING
        current = current[key]
    return current


def _first_int(data: dict[str, Any] | None, *paths: str) -> int | None:
    """Return the first found integer/numeric value converted to int, or None if none found.

    Differentiates between 0 (valid) and missing/None.
    """
    if not isinstance(data, dict):
        return None
    for path in paths:
        val = _get_nested_value(data, path)
        if val is not _MISSING and val is not None:
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                return int(val)
            if isinstance(val, str) and val.strip().isdigit():
                return int(val.strip())
    return None


class LLMClient:
    """Async client for interacting with OpenAI-compatible Chat Completion APIs."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 60.0,
        max_retries: int = 2,
    ) -> None:
        self.api_key = api_key if api_key is not None else (config.LLM_API_KEY or "")
        self.base_url = (
            base_url
            if base_url is not None
            else (config.LLM_BASE_URL or "https://openrouter.ai/api/v1")
        )
        self.model = (
            model
            if model is not None
            else (config.LLM_MODEL_NAME or "meta-llama/llama-3.3-70b-instruct")
        )
        self.timeout = timeout
        self.max_retries = max_retries
        self.last_usage: TokenUsage | None = None

    def _get_headers(self) -> dict[str, str]:
        """Construct standard and project-specific custom HTTP headers."""
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "VigilAgent/1.0",
            "x-client-name": "vigil-agent",
        }

    def _parse_usage(
        self,
        usage: dict[str, Any] | None,
        duration_seconds: float = 0.0,
    ) -> TokenUsage:
        """Normalize usage statistics across diverse LLM provider schemas."""
        if not isinstance(usage, dict):
            return TokenUsage(duration_seconds=duration_seconds)

        # 1. Normalize input/prompt tokens
        # Priority: prompt_tokens -> input_tokens -> promptTokenCount
        prompt_val = _first_int(usage, "prompt_tokens", "input_tokens", "promptTokenCount")
        prompt_tokens = 0 if prompt_val is None else prompt_val

        # 2. Normalize output/completion tokens
        # Priority: completion_tokens -> output_tokens -> candidatesTokenCount -> responseTokenCount
        completion_val = _first_int(
            usage,
            "completion_tokens",
            "output_tokens",
            "candidatesTokenCount",
            "responseTokenCount",
        )
        completion_tokens = 0 if completion_val is None else completion_val

        # 3. Normalize cache-read tokens (cached_tokens)
        # Priority: prompt_tokens_details.cached_tokens -> input_tokens_details.cached_tokens
        #          -> cache_read_input_tokens -> cachedContentTokenCount -> prompt_cache_hit_tokens
        cached_val = _first_int(
            usage,
            "prompt_tokens_details.cached_tokens",
            "input_tokens_details.cached_tokens",
            "cache_read_input_tokens",
            "cachedContentTokenCount",
            "prompt_cache_hit_tokens",
        )
        cached_tokens = 0 if cached_val is None else cached_val

        # 4. Normalize cache-write tokens
        # From: cache_creation_input_tokens and nested Anthropic-style:
        # cache_creation.ephemeral_5m_input_tokens + cache_creation.ephemeral_1h_input_tokens
        write_5m = _first_int(usage, "cache_creation.ephemeral_5m_input_tokens")
        write_1h = _first_int(usage, "cache_creation.ephemeral_1h_input_tokens")
        ttl_sum = 0
        has_ttl = False
        if write_5m is not None:
            ttl_sum += write_5m
            has_ttl = True
        if write_1h is not None:
            ttl_sum += write_1h
            has_ttl = True

        cache_creation_flat = _first_int(usage, "cache_creation_input_tokens")

        if has_ttl and ttl_sum > 0:
            cache_write_tokens = ttl_sum
        elif cache_creation_flat is not None:
            cache_write_tokens = cache_creation_flat
        elif has_ttl:
            cache_write_tokens = ttl_sum
        else:
            cache_write_tokens = 0

        # 5. Normalize reasoning/thinking tokens
        # Priority: completion_tokens_details.reasoning_tokens -> output_tokens_details.reasoning_tokens
        #          -> output_tokens_details.thinking_tokens -> thoughtsTokenCount
        reasoning_val = _first_int(
            usage,
            "completion_tokens_details.reasoning_tokens",
            "output_tokens_details.reasoning_tokens",
            "output_tokens_details.thinking_tokens",
            "thoughtsTokenCount",
        )
        reasoning_tokens = 0 if reasoning_val is None else reasoning_val

        # 6. Normalize total tokens
        # Priority: native total (total_tokens, totalTokenCount) -> fallback (prompt + completion)
        native_total = _first_int(usage, "total_tokens", "totalTokenCount")
        if native_total is not None:
            total_tokens = native_total
        else:
            total_tokens = prompt_tokens + completion_tokens

        return TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
            total_tokens=total_tokens,
            duration_seconds=duration_seconds,
            cache_write_tokens=cache_write_tokens,
            reasoning_tokens=reasoning_tokens,
        )

    async def generate_response(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> str:
        """Call the Chat Completions endpoint and return the model's raw text response.

        Retries on network errors or timeouts up to `max_retries`.
        Raises ValueError if configuration is missing or RuntimeError on HTTP errors.
        """
        if not self.api_key or not self.api_key.strip():
            raise ValueError("LLM_API_KEY is not configured or empty.")
        if not self.base_url or not self.base_url.strip():
            raise ValueError("LLM_BASE_URL is not configured or empty.")

        endpoint = f"{self.base_url.rstrip('/')}/chat/completions"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        headers = self._get_headers()
        start_time = time.perf_counter()
        last_exception: Exception | None = None
        response: httpx.Response | None = None

        logger.debug(
            "[DEBUG] LLM request -> endpoint=%s model=%s messages=%d temp=%.2f",
            endpoint,
            self.model,
            len(messages),
            temperature,
        )
        import json as _json
        logger.debug(
            "[DEBUG] LLM full payload messages:\n%s",
            _json.dumps(messages, ensure_ascii=False, indent=2),
        )

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(self.max_retries + 1):
                try:
                    response = await client.post(endpoint, json=payload, headers=headers)
                    break
                except (httpx.NetworkError, httpx.TimeoutException) as exc:
                    last_exception = exc
                    logger.warning(
                        "LLM request attempt %d failed (%s). Retrying...",
                        attempt + 1,
                        exc,
                    )
                    if attempt < self.max_retries:
                        await asyncio.sleep(0.5 * (2**attempt))
                        continue
                    is_to = isinstance(exc, httpx.TimeoutException)
                    raise LLMError(
                        f"LLM request timed out after {self.max_retries + 1} attempts"
                        if is_to
                        else f"LLM network error after {self.max_retries + 1} attempts: {exc}",
                        is_timeout=is_to,
                        is_network_error=not is_to,
                    ) from exc
            else:
                if last_exception:
                    is_to = isinstance(last_exception, httpx.TimeoutException)
                    raise LLMError(
                        "LLM request timed out" if is_to else f"LLM request failed: {last_exception}",
                        is_timeout=is_to,
                        is_network_error=not is_to,
                    ) from last_exception
                raise LLMError("LLM request failed with unknown error.")

        duration = time.perf_counter() - start_time

        if response is None:
            raise LLMError("No HTTP response received from LLM endpoint.")

        if response.status_code != 200:
            err_detail = ""
            try:
                err_json = response.json()
                if isinstance(err_json, dict) and "error" in err_json:
                    err_obj = err_json["error"]
                    if isinstance(err_obj, dict):
                        err_detail = err_obj.get("message") or ""
                    elif isinstance(err_obj, str):
                        err_detail = err_obj
            except Exception:
                pass
            reason = err_detail or response.reason_phrase or "API Error"
            raise LLMError(
                f"LLM API returned error status {response.status_code}: {reason}",
                status_code=response.status_code,
            )

        raw_text = response.text
        logger.debug(
            "[DEBUG] LLM HTTP response: status=%d body_length=%d body_preview:\n%s",
            response.status_code,
            len(raw_text),
            raw_text,
        )
        if not raw_text or not raw_text.strip():
            logger.error(
                "LLM API mengembalikan body kosong! Status: %s, Headers: %s",
                response.status_code,
                dict(response.headers),
            )
            raise ValueError("LLM endpoint mengembalikan body kosong (0 bytes).")

        data: dict[str, Any]
        try:
            parsed = response.json()
            if not isinstance(parsed, dict):
                raise ValueError(
                    f"JSON response is not a dictionary/object (type: {type(parsed).__name__})"
                )
            data = parsed
        except Exception as json_err:
            preview = raw_text[:500]
            logger.error(
                "Gagal parse JSON dari LLM API! Status: %s, Headers: %s, Raw Text (first 500 chars): %r",
                response.status_code,
                dict(response.headers),
                preview,
            )
            # Fallback jika respon berformat Server-Sent Events (SSE)
            stripped = raw_text.strip()
            if stripped.startswith("data:") or "\ndata:" in stripped:
                extracted = self._parse_sse_stream(raw_text)
                if extracted:
                    self.last_usage = TokenUsage(duration_seconds=duration)
                    logger.info("[LLM] Berhasil mengekstrak teks dari respon berformat SSE stream.")
                    return extracted
                logger.warning(
                    "[LLM] Respon berformat SSE terdeteksi namun tidak ada konten teks yang berhasil diekstrak."
                )

            raise RuntimeError(
                f"Respon LLM bukan JSON valid dan tidak dapat diparsing. Status: {response.status_code}, Body: {preview}"
            ) from json_err

        # Extract usage and prompt caching details safely
        usage = data.get("usage") if isinstance(data, dict) else None
        self.last_usage = self._parse_usage(
            usage,
            duration_seconds=duration,
        )

        logger.info(
            "[LLM] Sukses (%.2fs) | Prompt: %d | Cached: %d | Output: %d",
            duration,
            self.last_usage.prompt_tokens,
            self.last_usage.cached_tokens,
            self.last_usage.completion_tokens,
        )

        # Safe extraction of content across various backend variations (OpenAI, vLLM, Ollama)
        choices = data.get("choices")
        if isinstance(choices, list) and len(choices) > 0:
            first_choice = choices[0]
            if isinstance(first_choice, dict):
                # Standard OpenAI Chat Completions: choices[0].message.content
                message = first_choice.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str):
                        return content
                    if isinstance(content, list):
                        parts = [
                            str(part.get("text", ""))
                            for part in content
                            if isinstance(part, dict) and "text" in part
                        ]
                        return "".join(parts)
                # Raw completions format: choices[0].text
                if "text" in first_choice and isinstance(first_choice["text"], str):
                    return first_choice["text"]
                # Streaming delta format: choices[0].delta.content
                delta = first_choice.get("delta")
                if isinstance(delta, dict) and "content" in delta:
                    return str(delta.get("content") or "")

        # Fallbacks for Ollama native endpoints (/api/generate or /api/chat)
        if "response" in data and isinstance(data["response"], str):
            return data["response"]
        if "message" in data and isinstance(data["message"], dict):
            content = data["message"].get("content")
            if isinstance(content, str):
                return content

        return ""

    def _parse_sse_stream(self, raw_text: str) -> str:
        """Fallback parser to extract text from Server-Sent Events (SSE) stream."""
        content_parts: list[str] = []
        for line in raw_text.splitlines():
            line = line.strip()
            if not line or not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except Exception:
                continue

            if not isinstance(chunk, dict):
                continue

            choices = chunk.get("choices")
            if isinstance(choices, list) and len(choices) > 0:
                first = choices[0]
                if isinstance(first, dict):
                    delta = first.get("delta")
                    if isinstance(delta, dict):
                        content = delta.get("content")
                        if isinstance(content, str):
                            content_parts.append(content)
                        elif isinstance(content, list):
                            for part in content:
                                if isinstance(part, dict) and "text" in part:
                                    content_parts.append(str(part["text"]))
                    elif "text" in first and isinstance(first["text"], str):
                        content_parts.append(first["text"])

        return "".join(content_parts)

    async def check_connection(self) -> bool:
        """Verify endpoint connectivity."""
        if not self.base_url:
            return False
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(self.base_url)
                return resp.status_code < 500
        except Exception:
            return False


# Default ready-to-use instance
llm_client = LLMClient()
