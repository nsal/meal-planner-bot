"""Tests for deterministic dietary constraint rules."""

from datetime import date, datetime, timedelta, timezone

import pytest

from meal_planner.dietary_rules import (
    CONSTRAINT_PHRASE_REGISTRY,
    ConstraintExpansionResult,
    ConstraintSourceReference,
    PriorityClarification,
    assign_generated_target_weekdays,
    expand_constraint_entry,
    expand_constraint_terms,
    find_constraint_conflicts,
    has_constraint_conflict,
    project_dietary_obligations,
    resolve_priority_rules,
)
from meal_planner.models.schemas import (
    ConstraintEntry,
    DietaryRule,
    MealLogEntry,
    MealType,
    RuleOperator,
    Weekday,
)


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (1, [Weekday.MONDAY]),
        (2, [Weekday.MONDAY, Weekday.THURSDAY]),
        (3, [Weekday.MONDAY, Weekday.WEDNESDAY, Weekday.FRIDAY]),
        (
            4,
            [
                Weekday.MONDAY,
                Weekday.TUESDAY,
                Weekday.THURSDAY,
                Weekday.SATURDAY,
            ],
        ),
        (
            5,
            [
                Weekday.MONDAY,
                Weekday.TUESDAY,
                Weekday.WEDNESDAY,
                Weekday.FRIDAY,
                Weekday.SATURDAY,
            ],
        ),
        (
            6,
            [
                Weekday.MONDAY,
                Weekday.TUESDAY,
                Weekday.WEDNESDAY,
                Weekday.THURSDAY,
                Weekday.FRIDAY,
                Weekday.SATURDAY,
            ],
        ),
        (7, list(Weekday)),
    ],
)
def test_generated_weekdays_are_monday_anchored_and_evenly_spaced(
    count: int, expected: list[Weekday]
) -> None:
    """Every supported weekly count receives a stable generated schedule."""
    rule = DietaryRule(
        id="rule",
        source_text=f"eggs {count} times",
        foods_any_of=["eggs"],
        count=count,
        operator=RuleOperator.EXACTLY,
    )

    scheduled = assign_generated_target_weekdays(rule)

    assert scheduled.target_weekdays == expected
    assert scheduled.weekdays == []


def test_explicit_weekdays_are_preserved_by_generated_scheduler() -> None:
    """Application-generated targets never move a user-selected weekday."""
    rule = DietaryRule(
        id="rule",
        source_text="pancakes on Saturday",
        foods_any_of=["pancakes"],
        meal_type=MealType.BREAKFAST,
        weekdays=[Weekday.SATURDAY],
        count=1,
    )

    scheduled = assign_generated_target_weekdays(rule)

    assert scheduled is rule
    assert scheduled.weekdays == [Weekday.SATURDAY]
    assert scheduled.target_weekdays == []


def test_generated_scheduler_rejects_counts_beyond_weekly_targets() -> None:
    """A generated schedule cannot silently collapse extra meal occurrences."""
    rule = DietaryRule(
        id="rule",
        source_text="eggs eight times",
        foods_any_of=["eggs"],
        count=8,
    )

    with pytest.raises(ValueError, match="counts from 0 to 7"):
        assign_generated_target_weekdays(rule)


def test_expands_normalized_exact_and_dairy_alias_terms() -> None:
    """Normalize exact exclusions and expand the reviewed dairy category."""
    result = expand_constraint_terms(["  Peanuts ", "DAIRY"])

    assert result == ConstraintExpansionResult(
        terms=("peanut", "milk", "cheese", "butter", "cream", "whey", "casein"),
        unknown_terms=(),
    )


def test_constraint_matching_uses_whole_words_not_substrings() -> None:
    """A forbidden food must not match a larger unrelated word."""
    constraint = ConstraintEntry(
        id="c1",
        source_text="peanuts",
        forbidden_terms=["peanuts"],
    )

    expanded = expand_constraint_entry(constraint)

    assert expanded.matches("Peanut butter")
    assert expanded.matches("peanut-flavoured")
    assert not expanded.matches("peanutson")


