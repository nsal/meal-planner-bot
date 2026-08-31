"""Small LiteLLM wrapper for one plain-text plan-chat request."""

import asyncio
import logging
from typing import Any

import litellm

logger = logging.getLogger(__name__)


class LLMFailure(RuntimeError):
    """Base class for provider failures classified by the worker."""


class LLMTimeoutError(LLMFailure):
    """The provider request exceeded its transport timeout."""


class LLMTransientError(LLMFailure):
    """The provider returned a retryable service failure."""


class LLMPermanentError(LLMFailure):
    """The provider rejected the request permanently."""


class LLMTextResponseError(LLMFailure):
    """The provider response did not contain usable plain text."""


class LLMClient:
    """Make one bounded, unstructured LiteLLM completion request."""

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        api_key: str = "",
        reasoning_effort: str = "medium",
        request_timeout: float = 20.0,
    ) -> None:
        """Initialize the client with one bounded provider request."""
        self.model = model
        self.api_key = api_key
        self.reasoning_effort = reasoning_effort
        self.request_timeout = request_timeout

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        """Return whether a provider exception is operationally transient."""
        if isinstance(
            exc, (TimeoutError, asyncio.TimeoutError, ConnectionError)
        ):
            return True
        return getattr(exc, "status_code", None) in {
            408,
            409,
            429,
            500,
            502,
            503,
            504,
        }

    @staticmethod
    def _is_timeout(exc: Exception) -> bool:
        """Recognize native and provider-specific timeout exceptions."""
        return isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or (
            "timeout" in type(exc).__name__.casefold()
        )

    async def chat_text_strict(
        self, system_prompt: str, user_message: str
    ) -> str:
        """Return one plain-text response or a typed operational failure."""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "timeout": self.request_timeout,
            "max_retries": 0,
            "reasoning_effort": self.reasoning_effort,
        }
        if self.api_key:
            kwargs["api_key"] = self.api_key
        try:
            response = await litellm.acompletion(**kwargs)
        except Exception as exc:
            if self._is_timeout(exc):
                raise LLMTimeoutError from exc
            if self._is_transient(exc):
                raise LLMTransientError from exc
            raise LLMPermanentError from exc

        choices = getattr(response, "choices", [])
        if not choices:
            raise LLMTextResponseError("provider returned no choices")
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message", {})
            content = (
                message.get("content", "") if isinstance(message, dict) else ""
            )
        else:
            message = getattr(first, "message", None)
            content = (
                message.get("content", "")
                if isinstance(message, dict)
                else getattr(message, "content", "")
            )
        if not isinstance(content, str) or not content.strip():
            raise LLMTextResponseError("provider returned empty text")
        return content

    def chat_text_strict_sync(
        self, system_prompt: str, user_message: str
    ) -> str:
        """Synchronously invoke the one-request text operation."""
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                self.chat_text_strict(system_prompt, user_message)
            )
        finally:
            loop.close()
