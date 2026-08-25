"""Prompt builders and templates for LLM context assembly."""

import json
from datetime import date, timedelta
from typing import Literal, Optional, Sequence

from meal_planner.models.schemas import (
    ConstraintEntry,
    ConversationState,
    DietaryPreferenceEntry,
    DietaryRule,
    FamilyMember,
    MealLogEntry,
    MealOutcome,
    PreferenceRequirement,
    ProfileUpdateEntities,
    UserProfile,
    WeeklyPlan,
)

InterpretationMode = Literal[
    "constraint", "stored_preference", "current_plan_preference"
]


def _render_member_targets(
    member: FamilyMember, calorie_unit: str = "kcal"
) -> str:
    """Render one member's calorie and optional nutrient targets."""
    protein_target = (
        f"{member.protein_target} g/day"
        if member.protein_target is not None
        else "not set"
    )
    fibre_target = (
        f"{member.fibre_target} g/day"
        if member.fibre_target is not None
        else "not set"
    )
    return (
        f"{member.name} ({member.calorie_target} {calorie_unit}) "
        f"[protein target: {protein_target}; fibre target: {fibre_target}]"
    )


def _render_constraint_entries(entries: list[ConstraintEntry]) -> str:
    """Render saved constraint wording without exposing storage details."""
    return ", ".join(entry.source_text for entry in entries) or "None"


def _render_preference_entries(
    entries: list[DietaryPreferenceEntry],
) -> str:
    """Render saved preference wording without losing raw user language."""
    return ", ".join(entry.source_text for entry in entries) or "None"


def _render_rule(rule: DietaryRule | PreferenceRequirement) -> str:
    """Render one structured or legacy rule with bounded detail."""
    scope = rule.meal_type.value if rule.meal_type else "any meal"
    if isinstance(rule, DietaryRule):
        weekdays = ", ".join(str(day.value) for day in rule.weekdays)
        return (
            f"- {rule.id}: source_text: {rule.source_text}; "
            f"foods_any_of: {', '.join(rule.foods_any_of)}; "
            f"meal_type: {scope}; operator: {rule.operator.value}; "
            f"count: {rule.count}; strength: {rule.strength.value}; "
            f"weekdays: {weekdays or 'all days'}"
        )
    return (
        f"- {rule.id}: source_text: {rule.source_text}; "
        f"foods_any_of: {', '.join(rule.foods_any_of)}; "
        f"meal_type: {scope}; exact_count: {rule.exact_count}"
    )


def _render_rules(
    rules: Sequence[DietaryRule | PreferenceRequirement],
) -> str:
    """Render rules in stable order, or a bounded empty marker."""
    return "\n".join(_render_rule(rule) for rule in rules) or "None."


def _render_constraint_rules(entries: Sequence[ConstraintEntry]) -> str:
    """Render independent constraints for the planner prompt."""
    if not entries:
        return "None."
    return "\n".join(
        f"- {entry.id}: {entry.source_text}; forbidden_terms: "
        f"{', '.join(entry.forbidden_terms)}"
        for entry in entries
    )


def _profile_text(profile: UserProfile | None) -> str:
    """Render the trusted household profile for planner prompts."""
    if profile is None:
        return "No user profile established yet."
    members = (
        ", ".join(
            _render_member_targets(member, "kcal/day")
            for member in profile.family_members
        )
        or "None specified"
    )
    return (
        f"Family Name: {profile.name}\n"
        f"People Count: {profile.people_count}\n"
        f"Family Members: {members}\n"
        f"Dietary constraints: "
        f"{_render_constraint_entries(profile.dietary_constraints)}\n"
        f"Dietary Preferences: "
        f"{_render_preference_entries(profile.dietary_preferences)}"
    )