def test_unknown_or_unmatchable_constraint_fails_closed() -> None:
    """Unrepresentable input is unsafe instead of being silently ignored."""
    result = expand_constraint_terms(["---"])

    assert not result.is_safe
    assert result.terms == ()
    assert result.unknown_terms == ("---",)


@pytest.mark.parametrize(
    "label",
    ["vegetarian", "halal", "kosher", "low sodium"],
)
def test_unregistered_short_dietary_labels_fail_closed(label: str) -> None:
    """Short dietary labels are not literal foods without registration."""
    result = expand_constraint_terms([label])

    assert not result.is_safe
    assert result.terms == ()
    assert result.unknown_terms == (label,)


def test_registered_literal_foods_normalize_and_match_deterministically() -> (
    None
):
    """Reviewed literal foods retain aliases, deduplication, and boundaries."""
    result = expand_constraint_terms(
        ["Peanuts", "shellfish", "peanut butter", "PEANUT-BUTTER"]
    )

    assert result.is_safe
    assert result.terms == ("peanut", "shellfish", "peanut butter")
    assert result.matches("shellfish tacos")
    assert result.matches("peanut butter toast")
    assert not result.matches("peanutson toast")


@pytest.mark.parametrize(
    ("phrase", "expected_terms"),
    [
        ("allergic to peanuts", ("peanut",)),
        ("gluten-free", ("gluten", "wheat", "barley", "rye")),
        (
            "dairy-free",
            ("milk", "cheese", "butter", "cream", "whey", "casein"),
        ),
    ],
)
def test_semantic_legacy_phrases_expand_to_reviewed_terms(
    phrase: str, expected_terms: tuple[str, ...]
) -> None:
    """Recognized semantic phrases resolve through the canonical registry."""
    result = expand_constraint_terms([phrase])

    assert result.is_safe
    assert result.terms == expected_terms
    assert result.unknown_terms == ()


@pytest.mark.parametrize(
    "term",
    ["gluten", "wheat", "barley", "rye"],
)
def test_gluten_free_matches_direct_and_derived_evidence(term: str) -> None:
    """Every reviewed gluten-free evidence term blocks a candidate."""
    result = expand_constraint_terms(["gluten-free"])

    assert result.is_safe
    assert result.matches(term)


@pytest.mark.parametrize(
    "animal_derived_food",
    ["cheese", "butter", "shellfish", "gelatin", "honey"],
)
def test_vegan_is_uninterpretable_for_all_unreviewed_evidence(
    animal_derived_food: str,
) -> None:
    """Broad vegan wording cannot claim a partial safe denylist."""
    result = expand_constraint_terms(["vegan"])

    assert not result.is_safe
    assert result.terms == ()
    assert result.unknown_terms == ("vegan",)
    assert not result.matches(animal_derived_food)


def test_retained_semantic_registry_is_exhaustive_and_matchable() -> None:
    """Every retained phrase has a non-empty, directly matchable expansion."""
    assert CONSTRAINT_PHRASE_REGISTRY
    for phrase, terms in CONSTRAINT_PHRASE_REGISTRY.items():
        assert phrase.strip()
        assert terms
        assert all(term.strip() for term in terms)
        assert len(set(terms)) == len(terms)

        result = expand_constraint_terms([phrase])

        assert result.is_safe
        assert result.unknown_terms == ()
        assert result.terms == terms
        assert all(result.matches(term) for term in terms)


def test_unknown_semantic_phrase_is_not_treated_as_a_literal_term() -> None:
    """Unknown prose fails closed instead of becoming a raw match target."""
    result = expand_constraint_terms(["I react badly to mystery foods"])

    assert not result.is_safe
    assert result.terms == ()
    assert result.unknown_terms == ("I react badly to mystery foods",)


