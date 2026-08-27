"""Parsers for extracting structured models from LLM responses."""

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal, Optional, Union

from pydantic import ValidationError

from meal_planner.dietary_rules import canonicalize_interpretation_rules
from meal_planner.models.schemas import (
    MAX_PLAN_REQUIREMENTS,
    BatchLedgerEntry,
    BatchRule,
    ConstraintEntry,
    ConversationIntent,
    DietaryRule,
    GrocerySection,
    LLMResponseMetadata,
    MealType,
    RuleOperator,
    RuleStrength,
    Weekday,
    WeeklyPlan,
)
from meal_planner.normalization import normalize_food
from meal_planner.preferences import validate_batch_links

logger = logging.getLogger(__name__)

InterpretationMode = Literal[
    "constraint", "stored_preference", "current_plan_preference"
]
PreferenceValidationReason = Literal["food", "count", "scope", "schema"]

InterpretationRule = DietaryRule | BatchRule | ConstraintEntry
# Keep the additive batch-rule result compatible with the legacy no-mode
# caller while validating every runtime value as ``InterpretationRule``.
PreferenceInterpretation = tuple[list[Any], str | None]
PreferenceSignature = tuple[
    MealType | None, frozenset[tuple[str, ...]], frozenset[Weekday]
]

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
_NUMBER_WORD = (
    r"(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|"
    r"eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|"
    r"eighty|ninety|hundred)"
)
_TENS_NUMBER_WORD = r"(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)"
_UNIT_NUMBER_WORD = r"(?:one|two|three|four|five|six|seven|eight|nine)"
_NUMBER_TOKEN = (
    rf"(?:\d+|{_TENS_NUMBER_WORD}-{_UNIT_NUMBER_WORD}|{_NUMBER_WORD})"
)
_COMPARATIVE_LIMITING_PATTERN = re.compile(
    rf"\b(?:less[\s-]+than|fewer[\s-]+than|under)\s+"
    rf"{_NUMBER_TOKEN}(?![\w-])"
)
_FREQUENCY_PERIOD = r"(?:day|week|meal)s?"
_COMPACT_FREQUENCY_PATTERN = re.compile(
    rf"(?<![\w-]){_NUMBER_TOKEN}(?:"
    rf"\s*x\s*(?:(?:a|each|per)\s+{_FREQUENCY_PERIOD}|"
    rf"\s*/\s*{_FREQUENCY_PERIOD})"
    rf"|\s*/\s*{_FREQUENCY_PERIOD}"
    rf"|\s+(?:a|each|per)\s+{_FREQUENCY_PERIOD}"
    rf")(?![\w-])"
)
_UNSUPPORTED_SUBJECTIVE_PATTERN = re.compile(
    r"\b(?:healthy|fun|cozy|interesting|creative|exciting)\b",
    re.IGNORECASE,
)
_REQUIREMENT_KEYS = {
    "id",
    "source_text",
    "foods_any_of",
    "meal_type",
    "weekdays",
    "target_weekdays",
    "generated_weekdays",
    "cadence",
    "period",
    "schedule_kind",
    "schedule_source",
    "operator",
    "count",
    "exact_count",
    "strength",
}
_BATCH_RULE_KEYS = {
    "id",
    "source_text",
    "foods_any_of",
    "preparation_meal_types",
    "eligible_preparation_meal_types",
    "reuse_meal_types",
    "eligible_reuse_meal_types",
    "total_yield",
    "total_portions",
    "yield",
}
_EXCLUSION_KEYS = {"id", "source_text", "forbidden_terms", "uninterpretable"}


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


def _preference_validation_reason(
    error: ValidationError,
) -> PreferenceValidationReason:
    """Map validation locations to bounded, application-owned reason codes."""
    for item in error.errors(include_url=False):
        location = item.get("loc")
        if not isinstance(location, tuple) or not location:
            continue
        field = location[0]
        if field in {"foods_any_of", "forbidden_terms"}:
            return "food"
        if field in {"count", "exact_count", "operator"}:
            return "count"
        if field in {"meal_type", "weekdays"}:
            return "scope"
    return "schema"


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
    requirement: DietaryRule,
) -> PreferenceSignature:
    """Return the shared normalized signature for one requirement."""
    return (
        requirement.meal_type,
        frozenset(normalize_food(food) for food in requirement.foods_any_of),
        frozenset(requirement.weekdays),
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
    *,
    available_batches: list[BatchLedgerEntry] | None = None,
) -> Optional[WeeklyPlan]:
    """Parse raw text or dict into a WeeklyPlan model."""
    plan, _feedback = parse_plan_response_with_metadata(
        raw_text_or_dict, available_batches=available_batches
    )
    return plan


