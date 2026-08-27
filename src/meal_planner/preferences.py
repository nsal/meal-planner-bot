"""Evidence matching and generated-plan validation for preferences."""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Sequence

from meal_planner.dietary_rules import (
    SubmittedMealEvidence,
    expand_constraint_entry,
    match_submitted_meal,
    obligation_covers_date,
)
from meal_planner.models.schemas import (
    BatchLedgerEntry,
    BatchLedgerState,
    BatchMealRole,
    BatchRule,
    ConstraintEntry,
    DietaryObligation,
    DietaryRule,
    MealLogEntry,
    MealType,
    PlannedMeal,
    PreferenceRequirement,
    RuleOperator,
    RuleStrength,
    Weekday,
    WeeklyPlan,
    daily_meal_capacity,
)
from meal_planner.normalization import normalize_food

MAX_REQUIREMENT_MESSAGE_LENGTH = 1000
_MAX_DISPLAYED_ALTERNATIVES = 3

_REQUIRED_MEAL_TYPES: tuple[MealType, ...] = (
    MealType.BREAKFAST,
    MealType.LUNCH,
    MealType.DINNER,
)
_MEAL_TYPE_ORDER = {
    MealType.BREAKFAST: 0,
    MealType.LUNCH: 1,
    MealType.DINNER: 2,
    MealType.SNACK: 3,
}


def match_logged_meal(
    entry: MealLogEntry, rule: DietaryRule
) -> SubmittedMealEvidence | None:
    """Match one persisted meal without using planned-meal structures."""
    return match_submitted_meal(entry, rule)


@dataclass(frozen=True, slots=True)
class RuleHorizonFeasibility:
    """Capacity outcome for one rule over a concrete date horizon."""

    rule_id: str
    operator: RuleOperator
    requested_count: int
    eligible_slot_capacity: int
    weekday_capacities: tuple[tuple[Weekday, int], ...] = ()
    infeasible_weekdays: tuple[Weekday, ...] = ()
    strength: RuleStrength = RuleStrength.STRICT

    @property
    def is_feasible(self) -> bool:
        """Return whether the rule can fit within its available slots."""
        if self.weekday_capacities:
            return not self.infeasible_weekdays
        return not (
            self.operator in {RuleOperator.EXACTLY, RuleOperator.AT_LEAST}
            and self.requested_count > 0
            and self.requested_count > self.eligible_slot_capacity
        )

    @property
    def feasible(self) -> bool:
        """Return whether the rule can fit within its available slots."""
        return self.is_feasible

    @property
    def possible_count(self) -> int:
        """Return the total number of eligible slots in the horizon."""
        return self.eligible_slot_capacity

    @property
    def is_blocking(self) -> bool:
        """Return whether this shortfall must block plan generation."""
        return self.strength is RuleStrength.STRICT and not self.is_feasible


@dataclass(frozen=True, slots=True)
class HorizonFeasibilityResult:
    """Typed capacity outcome for resolved rules over a date horizon."""

    rules: tuple[RuleHorizonFeasibility, ...]
    clarification: str | None = None

    @property
    def is_feasible(self) -> bool:
        """Return whether every strict rule fits within the date horizon."""
        return not self.infeasible_rules

    @property
    def feasible(self) -> bool:
        """Return whether every rule fits within the date horizon."""
        return self.is_feasible

    @property
    def infeasible_rules(self) -> tuple[RuleHorizonFeasibility, ...]:
        """Return strict rules whose positive obligation exceeds capacity."""
        return tuple(rule for rule in self.rules if rule.is_blocking)

    @property
    def advisory_shortfalls(self) -> tuple[RuleHorizonFeasibility, ...]:
        """Return best-effort rules whose capacity is below their target."""
        return tuple(
            rule
            for rule in self.rules
            if not rule.is_feasible
            and rule.strength is RuleStrength.BEST_EFFORT
        )


@dataclass(frozen=True, slots=True)
class MealEvidence:
    """Evidence that one distinct planned meal satisfies a requirement."""

    day: int
    meal_type: MealType
    meal_name: str
    matched_foods: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuleValidation:
    """Typed evidence and outcome for one generalized dietary rule."""

    rule_id: str
    foods_any_of: tuple[str, ...]
    operator: RuleOperator
    strength: RuleStrength
    expected_count: int
    actual_count: int
    possible_count: int
    matched_meals: tuple[MealEvidence, ...]
    weekday_counts: tuple[tuple[Weekday, int], ...] = ()
    missing_weekdays: tuple[Weekday, ...] = ()
    obligation_id: str | None = None
    eligible_dates: tuple[date, ...] = ()
    matched_dates: tuple[date, ...] = ()

    @property
    def is_satisfied(self) -> bool:
        """Return whether the observed count satisfies the rule operator."""
        if self.weekday_counts:
            return not self.missing_weekdays
        if self.missing_weekdays:
            return False
        if self.operator is RuleOperator.EXACTLY:
            return self.actual_count == self.expected_count
        if self.operator is RuleOperator.AT_LEAST:
            return self.actual_count >= self.expected_count
        return self.actual_count <= self.expected_count

    @property
    def evidence(self) -> tuple[MealEvidence, ...]:
        """Return distinct meal evidence using a descriptive name."""
        return self.matched_meals


@dataclass(frozen=True, slots=True)
class ConstraintValidation:
    """Typed safety evidence for one independent dietary constraint."""

    constraint_id: str
    safe: bool
    matched_terms: tuple[str, ...]
    matched_meals: tuple[MealEvidence, ...]
    unknown_terms: tuple[str, ...] = ()

    @property
    def is_safe(self) -> bool:
        """Return whether the constraint can be safely evaluated."""
        return self.safe

    @property
    def violated(self) -> bool:
        """Return whether declared plan evidence violates the constraint."""
        return bool(self.matched_meals)


@dataclass(frozen=True, slots=True)
class RequirementValidation:
    """Counted evidence for one exact-count requirement."""

    requirement_id: str
    expected_count: int
    actual_count: int
    possible_count: int
    matched_meals: tuple[MealEvidence, ...]

    @property
    def is_satisfied(self) -> bool:
        """Return whether the requirement has exactly its requested count."""
        return self.actual_count == self.expected_count


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """Stable, structured feedback for an invalid generated plan."""

    code: str
    message: str
    day: int | None = None
    meal_type: MealType | None = None
    requirement_id: str | None = None
    rule_id: str | None = None
    constraint_id: str | None = None
    obligation_id: str | None = None
    obligation_date: date | None = None
    batch_id: str | None = None
    batch_role: BatchMealRole | None = None


@dataclass(frozen=True, slots=True)
class BatchLinkEvidence:
    """Application-owned evidence for one validated batch-linked meal."""

    batch_id: str
    role: BatchMealRole
    day: int
    meal_type: MealType
    portion: int
    source_date: date | None = None
    source_meal_type: MealType | None = None


@dataclass(frozen=True, slots=True)
class BatchValidationResult:
    """Bounded compliance evidence for generated batch links."""

    valid: bool
    links: tuple[BatchLinkEvidence, ...]
    issues: tuple[ValidationIssue, ...]

    @property
    def is_valid(self) -> bool:
        """Return whether every linked meal has a safe batch source."""
        return self.valid


