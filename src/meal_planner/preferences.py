"""Evidence matching and generated-plan validation for preferences."""

from dataclasses import dataclass
from typing import Sequence

from meal_planner.models.schemas import (
    MealType,
    PlannedMeal,
    PreferenceRequirement,
    WeeklyPlan,
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


@dataclass(frozen=True, slots=True)
class PlanValidationResult:
    """Complete validation outcome for a generated weekly plan."""

    valid: bool
    requirements: tuple[RequirementValidation, ...]
    issues: tuple[ValidationIssue, ...]

    @property
    def is_valid(self) -> bool:
        """Return whether completeness and all preference rules pass."""
        return self.valid


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
) -> PlanValidationResult:
    """Validate generated-plan completeness and exact preference evidence."""
    issues = _completeness_issues(plan)
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
    )


def format_satisfaction_summary(
    validation: PlanValidationResult,
    requirements: Sequence[PreferenceRequirement],
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