def _normalise_weekdays(value: object) -> object:
    """Normalize provider weekday names to the shared ISO enum."""
    if not isinstance(value, list):
        return value
    names = {
        "monday": Weekday.MONDAY,
        "tuesday": Weekday.TUESDAY,
        "wednesday": Weekday.WEDNESDAY,
        "thursday": Weekday.THURSDAY,
        "friday": Weekday.FRIDAY,
        "saturday": Weekday.SATURDAY,
        "sunday": Weekday.SUNDAY,
    }
    return [
        names[item.casefold()]
        if isinstance(item, str) and item.casefold() in names
        else item
        for item in value
    ]


def _is_best_effort_wording(source_text: str) -> bool:
    """Return whether wording explicitly permits omitting a rule."""
    return bool(
        re.search(
            r"\b(if convenient|if possible|when practical|optionally|"
            r"when convenient)\b",
            source_text.casefold(),
        )
    )


def _has_comparative_limiting_wording(source_text: str) -> bool:
    """Return whether wording contains a bounded comparative limit."""
    return bool(_COMPARATIVE_LIMITING_PATTERN.search(source_text))


def _has_explicit_frequency_wording(source_text: str) -> bool:
    """Return whether wording contains a bounded frequency expression."""
    unit = rf"(?:times?|{_FREQUENCY_PERIOD})"
    expanded_frequency = re.search(
        r"\b(?:daily|weekly|biweekly|monthly|yearly|"
        rf"every[\s-]+{_FREQUENCY_PERIOD}|"
        rf"each[\s-]+{_FREQUENCY_PERIOD}|"
        rf"{_NUMBER_TOKEN}[\s-]+{unit}|"
        rf"{_NUMBER_TOKEN}[\s-]+per[\s-]+{_FREQUENCY_PERIOD})\b",
        source_text,
    )
    return bool(
        expanded_frequency or _COMPACT_FREQUENCY_PATTERN.search(source_text)
    )


def _is_positive_request(source_text: str) -> bool:
    """Return whether wording is an unqualified positive food request."""
    folded_text = source_text.casefold().replace("’", "'")
    if re.search(
        r"\b(no|avoid|without|exclude|excluding|omit|limit|free of|"
        r"free from|not|never|do not|don't)\b",
        folded_text,
    ):
        return False
    if re.search(
        r"\b(exactly|at\s+least|at\s+most|once|twice|thrice|"
        r"times?|frequency|count)\b",
        folded_text,
    ):
        return False
    if _has_comparative_limiting_wording(folded_text):
        return False
    if _has_explicit_frequency_wording(folded_text):
        return False
    return True


def _has_valid_food_candidates(payload: dict[str, Any]) -> bool:
    """Return whether a payload contains at least one matchable food."""
    foods = payload.get("foods_any_of")
    return (
        isinstance(foods, list)
        and bool(foods)
        and all(
            isinstance(food, str) and bool(normalize_food(food))
            for food in foods
        )
    )


def _has_strict_wording(source_text: str) -> bool:
    """Return whether wording explicitly requires a preference."""
    return bool(
        re.search(
            r"\b(strict(?:ly)?|must|required|non[- ]negotiable)\b",
            source_text.casefold(),
        )
    )


def _has_ambiguous_operator_wording(source_text: str) -> bool:
    """Return whether wording names more than one count operator."""
    operators = re.findall(
        r"\b(exactly|at\s+least|at\s+most)\b", source_text.casefold()
    )
    return len(operators) > 1


def _has_unsupported_subjective_wording(source_text: str) -> bool:
    """Return whether a clause needs clarification rather than a guess."""
    return bool(_UNSUPPORTED_SUBJECTIVE_PATTERN.search(source_text))