def test_conflict_requires_all_food_alternatives_to_be_forbidden() -> None:
    """A safe alternative keeps an otherwise overlapping preference usable."""
    constraint = ConstraintEntry(
        id="c1",
        source_text="peanuts",
        forbidden_terms=["peanuts"],
    )
    safe_alternative = DietaryRule(
        id="r1",
        source_text="peanuts or chickpeas",
        foods_any_of=["peanuts", "chickpeas"],
        operator=RuleOperator.AT_LEAST,
        count=1,
    )
    forbidden_only = safe_alternative.model_copy(
        update={"id": "r2", "foods_any_of": ["peanuts"]}
    )

    assert not has_constraint_conflict(safe_alternative, [constraint])
    assert has_constraint_conflict(forbidden_only, [constraint])


def test_conflicts_apply_to_every_meal_and_weekday_scope() -> None:
    """An independent constraint cannot be bypassed by narrowing scope."""
    constraint = ConstraintEntry(
        id="c1",
        source_text="eggs",
        forbidden_terms=["eggs"],
    )
    monday_breakfast = DietaryRule(
        id="r1",
        source_text="eggs on Monday breakfast",
        foods_any_of=["eggs"],
        meal_type=MealType.BREAKFAST,
        weekdays=[Weekday.MONDAY],
        operator=RuleOperator.EXACTLY,
        count=1,
    )
    tuesday_lunch = monday_breakfast.model_copy(
        update={
            "id": "r2",
            "meal_type": MealType.LUNCH,
            "weekdays": [Weekday.TUESDAY],
        }
    )

    assert has_constraint_conflict(monday_breakfast, [constraint])
    assert has_constraint_conflict(tuesday_lunch, [constraint])


def test_zero_rule_does_not_conflict_with_an_exclusion_constraint() -> None:
    """An explicit zero request agrees with the independent constraint."""
    constraint = ConstraintEntry(
        id="c1",
        source_text="eggs",
        forbidden_terms=["eggs"],
    )
    rule = DietaryRule(
        id="r1",
        source_text="no eggs this week",
        foods_any_of=["eggs"],
        operator=RuleOperator.EXACTLY,
        count=0,
    )

    assert not has_constraint_conflict(rule, [constraint])


def test_conflict_results_are_stable_and_contain_only_bounded_references() -> (
    None
):
    """Conflict metadata identifies rules without exposing source wording."""
    constraint = ConstraintEntry(
        id="constraint-1",
        source_text="do not use peanuts",
        forbidden_terms=["peanuts"],
    )
    preference = DietaryRule(
        id="preference-1",
        source_text="peanut butter twice",
        foods_any_of=["peanut butter"],
        count=2,
    )

    conflicts = find_constraint_conflicts([preference], [constraint])

    assert len(conflicts) == 1
    assert conflicts[0].constraint_id == "constraint-1"
    assert conflicts[0].preference_id == "preference-1"
    assert conflicts[0].conflicting_terms == ("peanut",)
    assert conflicts[0].source_references == (
        ConstraintSourceReference(kind="constraint", rule_id="constraint-1"),
        ConstraintSourceReference(kind="preference", rule_id="preference-1"),
    )
    assert "do not use peanuts" not in repr(conflicts[0])
    assert "peanut butter twice" not in repr(conflicts[0])


def _rule(
    rule_id: str,
    food: str,
    *,
    operator: RuleOperator = RuleOperator.EXACTLY,
    count: int = 1,
    weekdays: list[Weekday] | None = None,
    meal_type: MealType | None = MealType.BREAKFAST,
) -> DietaryRule:
    """Build a concise rule for priority-resolution tests."""
    return DietaryRule(
        id=rule_id,
        source_text=f"{food} {count}",
        foods_any_of=[food],
        meal_type=meal_type,
        weekdays=weekdays or [],
        operator=operator,
        count=count,
    )