@dataclass(frozen=True, slots=True)
class PlanValidationResult:
    """Complete validation outcome for a generated weekly plan."""

    valid: bool
    requirements: tuple[RequirementValidation, ...]
    issues: tuple[ValidationIssue, ...]
    rules: tuple[RuleValidation, ...] = ()
    constraints: tuple[ConstraintValidation, ...] = ()
    batches: tuple[BatchLinkEvidence, ...] = ()

    @property
    def is_valid(self) -> bool:
        """Return whether completeness and all preference rules pass."""
        return self.valid

    @property
    def rule_results(self) -> tuple[RuleValidation, ...]:
        """Return generalized dietary-rule evidence."""
        return self.rules

    @property
    def constraint_results(self) -> tuple[ConstraintValidation, ...]:
        """Return typed constraint safety evidence."""
        return self.constraints

    @property
    def safety_issues(self) -> tuple[ValidationIssue, ...]:
        """Return only issues caused by constraints or strict rules."""
        return tuple(
            issue
            for issue in self.issues
            if issue.code
            in {
                "unsafe_constraint",
                "constraint_violation",
                "strict_rule_mismatch",
                "impossible_rule_count",
                "obligation_missing",
                "obligation_excess",
                "obligation_wrong_date",
                "obligation_wrong_meal_scope",
                "batch_invalid_meal_scope",
                "batch_missing_source",
                "batch_unknown_source",
                "batch_duplicate_source",
                "batch_duplicate_portion",
                "batch_excessive_reuse",
                "batch_wrong_source",
                "batch_reuse_before_preparation",
                "batch_unavailable_portion",
                "batch_noncanonical_ordinal_order",
                "batch_cross_week",
                "batch_reserved_id_collision",
                "batch_rule_duplicate_id",
                "batch_rule_missing_preparation",
                "batch_rule_wrong_food",
                "batch_rule_invalid_preparation_meal_type",
                "batch_rule_invalid_reuse_meal_type",
                "batch_rule_wrong_yield",
                "batch_rule_missing_leftover",
                "batch_rule_duplicate_leftover",
                "batch_rule_wrong_leftover_count",
                "batch_rule_wrong_leftover_ordinal",
                "batch_rule_wrong_source",
                "batch_rule_reuse_before_preparation",
                "batch_rule_ambiguous_match",
                "batch_rule_cross_linked",
                "batch_rule_unexpected_batch",
            }
        )


def _horizon_dates(start_date: date, end_date: date) -> tuple[date, ...]:
    """Return every inclusive date in a valid, bounded horizon."""
    if end_date < start_date:
        raise ValueError("end_date must not precede start_date")
    horizon_days = (end_date - start_date).days + 1
    return tuple(
        start_date + timedelta(days=offset) for offset in range(horizon_days)
    )


def _rule_capacity_for_dates(
    rule: DietaryRule, dates: Sequence[date]
) -> tuple[int, tuple[tuple[Weekday, int], ...]]:
    """Return total and per-weekday eligible slot capacity for a rule."""
    daily_capacity = daily_meal_capacity(rule.meal_type)
    if not rule.weekdays:
        return len(dates) * daily_capacity, ()

    weekday_capacities = tuple(
        (
            weekday,
            sum(
                daily_capacity
                for target_date in dates
                if target_date.isoweekday() == int(weekday)
            ),
        )
        for weekday in rule.weekdays
    )
    return sum(capacity for _, capacity in weekday_capacities), (
        weekday_capacities
    )


def _capacity_is_insufficient(rule: DietaryRule, capacity: int) -> bool:
    """Return whether a positive obligation exceeds its slot capacity."""
    return (
        rule.operator in {RuleOperator.EXACTLY, RuleOperator.AT_LEAST}
        and rule.count > 0
        and rule.count > capacity
    )


def validate_horizon_feasibility(
    rules: Sequence[DietaryRule],
    *,
    start_date: date,
    end_date: date,
) -> HorizonFeasibilityResult:
    """Check rule obligations against eligible slots in an inclusive horizon.

    This is a capacity check, not a claim that a food will match a slot.  An
    ``AT_MOST`` rule is always capacity-feasible because zero matches can
    satisfy it.  Named weekdays are checked independently, so a missing day
    can make a positive exact or lower-bound obligation impossible without
    making an upper bound impossible.
    """
    dates = _horizon_dates(start_date, end_date)
    results: list[RuleHorizonFeasibility] = []
    for rule in rules:
        capacity, weekday_capacities = _rule_capacity_for_dates(rule, dates)
        infeasible_weekdays = tuple(
            weekday
            for weekday, weekday_capacity in weekday_capacities
            if _capacity_is_insufficient(rule, weekday_capacity)
        )
        results.append(
            RuleHorizonFeasibility(
                rule_id=rule.id,
                operator=rule.operator,
                requested_count=rule.count,
                eligible_slot_capacity=capacity,
                weekday_capacities=weekday_capacities,
                infeasible_weekdays=infeasible_weekdays,
                strength=rule.strength,
            )
        )

    normalized_results = tuple(results)
    preliminary = HorizonFeasibilityResult(rules=normalized_results)
    return HorizonFeasibilityResult(
        rules=normalized_results,
        clarification=(
            format_horizon_clarification(preliminary)
            if not preliminary.is_feasible
            else None
        ),
    )


def _horizon_rule_line(
    result: RuleHorizonFeasibility,
) -> str:
    """Return one bounded application-owned horizon explanation."""
    operator = {
        RuleOperator.EXACTLY: "exactly",
        RuleOperator.AT_LEAST: "at least",
        RuleOperator.AT_MOST: "at most",
    }[result.operator]
    if result.weekday_capacities:
        details = ", ".join(
            f"{weekday.name.title()} ({capacity})"
            for weekday, capacity in result.weekday_capacities
            if weekday in result.infeasible_weekdays
        )
        return (
            f"• Rule '{result.rule_id}' needs {operator} "
            f"{result.requested_count} eligible meals on {details}."
        )
    return (
        f"• Rule '{result.rule_id}' needs {operator} "
        f"{result.requested_count} eligible meals, but the horizon "
        f"provides {result.eligible_slot_capacity}."
    )


def format_horizon_clarification(result: HorizonFeasibilityResult) -> str:
    """Format bounded horizon feedback without exposing rule source text."""
    if result.is_feasible:
        return ""

    prefix = (
        "Some preference rules cannot fit the requested plan horizon. "
        "Please clarify:\n"
    )
    suffix = "\nThe plan duration is retained for your clarification."
    lines: list[str] = []
    infeasible_rules = result.infeasible_rules
    for index, rule_result in enumerate(infeasible_rules):
        line = _horizon_rule_line(rule_result)
        remaining = len(infeasible_rules) - index - 1
        omission = f"• ... and {remaining} rules omitted."
        candidate = prefix + "\n".join([*lines, line]) + suffix
        reserved = len(omission) + 1 if remaining else 0
        if len(candidate) + reserved > MAX_REQUIREMENT_MESSAGE_LENGTH:
            lines.append(
                f"• ... and {len(infeasible_rules) - index} rules omitted."
            )
            break
        lines.append(line)
    return prefix + "\n".join(lines) + suffix


def matches_food(text: str, food: str) -> bool:
    """Return whether ``food`` occurs as whole normalized words in ``text``."""
    text_tokens = normalize_food(text)
    food_tokens = normalize_food(food)
    if (
        not text_tokens
        or not food_tokens
        or len(food_tokens) > len(text_tokens)
    ):
        return False
    width = len(food_tokens)
    return any(
        text_tokens[index : index + width] == food_tokens
        for index in range(len(text_tokens) - width + 1)
    )


def _meal_matches(
    meal: PlannedMeal, requirement: PreferenceRequirement
) -> tuple[str, ...]:
    """Return all alternative terms evidenced by a meal, without duplicates."""
    evidence_text = (
        meal.name,
        *(ingredient.item for ingredient in meal.ingredients),
    )
    return tuple(
        food
        for food in requirement.foods_any_of
        if any(matches_food(text, food) for text in evidence_text)
    )


