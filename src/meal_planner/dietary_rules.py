"""Deterministic scheduling, expansion, and conflict checks for dietary rules.

This module deliberately contains no language-model or network behavior.  A
constraint is safe to use only when each of its terms can be represented as
normalized whole-word evidence.  Constraints remain independent of meal and
weekday scopes: a preference cannot evade a constraint by narrowing its
scope.
"""

import json
from dataclasses import dataclass
from datetime import date, timedelta
from math import floor
from types import MappingProxyType
from typing import Literal, Mapping, Sequence

from meal_planner.models.schemas import (
    BatchRule,
    ConstraintEntry,
    DietaryObligation,
    DietaryRule,
    MealLogEntry,
    MealType,
    RuleCadence,
    RuleOperator,
    ScheduleKind,
    Weekday,
    application_owned_text_id,
    canonicalize_constraint_entry,
    canonicalize_dietary_rule,
    daily_meal_capacity,
)
from meal_planner.normalization import normalize_food, normalize_match_text

AliasRegistry = Mapping[str, tuple[str, ...]]
ConstraintReferenceKind = Literal["constraint", "preference"]


def assign_generated_target_weekdays(rule: DietaryRule) -> DietaryRule:
    """Assign stable, Monday-anchored targets to an unscoped weekly rule.

    The returned rule preserves explicit weekdays exactly.  Generated targets
    use the first ``count`` evenly distributed positions in a seven-day,
    Monday-anchored calendar.  Profile rules are deliberately copied through
    Pydantic so invalid meal-scope counts remain impossible to schedule.
    """
    if rule.schedule_kind is ScheduleKind.EXPLICIT or rule.weekdays:
        return rule
    if not 0 <= rule.count <= 7:
        raise ValueError(
            "generated target weekday assignment supports counts from 0 to 7"
        )
    if rule.count == 0:
        targets: list[Weekday] = []
    else:
        targets = [
            Weekday(floor(index * 7 / rule.count) + 1)
            for index in range(rule.count)
        ]
    return rule.model_copy(
        update={
            "target_weekdays": targets,
            "schedule_kind": ScheduleKind.GENERATED,
        }
    )


def assign_generated_target_days(rule: DietaryRule) -> DietaryRule:
    """Compatibility name for assigning application-owned target weekdays."""
    return assign_generated_target_weekdays(rule)


def canonicalize_batch_rule(
    rule: BatchRule, *, namespace: str = "profile-batch"
) -> BatchRule:
    """Replace a provider batch ID with a deterministic application ID."""
    payload = {
        "source_text": rule.source_text.casefold().strip(),
        "foods_any_of": sorted(
            " ".join(normalize_food(food)) for food in rule.foods_any_of
        ),
        "preparation_meal_types": sorted(
            meal_type.value for meal_type in rule.preparation_meal_types
        ),
        "reuse_meal_types": sorted(
            meal_type.value for meal_type in rule.reuse_meal_types
        ),
        "total_yield": rule.total_yield,
    }
    return rule.model_copy(
        update={
            "id": application_owned_text_id(
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                namespace=namespace,
            )
        }
    )


def canonicalize_interpretation_rules(
    rules: Sequence[DietaryRule | BatchRule | ConstraintEntry],
    *,
    mode: str,
) -> list[DietaryRule | BatchRule | ConstraintEntry]:
    """Canonicalize all successfully parsed rules without partial writes."""
    if mode == "constraint":
        return [
            canonicalize_constraint_entry(rule, namespace="profile-constraint")
            for rule in rules
            if isinstance(rule, ConstraintEntry)
        ]

    canonical: list[DietaryRule | BatchRule | ConstraintEntry] = []
    for rule in rules:
        if isinstance(rule, DietaryRule):
            scheduled = assign_generated_target_weekdays(rule)
            namespace = (
                "profile-preference"
                if mode == "stored_preference"
                else "current-plan-preference"
            )
            canonical.append(
                canonicalize_dietary_rule(scheduled, namespace=namespace)
            )
        elif isinstance(rule, BatchRule):
            canonical.append(
                canonicalize_batch_rule(
                    rule,
                    namespace=(
                        "profile-batch"
                        if mode == "stored_preference"
                        else "current-plan-batch"
                    ),
                )
            )
    return canonical


@dataclass(frozen=True, slots=True)
class SubmittedMealEvidence:
    """One distinct, persisted meal that matches a dietary rule."""

    evidence_id: str
    date: date
    meal_type: MealType
    description: str
    matched_foods: tuple[str, ...]


