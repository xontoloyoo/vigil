"""Unit tests for LLMClient._parse_usage schema normalization."""

import pytest
from src.core.llm import LLMClient, TokenUsage


@pytest.fixture
def client():
    """Provides an isolated LLMClient instance."""
    return LLMClient(api_key="test-key", base_url="https://api.test.com/v1")


def test_openai_basic_usage(client):
    """Verify standard OpenAI usage fields."""
    raw = {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
    }
    usage = client._parse_usage(raw, duration_seconds=1.2)
    assert usage.prompt_tokens == 100
    assert usage.completion_tokens == 50
    assert usage.total_tokens == 150
    assert usage.cached_tokens == 0
    assert usage.cache_write_tokens == 0
    assert usage.reasoning_tokens == 0
    assert usage.duration_seconds == 1.2


def test_openai_cached_usage(client):
    """Verify OpenAI cached prompt tokens and ensure cached_tokens does not subtract from prompt_tokens."""
    raw = {
        "prompt_tokens": 1000,
        "completion_tokens": 300,
        "total_tokens": 1300,
        "prompt_tokens_details": {
            "cached_tokens": 800,
        },
    }
    usage = client._parse_usage(raw, duration_seconds=2.0)
    assert usage.prompt_tokens == 1000
    assert usage.completion_tokens == 300
    assert usage.cached_tokens == 800
    assert usage.cache_write_tokens == 0
    assert usage.reasoning_tokens == 0
    assert usage.total_tokens == 1300


def test_openai_reasoning_usage(client):
    """Verify OpenAI reasoning tokens under completion_tokens_details."""
    raw = {
        "prompt_tokens": 500,
        "completion_tokens": 400,
        "total_tokens": 900,
        "completion_tokens_details": {
            "reasoning_tokens": 250,
        },
    }
    usage = client._parse_usage(raw)
    assert usage.prompt_tokens == 500
    assert usage.completion_tokens == 400
    assert usage.reasoning_tokens == 250
    assert usage.total_tokens == 900


def test_openrouter_cached_usage(client):
    """Verify OpenRouter-style prompt_tokens_details cached usage."""
    raw = {
        "prompt_tokens": 1200,
        "completion_tokens": 200,
        "total_tokens": 1400,
        "prompt_tokens_details": {
            "cached_tokens": 900,
        },
    }
    usage = client._parse_usage(raw)
    assert usage.prompt_tokens == 1200
    assert usage.completion_tokens == 200
    assert usage.cached_tokens == 900
    assert usage.total_tokens == 1400


def test_deepseek_cache_hit_and_reasoning(client):
    """Verify DeepSeek prompt_cache_hit_tokens and reasoning_tokens."""
    raw = {
        "prompt_tokens": 1500,
        "completion_tokens": 400,
        "total_tokens": 1900,
        "prompt_cache_hit_tokens": 1000,
        "completion_tokens_details": {
            "reasoning_tokens": 300,
        },
    }
    usage = client._parse_usage(raw)
    assert usage.prompt_tokens == 1500
    assert usage.completion_tokens == 400
    assert usage.cached_tokens == 1000
    assert usage.reasoning_tokens == 300
    assert usage.total_tokens == 1900


def test_anthropic_cache_read_write_and_thinking(client):
    """Verify Anthropic input/output tokens, cache read/write, and thinking tokens with fallback total."""
    raw = {
        "input_tokens": 1000,
        "output_tokens": 300,
        "cache_creation_input_tokens": 500,
        "cache_read_input_tokens": 800,
        "output_tokens_details": {
            "thinking_tokens": 100,
        },
    }
    usage = client._parse_usage(raw)
    assert usage.prompt_tokens == 1000
    assert usage.completion_tokens == 300
    assert usage.cached_tokens == 800
    assert usage.cache_write_tokens == 500
    assert usage.reasoning_tokens == 100
    # Native total missing -> fallback calculation (prompt + completion)
    assert usage.total_tokens == 1300