def _prepare_rule_payload(raw_rule: dict[str, Any]) -> dict[str, Any]:
    """Adapt legacy and wording-derived fields to :class:`DietaryRule`."""
    payload = dict(raw_rule)
    payload["weekdays"] = _normalise_weekdays(payload.get("weekdays", []))
    source_text = payload.get("source_text")
    if not isinstance(source_text, str):
        return payload
    explicit_count = "count" in payload or "exact_count" in payload
    explicit_operator = "operator" in payload or "exact_count" in payload
    if "count" not in payload and "exact_count" in payload:
        payload["count"] = payload.pop("exact_count")
        payload.setdefault("operator", RuleOperator.EXACTLY.value)
    if (
        not explicit_count
        and not explicit_operator
        and _has_valid_food_candidates(payload)
        and _is_positive_request(source_text)
    ):
        payload["count"] = 1
        payload["operator"] = RuleOperator.AT_LEAST.value
    if "count" not in payload:
        return payload
    if _is_best_effort_wording(source_text):
        payload["strength"] = RuleStrength.BEST_EFFORT.value
    else:
        payload["strength"] = RuleStrength.STRICT.value
    return payload


def _scope_overlaps(left: DietaryRule, right: DietaryRule) -> bool:
    """Return whether two rules can constrain the same meals."""
    if (
        left.meal_type is not None
        and right.meal_type is not None
        and left.meal_type is not right.meal_type
    ):
        return False
    left_days = set(left.weekdays) or set(Weekday)
    right_days = set(right.weekdays) or set(Weekday)
    return bool(left_days & right_days)


def _rules_conflict(left: DietaryRule, right: DietaryRule) -> bool:
    """Detect contradictory same-tier rules without selecting a winner."""
    left_foods = {normalize_food(food) for food in left.foods_any_of}
    right_foods = {normalize_food(food) for food in right.foods_any_of}
    if not left_foods & right_foods or not _scope_overlaps(left, right):
        return False

    def bounds(rule: DietaryRule) -> tuple[int, int | None]:
        if rule.operator is RuleOperator.EXACTLY:
            return rule.count, rule.count
        if rule.operator is RuleOperator.AT_LEAST:
            return rule.count, None
        return 0, rule.count

    left_min, left_max = bounds(left)
    right_min, right_max = bounds(right)
    lower = max(left_min, right_min)
    upper_values = [
        value for value in (left_max, right_max) if value is not None
    ]
    return bool(upper_values and lower > min(upper_values))