def build_conversational_prompt(
    profile: Optional[UserProfile] = None,
    profile_draft: Optional[ProfileUpdateEntities] = None,
    current_plan: Optional[WeeklyPlan] = None,
    recent_meals: Optional[list[MealLogEntry]] = None,
    conversation_state: Optional[ConversationState] = None,
    current_date: date | None = None,
) -> str:
    """Build conversational system prompt with user context."""
    profile_text = "No user profile established yet."
    if profile:
        members_str = (
            ", ".join(_render_member_targets(m) for m in profile.family_members)
            or "None specified"
        )
        dietary_str = _render_preference_entries(profile.dietary_preferences)
        dietary_constraints_str = _render_constraint_entries(
            profile.dietary_constraints
        )
        profile_text = (
            f"Family Name: {profile.name}\n"
            f"People Count: {profile.people_count}\n"
            f"Family Members: {members_str}\n"
            f"Dietary constraints: {dietary_constraints_str}\n"
            f"Dietary Preferences: {dietary_str}"
        )

    pending_profile_text = "No pending profile updates."
    if profile_draft:
        pending_lines = []
        pending_fields = (
            "name",
            "people_count",
            "family_members",
            "dietary_constraints",
            "dietary_preferences",
        )
        for field in pending_fields:
            value = getattr(profile_draft, field)
            label = (
                "Family Name"
                if field == "name"
                else field.replace("_", " ").title()
            )
            if value is None:
                rendered = "Missing"
            elif field == "family_members":
                rendered = (
                    ", ".join(
                        _render_member_targets(member) for member in value
                    )
                    or "None specified"
                )
            elif field == "dietary_constraints":
                rendered = _render_constraint_entries(value)
            elif field == "dietary_preferences":
                rendered = _render_preference_entries(value)
            elif isinstance(value, list):
                rendered = ", ".join(value) or "None specified"
            else:
                rendered = str(value)
            pending_lines.append(f"{label}: {rendered}")
        pending_profile_text = "\n".join(sorted(pending_lines))

    plan_text = "No active meal plan."
    if current_plan:
        days_summary = []
        for day in current_plan.days:
            meals_str = ", ".join(
                f"{m.meal_type.value}: {m.name}" for m in day.meals
            )
            days_summary.append(f"Day {day.day}: {meals_str}")
        plan_text = f"Status: {current_plan.status.value}\n" + "\n".join(
            days_summary
        )

    history_text = "No recent meal history logged."
    if recent_meals:
        history_lines = [
            f"- [{m.date.isoformat()}] {m.meal_type.value}: {m.description}"
            for m in recent_meals
        ]
        history_text = "\n".join(history_lines)

    workflow_text = "No pending workflow."
    if conversation_state:
        workflow_text = (
            f"Workflow: {conversation_state.workflow_kind.value}\n"
            f"Step: {conversation_state.step.value}"
        )
        if conversation_state.meal_draft:
            draft = conversation_state.meal_draft
            workflow_text += (
                "\nPending meal fields (unknown values are omitted):"
                f"\ndate: {draft.date.isoformat() if draft.date else 'missing'}"
                "\nmeal_type: "
                f"{draft.meal_type.value if draft.meal_type else 'missing'}"
                f"\ndescription: {draft.description or 'missing'}"
            )
    today_text = (current_date or date.today()).isoformat()

    return (
        "You are an intelligent family meal planning assistant.\n\n"
        "=== USER CONTEXT ===\n"
        f"--- Saved Profile ---\n{profile_text}\n\n"
        f"--- Pending Profile Updates ---\n{pending_profile_text}\n\n"
        f"--- Current Plan ---\n{plan_text}\n\n"
        f"--- Recent Meal History ---\n{history_text}\n\n"
        f"--- Pending Workflow ---\n{workflow_text}\n"
        "=== INSTRUCTIONS ===\n"
        "1. Respond conversationally to the user's message.\n"
        "2. At the end of your response, append a JSON block "
        "enclosed in ```json ... ``` with keys:\n"
        "   - 'intent': One of ['log_meal', 'edit_plan', 'update_profile', "
        "'confirm_plan', 'revise_plan', 'suggestion', 'chitchat']\n"
        "   - 'entities': Key-value details relevant to intent. Profile "
        "updates may include 'name', 'people_count', and 'family_members'. "
        "The top-level 'name' field means the household's family "
        "name, and must be collected separately from each individual "
        "member's name. Use family_members with one object per person. "
        "Each object must include the member's name and calorie_target, "
        "and may include protein_target and fibre_target as integer "
        "grams/day only when the user explicitly provides them. Keep "
        "each optional target on the correct member, omit absent optional "
        "fields, and never invent, infer, or default their values. Protein "
        "and fibre targets are optional and must never be treated as "
        "profile completion or planning prerequisites. Also extract "
        "dietary_constraints and dietary_preferences. Never use "
        "an individual member's name as the family name unless the user "
        "explicitly provides it. Meal dates must use "
        "YYYY-MM-DD. Today's date is "
        f"{today_text}. For a pending meal workflow, extract only fields "
        "explicitly present in the user's message; never invent a date, "
        "meal type, or description. Valid meal types are breakfast, lunch, "
        "dinner, and snack.\n"
        "3. When the current plan is an eligible draft, any request to "
        "change the whole plan or apply an aggregate rule is revise_plan. "
        "For revise_plan return only {'amendment': '<faithful request>'}; "
        "do not invent days, meals, or patch entities. Keep the user's "
        "natural-language amendment verbatim except for surrounding "
        "whitespace.\n"
        "4. Use confirm_plan only when the user asks to accept the current "
        "draft. Keep edit_plan for one targeted day and meal on an active "
        "confirmed plan, with entities day, meal_type, and requested meal "
        "fields.\n"
    )


