"""Parsers for extracting structured models from LLM responses."""

import json
import logging
import re
from typing import Any, Optional, Union

from pydantic import ValidationError

from meal_planner.models.schemas import (
    ConversationIntent,
    GrocerySection,
    LLMResponseMetadata,
    WeeklyPlan,
)

logger = logging.getLogger(__name__)


def _extract_json_block(text: str) -> tuple[str, Optional[dict[str, Any]]]:
    """Extract natural language text and parsed JSON dict from LLM response."""
    pattern = r"```(?:json)?\s*(\{.*?\})\s*```"
    match = re.search(pattern, text, re.DOTALL)
    if match:
        json_str = match.group(1)
        reply_text = text[: match.start()].strip()
        try:
            parsed = json.loads(json_str)
            if isinstance(parsed, dict):
                return reply_text, parsed
        except json.JSONDecodeError as exc:
            logger.warning("Failed to decode JSON block in response: %s", exc)
            return text.strip(), None

    text_stripped = text.strip()
    if text_stripped.startswith("{") and text_stripped.endswith("}"):
        try:
            parsed = json.loads(text_stripped)
            if isinstance(parsed, dict):
                return "", parsed
        except json.JSONDecodeError:
            pass

    return text_stripped, None


def parse_conversational_response(
    raw_text: str,
) -> tuple[str, LLMResponseMetadata]:
    """Extract text reply and LLMResponseMetadata from raw LLM output."""
    if not raw_text or not raw_text.strip():
        return "", LLMResponseMetadata(
            intent=ConversationIntent.CHITCHAT, entities={}
        )

    reply_text, json_dict = _extract_json_block(raw_text)
    if not json_dict:
        return (
            reply_text or raw_text.strip(),
            LLMResponseMetadata(
                intent=ConversationIntent.CHITCHAT, entities={}
            ),
        )

    try:
        metadata = LLMResponseMetadata.model_validate(json_dict)
        return reply_text, metadata
    except ValidationError as exc:
        logger.warning("Invalid LLM response metadata schema: %s", exc)
        return (
            reply_text or raw_text.strip(),
            LLMResponseMetadata(
                intent=ConversationIntent.CHITCHAT, entities={}
            ),
        )


def parse_plan_response(
    raw_text_or_dict: Union[str, dict[str, Any]],
) -> Optional[WeeklyPlan]:
    """Parse raw text or dict into a WeeklyPlan model."""
    if isinstance(raw_text_or_dict, dict):
        try:
            return WeeklyPlan.model_validate(raw_text_or_dict)
        except ValidationError as exc:
            logger.warning("Failed to validate plan dict: %s", exc)
            return None

    if not raw_text_or_dict or not raw_text_or_dict.strip():
        return None

    _, json_dict = _extract_json_block(raw_text_or_dict)
    if not json_dict:
        try:
            json_dict = json.loads(raw_text_or_dict.strip())
        except json.JSONDecodeError:
            logger.warning("Could not find or parse JSON in plan response")
            return None

    try:
        return WeeklyPlan.model_validate(json_dict)
    except ValidationError as exc:
        logger.warning("Failed to validate WeeklyPlan schema: %s", exc)
        return None


def parse_plan_response_with_feedback(
    raw_text_or_dict: Union[str, dict[str, Any]],
) -> tuple[Optional[WeeklyPlan], str | None]:
    """Parse a plan and return bounded Pydantic feedback for one repair."""
    data: Any = raw_text_or_dict
    if isinstance(raw_text_or_dict, str):
        if not raw_text_or_dict.strip():
            return None, "response was empty"
        _, json_dict = _extract_json_block(raw_text_or_dict)
        if json_dict is not None:
            data = json_dict
        else:
            try:
                data = json.loads(raw_text_or_dict.strip())
            except json.JSONDecodeError:
                return None, "response was not valid JSON"
    if not isinstance(data, dict):
        return None, "response must be a JSON object"
    try:
        return WeeklyPlan.model_validate(data), None
    except ValidationError as exc:
        errors = exc.errors(include_url=False)
        details = [
            f"{error.get('loc', ())}: {error.get('msg', 'invalid value')}"
            for error in errors[:6]
        ]
        feedback = "; ".join(details)
        return None, feedback[:800]


def parse_grocery_response(
    raw_text_or_dict: Union[str, dict[str, Any]],
) -> list[GrocerySection]:
    """Parse raw text or dict into a list of GrocerySection models."""
    data: Any = None
    if isinstance(raw_text_or_dict, dict):
        data = raw_text_or_dict
    elif isinstance(raw_text_or_dict, str) and raw_text_or_dict.strip():
        _, json_dict = _extract_json_block(raw_text_or_dict)
        if json_dict:
            data = json_dict
        else:
            try:
                data = json.loads(raw_text_or_dict.strip())
            except json.JSONDecodeError:
                logger.warning("Could not parse grocery JSON response")
                return []

    if isinstance(data, dict):
        raw_sections = data.get("sections", [])
    elif isinstance(data, list):
        raw_sections = data
    else:
        return []

    sections: list[GrocerySection] = []
    for item in raw_sections:
        try:
            sections.append(GrocerySection.model_validate(item))
        except ValidationError as exc:
            logger.warning("Skipping invalid grocery section: %s", exc)

    return sections
