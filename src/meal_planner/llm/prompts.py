"""Prompt builders and templates for LLM context assembly."""

import json
from datetime import date
from typing import Optional

from meal_planner.models.schemas import (
    ConversationState,
    MealLogEntry,
    MealOutcome,
    PreferenceRequirement,
    ProfileUpdateEntities,
    UserProfile,
    WeeklyPlan,
)


def _profile_text(profile: UserProfile | None) -> str:
    """Render the trusted household profile for planner prompts."""
    if profile is None:
        return "No user profile established yet."
    members = (
        ", ".join(
            f"{member.name} ({member.calorie_target} kcal/day)"
            for member in profile.family_members
        )
        or "None specified"
    )
    return (
        f"Family Name: {profile.name}\n"
        f"People Count: {profile.people_count}\n"
        f"Family Members: {members}\n"
        f"Allergies: {', '.join(profile.allergies) or 'None'}\n"
        f"Dietary Preferences: "
        f"{', '.join(profile.dietary_preferences) or 'None'}\n"
        f"Restrictions: {', '.join(profile.restrictions) or 'None'}\n"
        f"Goals: {', '.join(profile.goals) or 'None'}"
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
            ", ".join(
                f"{m.name} ({m.calorie_target} kcal)"
                for m in profile.family_members
            )
            or "None specified"
        )
        dietary_str = ", ".join(profile.dietary_preferences) or "None"
        allergies_str = ", ".join(profile.allergies) or "None"
        restrictions_str = ", ".join(profile.restrictions) or "None"
        goals_str = ", ".join(profile.goals) or "None"
        profile_text = (
            f"Family Name: {profile.name}\n"
            f"People Count: {profile.people_count}\n"
            f"Family Members: {members_str}\n"
            f"Allergies: {allergies_str}\n"
            f"Dietary Preferences: {dietary_str}\n"
            f"Restrictions: {restrictions_str}\n"
            f"Goals: {goals_str}"
        )

    pending_profile_text = "No pending profile updates."
    if profile_draft:
        pending_lines = []
        pending_fields = (
            "name",
            "people_count",
            "family_members",
            "allergies",
            "dietary_preferences",
            "restrictions",
            "goals",
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
                        f"{member.name} ({member.calorie_target} kcal)"
                        for member in value
                    )
                    or "None specified"
                )
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
        "member's name. Use family_members with each member's name and "
        "calorie_target, plus allergies, dietary_preferences, restrictions, "
        "and goals. Never use an individual member's name as the family "
        "name unless the user explicitly provides it. Meal dates must use "
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
    preference: str | None = None,
    requirements: list[PreferenceRequirement] | None = None,
    repair_feedback: str | None = None,
) -> str:
    """Build 7-day meal plan generation prompt."""
    profile_text = "General profile (2000 kcal/day target, 1 person)."
    if profile:
        members_str = (
            ", ".join(
                f"{m.name} ({m.calorie_target} kcal/day)"
                for m in profile.family_members
            )
            or f"1 person ({profile.name})"
        )
        dietary_str = ", ".join(profile.dietary_preferences) or "None"
        allergies_str = ", ".join(profile.allergies) or "None"
        restrictions_str = ", ".join(profile.restrictions) or "None"
        goals_str = ", ".join(profile.goals) or "None"
        profile_text = (
            f"Family Name: {profile.name}\n"
            f"Total People Count: {profile.people_count}\n"
            f"Family Members & Calorie Targets: {members_str}\n"
            f"Allergies: {allergies_str}\n"
            f"Dietary Preferences: {dietary_str}\n"
            f"Restrictions: {restrictions_str}\n"
            f"Goals: {goals_str}"
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

    requirement_text = "No interpreted measurable requirements."
    if requirements:
        requirement_lines = []
        for requirement in requirements:
            scope = (
                requirement.meal_type.value
                if requirement.meal_type
                else "any meal"
            )
            requirement_lines.append(
                f"- {requirement.id}: source_text: "
                f"{requirement.source_text}; "
                f"foods_any_of: {', '.join(requirement.foods_any_of)}; "
                f"meal_type: {scope}; "
                f"exact_count: {requirement.exact_count}"
            )
        requirement_text = "\n".join(requirement_lines)
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
        "Generate a 7-day meal plan based on the profile below.\n\n"
        "=== REQUIREMENTS ===\n"
        f"Week Start Date: {week_start}\n"
        "Include at most four meals per day.\n"
        f"Profile & Constraints:\n{profile_text}\n\n"
        "=== REQUEST-SPECIFIC PREFERENCE (HIGH PRIORITY) ===\n"
        f"{preference.strip() if preference else 'No additional preference.'}\n"
        "Use this request preference when compatible with the permanent "
        "profile constraints below. Allergies, restrictions, calorie "
        "targets, and safety requirements always take precedence.\n\n"
        "=== INTERPRETED PREFERENCE RULES (EXACT COMPLIANCE) ===\n"
        f"{requirement_text}\n"
        "Treat each exact_count as an exact weekly count. Do not omit, "
        "weaken, or reinterpret any listed rule. Permanent profile "
        "constraints remain higher priority.\n\n"
        "=== GENERATED PLAN CONTRACT ===\n"
        "Include exactly one breakfast, one lunch, and one dinner on each "
        "of the 7 days. Snack is optional, and do not add other meal "
        "types. Every present meal must include at least one non-empty "
        "ingredient item and positive est_calories.\n\n"
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


def build_preference_interpretation_prompt(preference: str) -> str:
    """Build a prompt for interpreting measurable plan preferences."""
    return (
        "You interpret a user's request-specific meal-plan preference into "
        "safe, measurable weekly rules. Do not generate a meal plan.\n\n"
        "=== USER PREFERENCE ===\n"
        f"{preference.strip()}\n\n"
        "=== INTERPRETATION RULES ===\n"
        "Return one requirement for each meaningful, supported clause. "
        "Every meaningful clause must be represented by a requirement or "
        "listed in unparsed_text; never silently discard a clause.\n"
        "Each requirement must express a positive exact weekly count. "
        "Combine alternative foods that satisfy one rule together in "
        "foods_any_of; "
        "alternatives count as one union, not separate requirements.\n"
        "Use meal_type only when the user names breakfast, lunch, dinner, "
        "or snack. Omit the scope with null when the rule applies to any "
        "meal.\n"
        "Use source_text for the user's bounded clause. Do not invent a "
        "count, food, scope, or interpretation that the user did not give.\n"
        "If wording is ambiguous, conflicting, impossible to count, or "
        "unsupported subjective wording, return a focused clarification. "
        "Unsupported or subjective requests such as 'make it healthy and "
        "fun' must not be guessed. A clarification is required whenever "
        "any clause remains unresolved.\n\n"
        "=== OUTPUT JSON SCHEMA ===\n"
        "Return only one JSON object with all three keys:\n"
        "{\n"
        '  "requirements": [\n'
        "    {\n"
        '      "id": "r1",\n'
        '      "source_text": "eggs three times for breakfast",\n'
        '      "foods_any_of": ["eggs"],\n'
        '      "meal_type": "breakfast",\n'
        '      "exact_count": 3\n'
        "    }\n"
        "  ],\n"
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
    week_start: str | None = None,
) -> str:
    """Build a complete-plan replacement prompt for a draft revision."""
    target_week = week_start or current_plan.week_start_date
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
    return (
        "You are an expert nutritionist revising an existing family meal "
        "plan. Return a complete replacement for the current seven-day "
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
        "unaffected choices. Permanent allergies, dietary restrictions, "
        "calorie targets, and safety rules take precedence over every "
        "request-specific instruction. Use the same week and return all "
        f"seven days for week start {target_week}. Do not return a patch.\n\n"
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