def build_plan_prompt(
    profile: Optional[UserProfile] = None,
    meal_history: Optional[list[MealLogEntry]] = None,
    previous_plan: Optional[WeeklyPlan] = None,
    week_start: str = "2026-08-10",
    plan_days: int = 7,
    preference: str | None = None,
    requirements: Sequence[PreferenceRequirement | DietaryRule] | None = None,
    stored_rules: Sequence[DietaryRule] | None = None,
    current_rules: Sequence[DietaryRule] | None = None,
    effective_rules: Sequence[DietaryRule] | None = None,
    constraint_rules: Sequence[ConstraintEntry] | None = None,
    constraints: Sequence[ConstraintEntry] | None = None,
    repair_feedback: str | None = None,
) -> str:
    """Build a meal-plan generation prompt for the requested duration."""
    end_date = date.fromisoformat(week_start) + timedelta(days=plan_days - 1)
    day_sequence = ", ".join(str(day) for day in range(1, plan_days + 1))
    profile_text = "General profile (2000 kcal/day target, 1 person)."
    if profile:
        members_str = (
            ", ".join(
                _render_member_targets(m, "kcal/day")
                for m in profile.family_members
            )
            or f"1 person ({profile.name})"
        )
        dietary_str = _render_preference_entries(profile.dietary_preferences)
        dietary_constraints_str = _render_constraint_entries(
            profile.dietary_constraints
        )
        profile_text = (
            f"Family Name: {profile.name}\n"
            f"Total People Count: {profile.people_count}\n"
            f"Family Members & Calorie Targets: {members_str}\n"
            f"Dietary constraints: {dietary_constraints_str}\n"
            f"Dietary Preferences: {dietary_str}"
        )

    history_text = "None."
    if meal_history:
        history_lines = [
            f"- [{m.date.isoformat()}] {m.meal_type.value}: {m.description}"
            for m in meal_history
        ]
        history_text = "\n".join(history_lines)

    prev_plan_text = "None."
    if previous_plan:
        cooked_meals = []
        skipped_meals = []
        swapped_meals = []
        for day in previous_plan.days:
            for meal in day.meals:
                if meal.outcome is MealOutcome.COOKED:
                    cooked_meals.append(meal.name)
                elif meal.outcome is MealOutcome.SKIPPED:
                    skipped_meals.append(meal.name)
                elif meal.outcome is MealOutcome.SWAPPED:
                    swapped_meals.append(meal.name)
        prev_plan_text = (
            f"Cooked: {', '.join(cooked_meals) or 'None'}\n"
            f"Skipped: {', '.join(skipped_meals) or 'None'}\n"
            f"Swapped: {', '.join(swapped_meals) or 'None'}"
        )

    effective = list(effective_rules or requirements or [])
    strict_rules = [
        rule
        for rule in effective
        if not isinstance(rule, DietaryRule) or rule.strength.value == "strict"
    ]
    best_effort_rules = [
        rule
        for rule in effective
        if isinstance(rule, DietaryRule)
        and rule.strength.value == "best_effort"
    ]
    requirement_text = _render_rules(effective)
    stored_text = _render_rules(stored_rules or [])
    current_text = preference.strip() if preference else "None."
    selected_constraints = list(
        constraint_rules if constraint_rules is not None else constraints or []
    )
    repair_text = ""
    if repair_feedback:
        repair_text = (
            "=== BOUNDED REPAIR FEEDBACK ===\n"
            f"{repair_feedback.strip()[:800]}\n\n"
            "Correct the feedback while preserving all permanent profile "
            "constraints and the exact rules above.\n\n"
        )

    return (
        "You are an expert nutritionist and meal planner.\n"
        f"Generate a {plan_days}-day meal plan based on the profile below.\n\n"
        "=== REQUIREMENTS ===\n"
        f"Week Start Date: {week_start}\n"
        f"Inclusive Plan Date Range: {week_start} through "
        f"{end_date.isoformat()}\n"
        "Include at most four meals per day.\n"
        f"Profile & Constraints:\n{profile_text}\n\n"
        "=== REQUEST-SPECIFIC PREFERENCE (HIGH PRIORITY) ===\n"
        f"{preference.strip() if preference else 'No additional preference.'}\n"
        "Use this request preference when compatible with the permanent "
        "profile constraints below. Dietary constraints and safety "
        "requirements always take precedence over calorie, protein, "
        "fibre, and request-specific preferences.\n\n"
        "=== STORED PREFERENCE WORDING (LOWER PRIORITY) ===\n"
        f"{stored_text}\n\n"
        "=== CURRENT PLAN PREFERENCE (HIGHER THAN STORED) ===\n"
        f"{current_text}\n\n"
        "=== DIETARY CONSTRAINTS (HIGHEST PRIORITY) ===\n"
        f"{_render_constraint_rules(selected_constraints)}\n"
        "Never weaken, replace, or reinterpret these constraints.\n\n"
        "=== EFFECTIVE STRICT RULES ===\n"
        f"{_render_rules(strict_rules)}\n"
        "Every strict rule must be satisfied by the generated plan.\n\n"
        "=== EFFECTIVE BEST-EFFORT RULES ===\n"
        f"{_render_rules(best_effort_rules)}\n"
        "Best-effort rules may be omitted when they conflict with a higher "
        "priority rule.\n\n"
        "=== PER-MEMBER NUTRITION TARGET GUIDANCE ===\n"
        "Use every supplied per-member calorie, protein, and fibre target "
        "to guide meal choices and portions. This is best-effort guidance; "
        "do not invent missing targets. The application does not calculate, "
        "validate, detect, or repair target compliance. Keep the returned "
        "plan JSON schema unchanged; do not add nutrient totals or "
        "per-member portions.\n\n"
        "=== INTERPRETED PREFERENCE RULES (EXACT COMPLIANCE) ===\n"
        f"{requirement_text}\n"
        "Treat each structured rule as an application-owned obligation. "
        "Do not omit, "
        "weaken, or reinterpret any listed rule. Permanent profile "
        "constraints remain higher priority.\n\n"
        "=== GENERATED PLAN CONTRACT ===\n"
        "Include exactly one breakfast, one lunch, and one dinner on each "
        f"of the {plan_days} days. Snack is optional, and do not add other "
        "meal "
        "types. Every present meal must include at least one non-empty "
        "ingredient item and positive est_calories.\n\n"
        f"The days array must contain exactly {plan_days} consecutive entries "
        f"with day numbers: {day_sequence}.\n\n"
        f"{repair_text}"
        f"Recent Meal History (avoid repeating):\n{history_text}\n\n"
        f"Previous Plan Feedback:\n{prev_plan_text}\n\n"
        "=== OUTPUT JSON SCHEMA ===\n"
        "Return strictly valid JSON matching this schema:\n"
        "{\n"
        '  "week_start_date": "YYYY-MM-DD",\n'
        '  "status": "draft",\n'
        '  "days": [\n'
        "    {\n"
        '      "day": 1,\n'
        '      "meals": [\n'
        "        {\n"
        '          "meal_type": "breakfast|lunch|dinner|snack",\n'
        '          "name": "Meal Name",\n'
        '          "ingredients": [\n'
        '            {"item": "Ingredient", "amount": "Quantity"}\n'
        "          ],\n"
        '          "est_calories": 500,\n'
        '          "outcome": "unreported"\n'
        "        }\n"
        "      ]\n"
        "    }\n"
        "  ]\n"
        "}\n"
    )


