"""Tests for deterministic dietary constraint rules."""

import pytest

from meal_planner.dietary_rules import (
    CONSTRAINT_PHRASE_REGISTRY,
    ConstraintExpansionResult,
    ConstraintSourceReference,
    PriorityClarification,
    expand_constraint_entry,
    expand_constraint_terms,
    find_constraint_conflicts,
    has_constraint_conflict,
    resolve_priority_rules,
)
from meal_planner.models.schemas import (
    ConstraintEntry,
    DietaryRule,
    MealType,
    RuleOperator,
    Weekday,
)


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