def _rule_matches_meal(
    meal: PlannedMeal, foods: Sequence[str]
) -> tuple[str, ...]:
    """Return normalized rule alternatives evidenced by one meal."""
    evidence_text = (
        meal.name,
        *(ingredient.item for ingredient in meal.ingredients),
    )
    return tuple(
        food
        for food in foods
        if any(matches_food(text, food) for text in evidence_text)
    )


def _rule_matches_day(rule: DietaryRule, day: int) -> bool:
    """Return whether a plan day is in a generalized rule's scope."""
    return not rule.weekdays or Weekday(day) in rule.weekdays


def match_rule(plan: WeeklyPlan, rule: DietaryRule) -> tuple[MealEvidence, ...]:
    """Find distinct meals matching a generalized rule's complete scope."""
    matches: list[MealEvidence] = []
    days = sorted(plan.days, key=lambda plan_day: plan_day.day)
    for plan_day in days:
        if not _rule_matches_day(rule, plan_day.day):
            continue
        meals = sorted(
            plan_day.meals,
            key=lambda planned_meal: _MEAL_TYPE_ORDER[planned_meal.meal_type],
        )
        for planned_meal in meals:
            if (
                rule.meal_type is not None
                and planned_meal.meal_type is not rule.meal_type
            ):
                continue
            matched_foods = _rule_matches_meal(planned_meal, rule.foods_any_of)
            if matched_foods:
                matches.append(
                    MealEvidence(
                        day=plan_day.day,
                        meal_type=planned_meal.meal_type,
                        meal_name=planned_meal.name,
                        matched_foods=matched_foods,
                    )
                )
    return tuple(matches)


def _plan_date(plan: WeeklyPlan, day: int) -> date:
    """Resolve a generated plan's relative day to its exact calendar date."""
    return plan.week_start + timedelta(days=day - 1)


def match_obligation(
    plan: WeeklyPlan, obligation: DietaryObligation
) -> tuple[MealEvidence, ...]:
    """Find evidence only on an obligation's exact owned dates and scope."""
    matches: list[MealEvidence] = []
    for plan_day in sorted(plan.days, key=lambda item: item.day):
        target_date = _plan_date(plan, plan_day.day)
        if not obligation_covers_date(obligation, target_date):
            continue
        for planned_meal in sorted(
            plan_day.meals,
            key=lambda item: _MEAL_TYPE_ORDER[item.meal_type],
        ):
            if (
                obligation.meal_type is not None
                and planned_meal.meal_type is not obligation.meal_type
            ):
                continue
            matched_foods = _rule_matches_meal(
                planned_meal, obligation.foods_any_of
            )
            if matched_foods:
                matches.append(
                    MealEvidence(
                        day=plan_day.day,
                        meal_type=planned_meal.meal_type,
                        meal_name=planned_meal.name,
                        matched_foods=matched_foods,
                    )
                )
    return tuple(matches)


def _obligation_out_of_scope_matches(
    plan: WeeklyPlan, obligation: DietaryObligation
) -> tuple[tuple[str, MealEvidence], ...]:
    """Find matching foods placed on an obligation's wrong date or scope."""
    matches: list[tuple[str, MealEvidence]] = []
    for plan_day in sorted(plan.days, key=lambda item: item.day):
        target_date = _plan_date(plan, plan_day.day)
        iso = target_date.isocalendar()
        if f"{iso.year:04d}-W{iso.week:02d}" != obligation.iso_week:
            continue
        for planned_meal in plan_day.meals:
            matched_foods = _rule_matches_meal(
                planned_meal, obligation.foods_any_of
            )
            if not matched_foods:
                continue
            evidence = MealEvidence(
                day=plan_day.day,
                meal_type=planned_meal.meal_type,
                meal_name=planned_meal.name,
                matched_foods=matched_foods,
            )
            if target_date not in obligation.eligible_dates:
                matches.append(("obligation_wrong_date", evidence))
            elif (
                obligation.meal_type is not None
                and planned_meal.meal_type is not obligation.meal_type
            ):
                matches.append(("obligation_wrong_meal_scope", evidence))
    return tuple(matches)


def _obligation_possible_count(
    plan: WeeklyPlan, obligation: DietaryObligation
) -> int:
    """Count eligible meal slots represented by this plan."""
    eligible_dates = set(obligation.eligible_dates)
    return sum(
        1
        for plan_day in plan.days
        if _plan_date(plan, plan_day.day) in eligible_dates
        for planned_meal in plan_day.meals
        if obligation.meal_type is None
        or planned_meal.meal_type is obligation.meal_type
    )


def _eligible_rule_meal_count(plan: WeeklyPlan, rule: DietaryRule) -> int:
    """Count distinct meal slots in a generalized rule's scope."""
    return sum(
        1
        for plan_day in plan.days
        if _rule_matches_day(rule, plan_day.day)
        for planned_meal in plan_day.meals
        if rule.meal_type is None or planned_meal.meal_type is rule.meal_type
    )


def _constraint_meal_terms(
    meal: PlannedMeal, terms: Sequence[str]
) -> tuple[str, ...]:
    """Return forbidden terms evidenced by a meal name or ingredient."""
    evidence_text = (
        meal.name,
        *(ingredient.item for ingredient in meal.ingredients),
    )
    return tuple(
        term
        for term in terms
        if any(matches_food(text, term) for text in evidence_text)
    )


def match_constraint(
    plan: WeeklyPlan, constraint: ConstraintEntry
) -> tuple[MealEvidence, ...]:
    """Find distinct meals containing any expanded forbidden term."""
    expansion = expand_constraint_entry(constraint)
    matches: list[MealEvidence] = []
    for plan_day in sorted(plan.days, key=lambda item: item.day):
        for planned_meal in sorted(
            plan_day.meals,
            key=lambda item: _MEAL_TYPE_ORDER[item.meal_type],
        ):
            matched_terms = _constraint_meal_terms(
                planned_meal, expansion.terms
            )
            if matched_terms:
                matches.append(
                    MealEvidence(
                        day=plan_day.day,
                        meal_type=planned_meal.meal_type,
                        meal_name=planned_meal.name,
                        matched_foods=matched_terms,
                    )
                )
    return tuple(matches)


def match_requirement(
    plan: WeeklyPlan, requirement: PreferenceRequirement
) -> tuple[MealEvidence, ...]:
    """Find distinct meals whose names or ingredients evidence a requirement."""
    matches: list[MealEvidence] = []
    days = sorted(plan.days, key=lambda plan_day: plan_day.day)
    for plan_day in days:
        meals = sorted(
            plan_day.meals,
            key=lambda planned_meal: _MEAL_TYPE_ORDER[planned_meal.meal_type],
        )
        for planned_meal in meals:
            if (
                requirement.meal_type is not None
                and planned_meal.meal_type is not requirement.meal_type
            ):
                continue
            matched_foods = _meal_matches(planned_meal, requirement)
            if matched_foods:
                matches.append(
                    MealEvidence(
                        day=plan_day.day,
                        meal_type=planned_meal.meal_type,
                        meal_name=planned_meal.name,
                        matched_foods=matched_foods,
                    )
                )
    return tuple(matches)


def _eligible_meal_count(
    plan: WeeklyPlan, requirement: PreferenceRequirement
) -> int:
    """Count distinct meal slots available to a requirement's scope."""
    return sum(
        1
        for plan_day in plan.days
        for planned_meal in plan_day.meals
        if (
            requirement.meal_type is None
            or planned_meal.meal_type is requirement.meal_type
        )
    )