def build_preference_interpretation_prompt(
    preference: str,
    *,
    mode: InterpretationMode = "current_plan_preference",
) -> str:
    """Build a prompt for interpreting one bounded dietary-rule mode."""
    mode_description = {
        "constraint": (
            "a non-negotiable dietary constraint. Emit forbidden food terms "
            "in exclusions and do not emit positive requirements."
        ),
        "stored_preference": (
            "a persistent stored dietary preference whose wording determines "
            "strictness."
        ),
        "current_plan_preference": (
            "a current plan preference that may override only conflicting "
            "stored preferences."
        ),
    }[mode]
    return (
        "You interpret one dietary request into the shared, typed rule "
        "contract. Do not generate a meal plan.\n\n"
        f"Interpretation mode: {mode_description}\n"
        "=== USER PREFERENCE ===\n"
        f"{preference.strip()}\n\n"
        "=== INTERPRETATION RULES ===\n"
        "Return one requirement for each meaningful, supported clause. "
        "Every meaningful clause must be represented by a requirement or "
        "listed in unparsed_text; never silently discard a clause.\n"
        "For preference modes, each requirement must use operator exactly, "
        "at_least, or at_most and a non-negative count. Zero is valid for "
        "an explicit exclusion such as 'no eggs this week'. Use strict by "
        "default.\n"
        "The legacy exact_count field is accepted only for compatibility; "
        "new responses must use operator and count.\n"
        "The phrase 'I'd like' means strict at_least 1 when no count is "
        "provided; it is never prompt-only guidance. Phrases such as 'if "
        "convenient', 'if possible', or 'when practical' mean best_effort. "
        "Do not invent a count, food, scope, weekday, or strength.\n"
        "Combine alternative foods that satisfy one rule together in "
        "foods_any_of; "
        "alternatives count as one union, not separate requirements.\n"
        "Use meal_type only when the user names breakfast, lunch, dinner, "
        "or snack. Omit the scope with null when the rule applies to any "
        "meal.\n"
        "Use source_text for the user's bounded clause. Do not invent a "
        "count, food, scope, or interpretation that the user did not give. "
        "Represent weekdays as ISO numbers Monday=1 through Sunday=7.\n"
        "In constraint mode, use exclusions with normalized forbidden_terms "
        "and preserve the source_text. Unknown or unmatchable constraint "
        "terms must be placed in unparsed_text for clarification.\n"
        "If wording is ambiguous, conflicting, impossible to count, or "
        "unsupported subjective wording, return a focused clarification. "
        "Unsupported or subjective requests such as 'make it healthy and "
        "fun' must not be guessed. A clarification is required whenever "
        "any clause remains unresolved.\n\n"
        "=== OUTPUT JSON SCHEMA ===\n"
        "Return only one JSON object with all three keys:\n"
        "{\n"
        f'  "mode": "{mode}",\n'
        '  "requirements": [\n'
        "    {\n"
        '      "id": "r1",\n'
        '      "source_text": "eggs three times for breakfast",\n'
        '      "foods_any_of": ["eggs"],\n'
        '      "meal_type": "breakfast",\n'
        '      "weekdays": [1, 3],\n'
        '      "operator": "at_least",\n'
        '      "count": 3,\n'
        '      "strength": "strict"\n'
        "    }\n"
        "  ],\n"
        '  "exclusions": [],\n'
        '  "clarification": null,\n'
        '  "unparsed_text": []\n'
        "}\n"
        "Set clarification to one focused question when the request is "
        "ambiguous or conflicting. Put every unresolved clause in "
        "unparsed_text, even when a clarification question is also given."
    )