def meal_log_evidence_id(entry: MealLogEntry) -> str:
    """Return a stable, bounded ID for one normalized meal-history slot."""
    payload = {
        "date": entry.date.isoformat(),
        "meal_type": entry.meal_type.value,
        "description": normalize_match_text(entry.description),
    }
    return application_owned_text_id(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        namespace="submitted-meal",
    )


def match_submitted_meal(
    entry: MealLogEntry, rule: DietaryRule
) -> SubmittedMealEvidence | None:
    """Match one submitted meal using normalized whole-word alternatives."""
    if rule.meal_type is not None and entry.meal_type is not rule.meal_type:
        return None
    if rule.weekdays and Weekday(entry.date.isoweekday()) not in rule.weekdays:
        return None
    matched_foods = tuple(
        food
        for food in rule.foods_any_of
        if _contains_token_sequence(
            normalize_food(entry.description), normalize_food(food)
        )
    )
    if not matched_foods:
        return None
    return SubmittedMealEvidence(
        evidence_id=meal_log_evidence_id(entry),
        date=entry.date,
        meal_type=entry.meal_type,
        description=entry.description,
        matched_foods=matched_foods,
    )


def obligation_covers_date(
    obligation: DietaryObligation, target_date: date
) -> bool:
    """Return whether an obligation owns one exact ISO-week plan date."""
    iso = target_date.isocalendar()
    target_iso_week = f"{iso.year:04d}-W{iso.week:02d}"
    return (
        obligation.iso_week == target_iso_week
        and obligation.horizon_start <= target_date <= obligation.horizon_end
        and target_date in obligation.eligible_dates
    )


def _projection_segments(
    start_date: date, end_date: date
) -> tuple[tuple[str, date, date], ...]:
    """Split an inclusive horizon into bounded ISO-week segments."""
    if end_date < start_date:
        raise ValueError("end_date must not precede start_date")
    if (end_date - start_date).days + 1 > 7:
        raise ValueError("dietary projection horizons may cover at most 7 days")
    segments: list[tuple[str, date, date]] = []
    current = start_date
    while current <= end_date:
        iso = current.isocalendar()
        week_start = date.fromisocalendar(iso.year, iso.week, 1)
        week_end = week_start + timedelta(days=6)
        segment_end = min(week_end, end_date)
        segments.append(
            (f"{iso.year:04d}-W{iso.week:02d}", current, segment_end)
        )
        current = segment_end + timedelta(days=1)
    return tuple(segments)


def _dates_between(start_date: date, end_date: date) -> tuple[date, ...]:
    """Return every date in an already validated inclusive interval."""
    return tuple(
        start_date + timedelta(days=offset)
        for offset in range((end_date - start_date).days + 1)
    )


def _distinct_rule_evidence(
    rule: DietaryRule,
    submitted_meals: Sequence[MealLogEntry],
    *,
    week_start: date,
    cutoff: date,
) -> tuple[SubmittedMealEvidence, ...]:
    """Match and deduplicate history before a segment's application cutoff."""
    distinct: dict[tuple[date, MealType, str], SubmittedMealEvidence] = {}
    for entry in submitted_meals:
        if not week_start <= entry.date < cutoff:
            continue
        evidence = match_submitted_meal(entry, rule)
        if evidence is None:
            continue
        key = (
            entry.date,
            entry.meal_type,
            normalize_match_text(entry.description),
        )
        distinct.setdefault(key, evidence)
    return tuple(
        sorted(
            distinct.values(),
            key=lambda item: (
                item.date,
                item.meal_type.value,
                item.evidence_id,
            ),
        )
    )


def _scheduled_target_dates(
    rule: DietaryRule, week_start: date
) -> tuple[date, ...]:
    """Resolve one rule's explicit or generated weekdays to dates."""
    if rule.weekdays:
        return tuple(
            week_start + timedelta(days=int(weekday) - 1)
            for weekday in rule.weekdays
        )
    scheduled = rule
    if not rule.target_weekdays:
        scheduled = assign_generated_target_weekdays(rule)
    return tuple(
        week_start + timedelta(days=int(weekday) - 1)
        for weekday in scheduled.target_weekdays
    )


def _eligible_slots(
    rule: DietaryRule,
    start_date: date,
    end_date: date,
) -> tuple[date, ...]:
    """Return bounded, deterministic meal-slot dates for a rule."""
    dates = _dates_between(start_date, end_date)
    if rule.weekdays:
        dates = tuple(
            current
            for current in dates
            if Weekday(current.isoweekday()) in rule.weekdays
        )
    capacity = daily_meal_capacity(rule.meal_type)
    return tuple(current for current in dates for _ in range(capacity))


