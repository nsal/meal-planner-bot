"""Parsers for extracting structured models from LLM responses."""

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional, Union

from pydantic import ValidationError

from meal_planner.models.schemas import (
    MAX_PLAN_REQUIREMENTS,
    ConversationIntent,
    GrocerySection,
    LLMResponseMetadata,
    MealType,
    PreferenceRequirement,
    WeeklyPlan,
)
from meal_planner.normalization import normalize_food

logger = logging.getLogger(__name__)

PreferenceInterpretation = tuple[list[PreferenceRequirement], str | None]
PreferenceSignature = tuple[MealType | None, frozenset[tuple[str, ...]]]

MAX_CLARIFICATION_LENGTH = 500
MAX_PROVIDER_CLARIFICATION_LENGTH = 500
MAX_UNPARSED_CLAUSES = 8
MAX_UNPARSED_CLAUSE_LENGTH = 160

_REPHRASE_CLARIFICATION = (
    "I couldn't safely interpret that preference. Please rephrase it with "
    "specific foods and counts."
)
_TOO_MANY_REQUIREMENTS_CLARIFICATION = (
    "That preference contains too many separate rules. Please combine or "
    "prioritize the most important rules, then try again."
)
_SAFE_LOCATION_PART = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class SafeValidationIssue:
    """Bounded validation metadata safe for repair transport and logs."""

    code: str
    location: str


@dataclass(frozen=True, slots=True)
class PlanResponseFeedback:
    """A classified plan-response failure without provider content."""

    category: str
    issues: tuple[SafeValidationIssue, ...]

    def render(self) -> str:
        """Render bounded machine-readable repair feedback."""
        rendered = "; ".join(
            f"code={issue.code} location={issue.location}"
            for issue in self.issues
        )
        return rendered[:800]


def _safe_schema_location(location: object) -> str:
    """Convert a Pydantic location into a bounded schema-only path."""
    if not isinstance(location, tuple):
        return "$"
    result = ""
    for part in location[:8]:
        if isinstance(part, int) and 0 <= part <= 28:
            result += f"[{part}]"
        elif isinstance(part, str) and _SAFE_LOCATION_PART.fullmatch(part):
            result += part if not result else f".{part}"
    return result[:120] or "$"


def _safe_validation_code(error_type: object) -> str:
    """Map provider-independent Pydantic types to bounded codes."""
    if not isinstance(error_type, str):
        return "schema_validation"
    if error_type.startswith("missing"):
        return "missing"
    if "type" in error_type:
        return "type"
    if error_type in {"literal_error", "enum", "value_error"}:
        return "value"
    return "schema_validation"


def _schema_feedback(error: ValidationError) -> PlanResponseFeedback:
    """Build safe structural metadata from a Pydantic validation error."""
    issues = tuple(
        SafeValidationIssue(
            code=_safe_validation_code(item.get("type")),
            location=_safe_schema_location(item.get("loc")),
        )
        for item in error.errors(include_url=False)[:6]
    )
    return PlanResponseFeedback(
        category="structural",
        issues=issues or (SafeValidationIssue("schema_validation", "$"),),
    )


def _preference_signature(
    requirement: PreferenceRequirement,
) -> PreferenceSignature:
    """Return the shared normalized signature for one requirement."""
    return (
        requirement.meal_type,
        frozenset(normalize_food(food) for food in requirement.foods_any_of),
    )


def _render_bounded_clarification(text: str) -> str:
    """Render one safe clarification without truncating its meaning."""
    rendered = text.strip()
    if not rendered or len(rendered) > MAX_CLARIFICATION_LENGTH:
        return _REPHRASE_CLARIFICATION
    return rendered


def _clarification_response(text: str) -> PreferenceInterpretation:
    """Return a parser clarification through the final bounded renderer."""
    return [], _render_bounded_clarification(text)


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
        except json.JSONDecodeError:
            logger.warning("LLM response contained malformed JSON")
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
    except ValidationError:
        logger.warning("LLM response metadata failed schema validation")
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
            del exc
            logger.warning("Plan response failed schema validation")
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
        del exc
        logger.warning("Plan response failed schema validation")
        return None


