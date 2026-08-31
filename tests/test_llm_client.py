"""Tests for the one-request plain-text LLM client."""

import inspect
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from meal_planner.llm.client import (
    LLMClient,
    LLMFailure,
    LLMPermanentError,
    LLMTextResponseError,
    LLMTimeoutError,
    LLMTransientError,
)


def _response(content: str) -> Any:
    """Return a provider-shaped response."""
    response = MagicMock()
    response.choices = [{"message": {"content": content}}]
    return response


@pytest.fixture
def client() -> LLMClient:
    """Return a configured one-attempt client."""
    return LLMClient(
        model="gpt-5.6-terra",
        api_key="key",
        reasoning_effort="high",
        request_timeout=7.0,
    )


@pytest.mark.asyncio
async def test_text_request_is_one_plain_completion(
    client: LLMClient, mocker: MockerFixture
) -> None:
    """The worker request does not ask for structured JSON output."""
    assert "max_retries" not in inspect.signature(LLMClient).parameters
    completion = mocker.patch(
        "litellm.acompletion", return_value=_response("Draft\n- Dinner")
    )

    assert await client.chat_text_strict("system", "prompt") == (
        "Draft\n- Dinner"
    )
    completion.assert_awaited_once()
    assert completion.call_args.kwargs["timeout"] == 7.0
    assert completion.call_args.kwargs["max_retries"] == 0
    assert "response_format" not in completion.call_args.kwargs
    assert not hasattr(client, "max_retries")


@pytest.mark.asyncio
async def test_blank_text_is_rejected(
    client: LLMClient, mocker: MockerFixture
) -> None:
    """Whitespace-only provider output is not a usable draft."""
    mocker.patch("litellm.acompletion", return_value=_response(" \n "))
    with pytest.raises(LLMTextResponseError):
        await client.chat_text_strict("system", "prompt")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TimeoutError("slow"), LLMTimeoutError),
        (ConnectionError("offline"), LLMTransientError),
        (ValueError("rejected"), LLMPermanentError),
    ],
)
async def test_provider_failures_are_typed(
    client: LLMClient,
    mocker: MockerFixture,
    error: Exception,
    expected: type[LLMFailure],
) -> None:
    """Technical provider failures retain only bounded classifications."""
    mocker.patch("litellm.acompletion", side_effect=error)
    with pytest.raises(expected):
        await client.chat_text_strict("system", "prompt")


def test_sync_text_wrapper_returns_unchanged_text(
    client: LLMClient, mocker: MockerFixture
) -> None:
    """The Lambda-facing wrapper preserves provider text exactly."""
    mocker.patch("litellm.acompletion", return_value=_response("A\n- meal"))
    assert client.chat_text_strict_sync("system", "prompt") == "A\n- meal"