def _generated_obligation_dates(
    rule: DietaryRule,
    week_start: date,
    segment_start: date,
    segment_end: date,
    due_count: int,
) -> tuple[date, ...]:
    """Place generated overdue slots before in-horizon target slots."""
    targets = _scheduled_target_dates(rule, week_start)
    overdue = [target for target in targets if target < segment_start]
    current_targets = [
        target for target in targets if segment_start <= target <= segment_end
    ]
    open_slots = list(_eligible_slots(rule, segment_start, segment_end))
    selected: list[date] = []
    for target in overdue + current_targets:
        if len(selected) >= due_count:
            break
        try:
            slot_index = open_slots.index(target)
        except ValueError:
            slot_index = 0 if open_slots else -1
        if slot_index < 0:
            break
        selected.append(open_slots.pop(slot_index))
    return tuple(selected)


def _project_rule_segment(
    rule: DietaryRule,
    submitted_meals: Sequence[MealLogEntry],
    *,
    iso_week: str,
    segment_start: date,
    segment_end: date,
) -> DietaryObligation | None:
    """Project one rule into one ISO-week horizon segment."""
    week_start = date.fromisocalendar(int(iso_week[:4]), int(iso_week[6:]), 1)
    evidence = _distinct_rule_evidence(
        rule,
        submitted_meals,
        week_start=week_start,
        cutoff=segment_start,
    )
    slots = _eligible_slots(rule, segment_start, segment_end)
    if not slots:
        return None

    if rule.weekdays:
        due_count = min(rule.count, len(slots))
        selected_dates = tuple(dict.fromkeys(slots))
    else:
        targets = _scheduled_target_dates(rule, week_start)
        due_count = sum(target <= segment_end for target in targets)
        selected_dates = tuple(
            dict.fromkeys(
                _generated_obligation_dates(
                    rule,
                    week_start,
                    segment_start,
                    segment_end,
                    due_count,
                )
            )
        )

    if rule.operator is RuleOperator.AT_MOST:
        required_count = max(rule.count - len(evidence), 0)
        required_count = min(required_count, len(slots))
        selected_dates = tuple(dict.fromkeys(slots))
    else:
        required_count = max(due_count - len(evidence), 0)
        selected_date_count = len(set(selected_dates))
        required_count = min(
            required_count,
            daily_meal_capacity(rule.meal_type) * selected_date_count,
        )

    if rule.operator is not RuleOperator.AT_MOST and required_count == 0:
        return None
    if not selected_dates:
        return None
    return DietaryObligation(
        id=f"{rule.id}-{iso_week}-{segment_start.isoformat()}",
        source_rule_id=rule.id,
        iso_week=iso_week,
        horizon_start=segment_start,
        horizon_end=segment_end,
        eligible_dates=list(selected_dates),
        operator=rule.operator,
        count=required_count,
        foods_any_of=rule.foods_any_of,
        meal_type=rule.meal_type,
        strength=rule.strength,
        evidence_ids=[item.evidence_id for item in evidence[:20]],
    )


def project_dietary_obligations(
    rules: Sequence[DietaryRule],
    submitted_meals: Sequence[MealLogEntry],
    *,
    start_date: date,
    end_date: date,
) -> tuple[DietaryObligation, ...]:
    """Project weekly quotas into deterministic, dated horizon obligations.

    Submitted history is the only evidence consumed here.  Drafts and
    confirmed plans are intentionally not accepted as an input type.  A
    short horizon caps catch-up at its meal-slot capacity; callers can ask
    again later for any remaining weekly quota.
    """
    if len({rule.id for rule in rules}) != len(rules):
        raise ValueError("dietary rule IDs must be unique")
    if any(rule.cadence is not RuleCadence.ISO_WEEK for rule in rules):
        raise ValueError("dietary rules must use ISO-week cadence")
    contradiction = _find_same_tier_contradiction(rules)
    if contradiction is not None:
        raise ValueError(
            "contradictory dietary rules: " + ", ".join(contradiction.rule_ids)
        )
    obligations: list[DietaryObligation] = []
    for iso_week, segment_start, segment_end in _projection_segments(
        start_date, end_date
    ):
        for rule in sorted(rules, key=lambda item: item.id):
            obligation = _project_rule_segment(
                rule,
                submitted_meals,
                iso_week=iso_week,
                segment_start=segment_start,
                segment_end=segment_end,
            )
            if obligation is not None:
                obligations.append(obligation)
    return tuple(obligations)


project_weekly_obligations = project_dietary_obligations
project_weekly_rule_obligations = project_dietary_obligations
match_logged_meal = match_submitted_meal