def build_grocery_prompt(
    plan: WeeklyPlan,
    people_count: int = 1,
) -> str:
    """Build grocery list prompt from confirmed 7-day plan."""
    plan_details = []
    for day in plan.days:
        for meal in day.meals:
            ing_parts = [f"{ing.amount} {ing.item}" for ing in meal.ingredients]
            ing_str = ", ".join(ing_parts) or "No ingredients listed"
            plan_details.append(
                f"- Day {day.day} {meal.meal_type.value} "
                f"({meal.name}): {ing_str}"
            )

    plan_text = "\n".join(plan_details)

    return (
        "You are an organized grocery manager.\n"
        "Generate a consolidated grocery list grouped by supermarket\n"
        "sections from the meal plan below.\n\n"
        f"Scale quantities for {people_count} people.\n\n"
        "=== MEAL PLAN INGREDIENTS ===\n"
        f"{plan_text}\n\n"
        "=== OUTPUT JSON SCHEMA ===\n"
        "Return strictly valid JSON matching this schema:\n"
        "{\n"
        '  "sections": [\n'
        "    {\n"
        '      "name": "Section Name (e.g. Produce, Dairy, Pantry)",\n'
        '      "items": ["Ingredient item with total quantity"]\n'
        "    }\n"
        "  ]\n"
        "}\n"
    )