def _rule_count_is_satisfied(rule: DietaryRule, actual_count: int) -> bool:
    """Return whether a generalized rule accepts an observed count."""
    if rule.operator is RuleOperator.EXACTLY:
        return actual_count == rule.count
    if rule.operator is RuleOperator.AT_LEAST:
        return actual_count >= rule.count
    return actual_count <= rule.count


def _rule_count_message(rule: DietaryRule, actual_count: int) -> str:
    """Return bounded, application-owned feedback for one strict rule."""
    operator = {
        RuleOperator.EXACTLY: "exactly",
        RuleOperator.AT_LEAST: "at least",
        RuleOperator.AT_MOST: "at most",
    }[rule.operator]
    return (
        f"Rule '{rule.id}' matched {actual_count} distinct meals; "
        f"expected {operator} {rule.count}."
    )


def _weekday_rule_counts(
    rule: DietaryRule,
    matched_meals: Sequence[MealEvidence],
    plan: WeeklyPlan,
) -> tuple[tuple[Weekday, int, int], ...]:
    """Return actual and possible counts independently for named weekdays."""
    if not rule.weekdays:
        return ()
    counts: list[tuple[Weekday, int, int]] = []
    for weekday in rule.weekdays:
        actual_count = sum(
            1 for matched_meal in matched_meals if matched_meal.day == weekday
        )
        possible_slots = sum(
            1
            for plan_day in plan.days
            if plan_day.day == weekday
            for planned_meal in plan_day.meals
            if rule.meal_type is None
            or planned_meal.meal_type is rule.meal_type
        )
        possible_count = min(
            possible_slots, daily_meal_capacity(rule.meal_type)
        )
        counts.append((weekday, actual_count, possible_count))
    return tuple(counts)


def _weekday_rule_issue(
    rule: DietaryRule,
    weekday: Weekday,
    actual_count: int,
    possible_count: int,
) -> ValidationIssue:
    """Return bounded feedback for one unmet named weekday."""
    impossible = (
        rule.operator in {RuleOperator.EXACTLY, RuleOperator.AT_LEAST}
        and rule.count > possible_count
    )
    if impossible:
        message = (
            f"Rule '{rule.id}' needs {rule.count} eligible meals on "
            f"{weekday.name.title()}, but the plan contains only "
            f"{possible_count}."
        )
    else:
        operator = {
            RuleOperator.EXACTLY: "exactly",
            RuleOperator.AT_LEAST: "at least",
            RuleOperator.AT_MOST: "at most",
        }[rule.operator]
        message = (
            f"Rule '{rule.id}' matched {actual_count} distinct meals on "
            f"{weekday.name.title()}; expected {operator} {rule.count}."
        )
    return ValidationIssue(
        code="impossible_rule_count" if impossible else "strict_rule_mismatch",
        message=message,
        day=int(weekday),
        meal_type=rule.meal_type,
        rule_id=rule.id,
    )


def _completeness_issues(plan: WeeklyPlan) -> list[ValidationIssue]:
    """Return stable issues for missing or unusable daily meals."""
    issues: list[ValidationIssue] = []
    days = sorted(plan.days, key=lambda plan_day: plan_day.day)
    for plan_day in days:
        meals_by_type = {meal.meal_type: meal for meal in plan_day.meals}
        for meal_type in _REQUIRED_MEAL_TYPES:
            planned_meal = meals_by_type.get(meal_type)
            if planned_meal is None:
                issues.append(
                    ValidationIssue(
                        code="missing_meal_type",
                        message=(
                            f"Day {plan_day.day} is missing {meal_type.value}."
                        ),
                        day=plan_day.day,
                        meal_type=meal_type,
                    )
                )
                continue
            issues.extend(_meal_completeness_issues(plan_day.day, planned_meal))

        for planned_meal in sorted(
            plan_day.meals,
            key=lambda meal: _MEAL_TYPE_ORDER[meal.meal_type],
        ):
            if planned_meal.meal_type not in _REQUIRED_MEAL_TYPES:
                issues.extend(
                    _meal_completeness_issues(plan_day.day, planned_meal)
                )
    return issues


def _meal_completeness_issues(
    day: int, planned_meal: PlannedMeal
) -> list[ValidationIssue]:
    """Return ingredient and calorie issues for one present meal."""
    issues: list[ValidationIssue] = []
    if not planned_meal.ingredients:
        issues.append(
            ValidationIssue(
                code="empty_ingredients",
                message=(
                    f"Day {day} {planned_meal.meal_type.value} "
                    "has no ingredients."
                ),
                day=day,
                meal_type=planned_meal.meal_type,
            )
        )
    if planned_meal.est_calories <= 0:
        issues.append(
            ValidationIssue(
                code="nonpositive_calories",
                message=(
                    f"Day {day} {planned_meal.meal_type.value} "
                    "must have positive calories."
                ),
                day=day,
                meal_type=planned_meal.meal_type,
            )
        )
    return issues