# Keep this registry intentionally small and application-owned.  Values are
# ordered so expansion and all derived conflict results are reproducible.
CONSTRAINT_ALIAS_REGISTRY: AliasRegistry = MappingProxyType(
    {
        "dairy": (
            "milk",
            "cheese",
            "butter",
            "cream",
            "whey",
            "casein",
        ),
    }
)
# This shorter name is convenient for callers while keeping one immutable
# registry as the source of truth.
CONSTRAINT_ALIASES: AliasRegistry = CONSTRAINT_ALIAS_REGISTRY

# These are the literal foods that the application has reviewed for direct
# whole-word matching.  A closed registry is intentionally safer than
# deciding that an arbitrary short phrase must be a food.
_REVIEWED_LITERAL_FOOD_NAMES = (
    "barley",
    "bean",
    "beef",
    "berry",
    "bread",
    "broccoli",
    "brownie",
    "butter",
    "café",
    "carrot",
    "casein",
    "cheese",
    "chicken",
    "chickpea",
    "cod",
    "corn",
    "cookie",
    "cream",
    "crepe",
    "egg",
    "fish",
    "garlic",
    "gluten",
    "ham",
    "honey",
    "lentil",
    "lobster",
    "lamb",
    "milk",
    "mushroom",
    "oat",
    "onion",
    "pancake",
    "pasta",
    "peanut",
    "peanut butter",
    "pie",
    "pork",
    "potato",
    "red pepper",
    "rice",
    "rye",
    "salmon",
    "sausage",
    "shellfish",
    "shrimp",
    "smoothie",
    "spinach",
    "tofu",
    "tomato",
    "trout",
    "tuna",
    "turkey",
    "wheat",
    "whey",
)
CONSTRAINT_LITERAL_FOOD_REGISTRY = frozenset(
    " ".join(normalize_food(food)) for food in _REVIEWED_LITERAL_FOOD_NAMES
)

# These phrases are common legacy/profile answers rather than literal food
# names.  Keep their meaning application-owned and deliberately bounded.
CONSTRAINT_PHRASE_REGISTRY: AliasRegistry = MappingProxyType(
    {
        "allergic to peanuts": ("peanut",),
        "allergy to peanuts": ("peanut",),
        "peanut allergy": ("peanut",),
        "gluten free": ("gluten", "wheat", "barley", "rye"),
        "dairy free": (
            "milk",
            "cheese",
            "butter",
            "cream",
            "whey",
            "casein",
        ),
    }
)


@dataclass(frozen=True, slots=True)
class ConstraintExpansionResult:
    """Normalized terms produced from one or more constraint terms."""

    terms: tuple[str, ...]
    unknown_terms: tuple[str, ...]

    @property
    def is_safe(self) -> bool:
        """Return whether all inputs have a deterministic representation."""
        return not self.unknown_terms and bool(self.terms)

    def matches(self, text: str) -> bool:
        """Return whether any expanded term occurs as whole normalized words."""
        text_tokens = normalize_food(text)
        return any(
            _contains_token_sequence(text_tokens, normalize_food(term))
            for term in self.terms
        )


@dataclass(frozen=True, slots=True)
class ConstraintSourceReference:
    """Bounded reference to a rule used in conflict metadata."""

    kind: ConstraintReferenceKind
    rule_id: str


@dataclass(frozen=True, slots=True)
class ConstraintConflict:
    """A preference obligation that cannot coexist with a constraint.

    Only stable IDs and normalized terms are retained.  Raw source wording is
    intentionally absent so this value is safe to put in logs or repair
    metadata.  ``fail_closed`` identifies an unrepresentable constraint for
    which a safe conflict could not be proven.
    """

    constraint_id: str
    preference_id: str
    conflicting_terms: tuple[str, ...]
    source_references: tuple[
        ConstraintSourceReference, ConstraintSourceReference
    ]
    fail_closed: bool = False


@dataclass(frozen=True, slots=True)
class ConstraintSafetyResult:
    """Explicit safety outcome for a collection of active constraints."""

    safe: bool
    unknown_constraint_ids: tuple[str, ...]

    @property
    def is_safe(self) -> bool:
        """Return whether generation may proceed with these constraints."""
        return self.safe


@dataclass(frozen=True, slots=True)
class PriorityClarification:
    """Typed clarification required before resolving contradictory rules."""

    code: Literal["same_tier_contradiction"]
    rule_ids: tuple[str, ...]
    message: str = (
        "Some dietary rules conflict at the same priority. Please clarify "
        "which count should apply."
    )