def build_plan_revision_prompt(
    profile: UserProfile,
    current_plan: WeeklyPlan,
    amendment: str,
    *,
    expected_plan_days: int | None = None,
    week_start: str | None = None,
) -> str:
    """Build a complete-plan replacement prompt for a draft revision."""
    plan_days = (
        len(current_plan.days)
        if expected_plan_days is None
        else expected_plan_days
    )
    target_week = week_start or current_plan.week_start_date
    target_week_end = current_plan.week_start + timedelta(days=plan_days - 1)
    plan_json = json.dumps(
        current_plan.model_dump(by_alias=True, mode="json"),
        indent=2,
        sort_keys=True,
    )
    instructions = (
        "\n".join(
            f"{index}. {instruction}"
            for index, instruction in enumerate(
                current_plan.planning_instructions, start=1
            )
        )
        or "None."
    )
    legacy_seven_day_text = " (seven days)" if plan_days == 7 else ""
    return (
        "You are an expert nutritionist revising an existing family meal "
        f"plan. Return a complete replacement for the current {plan_days}-day "
        "draft.\n\n"
        "=== TRUSTED HOUSEHOLD PROFILE AND SAFETY CONSTRAINTS ===\n"
        f"{_profile_text(profile)}\n\n"
        "=== TRUSTED CURRENT DRAFT (COMPLETE JSON) ===\n"
        f"{plan_json}\n\n"
        "=== TRUSTED PLAN-SPECIFIC INSTRUCTIONS, IN ORDER ===\n"
        f"{instructions}\n\n"
        "=== LATEST USER AMENDMENT (HIGHEST REQUEST PRIORITY) ===\n"
        f"{amendment}\n\n"
        "Satisfy all compatible instructions and preserve sensible "
        "unaffected choices. Use every supplied per-member calorie, "
        "protein, and fibre target to guide meal choices and portions. "
        "This is best-effort guidance; do not invent missing targets. "
        "Permanent dietary constraints and safety rules take precedence "
        "over target adjustment and every request-specific instruction. "
        "The application does not calculate, validate, detect, or repair "
        "target compliance. Keep the returned plan JSON schema unchanged; "
        "do not add nutrient totals or per-member portions. Use the same "
        f"week and return all {plan_days} days{legacy_seven_day_text} "
        f"for the inclusive date range from {target_week} through "
        f"{target_week_end.isoformat()}. Return days 1 through {plan_days} "
        "in order. Do not return a patch.\n\n"
        "=== OUTPUT JSON SCHEMA ===\n"
        "Return strictly valid JSON matching this schema:\n"
        "{\n"
        '  "week_start_date": "YYYY-MM-DD",\n'
        '  "status": "draft",\n'
        '  "days": [\n'
        "    {\n"
        '      "day": 1,\n'
        '      "meals": [\n'
        "        {\n"
        '          "meal_type": "breakfast|lunch|dinner|snack",\n'
        '          "name": "Meal Name",\n'
        '          "ingredients": [{"item": "Ingredient", '
        '"amount": "Quantity"}],\n'
        '          "est_calories": 500,\n'
        '          "outcome": "unreported"\n'
        "        }\n"
        "      ]\n"
        "    }\n"
        "  ]\n"
        "}\n"
    )
