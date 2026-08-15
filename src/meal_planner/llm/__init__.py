"""LLM module for LiteLLM integration, prompts, and response parsing."""

from meal_planner.llm.client import (
    LLMClient,
    LLMFailure,
    LLMPermanentError,
    LLMResponseFormatError,
    LLMTimeoutError,
    LLMTransientError,
)
from meal_planner.llm.parser import (
    parse_conversational_response,
    parse_grocery_response,
    parse_plan_response,
    parse_plan_response_with_feedback,
)
from meal_planner.llm.prompts import (
    build_conversational_prompt,
    build_grocery_prompt,
    build_plan_prompt,
    build_plan_revision_prompt,
)

__all__ = [
    "LLMClient",
    "LLMFailure",
    "LLMPermanentError",
    "LLMResponseFormatError",
    "LLMTimeoutError",
    "LLMTransientError",
    "build_conversational_prompt",
    "build_grocery_prompt",
    "build_plan_prompt",
    "build_plan_revision_prompt",
    "parse_conversational_response",
    "parse_grocery_response",
    "parse_plan_response",
    "parse_plan_response_with_feedback",
]