@dataclass(frozen=True, slots=True)
class PriorityResolution:
    """Deterministic effective preferences and independent constraints."""

    effective_rules: tuple[DietaryRule, ...]
    constraint_rules: tuple[ConstraintEntry, ...]
    clarification: PriorityClarification | None = None


# The longer name is useful at integration boundaries while the short name is
# convenient in application code.
PriorityResolutionResult = PriorityResolution


def _canonical_term(value: str) -> str:
    """Return one normalized phrase suitable for deterministic comparison."""
    return " ".join(normalize_food(value))


def _append_unique(items: list[str], value: str) -> None:
    """Append a term while preserving the first deterministic occurrence."""
    if value and value not in items:
        items.append(value)


def _append_expanded_terms(
    expanded: list[str], unknown: list[str], terms: Sequence[str]
) -> None:
    """Append normalized registry terms and preserve malformed entries."""
    for term in terms:
        canonical_term = _canonical_term(term)
        if canonical_term:
            _append_unique(expanded, canonical_term)
        else:
            bounded_unknown = term.strip()[:100]
            if bounded_unknown not in unknown:
                unknown.append(bounded_unknown)


def _known_literal_term(tokens: tuple[str, ...]) -> bool:
    """Return whether tokens identify a reviewed literal food."""
    return bool(tokens) and " ".join(tokens) in CONSTRAINT_LITERAL_FOOD_REGISTRY


def expand_constraint_terms(
    terms: Sequence[str],
    *,
    aliases: AliasRegistry = CONSTRAINT_ALIAS_REGISTRY,
) -> ConstraintExpansionResult:
    """Normalize exact exclusions and expand supported category aliases.

    Punctuation-only inputs are returned as unknown instead of being dropped.
    This gives callers an explicit fail-closed signal for legacy or
    uninterpretable constraints.
    """
    expanded: list[str] = []
    unknown: list[str] = []
    for raw_term in terms:
        normalized_tokens = normalize_food(raw_term)
        if not normalized_tokens:
            bounded_unknown = raw_term.strip()[:100]
            if bounded_unknown not in unknown:
                unknown.append(bounded_unknown)
            continue

        normalized = " ".join(normalized_tokens)
        alias_key = normalize_match_text(raw_term)
        phrase_terms = CONSTRAINT_PHRASE_REGISTRY.get(alias_key)
        if phrase_terms is not None:
            _append_expanded_terms(expanded, unknown, phrase_terms)
            continue

        alias_terms = aliases.get(alias_key, aliases.get(normalized))
        if alias_terms is None and alias_key.endswith("ies"):
            alias_terms = aliases.get(f"{alias_key[:-3]}y")
        if alias_terms is not None:
            _append_expanded_terms(expanded, unknown, alias_terms)
            continue

        if _known_literal_term(normalized_tokens):
            _append_unique(expanded, normalized)
            continue

        bounded_unknown = raw_term.strip()[:100]
        if bounded_unknown not in unknown:
            unknown.append(bounded_unknown)

    return ConstraintExpansionResult(
        terms=tuple(expanded),
        unknown_terms=tuple(unknown),
    )


def expand_constraint_entry(
    constraint: ConstraintEntry,
    *,
    aliases: AliasRegistry = CONSTRAINT_ALIAS_REGISTRY,
) -> ConstraintExpansionResult:
    """Expand the normalized terms belonging to one persisted constraint."""
    if constraint.uninterpretable:
        return ConstraintExpansionResult(terms=(), unknown_terms=())
    return expand_constraint_terms(constraint.forbidden_terms, aliases=aliases)


def _contains_token_sequence(
    text_tokens: tuple[str, ...], term_tokens: tuple[str, ...]
) -> bool:
    """Match a phrase as whole normalized tokens, never as a substring."""
    if (
        not text_tokens
        or not term_tokens
        or len(term_tokens) > len(text_tokens)
    ):
        return False
    width = len(term_tokens)
    return any(
        text_tokens[index : index + width] == term_tokens
        for index in range(len(text_tokens) - width + 1)
    )


def _alternative_is_forbidden(
    alternative: str, forbidden_terms: tuple[str, ...]
) -> tuple[bool, tuple[str, ...]]:
    """Return whether every deterministic form of an alternative is banned."""
    alternative_expansion = expand_constraint_terms((alternative,))
    if not alternative_expansion.is_safe:
        return True, ()

    matched_terms: list[str] = []
    for candidate in alternative_expansion.terms:
        candidate_tokens = normalize_food(candidate)
        candidate_matches = tuple(
            forbidden
            for forbidden in forbidden_terms
            if _contains_token_sequence(
                candidate_tokens, normalize_food(forbidden)
            )
        )
        if not candidate_matches:
            return False, ()
        for matched in candidate_matches:
            _append_unique(matched_terms, matched)
    return True, tuple(matched_terms)


