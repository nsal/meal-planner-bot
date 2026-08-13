"""Tests for LLMClient wrapper."""

from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from meal_planner.llm.client import FALLBACK_MESSAGE, LLMClient


@pytest.fixture
def llm_client() -> LLMClient:
    """Return LLMClient instance configured for fast testing."""
    return LLMClient(
        model="gpt-4o-mini",
        api_key="test-key",
        reasoning_effort="medium",
        max_retries=3,
        initial_backoff=0.001,
    )


def mock_response(content: str) -> Any:
    """Helper to generate a mock LiteLLM response object."""
    mock_obj = MagicMock()
    mock_obj.choices = [{"message": {"content": content}}]
    return mock_obj


@pytest.mark.asyncio
async def test_chat_success(
    llm_client: LLMClient, mocker: MockerFixture
) -> None:
    """Test successful chat response."""
    mock_acomp = mocker.patch(
        "litellm.acompletion",
        return_value=mock_response("Hello there!"),
    )

    reply = await llm_client.chat("You are helpful.", "Hi")
    assert reply == "Hello there!"
    mock_acomp.assert_called_once_with(
        model="gpt-4o-mini",
        api_key="test-key",
        reasoning_effort="medium",
        messages=[
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
        ],
    )


@pytest.mark.asyncio
async def test_chat_forwards_high_reasoning_effort(
    mocker: MockerFixture,
) -> None:
    """Allow a higher-effort planner configuration to reach LiteLLM."""
    mock_acomp = mocker.patch(
        "litellm.acompletion", return_value=mock_response("ok")
    )
    client = LLMClient(
        model="gpt-5.6-terra",
        api_key="test-key",
        reasoning_effort="high",
        max_retries=1,
    )

    assert await client.chat("System", "User") == "ok"
    assert mock_acomp.call_args.kwargs["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_chat_json_success(
    llm_client: LLMClient, mocker: MockerFixture
) -> None:
    """Test successful chat_json response."""
    json_str = '{"intent": "log_meal", "entities": {"meal": "salad"}}'
    mocker.patch(
        "litellm.acompletion",
        return_value=mock_response(json_str),
    )

    data = await llm_client.chat_json("System prompt", "User prompt")
    assert data == {"intent": "log_meal", "entities": {"meal": "salad"}}


@pytest.mark.asyncio
async def test_chat_retry_on_transient_error(
    llm_client: LLMClient, mocker: MockerFixture
) -> None:
    """Test retry logic recovering from transient errors."""
    mock_acomp = mocker.patch(
        "litellm.acompletion",
        side_effect=[
            RuntimeError("Rate limit exceeded"),
            RuntimeError("Timeout"),
            mock_response("Success response"),
        ],
    )

    reply = await llm_client.chat("System", "User")
    assert reply == "Success response"
    assert mock_acomp.call_count == 3


@pytest.mark.asyncio
async def test_chat_failure_max_retries(
    llm_client: LLMClient, mocker: MockerFixture
) -> None:
    """Test behavior when max retries are exhausted."""
    mocker.patch(
        "litellm.acompletion",
        side_effect=RuntimeError("API error"),
    )

    reply = await llm_client.chat("System", "User")
    assert reply == FALLBACK_MESSAGE


@pytest.mark.asyncio
async def test_chat_json_malformed_json(
    llm_client: LLMClient, mocker: MockerFixture
) -> None:
    """Test chat_json returning empty dict on malformed JSON."""
    mocker.patch(
        "litellm.acompletion",
        return_value=mock_response("Not a JSON string"),
    )

    data = await llm_client.chat_json("System", "User")
    assert data == {}


def test_chat_sync_returns_text(
    llm_client: LLMClient, mocker: MockerFixture
) -> None:
    """Test chat_sync() produces the same result as chat() without asyncio.run.

    Uses a fresh event loop internally so it must not raise RuntimeError
    even when called from synchronous code (Lambda handler context).
    """
    mocker.patch(
        "litellm.acompletion",
        return_value=mock_response("Sync reply"),
    )

    result = llm_client.chat_sync("System prompt", "User message")
    assert result == "Sync reply"


def test_chat_json_sync_returns_dict(
    llm_client: LLMClient, mocker: MockerFixture
) -> None:
    """Test chat_json_sync() parses JSON and returns a dict without asyncio.run.

    Uses a fresh event loop internally so it must not raise RuntimeError
    even when called from synchronous code (Lambda handler context).
    """
    json_payload = '{"week_start_date": "2026-08-10", "status": "draft"}'
    mocker.patch(
        "litellm.acompletion",
        return_value=mock_response(json_payload),
    )

    result = llm_client.chat_json_sync("System prompt", "User message")
    assert result == {"week_start_date": "2026-08-10", "status": "draft"}


def test_chat_sync_fallback_on_error(
    llm_client: LLMClient, mocker: MockerFixture
) -> None:
    """Test chat_sync() returns FALLBACK_MESSAGE when all retries fail."""
    mocker.patch(
        "litellm.acompletion",
        side_effect=RuntimeError("API down"),
    )

    result = llm_client.chat_sync("System", "User")
    assert result == FALLBACK_MESSAGE
