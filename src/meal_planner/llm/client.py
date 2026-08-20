"""LiteLLM wrapper with retry logic and JSON response support."""

import asyncio
import json
import logging
import math
from collections.abc import Callable, Iterable
from typing import Any, Optional, cast

import litellm

logger = logging.getLogger(__name__)

FALLBACK_MESSAGE = (
    "Sorry, I am having trouble connecting to the AI service right now. "
    "Please try again in a moment."
)


class LLMFailure(RuntimeError):
    """Base class for failures that strict callers must classify."""


class LLMTimeoutError(LLMFailure):
    """The provider request exceeded its transport timeout."""


class LLMTransientError(LLMFailure):
    """The provider returned a retryable transport or service failure."""


class LLMPermanentError(LLMFailure):
    """The provider rejected the request permanently."""


class LLMResponseFormatError(LLMFailure):
    """The provider response was not a JSON object."""


class LLMClient:
    """Wrapper around LiteLLM for text and structured JSON completion."""

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        api_key: str = "",
        reasoning_effort: str = "medium",
        max_retries: int = 3,
        initial_backoff: float = 1.0,
        request_timeout: float = 20.0,
    ) -> None:
        """Initialize LLM client with model settings and retry parameters."""
        self.model = model
        self.api_key = api_key
        self.reasoning_effort = reasoning_effort
        self.max_retries = max_retries
        self.initial_backoff: float = initial_backoff
        self.request_timeout: float = request_timeout

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        """Return whether an error is safe to retry."""
        if isinstance(
            exc, (TimeoutError, ConnectionError, asyncio.TimeoutError)
        ):
            return True
        status = getattr(exc, "status_code", None)
        return status in {408, 409, 429, 500, 502, 503, 504}

    @staticmethod
    def _is_timeout(exc: Exception) -> bool:
        """Recognize native and provider-specific timeout exceptions."""
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            return True
        return "timeout" in type(exc).__name__.casefold()

    @staticmethod
    def _retry_after_from_headers(headers: object) -> float | None:
        """Read a valid Retry-After value from a mapping-like object."""
        items_method = getattr(headers, "items", None)
        if not callable(items_method):
            return None
        typed_items_method = cast(Callable[[], object], items_method)
        try:
            header_items = typed_items_method()
        except AttributeError, TypeError, ValueError:
            return None
        if not isinstance(header_items, Iterable):
            return None
        for item in header_items:
            if not isinstance(item, tuple) or len(item) != 2:
                continue
            name, value = item
            if not isinstance(name, str) or name.lower() != "retry-after":
                continue
            parsed = LLMClient._parse_retry_after(value)
            if parsed is not None:
                return parsed
        return None

    @staticmethod
    def _parse_retry_after(value: object) -> float | None:
        """Parse finite guidance and cap it at five seconds."""
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            parsed = float(value)
        elif isinstance(value, str):
            try:
                parsed = float(value.strip())
            except ValueError:
                return None
        else:
            return None
        if not math.isfinite(parsed) or parsed < 0:
            return None
        return min(parsed, 5.0)

    def _retry_delay(self, exc: Exception, attempt: int) -> float:
        """Use provider retry guidance or bounded exponential backoff."""
        for headers in (
            getattr(exc, "headers", None),
            getattr(getattr(exc, "response", None), "headers", None),
        ):
            guided_delay = self._retry_after_from_headers(headers)
            if guided_delay is not None:
                return guided_delay
        guided_delay = self._parse_retry_after(
            getattr(exc, "retry_after", None)
        )
        if guided_delay is not None:
            return guided_delay
        delay: float = self.initial_backoff * float(2**attempt)
        return delay if delay < 5.0 else 5.0

    async def _execute_with_retry(
        self,
        messages: list[dict[str, str]],
        response_format: Optional[dict[str, str]] = None,
    ) -> Optional[str]:
        """Execute acompletion with exponential backoff retry logic."""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "timeout": self.request_timeout,
            "max_retries": 0,
            "reasoning_effort": self.reasoning_effort,
        }
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if response_format:
            kwargs["response_format"] = response_format

        for attempt in range(self.max_retries):
            try:
                response = await litellm.acompletion(**kwargs)
                choices = getattr(response, "choices", [])
                if choices:
                    first = choices[0]
                    if isinstance(first, dict):
                        msg = first.get("message", {})
                        if isinstance(msg, dict):
                            return str(msg.get("content", ""))
                        return str(getattr(msg, "content", ""))
                    msg = getattr(first, "message", None)
                    if isinstance(msg, dict):
                        return str(msg.get("content", ""))
                    if msg is not None:
                        return str(getattr(msg, "content", ""))
                return ""
            except Exception as exc:
                transient = self._is_transient(exc)
                category = "transient" if transient else "permanent"
                logger.warning(
                    "LLM request attempt %d of %d failed category=%s",
                    attempt + 1,
                    self.max_retries,
                    category,
                )
                if not transient:
                    logger.error("LLM request failed permanently")
                    return None
                if attempt >= self.max_retries - 1:
                    logger.error("LLM request failed after max retries")
                    return None
                await asyncio.sleep(self._retry_delay(exc, attempt))
        return None

    async def _execute_strict_once(
        self,
        messages: list[dict[str, str]],
    ) -> dict[str, Any]:
        """Make exactly one JSON request and retain its failure category."""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "timeout": self.request_timeout,
            "max_retries": 0,
            "reasoning_effort": self.reasoning_effort,
            "response_format": {"type": "json_object"},
        }
        if self.api_key:
            kwargs["api_key"] = self.api_key
        try:
            response = await litellm.acompletion(**kwargs)
        except Exception as exc:
            if self._is_timeout(exc):
                raise LLMTimeoutError(str(exc)) from exc
            if self._is_transient(exc):
                raise LLMTransientError(str(exc)) from exc
            raise LLMPermanentError(str(exc)) from exc

        choices = getattr(response, "choices", [])
        if not choices:
            raise LLMResponseFormatError("provider returned no choices")
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
            raise LLMResponseFormatError("provider returned empty content")
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMResponseFormatError(
                "provider returned invalid JSON"
            ) from exc
        if not isinstance(parsed, dict):
            raise LLMResponseFormatError("provider JSON was not an object")
        return parsed

    async def chat_json_strict(
        self, system_prompt: str, user_message: str
    ) -> dict[str, Any]:
        """Return one structured response or a typed, uncensored failure."""
        return await self._execute_strict_once(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ]
        )

    async def chat(self, system_prompt: str, user_message: str) -> str:
        """Send chat prompt and return text response."""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        result = await self._execute_with_retry(messages)
        if result is None:
            return FALLBACK_MESSAGE
        return result

    async def chat_json(
        self, system_prompt: str, user_message: str
    ) -> dict[str, Any]:
        """Send chat prompt requesting JSON output and return parsed dict."""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        result = await self._execute_with_retry(
            messages, response_format={"type": "json_object"}
        )
        if not result:
            return {}
        try:
            parsed = json.loads(result)
            if isinstance(parsed, dict):
                return parsed
            return {}
        except json.JSONDecodeError as exc:
            logger.error("Failed to parse JSON response from LLM: %s", exc)
            return {}

    def chat_sync(self, system_prompt: str, user_message: str) -> str:
        """Synchronous wrapper around chat() safe for Lambda context.

        Creates a fresh event loop instead of using asyncio.run() to avoid
        RuntimeError if an event loop is already running in the Lambda
        execution environment or LiteLLM internals.
        """
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                self.chat(system_prompt, user_message)
            )
        finally:
            loop.close()

    def chat_json_sync(
        self, system_prompt: str, user_message: str
    ) -> dict[str, Any]:
        """Synchronous wrapper around chat_json() safe for Lambda context.

        Creates a fresh event loop instead of using asyncio.run() to avoid
        RuntimeError if an event loop is already running in the Lambda
        execution environment or LiteLLM internals.
        """
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                self.chat_json(system_prompt, user_message)
            )
        finally:
            loop.close()

    def chat_json_strict_sync(
        self, system_prompt: str, user_message: str
    ) -> dict[str, Any]:
        """Synchronous wrapper for one strict structured request."""
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                self.chat_json_strict(system_prompt, user_message)
            )
        finally:
            loop.close()