@pytest.mark.parametrize(
    ("stored", "current", "expected"),
    [
        (
            _rule("stored-eggs", "eggs", count=3),
            _rule("current-eggs", "eggs", count=2),
            ("current-eggs",),
        ),
        (
            _rule("stored-eggs", "eggs", count=3),
            _rule(
                "current-eggs",
                "eggs",
                operator=RuleOperator.AT_MOST,
                count=2,
                meal_type=None,
            ),
            ("stored-eggs",),
        ),
        (
            _rule(
                "stored-eggs",
                "eggs",
                operator=RuleOperator.AT_MOST,
                count=1,
            ),
            _rule(
                "current-eggs",
                "eggs",
                operator=RuleOperator.AT_LEAST,
                count=3,
            ),
            ("current-eggs",),
        ),
        (
            _rule("stored-eggs", "eggs", count=2),
            _rule("current-eggs", "eggs", count=0),
            ("current-eggs",),
        ),
    ],
)
def test_priority_resolution_handles_precedence_matrix(
    stored: DietaryRule,
    current: DietaryRule,
    expected: tuple[str, ...],
) -> None:
    """Current rules replace, cap, raise, or zero lower obligations."""
    result = resolve_priority_rules([stored], [current])

    assert result.clarification is None
    assert tuple(rule.id for rule in result.effective_rules) == expected


def test_maximum_caps_exact_rule_on_preferred_stored_days() -> None:
    """A broad maximum preserves two of three preferred egg days."""
    stored = _rule(
        "stored-eggs",
        "eggs",
        count=3,
        weekdays=[Weekday.MONDAY, Weekday.WEDNESDAY, Weekday.FRIDAY],
        meal_type=None,
    )
    current = _rule(
        "current-eggs",
        "eggs",
        operator=RuleOperator.AT_MOST,
        count=2,
        meal_type=None,
    )

    result = resolve_priority_rules([stored], [current])

    assert result.effective_rules == (
        stored.model_copy(
            update={
                "operator": RuleOperator.EXACTLY,
                "count": 2,
                "weekdays": [Weekday.MONDAY, Weekday.WEDNESDAY],
            }
        ),
    )


def test_partial_weekday_overlap_preserves_lower_priority_fragment() -> None:
    """A current Wednesday rule leaves Monday and Friday intact."""
    stored = _rule(
        "stored-eggs",
        "eggs",
        count=3,
        weekdays=[Weekday.MONDAY, Weekday.WEDNESDAY, Weekday.FRIDAY],
        meal_type=None,
    )
    current = _rule(
        "current-eggs",
        "eggs",
        count=1,
        weekdays=[Weekday.WEDNESDAY],
    )

    result = resolve_priority_rules([stored], [current])

    assert tuple(rule.id for rule in result.effective_rules) == (
        "current-eggs",
        "stored-eggs-remaining",
    )
    assert result.effective_rules[1].count == 2
    assert result.effective_rules[1].weekdays == [
        Weekday.MONDAY,
        Weekday.FRIDAY,
    ]


def test_partial_food_overlap_preserves_unrelated_lower_alternative() -> None:
    """A current egg rule does not erase a stored tofu alternative."""
    stored = DietaryRule(
        id="stored-breakfast",
        source_text="eggs or tofu twice",
        foods_any_of=["eggs", "tofu"],
        meal_type=MealType.BREAKFAST,
        operator=RuleOperator.EXACTLY,
        count=2,
    )
    current = _rule("current-eggs", "eggs", count=1)

    result = resolve_priority_rules([stored], [current])

    assert result.effective_rules == (
        current,
        stored.model_copy(
            update={
                "id": "stored-breakfast-remaining",
                "foods_any_of": ["tofu"],
                "count": 1,
            }
        ),
    )


def test_scoped_maximum_remains_when_lower_rule_has_outside_days() -> None:
    """A Wednesday maximum still constrains a lower Monday-Friday rule."""
    stored = _rule(
        "stored-eggs",
        "eggs",
        count=4,
        weekdays=[
            Weekday.MONDAY,
            Weekday.TUESDAY,
            Weekday.WEDNESDAY,
            Weekday.THURSDAY,
            Weekday.FRIDAY,
        ],
        meal_type=None,
    )
    current = _rule(
        "current-wednesday-max",
        "eggs",
        operator=RuleOperator.AT_MOST,
        count=1,
        weekdays=[Weekday.WEDNESDAY],
    )

    result = resolve_priority_rules([stored], [current])

    assert result.effective_rules == (current, stored)