def validate_batch_links(
    plan: WeeklyPlan,
    *,
    available_batches: Sequence[BatchLedgerEntry] | None = None,
) -> BatchValidationResult:
    """Validate batch links against the plan and issued available portions.

    A preparation in the generated plan is the only way to create a new
    source.  A leftover must either point at that preparation or at an
    available ledger entry supplied by the application.  The model never
    decides whether inventory exists or how many portions remain.
    """
    links: list[BatchLinkEvidence] = []
    issues: list[ValidationIssue] = []
    preparations: dict[str, tuple[date, MealType, int, int | None]] = {}
    grouped: dict[str, list[BatchLinkEvidence]] = {}
    inventory = list(available_batches or [])
    known_batch_ids = {entry.batch_id for entry in inventory}
    available_by_id = {
        entry.batch_id: entry
        for entry in inventory
        if entry.state is BatchLedgerState.AVAILABLE
        and entry.remaining_portions > 0
    }

    for plan_day in sorted(plan.days, key=lambda item: item.day):
        meal_date = _plan_date(plan, plan_day.day)
        for planned_meal in sorted(
            plan_day.meals,
            key=lambda item: _MEAL_TYPE_ORDER[item.meal_type],
        ):
            planned_link = planned_meal.batch_link
            if planned_link is None:
                continue
            evidence = BatchLinkEvidence(
                batch_id=planned_link.batch_id,
                role=planned_link.role,
                day=plan_day.day,
                meal_type=planned_meal.meal_type,
                portion=planned_link.portion,
                source_date=planned_link.source_date,
                source_meal_type=planned_link.source_meal_type,
            )
            links.append(evidence)
            grouped.setdefault(planned_link.batch_id, []).append(evidence)
            if planned_meal.meal_type not in {
                MealType.LUNCH,
                MealType.DINNER,
            }:
                issues.append(
                    ValidationIssue(
                        code="batch_invalid_meal_scope",
                        message="Batch meals must use lunch or dinner slots.",
                        day=plan_day.day,
                        meal_type=planned_meal.meal_type,
                        batch_id=planned_link.batch_id,
                        batch_role=planned_link.role,
                    )
                )
            if planned_link.role is BatchMealRole.PREPARATION:
                if planned_link.batch_id in preparations:
                    issues.append(
                        ValidationIssue(
                            code="batch_duplicate_source",
                            message="A batch may have only one preparation.",
                            day=plan_day.day,
                            meal_type=planned_meal.meal_type,
                            batch_id=planned_link.batch_id,
                            batch_role=planned_link.role,
                        )
                    )
                else:
                    preparations[planned_link.batch_id] = (
                        meal_date,
                        planned_meal.meal_type,
                        plan_day.day,
                        planned_link.total_yield,
                    )

    for batch_id, batch_links in grouped.items():
        source = preparations.get(batch_id)
        available = available_by_id.get(batch_id)
        leftovers = [
            link for link in batch_links if link.role is BatchMealRole.LEFTOVER
        ]
        if not leftovers:
            if source is not None and batch_id in available_by_id:
                issues.append(
                    ValidationIssue(
                        code="batch_reserved_id_collision",
                        message="A new preparation reused an issued batch ID.",
                        day=source[2],
                        meal_type=source[1],
                        batch_id=batch_id,
                        batch_role=BatchMealRole.PREPARATION,
                    )
                )
            continue
        if source is not None and available is not None:
            issues.append(
                ValidationIssue(
                    code="batch_reserved_id_collision",
                    message="A preparation reused an issued batch ID.",
                    day=source[2],
                    meal_type=source[1],
                    batch_id=batch_id,
                    batch_role=BatchMealRole.PREPARATION,
                )
            )
        if source is None and available is None:
            code = (
                "batch_unavailable_portion"
                if batch_id in known_batch_ids
                else "batch_unknown_source"
                if available_batches is not None
                else "batch_missing_source"
            )
            for link in leftovers:
                issues.append(
                    ValidationIssue(
                        code=code,
                        message="A leftover has no known preparation source.",
                        day=link.day,
                        meal_type=link.meal_type,
                        batch_id=batch_id,
                        batch_role=link.role,
                    )
                )
            continue

        if source is not None:
            source_date, source_type, _source_day, declared_yield = source
        else:
            assert available is not None
            source_date = available.preparation_date
            source_type = available.preparation_meal_type
            declared_yield = available.total_portions
        seen_portions: set[int] = set()
        for link in leftovers:
            if link.portion in seen_portions:
                issues.append(
                    ValidationIssue(
                        code="batch_duplicate_portion",
                        message="A batch portion was reused more than once.",
                        day=link.day,
                        meal_type=link.meal_type,
                        batch_id=batch_id,
                        batch_role=link.role,
                    )
                )
            seen_portions.add(link.portion)
            target_date = plan.week_start + timedelta(days=link.day - 1)
            if (
                link.source_date != source_date
                or link.source_meal_type != source_type
            ):
                issues.append(
                    ValidationIssue(
                        code="batch_wrong_source",
                        message="A leftover source mismatch.",
                        day=link.day,
                        meal_type=link.meal_type,
                        batch_id=batch_id,
                        batch_role=link.role,
                    )
                )
            if (
                link.source_date is not None
                and target_date.isocalendar()[:2]
                != link.source_date.isocalendar()[:2]
            ):
                issues.append(
                    ValidationIssue(
                        code="batch_cross_week",
                        message="A batch cannot be reused across ISO weeks.",
                        day=link.day,
                        meal_type=link.meal_type,
                        batch_id=batch_id,
                        batch_role=link.role,
                    )
                )
            if target_date <= source_date:
                issues.append(
                    ValidationIssue(
                        code="batch_reuse_before_preparation",
                        message="A leftover must follow its preparation date.",
                        day=link.day,
                        meal_type=link.meal_type,
                        batch_id=batch_id,
                        batch_role=link.role,
                    )
                )
            if target_date.isocalendar()[:2] != source_date.isocalendar()[:2]:
                issues.append(
                    ValidationIssue(
                        code="batch_cross_week",
                        message="A batch cannot be reused across ISO weeks.",
                        day=link.day,
                        meal_type=link.meal_type,
                        batch_id=batch_id,
                        batch_role=link.role,
                    )
                )

        total_yield = declared_yield or max(
            2, max((link.portion for link in leftovers), default=1)
        )
        if source is not None:
            for link in leftovers:
                if link.portion > total_yield:
                    issues.append(
                        ValidationIssue(
                            code="batch_excessive_reuse",
                            message="A leftover exceeds its batch yield.",
                            day=link.day,
                            meal_type=link.meal_type,
                            batch_id=batch_id,
                            batch_role=link.role,
                        )
                    )
        if len(leftovers) > 2 or len(leftovers) > total_yield - 1:
            issues.append(
                ValidationIssue(
                    code="batch_excessive_reuse",
                    message="A batch has more leftovers than its yield allows.",
                    batch_id=batch_id,
                    batch_role=BatchMealRole.LEFTOVER,
                )
            )
        if (
            available is not None
            and len(leftovers) > available.remaining_portions
        ):
            issues.append(
                ValidationIssue(
                    code="batch_unavailable_portion",
                    message="The available batch has insufficient portions.",
                    batch_id=batch_id,
                    batch_role=BatchMealRole.LEFTOVER,
                )
            )
        if available is not None:
            first_available_portion = (
                available.total_portions - available.remaining_portions + 1
            )
            for link in leftovers:
                if not (
                    first_available_portion
                    <= link.portion
                    <= available.total_portions
                ):
                    issues.append(
                        ValidationIssue(
                            code="batch_unavailable_portion",
                            message=(
                                "The requested batch portion is not available."
                            ),
                            day=link.day,
                            meal_type=link.meal_type,
                            batch_id=batch_id,
                            batch_role=link.role,
                        )
                    )
            ordered_leftovers = sorted(
                leftovers,
                key=lambda link: (
                    link.day,
                    _MEAL_TYPE_ORDER[link.meal_type],
                ),
            )
            actual_portions = tuple(link.portion for link in ordered_leftovers)
            expected_portions = (
                tuple(
                    range(
                        first_available_portion,
                        first_available_portion + len(ordered_leftovers),
                    )
                )
                if ordered_leftovers
                else ()
            )
            if actual_portions != expected_portions:
                first_mismatch = next(
                    (
                        link
                        for link, expected in zip(
                            ordered_leftovers,
                            expected_portions,
                            strict=False,
                        )
                        if link.portion != expected
                    ),
                    ordered_leftovers[0],
                )
                issues.append(
                    ValidationIssue(
                        code="batch_noncanonical_ordinal_order",
                        message=(
                            "Available batch portions must follow their "
                            "chronological order."
                        ),
                        day=first_mismatch.day,
                        meal_type=first_mismatch.meal_type,
                        batch_id=batch_id,
                        batch_role=BatchMealRole.LEFTOVER,
                    )
                )

    return BatchValidationResult(
        valid=not issues,
        links=tuple(links),
        issues=tuple(issues),
    )


def _batch_rule_issue(
    rule: BatchRule,
    code: str,
    *,
    day: int | None = None,
    meal_type: MealType | None = None,
) -> ValidationIssue:
    """Build bounded, application-owned feedback for one confirmed rule."""
    return ValidationIssue(
        code=code,
        message="A confirmed batch rule was not satisfied.",
        day=day,
        meal_type=meal_type,
        rule_id=rule.id,
    )


def _batch_rule_meal_matches(
    planned_meal: PlannedMeal, rule: BatchRule
) -> bool:
    """Return whether a planned batch meal's ingredients contain rule food."""
    return any(
        matches_food(ingredient.item, food)
        for ingredient in planned_meal.ingredients
        for food in rule.foods_any_of
    )