def _rule_requires_food(rule: DietaryRule) -> bool:
    """Return whether a rule has a positive obligation to include food."""
    if rule.count <= 0:
        return False
    return rule.operator in {RuleOperator.EXACTLY, RuleOperator.AT_LEAST}


def _rule_conflict(
    rule: DietaryRule,
    expansion: ConstraintExpansionResult,
) -> tuple[bool, tuple[str, ...]]:
    """Check one preference against one expanded constraint."""
    if not _rule_requires_food(rule):
        return False, ()

    all_alternatives_forbidden = True
    matched_terms: list[str] = []
    for alternative in rule.foods_any_of:
        is_forbidden, terms = _alternative_is_forbidden(
            alternative, expansion.terms
        )
        if not is_forbidden:
            all_alternatives_forbidden = False
        for term in terms:
            _append_unique(matched_terms, term)
    return all_alternatives_forbidden, tuple(matched_terms)


def validate_constraints(
    constraints: Sequence[ConstraintEntry],
    *,
    aliases: AliasRegistry = CONSTRAINT_ALIAS_REGISTRY,
) -> ConstraintSafetyResult:
    """Return a fail-closed safety result for active constraints."""
    unknown_ids = tuple(
        sorted(
            constraint.id
            for constraint in constraints
            if not expand_constraint_entry(constraint, aliases=aliases).is_safe
        )
    )
    return ConstraintSafetyResult(
        safe=not unknown_ids,
        unknown_constraint_ids=unknown_ids,
    )


def has_constraint_conflict(
    preference: DietaryRule,
    constraints: Sequence[ConstraintEntry],
    *,
    aliases: AliasRegistry = CONSTRAINT_ALIAS_REGISTRY,
) -> bool:
    """Return whether a preference conflicts with any active constraint.

    Constraints have no meal or weekday scope.  Unknown constraint terms are
    treated as conflicts so callers cannot accidentally proceed unsafely.
    """
    return any(
        not (
            expansion := expand_constraint_entry(constraint, aliases=aliases)
        ).is_safe
        or _rule_conflict(preference, expansion)[0]
        for constraint in constraints
    )


def find_constraint_conflicts(
    preferences: Sequence[DietaryRule],
    constraints: Sequence[ConstraintEntry],
    *,
    aliases: AliasRegistry = CONSTRAINT_ALIAS_REGISTRY,
) -> tuple[ConstraintConflict, ...]:
    """Return stable, source-free conflict records in ID order."""
    conflicts: list[ConstraintConflict] = []
    ordered_preferences = sorted(preferences, key=lambda rule: rule.id)
    ordered_constraints = sorted(constraints, key=lambda rule: rule.id)
    for constraint in ordered_constraints:
        expansion = expand_constraint_entry(constraint, aliases=aliases)
        for preference in ordered_preferences:
            if not expansion.is_safe:
                if _rule_requires_food(preference):
                    conflicts.append(
                        ConstraintConflict(
                            constraint_id=constraint.id,
                            preference_id=preference.id,
                            conflicting_terms=(),
                            source_references=(
                                ConstraintSourceReference(
                                    kind="constraint", rule_id=constraint.id
                                ),
                                ConstraintSourceReference(
                                    kind="preference", rule_id=preference.id
                                ),
                            ),
                            fail_closed=True,
                        )
                    )
                continue

            is_conflict, conflicting_terms = _rule_conflict(
                preference, expansion
            )
            if is_conflict:
                conflicts.append(
                    ConstraintConflict(
                        constraint_id=constraint.id,
                        preference_id=preference.id,
                        conflicting_terms=conflicting_terms,
                        source_references=(
                            ConstraintSourceReference(
                                kind="constraint", rule_id=constraint.id
                            ),
                            ConstraintSourceReference(
                                kind="preference", rule_id=preference.id
                            ),
                        ),
                    )
                )
    return tuple(conflicts)


def _rule_foods(rule: DietaryRule) -> set[tuple[str, ...]]:
    """Return normalized food alternatives for scope comparison."""
    return {normalize_food(food) for food in rule.foods_any_of}


def _scope_overlaps(left: DietaryRule, right: DietaryRule) -> bool:
    """Return whether two preference rules can describe the same meals."""
    if (
        left.meal_type is not None
        and right.meal_type is not None
        and left.meal_type is not right.meal_type
    ):
        return False
    left_days = set(left.weekdays) or set(Weekday)
    right_days = set(right.weekdays) or set(Weekday)
    return bool(left_days & right_days)


