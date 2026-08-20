"""Bounded LiteLLM client tests."""

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from meal_planner.llm.client import FALLBACK_MESSAGE, LLMClient


class ProviderError(Exception):
    """Test provider error carrying retry metadata."""

    def __init__(
        self,
        status_code: int,
        *,
        headers: dict[str, object] | None = None,
        response_headers: dict[str, object] | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(f"provider status {status_code}")
        self.status_code = status_code
        self.headers = headers
        self.response = MagicMock()
        self.response.headers = response_headers
        self.retry_after = retry_after


def _response(content: str) -> Any:
    response = MagicMock()
    response.choices = [{"message": {"content": content}}]
    return response


@pytest.fixture
def client() -> LLMClient:
    return LLMClient(
        model="gpt-5.6-terra",
        api_key="key",
        reasoning_effort="high",
        max_retries=3,
        initial_backoff=0.25,
        request_timeout=7.0,
    )


@pytest.mark.asyncio
async def test_success_passes_bounded_timeout(
    client: LLMClient, mocker: MockerFixture
) -> None:
    completion = mocker.patch(
        "litellm.acompletion", return_value=_response("hello")
    )
    assert await client.chat("system", "user") == "hello"
    assert completion.call_args.kwargs["timeout"] == 7.0
    assert completion.call_args.kwargs["max_retries"] == 0
    assert completion.call_args.kwargs["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_transient_recovery_uses_provider_retry_guidance(
    client: LLMClient, mocker: MockerFixture
) -> None:
    completion = mocker.patch(
        "litellm.acompletion",
        side_effect=[
            ProviderError(429, headers={"retry-after": "0.4"}),
            _response("ok"),
        ],
    )
    sleep = mocker.patch("asyncio.sleep")
    assert await client.chat("system", "user") == "ok"
    assert completion.call_count == 2
    sleep.assert_awaited_once_with(0.4)


@pytest.mark.asyncio
async def test_provider_retry_guidance_is_capped(
    client: LLMClient, mocker: MockerFixture
) -> None:
    completion = mocker.patch(
        "litellm.acompletion",
        side_effect=[
            ProviderError(429, headers={"Retry-After": "99"}),
            _response("ok"),
        ],
    )
    sleep = mocker.patch("asyncio.sleep")
    assert await client.chat("system", "user") == "ok"
    assert completion.call_count == 2
    sleep.assert_awaited_once_with(5.0)


@pytest.mark.asyncio
async def test_response_headers_precede_legacy_retry_attribute(
    client: LLMClient, mocker: MockerFixture
) -> None:
    completion = mocker.patch(
        "litellm.acompletion",
        side_effect=[
            ProviderError(
                429,
                response_headers={"rEtRy-AfTeR": 0.75},
                retry_after=0.1,
            ),
            _response("ok"),
        ],
    )
    sleep = mocker.patch("asyncio.sleep")

    assert await client.chat("system", "user") == "ok"

    assert completion.call_count == 2
    sleep.assert_awaited_once_with(0.75)


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_after", ["-1", "not-a-number", "nan"])
async def test_invalid_retry_guidance_uses_exponential_fallback(
    client: LLMClient, mocker: MockerFixture, retry_after: str
) -> None:
    completion = mocker.patch(
        "litellm.acompletion",
        side_effect=[
            ProviderError(429, headers={"RETRY-AFTER": retry_after}),
            _response("ok"),
        ],
    )
    sleep = mocker.patch("asyncio.sleep")

    assert await client.chat("system", "user") == "ok"

    assert completion.call_count == 2
    sleep.assert_awaited_once_with(0.25)


def test_legacy_retry_attribute_remains_supported(client: LLMClient) -> None:
    assert client._retry_delay(ProviderError(429, retry_after=0.4), 0) == 0.4


@pytest.mark.asyncio
async def test_timeout_exhaustion_is_bounded(
    client: LLMClient, mocker: MockerFixture
) -> None:
    completion = mocker.patch(
        "litellm.acompletion", side_effect=TimeoutError("slow")
    )
    sleep = mocker.patch("asyncio.sleep")
    assert await client.chat("system", "user") == FALLBACK_MESSAGE
    assert completion.call_count == 3
    assert [call.args[0] for call in sleep.await_args_list] == [0.25, 0.5]


@pytest.mark.asyncio
async def test_permanent_failure_is_not_retried(
    client: LLMClient, mocker: MockerFixture
) -> None:
    completion = mocker.patch(
        "litellm.acompletion", side_effect=ProviderError(400)
    )
    assert await client.chat("system", "user") == FALLBACK_MESSAGE
    completion.assert_awaited_once()


@pytest.mark.parametrize(
    ("status_code", "expected_attempts", "expected_category"),
    [(429, 3, "transient"), (400, 1, "permanent")],
)
def test_chat_sync_retry_logs_never_include_provider_exception_text(
    client: LLMClient,
    mocker: MockerFixture,
    caplog: pytest.LogCaptureFixture,
    status_code: int,
    expected_attempts: int,
    expected_category: str,
) -> None:
    """Retry records contain only bounded application-owned metadata."""
    error = ProviderError(status_code)
    error.args = (
        "preference=PRIVATE_LOW_SODIUM\nrequest body contains private data",
    )
    completion = mocker.patch("litellm.acompletion", side_effect=error)
    mocker.patch("asyncio.sleep")

    with caplog.at_level(logging.WARNING, logger="meal_planner.llm.client"):
        result = client.chat_sync("system", "user")

    assert result == FALLBACK_MESSAGE
    assert completion.call_count == expected_attempts
    assert "PRIVATE_LOW_SODIUM" not in caplog.text
    assert "request body contains private data" not in caplog.text
    records = [
        record
        for record in caplog.records
        if record.name == "meal_planner.llm.client"
    ]
    assert records
    assert all(
        "PRIVATE_LOW_SODIUM" not in record.getMessage() for record in records
    )
    assert all(
        "request body contains private data" not in repr(record.__dict__)
        for record in records
    )
    warning = next(
        record for record in records if record.levelno == logging.WARNING
    )
    assert warning.getMessage() == (
        f"LLM request attempt 1 of {client.max_retries} failed "
        f"category={expected_category}"
    )


@pytest.mark.asyncio
async def test_chat_json_rejects_malformed_json(
    client: LLMClient, mocker: MockerFixture
) -> None:
    completion = mocker.patch(
        "litellm.acompletion", return_value=_response("not json")
    )
    assert await client.chat_json("system", "user") == {}
    assert completion.call_args.kwargs["timeout"] == 7.0
    assert completion.call_args.kwargs["max_retries"] == 0


@pytest.mark.asyncio
async def test_strict_json_forwards_configured_timeout(
    client: LLMClient, mocker: MockerFixture
) -> None:
    completion = mocker.patch(
        "litellm.acompletion", return_value=_response('{"value": 1}')
    )

    assert await client.chat_json_strict("system", "user") == {"value": 1}
    assert completion.call_args.kwargs["timeout"] == 7.0
    assert completion.call_args.kwargs["max_retries"] == 0


def test_sync_wrappers_return_text_and_json(
    client: LLMClient, mocker: MockerFixture
) -> None:
    mocker.patch(
        "litellm.acompletion",
        side_effect=[_response("hello"), _response('{"value": 1}')],
    )
    assert client.chat_sync("system", "user") == "hello"
    assert client.chat_json_sync("system", "user") == {"value": 1}