def _validate_confirmed_batch_rules(
    plan: WeeklyPlan, batch_rules: Sequence[BatchRule]
) -> list[ValidationIssue]:
    """Enforce each typed batch rule against application-owned links."""
    if not batch_rules:
        return []

    grouped: dict[str, list[tuple[int, date, PlannedMeal]]] = {}
    for plan_day in plan.days:
        meal_date = _plan_date(plan, plan_day.day)
        for planned_meal in plan_day.meals:
            link = planned_meal.batch_link
            if link is not None:
                grouped.setdefault(link.batch_id, []).append(
                    (plan_day.day, meal_date, planned_meal)
                )

    issues: list[ValidationIssue] = []
    assigned_batch_ids: set[str] = set()
    assigned_sources: set[tuple[date, MealType]] = set()
    seen_rule_ids: set[str] = set()
    preparation_groups = {
        batch_id: entries
        for batch_id, entries in grouped.items()
        if any(
            entry[2].batch_link is not None
            and entry[2].batch_link.role is BatchMealRole.PREPARATION
            for entry in entries
        )
    }

    for rule in batch_rules:
        if rule.id in seen_rule_ids:
            issues.append(_batch_rule_issue(rule, "batch_rule_duplicate_id"))
        seen_rule_ids.add(rule.id)

        matching_batches: list[str] = []
        for batch_id, entries in preparation_groups.items():
            if batch_id in assigned_batch_ids:
                continue
            if any(
                entry[2].batch_link is not None
                and entry[2].batch_link.role is BatchMealRole.PREPARATION
                and _batch_rule_meal_matches(entry[2], rule)
                for entry in entries
            ):
                matching_batches.append(batch_id)

        if not matching_batches:
            if not preparation_groups:
                issues.append(
                    _batch_rule_issue(rule, "batch_rule_missing_preparation")
                )
            elif len(preparation_groups) == 1:
                batch_id = next(iter(preparation_groups))
                preparation = next(
                    entry[2]
                    for entry in preparation_groups[batch_id]
                    if entry[2].batch_link is not None
                    and entry[2].batch_link.role is BatchMealRole.PREPARATION
                )
                issues.append(
                    _batch_rule_issue(
                        rule,
                        "batch_rule_wrong_food",
                        day=next(
                            entry[0]
                            for entry in preparation_groups[batch_id]
                            if entry[2] is preparation
                        ),
                        meal_type=preparation.meal_type,
                    )
                )
            else:
                issues.append(
                    _batch_rule_issue(rule, "batch_rule_ambiguous_match")
                )
            continue

        if len(matching_batches) != 1:
            issues.append(_batch_rule_issue(rule, "batch_rule_ambiguous_match"))
            continue

        batch_id = matching_batches[0]
        assigned_batch_ids.add(batch_id)
        entries = grouped[batch_id]
        preparations = [
            entry
            for entry in entries
            if entry[2].batch_link is not None
            and entry[2].batch_link.role is BatchMealRole.PREPARATION
        ]
        if len(preparations) != 1:
            issues.append(_batch_rule_issue(rule, "batch_rule_ambiguous_match"))
            continue

        preparation_day, preparation_date, preparation_meal = preparations[0]
        assigned_sources.add((preparation_date, preparation_meal.meal_type))
        if preparation_meal.meal_type not in rule.preparation_meal_types:
            issues.append(
                _batch_rule_issue(
                    rule,
                    "batch_rule_invalid_preparation_meal_type",
                    day=preparation_day,
                    meal_type=preparation_meal.meal_type,
                )
            )
        preparation_link = preparation_meal.batch_link
        assert preparation_link is not None
        if preparation_link.total_yield != rule.total_yield:
            issues.append(
                _batch_rule_issue(
                    rule,
                    "batch_rule_wrong_yield",
                    day=preparation_day,
                    meal_type=preparation_meal.meal_type,
                )
            )

        leftovers = [
            entry
            for entry in entries
            if entry[2].batch_link is not None
            and entry[2].batch_link.role is BatchMealRole.LEFTOVER
        ]
        ordered_leftovers = sorted(
            leftovers,
            key=lambda entry: (
                entry[1],
                _MEAL_TYPE_ORDER[entry[2].meal_type],
            ),
        )
        expected_portions = set(range(2, rule.total_yield + 1))
        seen_portions: set[int] = set()
        for day, target_date, leftover_meal in leftovers:
            leftover_link = leftover_meal.batch_link
            assert leftover_link is not None
            if leftover_link.portion in seen_portions:
                issues.append(
                    _batch_rule_issue(
                        rule,
                        "batch_rule_duplicate_leftover",
                        day=day,
                        meal_type=leftover_meal.meal_type,
                    )
                )
            seen_portions.add(leftover_link.portion)
            if leftover_meal.meal_type not in rule.reuse_meal_types:
                issues.append(
                    _batch_rule_issue(
                        rule,
                        "batch_rule_invalid_reuse_meal_type",
                        day=day,
                        meal_type=leftover_meal.meal_type,
                    )
                )
            if not _batch_rule_meal_matches(leftover_meal, rule):
                issues.append(
                    _batch_rule_issue(
                        rule,
                        "batch_rule_wrong_food",
                        day=day,
                        meal_type=leftover_meal.meal_type,
                    )
                )
            if (
                leftover_link.source_date != preparation_date
                or leftover_link.source_meal_type != preparation_meal.meal_type
            ):
                issues.append(
                    _batch_rule_issue(
                        rule,
                        "batch_rule_wrong_source",
                        day=day,
                        meal_type=leftover_meal.meal_type,
                    )
                )
            if target_date <= preparation_date:
                issues.append(
                    _batch_rule_issue(
                        rule,
                        "batch_rule_reuse_before_preparation",
                        day=day,
                        meal_type=leftover_meal.meal_type,
                    )
                )

        if len(leftovers) < len(expected_portions):
            issues.append(
                _batch_rule_issue(rule, "batch_rule_missing_leftover")
            )
        elif len(leftovers) > len(expected_portions):
            issues.append(
                _batch_rule_issue(rule, "batch_rule_wrong_leftover_count")
            )
        missing_portions = expected_portions.difference(seen_portions)
        if missing_portions and len(leftovers) >= len(expected_portions):
            issues.append(
                _batch_rule_issue(rule, "batch_rule_missing_leftover")
            )
        if seen_portions.difference(expected_portions):
            issues.append(
                _batch_rule_issue(rule, "batch_rule_wrong_leftover_ordinal")
            )
        canonical_portions = tuple(range(2, rule.total_yield + 1))
        ordered_portions = tuple(
            entry[2].batch_link.portion
            for entry in ordered_leftovers
            if entry[2].batch_link is not None
        )
        if (
            len(ordered_leftovers) == len(canonical_portions)
            and ordered_portions != canonical_portions
        ):
            first_mismatch = next(
                (
                    entry
                    for entry, expected in zip(
                        ordered_leftovers,
                        canonical_portions,
                        strict=True,
                    )
                    if entry[2].batch_link is not None
                    and entry[2].batch_link.portion != expected
                ),
                ordered_leftovers[0],
            )
            issues.append(
                _batch_rule_issue(
                    rule,
                    "batch_rule_wrong_leftover_ordinal",
                    day=first_mismatch[0],
                    meal_type=first_mismatch[2].meal_type,
                )
            )

    if len(batch_rules) > 0:
        for batch_id, entries in grouped.items():
            if batch_id in assigned_batch_ids:
                continue
            if any(
                entry[2].batch_link is not None
                and entry[2].batch_link.role is BatchMealRole.LEFTOVER
                for entry in entries
            ):
                cross_linked = any(
                    entry[2].batch_link is not None
                    and entry[2].batch_link.source_date is not None
                    and entry[2].batch_link.source_meal_type is not None
                    and (
                        entry[2].batch_link.source_date,
                        entry[2].batch_link.source_meal_type,
                    )
                    in assigned_sources
                    for entry in entries
                )
                issues.append(
                    _batch_rule_issue(batch_rules[0], "batch_rule_cross_linked")
                    if cross_linked
                    else _batch_rule_issue(
                        batch_rules[0], "batch_rule_unexpected_batch"
                    )
                )
            elif any(
                entry[2].batch_link is not None
                and entry[2].batch_link.role is BatchMealRole.PREPARATION
                for entry in entries
            ):
                issues.append(
                    _batch_rule_issue(
                        batch_rules[0], "batch_rule_unexpected_batch"
                    )
                )

    return issues


