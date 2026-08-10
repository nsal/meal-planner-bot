"""LiteLLM wrapper with retry logic and JSON response support."""

import asyncio
import json
import logging
from typing import Any, Optional

import litellm

logger = logging.getLogger(__name__)

FALLBACK_MESSAGE = (
    "Sorry, I am having trouble connecting to the AI service right now. "
    "Please try again in a moment."
)


class LLMClient:
    """Wrapper around LiteLLM for text and structured JSON completion."""

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        api_key: str = "",
        max_retries: int = 3,
        initial_backoff: float = 0.01,
    ) -> None:
        """Initialize LLM client with model settings and retry parameters."""
        self.model = model
        self.api_key = api_key
        self.max_retries = max_retries
        self.initial_backoff = initial_backoff

    async def _execute_with_retry(
        self,
        messages: list[dict[str, str]],
        response_format: Optional[dict[str, str]] = None,
    ) -> Optional[str]:
        """Execute acompletion with exponential backoff retry logic."""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
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
                logger.warning(
                    "LLM request attempt %d failed: %s", attempt + 1, exc
                )
                if attempt < self.max_retries - 1:
                    sleep_time = self.initial_backoff * (2**attempt)
                    await asyncio.sleep(sleep_time)
                else:
                    logger.error("LLM request failed after max retries.")
                    return None
        return None

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