def parse_preference_interpretation(
    raw_text_or_dict: Union[str, dict[str, Any]],
) -> PreferenceInterpretation:
    """Parse an LLM preference interpretation into rules or clarification."""
    data: Any = raw_text_or_dict
    if isinstance(raw_text_or_dict, str):
        if not raw_text_or_dict.strip():
            return _clarification_response(
                "Please provide a measurable meal preference."
            )
        _, json_dict = _extract_json_block(raw_text_or_dict)
        if json_dict is not None:
            data = json_dict
        else:
            try:
                data = json.loads(raw_text_or_dict.strip())
            except json.JSONDecodeError:
                return _clarification_response(
                    "I could not parse the preference interpretation."
                )

    if not isinstance(data, dict):
        return _clarification_response(
            "The preference interpretation must be a JSON object."
        )

    required_keys = {"requirements", "clarification", "unparsed_text"}
    missing_keys = required_keys.difference(data)
    if missing_keys:
        missing = ", ".join(sorted(missing_keys))
        return _clarification_response(
            f"The interpretation omitted required fields: {missing}."
        )

    raw_requirements = data["requirements"]
    if not isinstance(raw_requirements, list):
        return _clarification_response(
            "The interpretation requirements must be a list."
        )
    if len(raw_requirements) > MAX_PLAN_REQUIREMENTS:
        return _clarification_response(_TOO_MANY_REQUIREMENTS_CLARIFICATION)

    raw_clarification = data["clarification"]
    if raw_clarification is not None and not isinstance(raw_clarification, str):
        return _clarification_response(
            "The interpretation clarification must be text or null."
        )
    if (
        isinstance(raw_clarification, str)
        and len(raw_clarification) > MAX_PROVIDER_CLARIFICATION_LENGTH
    ):
        return _clarification_response(_REPHRASE_CLARIFICATION)
    clarification = raw_clarification.strip() if raw_clarification else None
    if raw_clarification is not None and not clarification:
        return _clarification_response(
            "The interpretation clarification must not be blank."
        )

    raw_unparsed = data["unparsed_text"]
    if isinstance(raw_unparsed, str):
        unparsed_text = [raw_unparsed.strip()] if raw_unparsed.strip() else []
    elif isinstance(raw_unparsed, list) and all(
        isinstance(item, str) and item.strip() for item in raw_unparsed
    ):
        unparsed_text = [item.strip() for item in raw_unparsed]
    else:
        return _clarification_response(
            "The interpretation unparsed_text must contain text."
        )

    if len(unparsed_text) > MAX_UNPARSED_CLAUSES or any(
        len(clause) > MAX_UNPARSED_CLAUSE_LENGTH for clause in unparsed_text
    ):
        return _clarification_response(_REPHRASE_CLARIFICATION)

    if clarification or unparsed_text:
        if clarification:
            return _clarification_response(clarification)
        clauses = "; ".join(unparsed_text)
        return _clarification_response(
            f"Please clarify these preference clauses: {clauses}"
        )

    if not raw_requirements:
        return _clarification_response(
            "Please provide a measurable meal preference."
        )

    requirements: list[PreferenceRequirement] = []
    seen_ids: set[str] = set()
    for raw_requirement in raw_requirements:
        if not isinstance(raw_requirement, dict):
            return _clarification_response(
                "Each preference requirement must be an object."
            )
        try:
            requirement = PreferenceRequirement.model_validate(raw_requirement)
        except ValidationError as exc:
            del exc
            logger.warning("Preference requirement failed schema validation")
            return _clarification_response(
                "One or more preference requirements are malformed."
            )
        if requirement.id in seen_ids:
            return _clarification_response(
                f"The interpretation contains duplicate requirement id: "
                f"{requirement.id}.",
            )
        seen_ids.add(requirement.id)
        requirements.append(requirement)

    counts_by_signature: dict[PreferenceSignature, int] = {}
    for requirement in requirements:
        signature = _preference_signature(requirement)
        previous_count = counts_by_signature.get(signature)
        if (
            previous_count is not None
            and previous_count != requirement.exact_count
        ):
            return _clarification_response(
                "Some preference requirements conflict. Please clarify the "
                "desired count for the same foods and meal scope.",
            )
        counts_by_signature[signature] = requirement.exact_count

    return requirements, None


def parse_plan_response_with_feedback(
    raw_text_or_dict: Union[str, dict[str, Any]],
) -> tuple[Optional[WeeklyPlan], str | None]:
    """Parse a plan and return bounded feedback for one repair."""
    plan, feedback = parse_plan_response_with_metadata(raw_text_or_dict)
    return plan, feedback.render() if feedback is not None else None


def parse_plan_response_with_metadata(
    raw_text_or_dict: Union[str, dict[str, Any]],
) -> tuple[Optional[WeeklyPlan], PlanResponseFeedback | None]:
    """Parse a plan while retaining safe failure codes and locations."""
    data: Any = raw_text_or_dict
    if isinstance(raw_text_or_dict, str):
        if not raw_text_or_dict.strip():
            return None, PlanResponseFeedback(
                category="structural",
                issues=(SafeValidationIssue("empty_response", "$"),),
            )
        _, json_dict = _extract_json_block(raw_text_or_dict)
        if json_dict is not None:
            data = json_dict
        else:
            try:
                data = json.loads(raw_text_or_dict.strip())
            except json.JSONDecodeError:
                return None, PlanResponseFeedback(
                    category="structural",
                    issues=(SafeValidationIssue("invalid_json", "$"),),
                )
    if not isinstance(data, dict):
        return None, PlanResponseFeedback(
            category="structural",
            issues=(SafeValidationIssue("not_object", "$"),),
        )
    try:
        return WeeklyPlan.model_validate(data), None
    except ValidationError as exc:
        return None, _schema_feedback(exc)


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
                logger.warning("Grocery response contained malformed JSON")
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
            del exc
            logger.warning("Skipping grocery section with invalid schema")

    return sections