def validate_generated_plan(
    plan: WeeklyPlan,
    requirements: Sequence[PreferenceRequirement] = (),
    *,
    rules: Sequence[DietaryRule] = (),
    batch_rules: Sequence[BatchRule] = (),
    constraints: Sequence[ConstraintEntry] = (),
    obligations: Sequence[DietaryObligation] | None = None,
    available_batches: Sequence[BatchLedgerEntry] | None = None,
) -> PlanValidationResult:
    """Validate constraints, generalized rules, and plan completeness.

    ``requirements`` remains the legacy exact-count input used by existing
    planner events.  When ``obligations`` is supplied it is the authoritative
    dated snapshot and broad ``rules`` are ignored for validation.
    """
    issues: list[ValidationIssue] = []
    batch_validation = validate_batch_links(
        plan, available_batches=available_batches
    )
    issues.extend(batch_validation.issues)
    issues.extend(_validate_confirmed_batch_rules(plan, batch_rules))
    constraint_results: list[ConstraintValidation] = []
    seen_constraint_ids: set[str] = set()

    for constraint in constraints:
        expansion = expand_constraint_entry(constraint)
        matched_meals = match_constraint(plan, constraint)
        matched_terms: list[str] = []
        for matched_meal in matched_meals:
            for term in matched_meal.matched_foods:
                if term not in matched_terms:
                    matched_terms.append(term)
        constraint_result = ConstraintValidation(
            constraint_id=constraint.id,
            safe=expansion.is_safe,
            matched_terms=tuple(matched_terms),
            matched_meals=matched_meals,
            unknown_terms=expansion.unknown_terms,
        )
        constraint_results.append(constraint_result)

        if constraint.id in seen_constraint_ids:
            issues.append(
                ValidationIssue(
                    code="duplicate_constraint_id",
                    message=f"Constraint '{constraint.id}' is duplicated.",
                    constraint_id=constraint.id,
                )
            )
        seen_constraint_ids.add(constraint.id)

        if not constraint_result.safe:
            issues.append(
                ValidationIssue(
                    code="unsafe_constraint",
                    message=(
                        f"Constraint '{constraint.id}' cannot be safely "
                        "validated."
                    ),
                    constraint_id=constraint.id,
                )
            )
        for matched_meal in matched_meals:
            issues.append(
                ValidationIssue(
                    code="constraint_violation",
                    message=(
                        f"Constraint '{constraint.id}' is violated by "
                        "declared meal evidence."
                    ),
                    day=matched_meal.day,
                    meal_type=matched_meal.meal_type,
                    constraint_id=constraint.id,
                )
            )

    rule_results: list[RuleValidation] = []
    seen_rule_ids: set[str] = set()
    # A supplied snapshot is authoritative.  Legacy broad rules are retained
    # for compatibility only when no dated snapshot exists.
    rules_to_validate = () if obligations is not None else rules
    for rule in rules_to_validate:
        matched_meals = match_rule(plan, rule)
        weekday_counts = _weekday_rule_counts(rule, matched_meals, plan)
        missing_weekdays = tuple(
            weekday
            for weekday, actual_count, _ in weekday_counts
            if not _rule_count_is_satisfied(rule, actual_count)
        )
        rule_result = RuleValidation(
            rule_id=rule.id,
            foods_any_of=tuple(rule.foods_any_of),
            operator=rule.operator,
            strength=rule.strength,
            expected_count=rule.count,
            actual_count=len(matched_meals),
            possible_count=_eligible_rule_meal_count(plan, rule),
            matched_meals=matched_meals,
            weekday_counts=tuple(
                (weekday, actual_count)
                for weekday, actual_count, _ in weekday_counts
            ),
            missing_weekdays=missing_weekdays,
        )
        rule_results.append(rule_result)

        if rule.id in seen_rule_ids:
            issues.append(
                ValidationIssue(
                    code="duplicate_rule_id",
                    message=f"Rule '{rule.id}' is duplicated.",
                    rule_id=rule.id,
                )
            )
        seen_rule_ids.add(rule.id)

        if rule.strength is RuleStrength.STRICT:
            if weekday_counts:
                issues.extend(
                    _weekday_rule_issue(
                        rule, weekday, actual_count, possible_count
                    )
                    for weekday, actual_count, possible_count in weekday_counts
                    if weekday in missing_weekdays
                )
            elif not rule_result.is_satisfied:
                impossible = (
                    rule.operator
                    in {RuleOperator.EXACTLY, RuleOperator.AT_LEAST}
                    and rule.count > rule_result.possible_count
                )
                issue_code = (
                    "impossible_rule_count"
                    if impossible
                    else "strict_rule_mismatch"
                )
                issues.append(
                    ValidationIssue(
                        code=issue_code,
                        message=(
                            f"Rule '{rule.id}' needs {rule.count} eligible "
                            f"meals, but the plan contains only "
                            f"{rule_result.possible_count}."
                            if impossible
                            else _rule_count_message(
                                rule, rule_result.actual_count
                            )
                        ),
                        rule_id=rule.id,
                    )
                )

    if obligations is not None:
        seen_obligation_ids: set[str] = set()
        for obligation in obligations:
            matched_meals = match_obligation(plan, obligation)
            actual_count = len(matched_meals)
            possible_count = _obligation_possible_count(plan, obligation)
            result = RuleValidation(
                rule_id=obligation.source_rule_id,
                obligation_id=obligation.id,
                foods_any_of=tuple(obligation.foods_any_of),
                operator=obligation.operator,
                strength=obligation.strength,
                expected_count=obligation.count,
                actual_count=actual_count,
                possible_count=possible_count,
                matched_meals=matched_meals,
                eligible_dates=tuple(sorted(obligation.eligible_dates)),
                matched_dates=tuple(
                    sorted(_plan_date(plan, meal.day) for meal in matched_meals)
                ),
            )
            rule_results.append(result)

            if obligation.id in seen_obligation_ids:
                issues.append(
                    ValidationIssue(
                        code="duplicate_rule_id",
                        message="A dated obligation was duplicated.",
                        rule_id=obligation.source_rule_id,
                    )
                )
            seen_obligation_ids.add(obligation.id)

            if obligation.strength is not RuleStrength.STRICT:
                continue

            for issue_code, evidence in _obligation_out_of_scope_matches(
                plan, obligation
            ):
                issues.append(
                    ValidationIssue(
                        code=issue_code,
                        message="A dietary obligation was matched outside "
                        "its application-owned date or meal scope.",
                        day=evidence.day,
                        meal_type=evidence.meal_type,
                        rule_id=obligation.source_rule_id,
                        obligation_id=obligation.id,
                        obligation_date=(_plan_date(plan, evidence.day)),
                    )
                )

            if (
                obligation.operator
                in {
                    RuleOperator.EXACTLY,
                    RuleOperator.AT_LEAST,
                }
                and actual_count < obligation.count
            ):
                issues.append(
                    ValidationIssue(
                        code="obligation_missing",
                        message="A dated dietary obligation was not met.",
                        rule_id=obligation.source_rule_id,
                        obligation_id=obligation.id,
                        obligation_date=(
                            obligation.eligible_dates[0]
                            if obligation.eligible_dates
                            else None
                        ),
                    )
                )
            if (
                obligation.operator
                in {
                    RuleOperator.EXACTLY,
                    RuleOperator.AT_MOST,
                }
                and actual_count > obligation.count
            ):
                issues.append(
                    ValidationIssue(
                        code="obligation_excess",
                        message="A dated dietary obligation was exceeded.",
                        rule_id=obligation.source_rule_id,
                        obligation_id=obligation.id,
                        obligation_date=(
                            obligation.eligible_dates[0]
                            if obligation.eligible_dates
                            else None
                        ),
                    )
                )

    issues.extend(_completeness_issues(plan))
    requirement_results: list[RequirementValidation] = []
    seen_ids: set[str] = set()

    for requirement in requirements:
        matched_meals = match_requirement(plan, requirement)
        possible_count = _eligible_meal_count(plan, requirement)
        requirement_result = RequirementValidation(
            requirement_id=requirement.id,
            expected_count=requirement.exact_count,
            actual_count=len(matched_meals),
            possible_count=possible_count,
            matched_meals=matched_meals,
        )
        requirement_results.append(requirement_result)

        if requirement.id in seen_ids:
            issues.append(
                ValidationIssue(
                    code="duplicate_requirement_id",
                    message=f"Requirement '{requirement.id}' is duplicated.",
                    requirement_id=requirement.id,
                )
            )
        seen_ids.add(requirement.id)

        if requirement.exact_count > possible_count:
            scope = (
                requirement.meal_type.value
                if requirement.meal_type is not None
                else "eligible"
            )
            issues.append(
                ValidationIssue(
                    code="impossible_requirement_count",
                    message=(
                        f"Requirement '{requirement.id}' needs "
                        f"{requirement.exact_count} distinct {scope} meals, "
                        f"but the plan contains only {possible_count}."
                    ),
                    requirement_id=requirement.id,
                )
            )
        elif not requirement_result.is_satisfied:
            issues.append(
                ValidationIssue(
                    code="requirement_count_mismatch",
                    message=(
                        f"Requirement '{requirement.id}' matched "
                        f"{requirement_result.actual_count} distinct "
                        f"meals; expected exactly "
                        f"{requirement_result.expected_count}."
                    ),
                    requirement_id=requirement.id,
                )
            )

    return PlanValidationResult(
        valid=not issues,
        requirements=tuple(requirement_results),
        issues=tuple(issues),
        rules=tuple(rule_results),
        constraints=tuple(constraint_results),
        batches=batch_validation.links,
    )