def _scope_contains(outer: DietaryRule, inner: DietaryRule) -> bool:
    """Return whether ``outer`` covers the complete scope of ``inner``."""
    if outer.meal_type is not None and outer.meal_type is not inner.meal_type:
        return False

    outer_days = set(outer.weekdays) or set(Weekday)
    inner_days = set(inner.weekdays) or set(Weekday)
    if not inner_days <= outer_days:
        return False

    return _rule_foods(inner) <= _rule_foods(outer)


def _rules_overlap(left: DietaryRule, right: DietaryRule) -> bool:
    """Return whether two rules share food and meal scope."""
    return bool(_rule_foods(left) & _rule_foods(right)) and _scope_overlaps(
        left, right
    )


def _rule_bounds(rule: DietaryRule) -> tuple[int, int | None]:
    """Return the inclusive lower and optional upper count bounds."""
    if rule.operator is RuleOperator.EXACTLY:
        return rule.count, rule.count
    if rule.operator is RuleOperator.AT_LEAST:
        return rule.count, None
    return 0, rule.count


def _rules_contradict(left: DietaryRule, right: DietaryRule) -> bool:
    """Return whether overlapping rules cannot both be satisfied."""
    if not _rules_overlap(left, right):
        return False
    left_min, left_max = _rule_bounds(left)
    right_min, right_max = _rule_bounds(right)
    lower = max(left_min, right_min)
    upper_values = [
        value for value in (left_max, right_max) if value is not None
    ]
    return bool(upper_values and lower > min(upper_values))


def _find_same_tier_contradiction(
    rules: Sequence[DietaryRule],
) -> PriorityClarification | None:
    """Find the first deterministic same-tier contradiction."""
    ordered = sorted(rules, key=lambda rule: rule.id)
    for index, left in enumerate(ordered):
        for right in ordered[index + 1 :]:
            if _rules_contradict(left, right):
                return PriorityClarification(
                    code="same_tier_contradiction",
                    rule_ids=(left.id, right.id),
                )
    return None


def _copy_rule(
    rule: DietaryRule,
    *,
    rule_id: str | None = None,
    foods: Sequence[str] | None = None,
    weekdays: Sequence[Weekday] | None = None,
    operator: RuleOperator | None = None,
    count: int | None = None,
) -> DietaryRule:
    """Copy a rule through validation with deterministic field updates."""
    payload = rule.model_dump(mode="python")
    if rule_id is not None:
        payload["id"] = rule_id
    if foods is not None:
        payload["foods_any_of"] = list(foods)
    if weekdays is not None:
        payload["weekdays"] = list(weekdays)
    if operator is not None:
        payload["operator"] = operator
    if count is not None:
        payload["count"] = count
    return DietaryRule.model_validate(payload)


def _remaining_scope_rule(
    lower: DietaryRule, higher: DietaryRule
) -> DietaryRule | None:
    """Preserve the lower rule's non-overlapping weekday/food fragment."""
    lower_days = set(lower.weekdays) or set(Weekday)
    higher_days = set(higher.weekdays) or set(Weekday)
    lower_foods = _rule_foods(lower)
    higher_foods = _rule_foods(higher)
    day_overlap = lower_days & higher_days
    food_overlap = lower_foods & higher_foods
    if not day_overlap or not food_overlap:
        return None

    remaining_days = lower_days - higher_days
    if remaining_days:
        # A weekday fragment remains usable with every lower alternative.  A
        # higher rule only wins on the days where its scope overlaps.
        remaining_foods = list(lower.foods_any_of)
    else:
        remaining_foods = [
            food
            for food in lower.foods_any_of
            if normalize_food(food) not in higher_foods
        ]
        remaining_days = lower_days

    if not remaining_foods:
        return None
    if not remaining_days:
        return None

    ordered_days = [day for day in sorted(remaining_days, key=int)]
    output_weekdays: list[Weekday] = ordered_days
    if not lower.weekdays and remaining_days == set(Weekday):
        output_weekdays = []
    remaining_capacity = len(ordered_days)
    remaining_count = lower.count
    if lower.operator in {RuleOperator.EXACTLY, RuleOperator.AT_LEAST}:
        remaining_count = max(remaining_count - higher.count, 0)
        remaining_count = min(remaining_count, remaining_capacity)
    if remaining_count == 0 and lower.operator is not RuleOperator.AT_MOST:
        return None

    return _copy_rule(
        lower,
        rule_id=f"{lower.id}-remaining",
        foods=remaining_foods,
        weekdays=output_weekdays,
        count=remaining_count,
    )