def test_maximum_caps_lower_minimum_to_a_bounded_exact_request() -> None:
    """A maximum converts an incompatible lower minimum to its cap."""
    stored = _rule(
        "stored-eggs",
        "eggs",
        operator=RuleOperator.AT_LEAST,
        count=3,
    )
    current = _rule(
        "current-eggs-max",
        "eggs",
        operator=RuleOperator.AT_MOST,
        count=2,
        meal_type=None,
    )

    result = resolve_priority_rules([stored], [current])

    assert result.effective_rules == (
        stored.model_copy(
            update={
                "operator": RuleOperator.EXACTLY,
                "count": 2,
            }
        ),
    )


def test_same_tier_minimum_and_maximum_contradiction_is_typed() -> None:
    """A same-tier incompatible bound returns clarification metadata."""
    minimum = _rule(
        "stored-minimum",
        "eggs",
        operator=RuleOperator.AT_LEAST,
        count=2,
    )
    maximum = _rule(
        "stored-maximum",
        "eggs",
        operator=RuleOperator.AT_MOST,
        count=1,
    )

    result = resolve_priority_rules([minimum, maximum], [])

    assert result.clarification == PriorityClarification(
        code="same_tier_contradiction",
        rule_ids=("stored-maximum", "stored-minimum"),
    )


def test_unrelated_rules_and_constraints_are_preserved_outside_resolution() -> (
    None
):
    """Only overlapping preference tiers are replaceable."""
    stored = _rule("stored-eggs", "eggs", count=3)
    unrelated = _rule("stored-salmon", "salmon", count=2)
    current = _rule("current-eggs", "eggs", count=1)
    constraint = ConstraintEntry(
        id="constraint-eggs",
        source_text="no eggs",
        forbidden_terms=["eggs"],
    )

    result = resolve_priority_rules(
        [stored, unrelated], [current], constraints=[constraint]
    )

    assert tuple(rule.id for rule in result.effective_rules) == (
        "current-eggs",
        "stored-salmon",
    )
    assert result.constraint_rules == (constraint,)


def test_same_tier_contradiction_returns_typed_clarification() -> None:
    """Contradictory rules in one tier never choose an arbitrary winner."""
    first = _rule("stored-one", "eggs", count=1)
    second = _rule("stored-zero", "eggs", count=0)

    result = resolve_priority_rules([first, second], [])

    assert isinstance(result.clarification, PriorityClarification)
    assert result.clarification.code == "same_tier_contradiction"
    assert result.clarification.rule_ids == ("stored-one", "stored-zero")
    assert result.effective_rules == ()


def test_resolution_is_stable_for_retries_and_rule_serialization() -> None:
    """Repeated resolution has stable IDs, order, and JSON rule payloads."""
    stored = [
        _rule("stored-salmon", "salmon", count=1),
        _rule("stored-eggs", "eggs", count=3),
    ]
    current = [_rule("current-eggs", "eggs", count=2)]

    first = resolve_priority_rules(stored, current)
    second = resolve_priority_rules(list(reversed(stored)), current)

    first_payload = [
        rule.model_dump(mode="json") for rule in first.effective_rules
    ]
    second_payload = [
        rule.model_dump(mode="json") for rule in second.effective_rules
    ]
    assert first_payload == second_payload
    assert tuple(rule.id for rule in first.effective_rules) == (
        "current-eggs",
        "stored-salmon",
    )
    assert [
        DietaryRule.model_validate(payload).model_dump(mode="json")
        for payload in first_payload
    ] == first_payload


def _logged_meal(
    day: date,
    meal_type: MealType,
    description: str,
    hour: int = 12,
) -> MealLogEntry:
    return MealLogEntry(
        date=day,
        meal_type=meal_type,
        description=description,
        created_at=datetime(
            day.year, day.month, day.day, hour, tzinfo=timezone.utc
        ),
    )