def format_satisfaction_summary(
    validation: PlanValidationResult,
    requirements: Sequence[PreferenceRequirement] = (),
) -> str:
    """Format application-derived evidence for an accepted plan."""
    if not validation.is_valid:
        raise ValueError("cannot summarize an invalid generated plan")

    requirements_by_id = {
        requirement.id: requirement for requirement in requirements
    }
    requirement_lines: list[str] = []
    for result in validation.requirements:
        requirement = requirements_by_id.get(result.requirement_id)
        if requirement is None:
            raise ValueError(
                "validation result contains an unknown requirement"
            )
        displayed_foods = requirement.foods_any_of[:_MAX_DISPLAYED_ALTERNATIVES]
        foods = " or ".join(
            " ".join(food.split()).title() for food in displayed_foods
        )
        omitted_alternatives = len(requirement.foods_any_of) - len(
            displayed_foods
        )
        if omitted_alternatives:
            foods = f"{foods} (+{omitted_alternatives} alternatives)"
        scope = (
            requirement.meal_type.value
            if requirement.meal_type is not None
            else "meal"
        )
        scope = scope if result.actual_count == 1 else f"{scope}s"
        requirement_lines.append(f"• {foods}: {result.actual_count} {scope}")

    for rule_result in validation.rules:
        if rule_result.strength is not RuleStrength.STRICT:
            continue
        if rule_result.obligation_id:
            label = rule_result.obligation_id
            dates = ", ".join(
                target_date.isoformat()
                for target_date in rule_result.eligible_dates
            )
            requirement_lines.append(
                f"• {label} ({dates}): {rule_result.actual_count} "
                "matching meals"
            )
        else:
            label = rule_result.rule_id[:1].upper()
            label += rule_result.rule_id[1:]
            requirement_lines.append(
                f"• {label}: {rule_result.actual_count} distinct meals"
            )

    lines = ["Preferences satisfied:"]
    for index, requirement_line in enumerate(requirement_lines):
        remaining = len(requirement_lines) - index - 1
        reserved = (
            1 + len(_omitted_requirements_line(remaining)) if remaining else 0
        )
        candidate = "\n".join([*lines, requirement_line])
        if len(candidate) + reserved > MAX_REQUIREMENT_MESSAGE_LENGTH:
            lines.append(
                _omitted_requirements_line(len(requirement_lines) - index)
            )
            break
        lines.append(requirement_line)
    return "\n".join(lines)


def format_best_effort_summary(validation: PlanValidationResult) -> str:
    """Format bounded outcomes for best-effort rules without raw wording."""
    lines = ["Best-effort preferences:"]
    outcomes = [
        result
        for result in validation.rules
        if result.strength is RuleStrength.BEST_EFFORT
    ]
    if not outcomes:
        return "\n".join(lines)

    for index, result in enumerate(outcomes):
        remaining = len(outcomes) - index - 1
        label = result.obligation_id or result.rule_id
        if result.obligation_id and result.eligible_dates:
            label = f"{label} ({result.eligible_dates[0].isoformat()})"
        elif not result.obligation_id:
            label = label[:1].upper() + label[1:]
        outcome = "met" if result.is_satisfied else "not met"
        line = f"• {label}: {outcome}"
        omission = f"• ... and {remaining} best-effort rules omitted."
        candidate = "\n".join([*lines, line])
        reserved = len(omission) + 1 if remaining else 0
        if len(candidate) + reserved > MAX_REQUIREMENT_MESSAGE_LENGTH:
            lines.append(
                f"• ... and {len(outcomes) - index} best-effort rules omitted."
            )
            break
        lines.append(line)
    return "\n".join(lines)


def _omitted_requirements_line(count: int) -> str:
    """Return an application-owned requirement omission marker."""
    return f"• ... and {count} requirements omitted."


def _omitted_preference_clauses_line(count: int) -> str:
    """Return an application-owned clause omission marker."""
    return f"• ... and {count} preference clauses omitted."


def format_unmet_preference_clauses(clauses: Sequence[str]) -> str:
    """Format unmet clauses within the bounded Telegram message length."""
    prefix = (
        "The AI returned an invalid meal plan because these preference "
        "clauses were not met:\n"
    )
    suffix = (
        "\nNo draft was saved. Your preference is retained; use /plan to retry."
    )
    normalized_clauses = [" ".join(clause.split()) for clause in clauses]
    if not normalized_clauses:
        normalized_clauses = ["One or more saved preference clauses"]

    lines: list[str] = []
    for index, clause in enumerate(normalized_clauses):
        remaining = len(normalized_clauses) - index - 1
        candidate_line = f"• {clause}"
        omission_line = _omitted_preference_clauses_line(remaining)
        candidate = prefix + "\n".join([*lines, candidate_line]) + suffix
        reserved = len(omission_line) + 1 if remaining else 0
        if len(candidate) + reserved > MAX_REQUIREMENT_MESSAGE_LENGTH:
            lines.append(
                _omitted_preference_clauses_line(
                    len(normalized_clauses) - index
                )
            )
            break
        lines.append(candidate_line)
    return prefix + "\n".join(lines) + suffix
