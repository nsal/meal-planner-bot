"""Bounded LiteLLM client tests."""

from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from meal_planner.llm.client import FALLBACK_MESSAGE, LLMClient


class ProviderError(Exception):
    """Test provider error carrying retry metadata."""

    def __init__(
        self, status_code: int, *, retry_after: float | None = None
    ) -> None:
        super().__init__(f"provider status {status_code}")
        self.status_code = status_code
        self.retry_after = retry_after


def _response(content: str) -> Any:
    response = MagicMock()
    response.choices = [{"message": {"content": content}}]
    return response


@pytest.fixture
def client() -> LLMClient:
    return LLMClient(
        api_key="key",
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


@pytest.mark.asyncio
async def test_transient_recovery_uses_provider_retry_guidance(
    client: LLMClient, mocker: MockerFixture
) -> None:
    completion = mocker.patch(
        "litellm.acompletion",
        side_effect=[ProviderError(429, retry_after=0.4), _response("ok")],
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
        side_effect=[ProviderError(429, retry_after=99), _response("ok")],
    )
    sleep = mocker.patch("asyncio.sleep")
    assert await client.chat("system", "user") == "ok"
    assert completion.call_count == 2
    sleep.assert_awaited_once_with(5.0)


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


def test_sync_wrappers_return_text_and_json(
    client: LLMClient, mocker: MockerFixture
) -> None:
    mocker.patch(
        "litellm.acompletion",
        side_effect=[_response("hello"), _response('{"value": 1}')],
    )
    assert client.chat_sync("system", "user") == "hello"
    assert client.chat_json_sync("system", "user") == {"value": 1}