@pytest.mark.parametrize("start_weekday", range(1, 8))
@pytest.mark.parametrize("horizon_days", range(1, 8))
def test_projection_covers_every_start_weekday_and_horizon(
    start_weekday: int, horizon_days: int
) -> None:
    """Every bounded horizon produces only dates in its requested range."""
    start = date(2026, 8, 3) + timedelta(days=start_weekday - 1)
    end = start + timedelta(days=horizon_days - 1)
    rule = DietaryRule(
        id="eggs",
        source_text="eggs weekly",
        foods_any_of=["eggs"],
        meal_type=MealType.BREAKFAST,
        count=3,
        operator=RuleOperator.EXACTLY,
        target_weekdays=[Weekday.MONDAY, Weekday.WEDNESDAY, Weekday.FRIDAY],
    )

    obligations = project_dietary_obligations(
        [rule], submitted_meals=[], start_date=start, end_date=end
    )

    assert all(
        obligation.horizon_start >= start
        and obligation.horizon_end <= end
        and all(start <= day <= end for day in obligation.eligible_dates)
        for obligation in obligations
    )
    assert all(
        obligation.iso_week
        == f"{day.isocalendar().year:04d}-W{day.isocalendar().week:02d}"
        for obligation in obligations
        for day in obligation.eligible_dates
    )


def test_projection_counts_distinct_evidence_and_alternatives() -> None:
    """Only one normalized meal per slot satisfies a weekly food quota."""
    rule = DietaryRule(
        id="fish",
        source_text="fish weekly",
        foods_any_of=["fish", "tofu"],
        meal_type=MealType.DINNER,
        count=2,
        operator=RuleOperator.AT_LEAST,
        target_weekdays=[Weekday.MONDAY, Weekday.WEDNESDAY],
    )
    submitted = [
        _logged_meal(date(2026, 8, 3), MealType.DINNER, "Tofu stir fry"),
        _logged_meal(date(2026, 8, 3), MealType.DINNER, "Tofu stir fry", 13),
    ]

    obligations = project_dietary_obligations(
        [rule],
        submitted_meals=submitted,
        start_date=date(2026, 8, 5),
        end_date=date(2026, 8, 5),
    )

    assert len(obligations) == 1
    assert obligations[0].count == 1
    assert len(obligations[0].evidence_ids) == 1


def test_projection_ignores_future_and_wrong_scope_submissions() -> None:
    """Evidence is limited to prior dates and the rule's meal scope."""
    rule = DietaryRule(
        id="eggs",
        source_text="eggs for breakfast",
        foods_any_of=["eggs"],
        meal_type=MealType.BREAKFAST,
        count=2,
        target_weekdays=[Weekday.MONDAY, Weekday.WEDNESDAY],
    )
    submitted = [
        _logged_meal(date(2026, 8, 3), MealType.LUNCH, "Eggs"),
        _logged_meal(date(2026, 8, 4), MealType.BREAKFAST, "Eggs"),
        _logged_meal(date(2026, 8, 6), MealType.BREAKFAST, "Eggs"),
    ]

    obligations = project_dietary_obligations(
        [rule],
        submitted_meals=submitted,
        start_date=date(2026, 8, 5),
        end_date=date(2026, 8, 5),
    )

    assert obligations[0].count == 1
    assert len(obligations[0].evidence_ids) == 1


def test_projection_supports_four_unscoped_meals_on_one_day() -> None:
    """Unscoped rules retain the reviewed four-meal daily capacity."""
    rule = DietaryRule(
        id="beans",
        source_text="beans four times",
        foods_any_of=["beans"],
        count=4,
        operator=RuleOperator.EXACTLY,
        weekdays=[Weekday.WEDNESDAY],
        schedule_kind="explicit",
    )
    obligations = project_dietary_obligations(
        [rule],
        submitted_meals=[],
        start_date=date(2026, 8, 5),
        end_date=date(2026, 8, 5),
    )

    assert obligations[0].count == 4
    assert obligations[0].eligible_dates == [date(2026, 8, 5)]


