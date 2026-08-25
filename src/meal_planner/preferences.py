"""Evidence matching and generated-plan validation for preferences."""

from dataclasses import dataclass
from typing import Sequence

from meal_planner.dietary_rules import expand_constraint_entry
from meal_planner.models.schemas import (
    ConstraintEntry,
    DietaryRule,
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


@dataclass(frozen=True, slots=True)
class PlanValidationResult:
    """Complete validation outcome for a generated weekly plan."""

    valid: bool
    requirements: tuple[RequirementValidation, ...]
    issues: tuple[ValidationIssue, ...]
    rules: tuple[RuleValidation, ...] = ()
    constraints: tuple[ConstraintValidation, ...] = ()

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
            }
        )


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


def validate_generated_plan(
    plan: WeeklyPlan,
    requirements: Sequence[PreferenceRequirement] = (),
    *,
    rules: Sequence[DietaryRule] = (),
    constraints: Sequence[ConstraintEntry] = (),
) -> PlanValidationResult:
    """Validate constraints, generalized rules, and plan completeness.

    ``requirements`` remains the legacy exact-count input used by existing
    planner events.  New callers should pass structured ``rules`` and
    ``constraints``; both forms use one distinct meal as one unit of evidence.
    """
    issues: list[ValidationIssue] = []
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
    for rule in rules:
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

    issues.extend(_completeness_issues(plan))
    requirement_results: list[RequirementValidation] = []
    seen_ids: set[str] = set()

    for requirement in requirements:
        matched_meals = match_requirement(plan, requirement)
        possible_count = _eligible_meal_count(plan, requirement)
        result = RequirementValidation(
            requirement_id=requirement.id,
            expected_count=requirement.exact_count,
            actual_count=len(matched_meals),
            possible_count=possible_count,
            matched_meals=matched_meals,
        )
        requirement_results.append(result)

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
        elif not result.is_satisfied:
            issues.append(
                ValidationIssue(
                    code="requirement_count_mismatch",
                    message=(
                        f"Requirement '{requirement.id}' matched "
                        f"{result.actual_count} distinct meals; expected "
                        f"exactly {result.expected_count}."
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
        label = rule_result.rule_id[:1].upper() + rule_result.rule_id[1:]
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
        label = result.rule_id[:1].upper() + result.rule_id[1:]
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