def _maximum_capacity_for_lower(
    lower: DietaryRule, higher: DietaryRule
) -> int | None:
    """Return the largest lower count allowed by a higher maximum."""
    if higher.operator is not RuleOperator.AT_MOST:
        return None
    lower_days = set(lower.weekdays) or set(Weekday)
    higher_days = set(higher.weekdays) or set(Weekday)
    overlap_days = lower_days & higher_days
    if not overlap_days:
        return None
    outside_days = lower_days - higher_days
    if higher.meal_type is not None and lower.meal_type != higher.meal_type:
        return None
    return len(outside_days) + higher.count


def _cap_lower_rule(
    lower: DietaryRule, higher: DietaryRule
) -> tuple[DietaryRule, bool]:
    """Cap a lower obligation and report whether the higher max is absorbed."""
    capacity = _maximum_capacity_for_lower(lower, higher)
    if capacity is None:
        return lower, False

    fully_covered = _scope_contains(higher, lower)
    if lower.count <= capacity:
        if fully_covered and lower.operator is not RuleOperator.AT_LEAST:
            return lower, True
        return lower, False

    capped_count = min(lower.count, capacity)
    if lower.operator in {RuleOperator.EXACTLY, RuleOperator.AT_LEAST}:
        capped_operator = RuleOperator.EXACTLY
    else:
        capped_operator = RuleOperator.AT_MOST

    capped_weekdays: list[Weekday] | None = None
    lower_days = set(lower.weekdays) or set(Weekday)
    higher_days = set(higher.weekdays) or set(Weekday)
    if not (lower_days - higher_days) and lower.weekdays:
        capped_weekdays = sorted(lower_days, key=int)[:capped_count]
    capped = _copy_rule(
        lower,
        operator=capped_operator,
        count=capped_count,
        weekdays=capped_weekdays,
    )
    return capped, fully_covered


def _apply_current_rule(
    lower_rules: Sequence[DietaryRule], current: DietaryRule
) -> tuple[list[DietaryRule], bool]:
    """Apply one current rule to stored rules and return absorption state."""
    result: list[DietaryRule] = []
    absorbed = False
    for lower in lower_rules:
        if not _rules_overlap(lower, current):
            result.append(lower)
            continue

        if current.operator is RuleOperator.AT_MOST:
            capped, max_absorbed = _cap_lower_rule(lower, current)
            if capped is not lower:
                result.append(capped)
            else:
                result.append(lower)
            absorbed = absorbed or max_absorbed
            continue

        if current.operator is RuleOperator.AT_LEAST:
            lower_min, lower_max = _rule_bounds(lower)
            current_min = current.count
            if lower.operator is RuleOperator.AT_MOST:
                if lower.count < current_min:
                    absorbed = True
                    continue
                result.append(lower)
                continue
            if lower_max is not None and lower_max >= current_min:
                result.append(lower)
                continue
            fragment = _remaining_scope_rule(lower, current)
            if fragment is not None:
                result.append(fragment)
            else:
                absorbed = True
            continue

        fragment = _remaining_scope_rule(lower, current)
        if fragment is not None:
            result.append(fragment)
        else:
            absorbed = True

    if current.operator is not RuleOperator.AT_MOST or not absorbed:
        result.append(current)
    return result, absorbed


def resolve_priority_rules(
    stored_rules: Sequence[DietaryRule],
    current_rules: Sequence[DietaryRule],
    *,
    constraints: Sequence[ConstraintEntry] = (),
) -> PriorityResolution:
    """Resolve current plan rules over stored rules deterministically.

    Constraints are returned separately and are never replaced by either
    preference tier.  Contradictions within one tier produce a typed
    clarification result before any precedence is applied.
    """
    ordered_stored = sorted(stored_rules, key=lambda rule: rule.id)
    ordered_current = sorted(current_rules, key=lambda rule: rule.id)
    clarification = _find_same_tier_contradiction(ordered_stored)
    if clarification is None:
        clarification = _find_same_tier_contradiction(ordered_current)
    ordered_constraints = tuple(sorted(constraints, key=lambda rule: rule.id))
    if clarification is not None:
        return PriorityResolution(
            effective_rules=(),
            constraint_rules=ordered_constraints,
            clarification=clarification,
        )

    effective: list[DietaryRule] = list(ordered_stored)
    for current in ordered_current:
        effective, absorbed = _apply_current_rule(effective, current)
        if absorbed and current.operator is RuleOperator.AT_MOST:
            continue
    return PriorityResolution(
        effective_rules=tuple(sorted(effective, key=lambda rule: rule.id)),
        constraint_rules=ordered_constraints,
    )