@pytest.mark.parametrize(
    ("operator", "submitted_count", "expected_count"),
    [
        (RuleOperator.EXACTLY, 0, 1),
        (RuleOperator.EXACTLY, 1, 0),
        (RuleOperator.EXACTLY, 2, 0),
        (RuleOperator.AT_LEAST, 0, 1),
        (RuleOperator.AT_LEAST, 1, 0),
        (RuleOperator.AT_MOST, 0, 1),
        (RuleOperator.AT_MOST, 1, 0),
    ],
)
def test_projection_applies_count_operators_and_caps_short_horizons(
    operator: RuleOperator, submitted_count: int, expected_count: int
) -> None:
    """Short horizons defer excess due work instead of becoming infeasible."""
    rule = DietaryRule(
        id="eggs",
        source_text="eggs",
        foods_any_of=["eggs"],
        meal_type=MealType.BREAKFAST,
        count=1,
        operator=operator,
        target_weekdays=[Weekday.MONDAY],
    )
    submitted = [
        _logged_meal(
            date(2026, 8, 3) + timedelta(days=index),
            MealType.BREAKFAST,
            "Eggs",
        )
        for index in range(submitted_count)
    ]
    obligations = project_dietary_obligations(
        [rule],
        submitted_meals=submitted,
        start_date=date(2026, 8, 5),
        end_date=date(2026, 8, 5),
    )
    assert sum(obligation.count for obligation in obligations) == expected_count


def test_projection_preserves_explicit_days_and_carries_targets() -> None:
    """Explicit Saturday stays Saturday; generated overdue slots catch up."""
    explicit = DietaryRule(
        id="pancake",
        source_text="pancake Saturday",
        foods_any_of=["pancake"],
        meal_type=MealType.BREAKFAST,
        weekdays=[Weekday.SATURDAY],
        count=1,
        schedule_kind="explicit",
    )
    generated = DietaryRule(
        id="egg",
        source_text="egg twice",
        foods_any_of=["egg"],
        meal_type=MealType.BREAKFAST,
        count=2,
        target_weekdays=[Weekday.MONDAY, Weekday.WEDNESDAY],
    )

    explicit_result = project_dietary_obligations(
        [explicit],
        submitted_meals=[],
        start_date=date(2026, 8, 5),
        end_date=date(2026, 8, 7),
    )
    generated_result = project_dietary_obligations(
        [generated],
        submitted_meals=[],
        start_date=date(2026, 8, 5),
        end_date=date(2026, 8, 7),
    )

    assert explicit_result == ()
    assert generated_result[0].eligible_dates == [
        date(2026, 8, 5),
        date(2026, 8, 6),
    ]


def test_projection_crosses_iso_sunday_without_mixing_week_evidence() -> None:
    """A Sunday-to-Monday horizon yields independent weekly obligations."""
    rule = DietaryRule(
        id="eggs",
        source_text="eggs weekly",
        foods_any_of=["eggs"],
        meal_type=MealType.BREAKFAST,
        count=1,
        target_weekdays=[Weekday.MONDAY],
    )
    obligations = project_dietary_obligations(
        [rule],
        submitted_meals=[
            _logged_meal(date(2026, 8, 3), MealType.BREAKFAST, "Eggs")
        ],
        start_date=date(2026, 8, 9),
        end_date=date(2026, 8, 10),
    )

    assert [obligation.iso_week for obligation in obligations] == ["2026-W33"]
    assert [obligation.count for obligation in obligations] == [1]


def test_projection_rejects_invalid_horizon_and_contradictory_rules() -> None:
    """Only malformed or contradictory rule input is an infeasibility error."""
    rule = DietaryRule(
        id="eggs",
        source_text="eggs",
        foods_any_of=["eggs"],
        count=1,
    )
    with pytest.raises(ValueError, match="at most 7 days"):
        project_dietary_obligations(
            [rule],
            submitted_meals=[],
            start_date=date(2026, 8, 3),
            end_date=date(2026, 8, 10),
        )

    contradictory = rule.model_copy(update={"id": "eggs-zero", "count": 0})
    with pytest.raises(ValueError, match="contradictory"):
        project_dietary_obligations(
            [rule, contradictory],
            submitted_meals=[],
            start_date=date(2026, 8, 3),
            end_date=date(2026, 8, 3),
        )
