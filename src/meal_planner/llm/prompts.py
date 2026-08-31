"""Plain-text prompt construction for conversational meal drafts."""

from collections.abc import Sequence
from datetime import date, datetime, timedelta

from meal_planner.models.schemas import (
    FamilyMember,
    MealLogEntry,
    UserProfile,
)

MAX_MEAL_HISTORY_RECORDS = 50
MAX_MEAL_HISTORY_CHARACTERS = 12_000
_MEAL_TYPE_ORDER: dict[str, int] = {
    "breakfast": 0,
    "lunch": 1,
    "dinner": 2,
    "snack": 3,
}


def _escape_text(value: str) -> str:
    """Keep bounded user text inside its prompt section."""
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("---", "‑‑‑").replace("===", "＝＝＝")


def _render_value(value: str) -> str:
    """Indent user text so its lines remain content, not prompt headings."""
    return "\n".join(f"  {line}" for line in _escape_text(value).split("\n"))


def _render_member(member: FamilyMember) -> str:
    """Render one member and only the nutrition targets they supplied."""
    targets = [f"{member.calorie_target} kcal/day"]
    if member.protein_target is not None:
        targets.append(f"protein target: {member.protein_target} g/day")
    if member.fibre_target is not None:
        targets.append(f"fibre target: {member.fibre_target} g/day")
    return f"- {_escape_text(member.name)} — {', '.join(targets)}"


def _render_dietary_entries(entries: Sequence[str], empty: str) -> str:
    """Render raw profile wording without interpreting it."""
    if not entries:
        return empty
    lines: list[str] = []
    for entry in entries:
        lines.extend(f"- {line}" for line in _escape_text(entry).split("\n"))
    return "\n".join(lines)


def _history_sort_key(
    meal: MealLogEntry,
) -> tuple[date, datetime, int, str]:
    """Return a stable chronological key for one history record."""
    return (
        meal.date,
        meal.created_at,
        _MEAL_TYPE_ORDER[meal.meal_type.value],
        meal.description,
    )


def _render_history_records(meals: Sequence[MealLogEntry]) -> str:
    """Render records in chronological date and meal-type order."""
    grouped: dict[date, list[MealLogEntry]] = {}
    for meal in meals:
        grouped.setdefault(meal.date, []).append(meal)

    lines: list[str] = []
    for meal_date in sorted(grouped):
        lines.append(f"{meal_date.isoformat()}:")
        for meal in sorted(
            grouped[meal_date],
            key=lambda item: _MEAL_TYPE_ORDER[item.meal_type.value],
        ):
            description = _escape_text(meal.description)
            lines.extend(
                f"- {meal.meal_type.value}: {line}"
                for line in description.split("\n")
            )
    return "\n".join(lines)


def _history_truncation_marker(omitted_count: int) -> str:
    """Return the stable marker used when older records are omitted."""
    return f"Older meal history omitted: {omitted_count} records."


def _render_history(
    meals: Sequence[MealLogEntry], context_date: date | None
) -> str:
    """Render bounded submitted meal history in the active window."""
    selected = list(meals)
    if context_date is not None:
        start = context_date - timedelta(days=20)
        selected = [
            meal for meal in selected if start <= meal.date <= context_date
        ]
    if not selected:
        return "No submitted meals in the supplied 21-day window."

    indexed_meals = list(enumerate(selected))
    newest_first = sorted(
        indexed_meals,
        key=lambda indexed: _history_sort_key(indexed[1]),
        reverse=True,
    )
    candidate_count = min(len(newest_first), MAX_MEAL_HISTORY_RECORDS)
    for retained_count in range(candidate_count, -1, -1):
        retained_indexes = {index for index, _ in newest_first[:retained_count]}
        retained = [
            meal for index, meal in indexed_meals if index in retained_indexes
        ]
        omitted_count = len(newest_first) - retained_count
        rendered = _render_history_records(
            sorted(retained, key=_history_sort_key)
        )
        if omitted_count:
            marker = _history_truncation_marker(omitted_count)
            rendered = f"{rendered}\n{marker}" if rendered else marker
        if len(rendered) <= MAX_MEAL_HISTORY_CHARACTERS:
            return rendered

    # The marker is deliberately short enough to fit the configured limit.
    return _history_truncation_marker(len(newest_first))


def build_plan_chat_prompt(
    profile: UserProfile | None = None,
    meal_history: Sequence[MealLogEntry] | None = None,
    initial_request: str = "",
    latest_response: str | None = None,
    pending_message: str = "",
    context_date: date | None = None,
) -> str:
    """Build one bounded, plain-text prompt for a conversational draft."""
    if profile is None:
        household = "No household profile provided."
        constraints: Sequence[str] = []
        preferences: Sequence[str] = []
    else:
        members = "\n".join(
            _render_member(member) for member in profile.family_members
        )
        household = (
            f"Household name: {_escape_text(profile.name)}\n"
            f"Household size: {profile.people_count}\n"
            "Members and daily targets:\n"
            f"{members or '- No members listed.'}"
        )
        constraints = profile.dietary_constraints
        preferences = profile.dietary_preferences

    if context_date is None:
        history_heading = (
            "Submitted meal history (previous inclusive 21-day window):"
        )
    else:
        start = context_date - timedelta(days=20)
        history_heading = (
            "Submitted meal history (inclusive 21-day window: "
            f"{start.isoformat()} through {context_date.isoformat()}):"
        )

    previous = (
        _render_value(latest_response)
        if latest_response is not None
        else "  No previous draft response; this is the first request."
    )
    history = _render_history(meal_history or [], context_date)
    constraints_text = _render_dietary_entries(
        constraints, "No dietary constraints provided."
    )
    preferences_text = _render_dietary_entries(
        preferences, "No dietary preferences provided."
    )
    return (
        "You are a thoughtful family meal-planning assistant.\n\n"
        "--- BEGIN DRAFT INSTRUCTIONS ---\n"
        "Draft status: draft. Every menu is an editable draft for the "
        "household to review, not a promise or final prescription.\n"
        "Give explicit constraints and the current instruction greater "
        "importance than patterns in meal history.\n"
        "Treat submitted meal history as preference evidence, not an "
        "obligation.\n"
        "Use plain-text headings and bullets suitable for Telegram. Keep "
        "meal details short and include approximate calorie estimates where "
        "relevant. Do not use Markdown tables.\n"
        "If essential information is missing, ask at most one focused "
        "clarification question. Do not claim medical or nutritional "
        "certainty or that targets were met.\n"
        "--- END DRAFT INSTRUCTIONS ---\n\n"
        "--- BEGIN HOUSEHOLD MEMBERS AND TARGETS ---\n"
        f"{household}\n"
        "--- END HOUSEHOLD MEMBERS AND TARGETS ---\n\n"
        "--- BEGIN RAW DIETARY CONSTRAINTS ---\n"
        f"{constraints_text}\n"
        "--- END RAW DIETARY CONSTRAINTS ---\n\n"
        "--- BEGIN RAW DIETARY PREFERENCES ---\n"
        f"{preferences_text}\n"
        "--- END RAW DIETARY PREFERENCES ---\n\n"
        "--- BEGIN SUBMITTED MEALS ---\n"
        f"{history_heading}\n{history}\n"
        "--- END SUBMITTED MEALS ---\n\n"
        "--- BEGIN PLANNING CONVERSATION ---\n"
        f"Original request:\n{_render_value(initial_request)}\n"
        f"Previous draft response:\n{previous}\n"
        f"Current instruction:\n{_render_value(pending_message)}\n"
        "--- END PLANNING CONVERSATION ---\n"
    )
