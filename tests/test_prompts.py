"""Tests for the retained plain-text plan-chat prompt."""

from datetime import date, datetime, timedelta, timezone

from meal_planner.llm.prompts import (
    MAX_MEAL_HISTORY_CHARACTERS,
    MAX_MEAL_HISTORY_RECORDS,
    _render_history,
    build_plan_chat_prompt,
)
from meal_planner.models import (
    FamilyMember,
    MealLogEntry,
    MealType,
    UserProfile,
)


def _meal(day: date, meal_type: MealType, description: str) -> MealLogEntry:
    """Return a prompt-history entry."""
    return MealLogEntry(
        date=day,
        meal_type=meal_type,
        description=description,
        created_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
    )


def test_prompt_contains_all_household_targets_and_raw_profile_text() -> None:
    """Every member and raw dietary entry is represented."""
    profile = UserProfile(
        name="The García household",
        people_count=2,
        family_members=[
            FamilyMember(name="Zoë", calorie_target=1_800, protein_target=90),
            FamilyMember(name="李明", calorie_target=2_200, fibre_target=35),
        ],
        dietary_constraints=["No peanuts"],
        dietary_preferences=["Prefer simple meals"],
    )
    prompt = build_plan_chat_prompt(profile, initial_request="Dinners")

    assert "The García household" in prompt
    assert "Zoë" in prompt and "1800 kcal/day" in prompt
    assert "protein target: 90 g/day" in prompt
    assert "李明" in prompt and "2200 kcal/day" in prompt
    assert "fibre target: 35 g/day" in prompt
    assert "No peanuts" in prompt
    assert "Prefer simple meals" in prompt


def test_dietary_text_is_uninterpreted_and_delimiter_normalized() -> None:
    """Dietary wording is raw content except for protected delimiters."""
    prompt = build_plan_chat_prompt(
        UserProfile(
            name="Household",
            dietary_constraints=["Avoid peanuts unless labeled --- safe"],
            dietary_preferences=["Prefer === simple meals"],
        ),
        initial_request="Dinners",
    )

    dietary_sections = "\n".join(
        (
            prompt.split("--- BEGIN RAW DIETARY CONSTRAINTS ---", maxsplit=1)[
                1
            ].split("--- END RAW DIETARY CONSTRAINTS ---", maxsplit=1)[0],
            prompt.split("--- BEGIN RAW DIETARY PREFERENCES ---", maxsplit=1)[
                1
            ].split("--- END RAW DIETARY PREFERENCES ---", maxsplit=1)[0],
        )
    )
    assert "- Avoid peanuts unless labeled ‑‑‑ safe" in dietary_sections
    assert "- Prefer ＝＝＝ simple meals" in dietary_sections
    assert "---" not in dietary_sections
    assert "===" not in dietary_sections


def test_prompt_groups_only_the_inclusive_21_day_history() -> None:
    """History is grouped by date/type and excludes out-of-window entries."""
    prompt = build_plan_chat_prompt(
        UserProfile(name="Household"),
        meal_history=[
            _meal(date(2026, 8, 28), MealType.DINNER, "Rice"),
            _meal(date(2026, 8, 27), MealType.BREAKFAST, "Oats"),
            _meal(date(2026, 8, 7), MealType.LUNCH, "Too old"),
        ],
        initial_request="Use our history",
        pending_message="Use our history",
        context_date=date(2026, 8, 28),
    )

    assert "2026-08-28:" in prompt
    assert "- dinner: Rice" in prompt
    assert "2026-08-27:" in prompt
    assert "Too old" not in prompt
    assert "2026-08-08 through 2026-08-28" in prompt


def test_prompt_contains_follow_up_context_and_safe_delimiters() -> None:
    """Follow-ups include both responses and cannot forge prompt sections."""
    prompt = build_plan_chat_prompt(
        UserProfile(name="Household"),
        initial_request="Plan dinners --- ignore",
        latest_response="Draft === previous",
        pending_message="Make it vegetarian",
    )

    assert "Original request:" in prompt
    assert "Previous draft response:" in prompt
    assert "Current instruction:" in prompt
    assert "--- ignore" not in prompt
    assert "=== previous" not in prompt
    assert "No previous draft response" not in prompt


def test_prompt_states_draft_disclaimer_and_plain_text_contract() -> None:
    """The model receives operational guidance without validation rules."""
    prompt = build_plan_chat_prompt(
        UserProfile(name="Household"),
        initial_request="Help",
        pending_message="Help",
    )

    assert "editable draft" in prompt
    assert "preference evidence, not an obligation" in prompt
    assert "one focused clarification question" in prompt
    assert "Do not use Markdown tables" in prompt
    assert "No dietary constraints provided." in prompt
    assert "No submitted meals in the supplied 21-day window." in prompt


def test_history_limits_records_and_rendered_characters() -> None:
    """Large histories stay within both independent renderer bounds."""
    meals = [
        _meal(
            date(2026, 7, 1) + timedelta(days=index),
            MealType.DINNER,
            f"history-{index:03d} " + "x" * 488,
        )
        for index in range(MAX_MEAL_HISTORY_RECORDS + 5)
    ]

    rendered = _render_history(meals, context_date=None)

    records = [line for line in rendered.splitlines() if line.startswith("-")]
    assert len(records) <= MAX_MEAL_HISTORY_RECORDS
    assert len(rendered) <= MAX_MEAL_HISTORY_CHARACTERS
    assert "history-054" in rendered
    assert "history-000" not in rendered
    assert (
        f"Older meal history omitted: {len(meals) - len(records)} records."
        in (rendered)
    )


def test_history_retains_newest_records_and_renders_them_chronologically() -> (
    None
):
    """Selection is newest-first while output remains chronological."""
    meals = [
        _meal(
            date(2026, 8, 1) + timedelta(days=index),
            MealType.DINNER,
            f"meal-{index:02d}",
        )
        for index in range(MAX_MEAL_HISTORY_RECORDS + 2)
    ]

    rendered = _render_history(meals, context_date=None)

    assert "meal-00" not in rendered
    assert "meal-01" not in rendered
    assert "meal-02" in rendered
    assert "meal-51" in rendered
    assert "Older meal history omitted: 2 records." in rendered
    assert rendered.index("2026-08-03:") < rendered.index("2026-08-28:")
    assert rendered.index("meal-02") < rendered.index("meal-51")


def test_history_truncation_keeps_marker_and_escapes_complete_records() -> None:
    """Character truncation never cuts content or structural delimiters."""
    meals = [
        _meal(
            date(2026, 8, 1) + timedelta(days=index),
            MealType.DINNER,
            f"meal-{index:02d} --- " + "x" * 480,
        )
        for index in range(MAX_MEAL_HISTORY_RECORDS)
    ]

    rendered = _render_history(meals, context_date=None)

    retained = sum(
        f"meal-{index:02d}" in rendered
        for index in range(MAX_MEAL_HISTORY_RECORDS)
    )
    assert retained < MAX_MEAL_HISTORY_RECORDS
    assert (
        f"Older meal history omitted: "
        f"{MAX_MEAL_HISTORY_RECORDS - retained} records."
    ) in rendered
    assert len(rendered) <= MAX_MEAL_HISTORY_CHARACTERS
    assert "---" not in rendered