def _parse_interpretation_data(
    raw_text_or_dict: Union[str, dict[str, Any]],
) -> Any:
    """Decode only a JSON object with no surrounding provider prose."""
    if isinstance(raw_text_or_dict, dict):
        return raw_text_or_dict
    text = raw_text_or_dict.strip()
    if not text:
        return None

    if text.startswith("```") and text.endswith("```"):
        fenced = re.fullmatch(
            r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL | re.IGNORECASE
        )
        if fenced is None:
            return None
        text = fenced.group(1).strip()
    elif not (text.startswith("{") and text.endswith("}")):
        return None

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_preference_interpretation(
    raw_text_or_dict: Union[str, dict[str, Any]],
    *,
    mode: InterpretationMode | None = None,
) -> PreferenceInterpretation:
    """Parse one interpretation mode into shared rules or clarification."""
    data = _parse_interpretation_data(raw_text_or_dict)
    if data is None:
        return _clarification_response(
            "I could not parse the preference interpretation."
        )

    if not isinstance(data, dict):
        return _clarification_response(
            "The preference interpretation must be a JSON object."
        )

    allowed_keys = {
        "mode",
        "requirements",
        "batch_rules",
        "exclusions",
        "clarification",
        "unparsed_text",
    }
    if set(data).difference(allowed_keys):
        return _clarification_response(
            "The preference interpretation has unsupported fields."
        )

    declared_mode = data.get("mode")
    valid_modes = {
        "constraint",
        "stored_preference",
        "current_plan_preference",
    }
    if declared_mode is not None and declared_mode not in valid_modes:
        return _clarification_response(
            "The interpretation mode is unsupported; please retry clearly."
        )
    selected_mode: InterpretationMode = (
        mode
        if mode is not None
        else (
            declared_mode
            if declared_mode
            in {"constraint", "stored_preference", "current_plan_preference"}
            else "current_plan_preference"
        )
    )
    if mode is not None and declared_mode is not None and declared_mode != mode:
        return _clarification_response(
            "The interpretation mode does not match the requested operation."
        )
    required_keys = {"requirements", "clarification", "unparsed_text"}
    missing_keys = required_keys.difference(data)
    if selected_mode == "constraint" and "exclusions" not in data:
        missing_keys.add("exclusions")
    if missing_keys:
        missing = ", ".join(sorted(missing_keys))
        return _clarification_response(
            f"The interpretation omitted required fields: {missing}."
        )

    raw_requirements = data["requirements"]
    raw_batch_rules = data.get("batch_rules", [])
    raw_exclusions = data.get("exclusions", [])
    if not isinstance(raw_requirements, list):
        return _clarification_response(
            "The interpretation requirements must be a list."
        )
    if not isinstance(raw_batch_rules, list):
        return _clarification_response(
            "The interpretation batch_rules must be a list."
        )
    if not isinstance(raw_exclusions, list):
        return _clarification_response(
            "The interpretation exclusions must be a list."
        )
    if (
        len(raw_requirements) + len(raw_batch_rules) + len(raw_exclusions)
        > MAX_PLAN_REQUIREMENTS
    ):
        return _clarification_response(_TOO_MANY_REQUIREMENTS_CLARIFICATION)
    if selected_mode == "constraint" and raw_requirements:
        return _clarification_response(
            "Constraint mode must return exclusions, not preference rules."
        )
    if selected_mode == "constraint" and raw_batch_rules:
        return _clarification_response(
            "Constraint mode must not return batch rules."
        )
    if selected_mode != "constraint" and raw_exclusions:
        return _clarification_response(
            "Preference modes must return rules, not exclusions."
        )

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

    if selected_mode == "constraint":
        exclusions: list[InterpretationRule] = []
        seen_exclusion_ids: set[str] = set()
        for raw_exclusion in raw_exclusions:
            if not isinstance(raw_exclusion, dict):
                return _clarification_response(
                    "Each exclusion must be an object."
                )
            try:
                exclusion = ConstraintEntry.model_validate(raw_exclusion)
            except ValidationError as exc:
                reason_code = _preference_validation_reason(exc)
                logger.warning(
                    "Constraint exclusion failed schema validation "
                    f"(interpretation_mode={selected_mode} "
                    f"reason_code={reason_code})",
                    extra={
                        "interpretation_mode": selected_mode,
                        "reason_code": reason_code,
                    },
                )
                return _clarification_response(
                    "One or more constraint exclusions are malformed."
                )
            if set(raw_exclusion).difference(_EXCLUSION_KEYS):
                logger.warning(
                    "Constraint exclusion failed schema validation "
                    f"(interpretation_mode={selected_mode} "
                    "reason_code=schema)",
                    extra={
                        "interpretation_mode": selected_mode,
                        "reason_code": "schema",
                    },
                )
                return _clarification_response(
                    "One or more constraint exclusions are malformed."
                )
            if exclusion.id in seen_exclusion_ids:
                return _clarification_response(
                    f"The interpretation contains duplicate exclusion id: "
                    f"{exclusion.id}."
                )
            seen_exclusion_ids.add(exclusion.id)
            exclusions.append(exclusion)
        if not exclusions:
            return _clarification_response(
                "Please provide a specific food to exclude."
            )
        return exclusions, None

    if not raw_requirements and not raw_batch_rules:
        return _clarification_response(
            "Please provide a measurable meal preference."
        )

    requirements: list[DietaryRule] = []
    seen_ids: set[str] = set()
    for raw_requirement in raw_requirements:
        if not isinstance(raw_requirement, dict):
            return _clarification_response(
                "Each preference requirement must be an object."
            )
        source_text = raw_requirement.get("source_text")
        if isinstance(source_text, str):
            if _has_unsupported_subjective_wording(source_text):
                return _clarification_response(
                    "Some preference wording is subjective; please name "
                    "specific foods, counts, and meal scopes."
                )
            if _is_best_effort_wording(source_text) and _has_strict_wording(
                source_text
            ):
                return _clarification_response(
                    "The preference mixes strict and flexible wording. "
                    "Please choose one."
                )
            if _has_ambiguous_operator_wording(source_text):
                return _clarification_response(
                    "The preference names multiple count operators. Please "
                    "clarify the desired operator."
                )
        try:
            requirement = DietaryRule.model_validate(
                _prepare_rule_payload(raw_requirement)
            )
        except (TypeError, ValueError, ValidationError) as exc:
            reason_code = (
                _preference_validation_reason(exc)
                if isinstance(exc, ValidationError)
                else "schema"
            )
            logger.warning(
                "Preference requirement failed schema validation "
                f"(interpretation_mode={selected_mode} "
                f"reason_code={reason_code})",
                extra={
                    "interpretation_mode": selected_mode,
                    "reason_code": reason_code,
                },
            )
            return _clarification_response(
                "One or more preference requirements are malformed."
            )
        if set(raw_requirement).difference(_REQUIREMENT_KEYS):
            logger.warning(
                "Preference requirement failed schema validation "
                f"(interpretation_mode={selected_mode} reason_code=schema)",
                extra={
                    "interpretation_mode": selected_mode,
                    "reason_code": "schema",
                },
            )
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

    for index, requirement in enumerate(requirements):
        if not isinstance(requirement, DietaryRule):
            continue
        if any(
            isinstance(other, DietaryRule)
            and _rules_conflict(requirement, other)
            for other in requirements[index + 1 :]
        ):
            return _clarification_response(
                "Some preference requirements conflict. Please clarify the "
                "desired counts for the same foods and scope."
            )

    batch_rules: list[BatchRule] = []
    seen_batch_ids: set[str] = set()
    for raw_batch_rule in raw_batch_rules:
        if not isinstance(raw_batch_rule, dict):
            return _clarification_response("Each batch rule must be an object.")
        try:
            batch_rule = BatchRule.model_validate(raw_batch_rule)
        except ValidationError:
            logger.warning(
                "Batch rule failed schema validation "
                f"(interpretation_mode={selected_mode} reason_code=schema)",
                extra={
                    "interpretation_mode": selected_mode,
                    "reason_code": "schema",
                },
            )
            return _clarification_response(
                "One or more batch rules are malformed."
            )
        if set(raw_batch_rule).difference(_BATCH_RULE_KEYS):
            logger.warning(
                "Batch rule failed schema validation "
                f"(interpretation_mode={selected_mode} reason_code=schema)",
                extra={
                    "interpretation_mode": selected_mode,
                    "reason_code": "schema",
                },
            )
            return _clarification_response(
                "One or more batch rules are malformed."
            )
        if batch_rule.id in seen_batch_ids:
            return _clarification_response(
                "The interpretation contains duplicate batch rule IDs."
            )
        seen_batch_ids.add(batch_rule.id)
        batch_rules.append(batch_rule)

    all_rules: list[DietaryRule | BatchRule] = requirements + batch_rules
    return canonicalize_interpretation_rules(
        all_rules, mode=selected_mode
    ), None


def parse_plan_response_with_feedback(
    raw_text_or_dict: Union[str, dict[str, Any]],
    *,
    available_batches: list[BatchLedgerEntry] | None = None,
) -> tuple[Optional[WeeklyPlan], str | None]:
    """Parse a plan and return bounded feedback for one repair."""
    plan, feedback = parse_plan_response_with_metadata(
        raw_text_or_dict, available_batches=available_batches
    )
    return plan, feedback.render() if feedback is not None else None


def parse_plan_response_with_metadata(
    raw_text_or_dict: Union[str, dict[str, Any]],
    *,
    available_batches: list[BatchLedgerEntry] | None = None,
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
        plan = WeeklyPlan.model_validate(data)
    except ValidationError as exc:
        return None, _schema_feedback(exc)
    batch_validation = validate_batch_links(
        plan, available_batches=available_batches or []
    )
    if not batch_validation.is_valid:
        first_issue = batch_validation.issues[0]
        location = "days"
        if first_issue.day is not None:
            location = f"days[{first_issue.day - 1}].meals"
        return None, PlanResponseFeedback(
            category="compliance",
            issues=(SafeValidationIssue(first_issue.code, location),),
        )
    return plan, None


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