def test_anthropic_ephemeral_ttl_cache_write(client):
    """Verify Anthropic nested cache_creation ephemeral TTL breakdown sum."""
    raw = {
        "input_tokens": 2000,
        "output_tokens": 500,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 300,
            "ephemeral_1h_input_tokens": 200,
        },
        "cache_read_input_tokens": 400,
    }
    usage = client._parse_usage(raw)
    assert usage.prompt_tokens == 2000
    assert usage.completion_tokens == 500
    assert usage.cached_tokens == 400
    assert usage.cache_write_tokens == 500
    assert usage.total_tokens == 2500


def test_gemini_camel_case_usage(client):
    """Verify Gemini camelCase usage fields."""
    raw = {
        "promptTokenCount": 1000,
        "cachedContentTokenCount": 800,
        "candidatesTokenCount": 300,
        "thoughtsTokenCount": 100,
        "totalTokenCount": 1400,
    }
    usage = client._parse_usage(raw)
    assert usage.prompt_tokens == 1000
    assert usage.completion_tokens == 300
    assert usage.cached_tokens == 800
    assert usage.reasoning_tokens == 100
    # Native total prioritized over prompt+completion (1400 != 1300)
    assert usage.total_tokens == 1400


def test_missing_usage(client):
    """Verify None and non-dict usage returns empty TokenUsage with duration."""
    for invalid in [None, [], "not_a_dict", 123]:
        usage = client._parse_usage(invalid, duration_seconds=0.75)
        assert usage.prompt_tokens == 0
        assert usage.completion_tokens == 0
        assert usage.cached_tokens == 0
        assert usage.cache_write_tokens == 0
        assert usage.reasoning_tokens == 0
        assert usage.total_tokens == 0
        assert usage.duration_seconds == 0.75


def test_empty_usage(client):
    """Verify empty dictionary usage returns zeroed TokenUsage."""
    usage = client._parse_usage({}, duration_seconds=1.0)
    assert usage.prompt_tokens == 0
    assert usage.completion_tokens == 0
    assert usage.cached_tokens == 0
    assert usage.cache_write_tokens == 0
    assert usage.reasoning_tokens == 0
    assert usage.total_tokens == 0
    assert usage.duration_seconds == 1.0


def test_total_explicitly_provided_prioritized(client):
    """Verify provider native total is strictly preserved and not recalculated."""
    # When provider reports total that differs from prompt + completion
    raw = {
        "prompt_tokens": 1000,
        "completion_tokens": 300,
        "total_tokens": 1200,
    }
    usage = client._parse_usage(raw)
    assert usage.total_tokens == 1200

    raw_camel = {
        "promptTokenCount": 1000,
        "candidatesTokenCount": 300,
        "totalTokenCount": 1500,
    }
    usage_camel = client._parse_usage(raw_camel)
    assert usage_camel.total_tokens == 1500


def test_total_missing_fallback_calculation(client):
    """Verify fallback calculation when native total is not provided."""
    raw = {
        "prompt_tokens": 750,
        "completion_tokens": 250,
    }
    usage = client._parse_usage(raw)
    assert usage.total_tokens == 1000


def test_zero_valued_fields_handled_correctly(client):
    """Verify zero-valued fields are treated as valid numbers and not discarded."""
    raw = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "prompt_tokens_details": {
            "cached_tokens": 0,
        },
        "completion_tokens_details": {
            "reasoning_tokens": 0,
        },
        "cache_creation_input_tokens": 0,
    }
    usage = client._parse_usage(raw)
    assert usage.prompt_tokens == 0
    assert usage.completion_tokens == 0
    assert usage.total_tokens == 0
    assert usage.cached_tokens == 0
    assert usage.cache_write_tokens == 0
    assert usage.reasoning_tokens == 0


def test_cached_tokens_does_not_subtract_from_prompt_tokens(client):
    """Verify cached tokens does NOT deduct from prompt/input tokens accounting."""
    raw = {
        "prompt_tokens": 1000,
        "completion_tokens": 200,
        "total_tokens": 1200,
        "prompt_tokens_details": {
            "cached_tokens": 800,
        },
    }
    usage = client._parse_usage(raw)
    assert usage.prompt_tokens == 1000
    assert usage.cached_tokens == 800
    assert usage.total_tokens == 1200
