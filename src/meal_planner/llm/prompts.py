"""Prompt builders and templates for LLM context assembly."""

from datetime import date
from typing import Optional

from meal_planner.models.schemas import (
    ConversationState,
    MealLogEntry,
    MealOutcome,
    ProfileUpdateEntities,
    UserProfile,
    WeeklyPlan,
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
        "'confirm_plan', 'suggestion', 'chitchat']\n"
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
    )


def build_plan_prompt(
    profile: Optional[UserProfile] = None,
    meal_history: Optional[list[MealLogEntry]] = None,
    previous_plan: Optional[WeeklyPlan] = None,
    week_start: str = "2026-08-10",
    preference: str | None = None,
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
