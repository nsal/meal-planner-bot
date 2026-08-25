"""Planner generation and grocery-finalization workflow tests."""

import json
import logging
import signal
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from botocore.config import Config

from meal_planner.db.dynamo import RepairPublicationOutcome
from meal_planner.llm.client import (
    LLMPermanentError,
    LLMResponseFormatError,
    LLMTimeoutError,
    LLMTransientError,
)
from meal_planner.models.schemas import (
    ConstraintEntry,
    ConversationState,
    ConversationWorkflowKind,
    ConversationWorkflowStep,
    DietaryRule,
    GroceryStatus,
    MealOutcome,
    MealType,
    PlanGenerationContext,
    PlanRevisionContext,
    PlanStatus,
    PreferenceRequirement,
    RuleStrength,
    Weekday,
)
from meal_planner.planner_handler import (
    PlannerDeadlineExceeded,
    PlannerHandler,
    lambda_handler,
    planner_deadline,
)
from meal_planner.preferences import (
    PlanValidationResult,
    ValidationIssue,
)
from meal_planner.telegram.api import split_text
from tests.factories import make_plan, make_plan_payload, make_profile


def _revision_state(
    week: date,
    *,
    request_id: str = "revision-1",
    revision: int = 0,
    expected_plan_revision: int = 4,
) -> ConversationState:
    now = datetime.now(timezone.utc)
    return ConversationState(
        workflow_kind=ConversationWorkflowKind.PLAN_REVISION,
        step=ConversationWorkflowStep.GENERATING,
        amendment="Avoid cauliflower",
        target_week=week,
        expected_plan_revision=expected_plan_revision,
        request_id=request_id,
        revision=revision,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
    )


def _plan_request_state(
    week: date,
    *,
    request_id: str = "plan-1",
    revision: int = 0,
) -> ConversationState:
    now = datetime.now(timezone.utc)
    return ConversationState(
        workflow_kind=ConversationWorkflowKind.PLAN_REQUEST,
        step=ConversationWorkflowStep.GENERATING,
        preference="low sodium",
        request_id=request_id,
        revision=revision,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
    )


def _complete_plan_payload(week: date) -> dict[str, Any]:
    """Return a generated-plan payload that meets completeness rules."""
    payload = make_plan_payload(week)
    for plan_day in payload["days"]:
        day = plan_day["day"]
        plan_day["meals"] = [
            {
                "meal_type": "breakfast",
                "name": f"Oats breakfast {day}",
                "ingredients": [{"item": "oats"}],
                "est_calories": 400,
            },
            {
                "meal_type": "lunch",
                "name": f"Bean lunch {day}",
                "ingredients": [{"item": "beans"}],
                "est_calories": 500,
            },
            {
                "meal_type": "dinner",
                "name": f"Rice dinner {day}",
                "ingredients": [{"item": "rice"}],
                "est_calories": 600,
            },
        ]
    return payload


def _preference_requirement(
    foods: list[str],
    exact_count: int,
    meal_type: MealType = MealType.BREAKFAST,
) -> PreferenceRequirement:
    """Build a concise requirement for planner workflow tests."""
    return PreferenceRequirement(
        id="r1",
        source_text=" or ".join(foods),
        foods_any_of=foods,
        meal_type=meal_type,
        exact_count=exact_count,
    )


def _dietary_constraint(
    term: str = "peanuts",
    *,
    identifier: str = "constraint-1",
) -> ConstraintEntry:
    """Build one deterministic constraint for planner safety tests."""
    return ConstraintEntry(
        id=identifier,
        source_text=f"Avoid {term}",
        forbidden_terms=[term],
    )


def _dietary_rule(
    *,
    identifier: str = "rule-1",
    strength: RuleStrength = RuleStrength.STRICT,
) -> DietaryRule:
    """Build one structured egg rule for planner validation tests."""
    return DietaryRule(
        id=identifier,
        source_text="Egg breakfasts",
        foods_any_of=["egg"],
        meal_type=MealType.BREAKFAST,
        count=1,
        strength=strength,
    )


def test_generate_plan_saves_draft_without_groceries(mocker: Any) -> None:
    repo = mocker.MagicMock()
    events: list[str] = []
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    week = date(2026, 8, 10)
    llm.chat_json_sync.return_value = _complete_plan_payload(week)
    repo.save_generated_draft.side_effect = lambda *_args, **_kwargs: (
        events.append("persist") or True
    )
    api.send_plan.side_effect = lambda *_args: events.append("send_plan")
    api.send_message.side_effect = lambda *_args: events.append("send_message")
    PlannerHandler(repo, api, llm).generate_plan("user", 1, week_start=week)
    saved = repo.save_generated_draft.call_args.args[1]
    assert saved.status is PlanStatus.DRAFT
    assert saved.grocery_status is GroceryStatus.NOT_REQUESTED
    assert saved.grocery_list == []
    repo.save_generated_draft.assert_called_once_with(
        "user", saved, expected_revision=None
    )
    api.send_plan.assert_called_once()
    api.send_message.assert_called_once_with(
        1, "Review this draft, request edits, then tell me to confirm it."
    )
    assert events == ["persist", "send_plan", "send_message"]


def test_generate_plan_saves_compliant_draft_once_with_evidence_summary(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.save_generated_draft.return_value = True
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    week = date(2026, 8, 10)
    payload = _complete_plan_payload(week)
    for plan_day in payload["days"][:3]:
        plan_day["meals"][0]["name"] = "Egg breakfast"
        plan_day["meals"][0]["ingredients"] = [{"item": "eggs"}]
    llm.chat_json_sync.return_value = payload
    requirement = _preference_requirement(["eggs"], 3)

    PlannerHandler(repo, api, llm).generate_plan(
        "user",
        1,
        week_start=week,
        preference="Three egg breakfasts",
        requirements=[requirement],
    )

    saved = repo.save_generated_draft.call_args.args[1]
    repo.save_generated_draft.assert_called_once_with(
        "user", saved, expected_revision=None
    )
    api.send_plan.assert_called_once_with(1, saved)
    assert api.send_message.call_args_list[0].args == (
        1,
        "Preferences satisfied:\n• Eggs: 3 breakfasts",
    )
    assert api.send_message.call_args_list[1].args == (
        1,
        "Review this draft, request edits, then tell me to confirm it.",
    )


def test_generate_plan_maximum_requirements_sends_one_bounded_summary(
    mocker: Any,
) -> None:
    """Success delivery keeps a maximum requirement summary to one chunk."""
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.save_generated_draft.return_value = True
    api = mocker.MagicMock()
    events: list[str] = []
    api.send_plan.side_effect = lambda *_args: events.append("send_plan")
    api.send_message.side_effect = lambda *_args: events.append("message")
    llm = mocker.MagicMock()
    week = date(2026, 8, 10)
    payload = _complete_plan_payload(week)
    requirements: list[PreferenceRequirement] = []
    meal_index = 0
    for plan_day in payload["days"]:
        for planned_meal in plan_day["meals"]:
            if meal_index == 20:
                break
            food_prefix = f"food-{meal_index}-0"
            planned_meal["ingredients"] = [{"item": food_prefix}]
            requirements.append(
                PreferenceRequirement(
                    id=f"requirement-{meal_index}",
                    source_text=f"source {meal_index} " + ("x" * 487),
                    foods_any_of=[
                        f"food-{meal_index}-{alternative}"
                        for alternative in range(20)
                    ],
                    exact_count=1,
                )
            )
            meal_index += 1
        if meal_index == 20:
            break
    llm.chat_json_sync.return_value = payload

    PlannerHandler(repo, api, llm).generate_plan(
        "user",
        1,
        week_start=week,
        preference="maximum requirement test",
        requirements=requirements,
    )

    summary = api.send_message.call_args_list[0].args[1]
    assert len(summary) <= 1000
    assert len(split_text(summary)) == 1
    assert events == ["send_plan", "message", "message"]
    assert (
        api.send_message.call_args_list[1]
        .args[1]
        .startswith("Review this draft")
    )


def test_terminal_compliance_delivery_compacts_maximum_source_clauses() -> None:
    """Terminal compliance output stays bounded for maximum source clauses."""
    requirements = [
        PreferenceRequirement(
            id=f"requirement-{index}",
            source_text=f"clause {index} " + ("x" * 487),
            foods_any_of=[f"food-{index}"],
            exact_count=1,
        )
        for index in range(20)
    ]
    validation = PlanValidationResult(
        valid=False,
        requirements=(),
        issues=tuple(
            ValidationIssue(
                code="requirement_count_mismatch",
                message="bounded test issue",
                requirement_id=requirement.id,
            )
            for requirement in requirements
        ),
    )

    message = PlannerHandler._generation_failure_message(
        "initial",
        "invalid plan",
        category="compliance",
        validation=validation,
        requirements=requirements,
    )

    assert len(message) <= 1000
    assert len(split_text(message)) == 1
    assert "preference clauses omitted" in message
    assert "clause 0" in message


@pytest.mark.parametrize(
    ("case", "payload_factory"),
    [
        (
            "incomplete",
            lambda week: {
                **_complete_plan_payload(week),
                "days": [
                    {
                        **plan_day,
                        "meals": [
                            meal
                            for meal in plan_day["meals"]
                            if meal["meal_type"] != "dinner"
                        ],
                    }
                    if plan_day["day"] == 1
                    else plan_day
                    for plan_day in _complete_plan_payload(week)["days"]
                ],
            },
        ),
        (
            "undercount",
            lambda week: _complete_plan_payload(week),
        ),
        (
            "overcount",
            lambda week: _complete_plan_payload(week),
        ),
    ],
)
def test_generate_plan_rejects_noncompliant_plan_before_save_or_display(
    mocker: Any,
    case: str,
    payload_factory: Any,
) -> None:
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    week = date(2026, 8, 10)
    payload = payload_factory(week)
    if case == "undercount":
        payload["days"][0]["meals"][0]["name"] = "Egg breakfast"
        payload["days"][0]["meals"][0]["ingredients"] = [{"item": "egg"}]
        expected_count = 2
    elif case == "overcount":
        for plan_day in payload["days"]:
            plan_day["meals"][0]["name"] = "Egg breakfast"
            plan_day["meals"][0]["ingredients"] = [{"item": "egg"}]
        expected_count = 3
    else:
        expected_count = 1
    llm.chat_json_sync.return_value = payload

    PlannerHandler(repo, api, llm).generate_plan(
        "user",
        1,
        week_start=week,
        preference="Egg breakfasts",
        requirements=[_preference_requirement(["egg"], expected_count)],
    )

    repo.save_generated_draft.assert_not_called()
    api.send_plan.assert_not_called()
    assert "invalid meal plan" in api.send_message.call_args.args[1]


def test_generate_plan_rejects_invalid_snack_before_save_or_display(
    mocker: Any,
) -> None:
    """An invalid snack cannot satisfy an unscoped preference on publish."""
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    week = date(2026, 8, 10)
    payload = _complete_plan_payload(week)
    payload["days"][0]["meals"].append(
        {
            "meal_type": "snack",
            "name": "Salmon snack",
            "ingredients": [],
            "est_calories": 0,
        }
    )
    llm.chat_json_sync.return_value = payload

    PlannerHandler(repo, api, llm).generate_plan(
        "user",
        1,
        week_start=week,
        preference="One salmon meal",
        requirements=[_preference_requirement(["salmon"], 1, meal_type=None)],
    )

    repo.save_generated_draft.assert_not_called()
    api.send_plan.assert_not_called()
    assert "invalid meal plan" in api.send_message.call_args.args[1]


def test_generate_plan_rejects_structurally_invalid_plan_before_save_or_display(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.chat_json_sync.return_value = {"days": []}

    PlannerHandler(repo, api, llm).generate_plan(
        "user",
        1,
        week_start=date(2026, 8, 10),
        preference="Egg breakfasts",
        requirements=[_preference_requirement(["egg"], 1)],
    )

    repo.save_generated_draft.assert_not_called()
    api.send_plan.assert_not_called()


def test_invalid_first_attempt_schedules_fresh_repair_without_publication(
    mocker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first invalid result remains generating while repair is queued."""
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    state = _plan_request_state(week)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.get_conversation_state.return_value = state
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    payload = _complete_plan_payload(week)
    payload["days"][0]["meals"][0]["name"] = "Egg breakfast"
    payload["days"][0]["meals"][0]["ingredients"] = [{"item": "egg"}]
    llm.chat_json_sync.return_value = payload
    lambda_client = mocker.MagicMock()
    lambda_client.invoke.return_value = {"StatusCode": 202}
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "meal-planner-planner")
    requirement = _preference_requirement(["egg"], 3)

    PlannerHandler(
        repo,
        api,
        llm,
        lambda_client=lambda_client,
    ).generate_plan(
        "user",
        1,
        week_start=week,
        preference="Three egg breakfasts",
        requirements=[requirement],
        request_id=state.request_id,
        state_revision=state.revision,
    )

    repo.save_generated_draft.assert_not_called()
    repo.mark_conversation_retry_ready.assert_not_called()
    api.send_plan.assert_not_called()
    api.send_message.assert_not_called()
    lambda_client.invoke.assert_called_once()
    invocation = lambda_client.invoke.call_args.kwargs
    assert invocation["InvocationType"] == "Event"
    repair_event = json.loads(invocation["Payload"])
    assert repair_event["attempt"] == 2
    assert repair_event["request_id"] == state.request_id
    assert repair_event["state_revision"] == state.revision
    assert repair_event["requirements"] == [requirement.model_dump(mode="json")]
    assert "code=requirement_count_mismatch" in repair_event["repair_feedback"]
    assert "location=requirements" in repair_event["repair_feedback"]
    assert "Egg breakfast" not in repair_event["repair_feedback"]


def test_constraint_violation_schedules_one_repair_without_publication(
    mocker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Safety failures are rejected before publication and repaired once."""
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    state = _plan_request_state(week)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.get_conversation_state.return_value = state
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    payload = _complete_plan_payload(week)
    payload["days"][0]["meals"][0]["ingredients"] = [{"item": "peanuts"}]
    llm.chat_json_sync.return_value = payload
    lambda_client = mocker.MagicMock()
    lambda_client.invoke.return_value = {"StatusCode": 202}
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "meal-planner-planner")
    constraint = _dietary_constraint()

    PlannerHandler(
        repo,
        api,
        llm,
        lambda_client=lambda_client,
    ).generate_plan(
        "user",
        1,
        week_start=week,
        preference="Avoid peanuts",
        constraint_rules=[constraint],
        request_id=state.request_id,
        state_revision=state.revision,
    )

    repo.save_generated_draft.assert_not_called()
    api.send_plan.assert_not_called()
    api.send_message.assert_not_called()
    lambda_client.invoke.assert_called_once()
    repair_event = json.loads(lambda_client.invoke.call_args.kwargs["Payload"])
    assert repair_event["attempt"] == 2
    assert repair_event["constraint_rules"] == [
        constraint.model_dump(mode="json")
    ]
    feedback = repair_event["repair_feedback"]
    assert "code=constraint_violation" in feedback
    assert "location=days[0].meals.breakfast" in feedback
    assert len(feedback) <= 800
    assert "peanuts" not in feedback.casefold()


@pytest.mark.parametrize("violating_food", ["gluten", "wheat"])
def test_semantic_legacy_constraint_violation_schedules_repair(
    mocker: Any,
    monkeypatch: pytest.MonkeyPatch,
    violating_food: str,
) -> None:
    """Legacy semantic constraints block publication on violating meals."""
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    state = _plan_request_state(week)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.get_conversation_state.return_value = state
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    payload = _complete_plan_payload(week)
    payload["days"][0]["meals"][0]["ingredients"] = [{"item": violating_food}]
    llm.chat_json_sync.return_value = payload
    lambda_client = mocker.MagicMock()
    lambda_client.invoke.return_value = {"StatusCode": 202}
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "meal-planner-planner")
    constraint = ConstraintEntry(
        id="legacy-constraint",
        source_text="gluten-free",
        forbidden_terms=["gluten-free"],
    )

    PlannerHandler(
        repo,
        api,
        llm,
        lambda_client=lambda_client,
    ).generate_plan(
        "user",
        1,
        week_start=week,
        constraint_rules=[constraint],
        request_id=state.request_id,
        state_revision=state.revision,
    )

    repo.save_generated_draft.assert_not_called()
    api.send_plan.assert_not_called()
    lambda_client.invoke.assert_called_once()
    repair_event = json.loads(lambda_client.invoke.call_args.kwargs["Payload"])
    assert "code=constraint_violation" in repair_event["repair_feedback"]


def test_vegan_constraint_blocks_provider_and_publication(
    mocker: Any,
) -> None:
    """An incomplete vegan constraint cannot reach generation or saving."""
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    constraint = ConstraintEntry(
        id="vegan-constraint",
        source_text="vegan",
        forbidden_terms=["vegan"],
    )

    PlannerHandler(repo, api, llm).generate_plan(
        "user",
        1,
        constraint_rules=[constraint],
    )

    llm.chat_json_sync.assert_not_called()
    repo.save_generated_draft.assert_not_called()
    api.send_plan.assert_not_called()


def test_uninterpretable_constraint_is_rejected_before_provider_call(
    mocker: Any,
) -> None:
    """An explicitly uninterpretable constraint cannot reach generation."""
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    constraint = ConstraintEntry(
        id="unknown-constraint",
        source_text="I react badly to mystery foods",
        forbidden_terms=[],
        uninterpretable=True,
    )

    PlannerHandler(repo, api, llm).generate_plan(
        "user",
        1,
        constraint_rules=[constraint],
    )

    llm.chat_json_sync.assert_not_called()
    api.send_plan.assert_not_called()


def test_repaired_safe_plan_is_published_with_relevant_raw_instruction(
    mocker: Any,
) -> None:
    """A repaired safe plan is saved and retains only request wording."""
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    state = _plan_request_state(week)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.get_conversation_state.return_value = state
    repo.save_generated_draft_and_clear_conversation_state.return_value = True
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.chat_json_sync.return_value = _complete_plan_payload(week)
    constraint = _dietary_constraint()
    raw_preference = "Avoid peanuts for this plan"

    PlannerHandler(repo, api, llm).generate_plan(
        "user",
        1,
        week_start=week,
        preference=raw_preference,
        constraint_rules=[constraint],
        attempt=2,
        repair_feedback="code=constraint_violation location=days[0]",
        request_id=state.request_id,
        state_revision=state.revision,
    )

    saved = (
        repo.save_generated_draft_and_clear_conversation_state.call_args.args[1]
    )
    assert saved.planning_instructions == [raw_preference]
    repo.save_generated_draft_and_clear_conversation_state.assert_called_once()
    api.send_plan.assert_called_once_with(1, saved)
    assert api.send_message.call_count == 1


def test_repaired_safety_failure_keeps_previous_draft_retry_ready(
    mocker: Any,
) -> None:
    """A second safety failure changes neither draft nor publication state."""
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    previous = make_plan(week_start=week, revision=4)
    state = _plan_request_state(week)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = previous
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = previous
    repo.get_conversation_state.return_value = state
    repo.mark_conversation_retry_ready.return_value = True
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    payload = _complete_plan_payload(week)
    payload["days"][0]["meals"][0]["ingredients"] = [{"item": "peanuts"}]
    llm.chat_json_sync.return_value = payload
    constraint = _dietary_constraint()

    PlannerHandler(repo, api, llm).generate_plan(
        "user",
        1,
        week_start=week,
        preference="Avoid peanuts",
        constraint_rules=[constraint],
        attempt=2,
        repair_feedback="code=constraint_violation location=days[0]",
        request_id=state.request_id,
        state_revision=state.revision,
    )

    repo.save_generated_draft.assert_not_called()
    repo.save_generated_draft_and_clear_conversation_state.assert_not_called()
    api.send_plan.assert_not_called()
    repo.mark_conversation_retry_ready.assert_called_once()
    retry_state = repo.mark_conversation_retry_ready.call_args.args[1]
    assert retry_state.step is ConversationWorkflowStep.RETRY_READY
    assert previous.revision == 4
    message = api.send_message.call_args.args[1]
    assert "safety constraint" in message
    assert "peanuts" not in message.casefold()


def test_constraint_feedback_precedes_rules_and_completeness_on_both_attempts(
    mocker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both attempts apply the safety gate before other validation gates."""
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    state = _plan_request_state(week)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.get_conversation_state.return_value = state
    repo.mark_conversation_retry_ready.return_value = True
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    payload = _complete_plan_payload(week)
    payload["days"][0]["meals"] = [
        {
            "meal_type": "breakfast",
            "name": "Peanut breakfast",
            "ingredients": [{"item": "peanuts"}],
            "est_calories": 400,
        }
    ]
    llm.chat_json_sync.return_value = payload
    lambda_client = mocker.MagicMock()
    lambda_client.invoke.return_value = {"StatusCode": 202}
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "meal-planner-planner")
    constraint = _dietary_constraint()
    rule = _dietary_rule()
    planner = PlannerHandler(
        repo,
        api,
        llm,
        lambda_client=lambda_client,
    )

    planner.generate_plan(
        "user",
        1,
        week_start=week,
        preference="Egg breakfasts",
        effective_rules=[rule],
        constraint_rules=[constraint],
        request_id=state.request_id,
        state_revision=state.revision,
    )
    first_feedback = json.loads(
        lambda_client.invoke.call_args.kwargs["Payload"]
    )["repair_feedback"]
    planner.generate_plan(
        "user",
        1,
        week_start=week,
        preference="Egg breakfasts",
        effective_rules=[rule],
        constraint_rules=[constraint],
        attempt=2,
        repair_feedback=first_feedback,
        request_id=state.request_id,
        state_revision=state.revision,
    )

    assert first_feedback.index("constraint_violation") < first_feedback.index(
        "strict_rule_mismatch"
    )
    assert first_feedback.index("strict_rule_mismatch") < first_feedback.index(
        "missing_meal_type"
    )
    assert lambda_client.invoke.call_count == 1
    assert repo.mark_conversation_retry_ready.called


def test_best_effort_miss_does_not_schedule_repair(
    mocker: Any,
) -> None:
    """Best-effort misses are reported without consuming a repair."""
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.save_generated_draft.return_value = True
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.chat_json_sync.return_value = _complete_plan_payload(week)

    PlannerHandler(repo, api, llm).generate_plan(
        "user",
        1,
        week_start=week,
        preference="If convenient, include eggs",
        effective_rules=[
            _dietary_rule(
                strength=RuleStrength.BEST_EFFORT,
            )
        ],
    )

    repo.save_generated_draft.assert_called_once()
    api.send_plan.assert_called_once()
    messages = [call.args[1] for call in api.send_message.call_args_list]
    assert any("Best-effort preferences:" in message for message in messages)
    assert any("not met" in message for message in messages)
    assert any("Review this draft" in message for message in messages)


@pytest.mark.parametrize("matched_count", [0, 1, 2])
def test_structured_maximum_accepts_zero_through_two_matches(
    mocker: Any, matched_count: int
) -> None:
    """A structured maximum never becomes an exact-count requirement."""
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.save_generated_draft.return_value = True
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    payload = _complete_plan_payload(week)
    for day, plan_day in enumerate(payload["days"], start=1):
        if day <= matched_count:
            plan_day["meals"][0]["name"] = "Egg breakfast"
            plan_day["meals"][0]["ingredients"] = [{"item": "egg"}]
    llm.chat_json_sync.return_value = payload
    rule = DietaryRule(
        id="rule-at-most-two",
        source_text="eggs at most twice",
        foods_any_of=["egg"],
        meal_type=MealType.BREAKFAST,
        operator="at_most",
        count=2,
    )

    PlannerHandler(repo, api, llm).generate_plan(
        "user",
        1,
        week_start=week,
        preference=None,
        requirements=[],
        effective_rules=[rule],
    )

    repo.save_generated_draft.assert_called_once()
    api.send_plan.assert_called_once()
    assert not any(
        "invalid meal plan" in call.args[1].lower()
        for call in api.send_message.call_args_list
    )


def test_interpreted_legacy_strict_rule_blocks_a_violating_plan(
    mocker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typed legacy rule still reaches the strict safety gate."""
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    state = _plan_request_state(week)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.get_conversation_state.return_value = state
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    payload = _complete_plan_payload(week)
    payload["days"][0]["meals"][0]["ingredients"] = [{"item": "oats"}]
    llm.chat_json_sync.return_value = payload
    lambda_client = mocker.MagicMock()
    lambda_client.invoke.return_value = {"StatusCode": 202}
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "meal-planner-planner")
    rule = DietaryRule(
        id="r-stored-legacy-eggs",
        source_text="eggs for breakfast",
        foods_any_of=["eggs"],
        meal_type=MealType.BREAKFAST,
        operator="at_least",
        count=1,
        strength=RuleStrength.STRICT,
    )

    PlannerHandler(
        repo,
        api,
        llm,
        lambda_client=lambda_client,
    ).generate_plan(
        "user",
        1,
        week_start=week,
        preference=None,
        stored_rules=[rule],
        effective_rules=[rule],
        request_id=state.request_id,
        state_revision=state.revision,
    )

    repo.save_generated_draft.assert_not_called()
    api.send_plan.assert_not_called()
    lambda_client.invoke.assert_called_once()
    repair_event = json.loads(lambda_client.invoke.call_args.kwargs["Payload"])
    assert "code=strict_rule_mismatch" in repair_event["repair_feedback"]


def test_strict_weekday_failure_identifies_missing_day_in_repair_feedback(
    mocker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Planner repair feedback identifies a missing named weekday."""
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    state = _plan_request_state(week)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.get_conversation_state.return_value = state
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    payload = _complete_plan_payload(week)
    payload["days"][0]["meals"][0]["ingredients"] = [{"item": "egg"}]
    llm.chat_json_sync.return_value = payload
    lambda_client = mocker.MagicMock()
    lambda_client.invoke.return_value = {"StatusCode": 202}
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "meal-planner-planner")
    rule = DietaryRule(
        id="weekday-eggs",
        source_text="eggs for breakfast on Monday and Wednesday",
        foods_any_of=["egg"],
        meal_type=MealType.BREAKFAST,
        weekdays=[Weekday.MONDAY, Weekday.WEDNESDAY],
        operator="at_least",
        count=1,
        strength=RuleStrength.STRICT,
    )

    PlannerHandler(
        repo,
        api,
        llm,
        lambda_client=lambda_client,
    ).generate_plan(
        "user",
        1,
        week_start=week,
        preference="Egg breakfasts",
        effective_rules=[rule],
        request_id=state.request_id,
        state_revision=state.revision,
    )

    repo.save_generated_draft.assert_not_called()
    api.send_plan.assert_not_called()
    lambda_client.invoke.assert_called_once()
    repair_event = json.loads(lambda_client.invoke.call_args.kwargs["Payload"])
    assert "days[2].meals.breakfast" in repair_event["repair_feedback"]
    assert "strict_rule_mismatch" in repair_event["repair_feedback"]


def test_unscoped_weekday_failure_preserves_day_only_repair_location(
    mocker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unscoped weekday repair retains the missing day's location."""
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    state = _plan_request_state(week)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.get_conversation_state.return_value = state
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    payload = _complete_plan_payload(week)
    payload["days"][0]["meals"][0]["ingredients"] = [{"item": "egg"}]
    llm.chat_json_sync.return_value = payload
    lambda_client = mocker.MagicMock()
    lambda_client.invoke.return_value = {"StatusCode": 202}
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "meal-planner-planner")
    rule = DietaryRule(
        id="weekday-eggs-unscoped",
        source_text="eggs on Monday and Wednesday",
        foods_any_of=["egg"],
        meal_type=None,
        weekdays=[Weekday.MONDAY, Weekday.WEDNESDAY],
        operator="at_least",
        count=1,
        strength=RuleStrength.STRICT,
    )

    PlannerHandler(
        repo,
        api,
        llm,
        lambda_client=lambda_client,
    ).generate_plan(
        "user",
        1,
        week_start=week,
        preference="eggs on Monday and Wednesday",
        effective_rules=[rule],
        request_id=state.request_id,
        state_revision=state.revision,
    )

    repo.save_generated_draft.assert_not_called()
    api.send_plan.assert_not_called()
    lambda_client.invoke.assert_called_once()
    repair_event = json.loads(lambda_client.invoke.call_args.kwargs["Payload"])
    feedback = repair_event["repair_feedback"]
    assert repair_event["attempt"] == 2
    assert "code=strict_rule_mismatch location=days[2]" in feedback
    assert "location=rules" not in feedback
    assert rule.source_text not in feedback
    assert "Egg breakfast" not in feedback
    assert len(feedback) <= 800


@pytest.mark.parametrize(
    ("issue", "expected_location"),
    [
        (
            ValidationIssue(
                code="strict_rule_mismatch",
                message="private rule details",
                day=3,
                rule_id="rule-1",
            ),
            "days[2]",
        ),
        (
            ValidationIssue(
                code="missing_meal_type",
                message="private meal details",
                day=3,
                meal_type=MealType.DINNER,
            ),
            "days[2].meals.dinner",
        ),
        (
            ValidationIssue(
                code="strict_rule_mismatch",
                message="private rule details",
                rule_id="rule-1",
            ),
            "rules",
        ),
        (
            ValidationIssue(
                code="constraint_violation",
                message="private constraint details",
                constraint_id="constraint-1",
            ),
            "constraints",
        ),
        (
            ValidationIssue(
                code="requirement_count_mismatch",
                message="private requirement details",
                requirement_id="requirement-1",
            ),
            "requirements",
        ),
        (
            ValidationIssue(code="missing", message="private plan details"),
            "plan",
        ),
    ],
)
def test_validation_location_preserves_bounded_issue_paths(
    issue: ValidationIssue, expected_location: str
) -> None:
    """Validation issue paths retain existing non-day precedence."""
    validation = PlanValidationResult(
        valid=False,
        requirements=(),
        issues=(issue,),
    )

    feedback = PlannerHandler._validation_feedback(validation)

    assert f"location={expected_location}" in feedback
    assert issue.message not in feedback


@pytest.mark.parametrize("meal_type", list(MealType))
def test_validation_feedback_preserves_exact_meal_slot(
    meal_type: MealType,
) -> None:
    """Meal validation feedback identifies its safe enum-valued slot."""
    validation = PlanValidationResult(
        valid=False,
        requirements=(),
        issues=(
            ValidationIssue(
                code="missing_meal_type",
                message="Egg breakfast has raw ingredient details.",
                day=1,
                meal_type=meal_type,
            ),
        ),
    )

    feedback = PlannerHandler._validation_feedback(validation)

    assert f"location=days[0].meals.{meal_type.value}" in feedback
    assert "location=days[0].meals;" not in feedback
    assert "Egg breakfast" not in feedback
    assert "ingredient" not in feedback
    assert "code=missing_meal_type" in feedback


def test_empty_day_repair_feedback_preserves_required_slots(
    mocker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty-day repair feedback distinguishes all required meal slots."""
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    state = _plan_request_state(week)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.get_conversation_state.return_value = state
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    payload = _complete_plan_payload(week)
    payload["days"][0]["meals"] = []
    llm.chat_json_sync.return_value = payload
    lambda_client = mocker.MagicMock()
    lambda_client.invoke.return_value = {"StatusCode": 202}
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "meal-planner-planner")

    PlannerHandler(
        repo,
        api,
        llm,
        lambda_client=lambda_client,
    ).generate_plan(
        "user",
        1,
        week_start=week,
        preference="Complete meals",
        request_id=state.request_id,
        state_revision=state.revision,
    )

    repair_event = json.loads(lambda_client.invoke.call_args.kwargs["Payload"])
    feedback = repair_event["repair_feedback"]
    assert "code=missing_meal_type location=days[0].meals.breakfast" in feedback
    assert "code=missing_meal_type location=days[0].meals.lunch" in feedback
    assert "code=missing_meal_type location=days[0].meals.dinner" in feedback
    assert len(feedback) <= 800
    assert "Oats breakfast 1" not in feedback
    assert "ingredients" not in feedback
    assert "plan" not in repair_event


def test_invalid_untracked_first_attempt_forwards_stable_repair_id(
    mocker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An untracked repair event forwards its initial stable token."""
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.chat_json_sync.return_value = {}
    lambda_client = mocker.MagicMock()
    lambda_client.invoke.return_value = {"StatusCode": 202}
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "meal-planner-planner")

    PlannerHandler(repo, api, llm, lambda_client=lambda_client).generate_plan(
        "user", 1, week_start=week, repair_id="repair-123"
    )

    repair_event = json.loads(lambda_client.invoke.call_args.kwargs["Payload"])
    assert repair_event["repair_id"] == "repair-123"


def test_replayed_untracked_attempt_one_keeps_one_repair_identity(
    mocker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redelivered attempt one events queue the same repair identity."""
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    repair_payload = _complete_plan_payload(week)
    for plan_day in repair_payload["days"][:3]:
        plan_day["meals"][0]["name"] = "Egg breakfast"
        plan_day["meals"][0]["ingredients"] = [{"item": "egg"}]
    llm.chat_json_sync.side_effect = [
        {},
        {},
        repair_payload,
        repair_payload,
    ]
    lambda_client = mocker.MagicMock()
    lambda_client.invoke.return_value = {"StatusCode": 202}
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "meal-planner-planner")
    planner = PlannerHandler(repo, api, llm, lambda_client=lambda_client)
    event = {
        "action": "generate_plan",
        "user_id": "user",
        "chat_id": 1,
        "week_start": week.isoformat(),
        "preference": "Three egg breakfasts",
        "requirements": [
            _preference_requirement(["egg"], 3).model_dump(mode="json")
        ],
        "attempt": 1,
        "repair_id": "repair-123",
    }

    assert planner.handle_event(event)
    assert planner.handle_event(event)

    queued_events = [
        json.loads(call.kwargs["Payload"])
        for call in lambda_client.invoke.call_args_list
    ]
    assert [queued["repair_id"] for queued in queued_events] == [
        "repair-123",
        "repair-123",
    ]
    published = False

    def save_once(*_args: Any, **_kwargs: Any) -> RepairPublicationOutcome:
        nonlocal published
        if published:
            return RepairPublicationOutcome.DUPLICATE
        published = True
        return RepairPublicationOutcome.PUBLISHED

    repo.save_repaired_draft_once.side_effect = save_once
    for queued_event in queued_events:
        assert planner.handle_event(queued_event)

    assert repo.save_repaired_draft_once.call_count == 2
    assert api.send_plan.call_count == 1
    assert api.send_message.call_count == 2


def test_legacy_untracked_attempt_one_does_not_schedule_repair(
    mocker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy untracked events use terminal recovery without a token."""
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.chat_json_sync.return_value = {}
    lambda_client = mocker.MagicMock()
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "meal-planner-planner")

    planner = PlannerHandler(repo, api, llm, lambda_client=lambda_client)

    assert planner.handle_event(
        {
            "action": "generate_plan",
            "user_id": "user",
            "chat_id": 1,
            "week_start": week.isoformat(),
            "attempt": 1,
        }
    )

    lambda_client.invoke.assert_not_called()
    repo.save_generated_draft.assert_not_called()
    api.send_plan.assert_not_called()
    api.send_message.assert_called_once()


def _replayed_untracked_event() -> dict[str, Any]:
    """Return the same complete untracked attempt-two event each time."""
    week = date(2026, 8, 10)
    requirement = _preference_requirement(["egg"], 3)
    return {
        "action": "generate_plan",
        "user_id": "user",
        "chat_id": 1,
        "week_start": week.isoformat(),
        "preference": "Three egg breakfasts",
        "requirements": [requirement.model_dump(mode="json")],
        "attempt": 2,
        "repair_feedback": (
            "code=requirement_count_mismatch location=requirements"
        ),
        "repair_id": "repair-123",
    }


def _run_replayed_untracked_event(
    mocker: Any,
    event_count: int,
    *,
    concurrent: bool,
) -> tuple[Any, Any, Any]:
    """Run replayed events against a shared at-most-once repository mock."""
    from threading import Lock

    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    payload = _complete_plan_payload(week)
    for plan_day in payload["days"][:3]:
        plan_day["meals"][0]["name"] = "Egg breakfast"
        plan_day["meals"][0]["ingredients"] = [{"item": "egg"}]
    llm.chat_json_sync.return_value = payload
    lock = Lock()
    published = False

    def save_once(*_args: Any, **_kwargs: Any) -> RepairPublicationOutcome:
        nonlocal published
        with lock:
            if published:
                return RepairPublicationOutcome.DUPLICATE
            published = True
            return RepairPublicationOutcome.PUBLISHED

    repo.save_repaired_draft_once.side_effect = save_once
    planner = PlannerHandler(repo, api, llm)
    event = _replayed_untracked_event()

    if concurrent:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=event_count) as executor:
            results = list(
                executor.map(planner.handle_event, [event] * event_count)
            )
    else:
        results = [planner.handle_event(event) for _ in range(event_count)]

    assert all(results)
    return repo, api, llm


def test_untracked_attempt_two_sequential_replay_delivers_once(
    mocker: Any,
) -> None:
    """Sequential Lambda redelivery persists and displays one draft."""
    repo, api, _llm = _run_replayed_untracked_event(mocker, 2, concurrent=False)

    assert repo.save_repaired_draft_once.call_count == 2
    assert api.send_plan.call_count == 1
    assert api.send_message.call_count == 2
    assert (
        "Preferences satisfied:" in api.send_message.call_args_list[0].args[1]
    )
    assert "Review this draft" in api.send_message.call_args_list[1].args[1]


def test_untracked_attempt_two_concurrent_replay_delivers_once(
    mocker: Any,
) -> None:
    """Concurrent Lambda redelivery has one persistence and delivery path."""
    repo, api, _llm = _run_replayed_untracked_event(mocker, 2, concurrent=True)

    assert repo.save_repaired_draft_once.call_count == 2
    assert api.send_plan.call_count == 1
    assert api.send_message.call_count == 2
    messages = [call.args[1] for call in api.send_message.call_args_list]
    assert any("Preferences satisfied:" in message for message in messages)
    assert any("Review this draft" in message for message in messages)
    assert not any("stale" in message.lower() for message in messages)


def test_invalid_structural_first_attempt_schedules_one_repair(
    mocker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A structural failure gets one fresh invocation without publication."""
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    state = _plan_request_state(week)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.get_conversation_state.return_value = state
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.chat_json_sync.return_value = {}
    lambda_client = mocker.MagicMock()
    lambda_client.invoke.return_value = {"StatusCode": 202}
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "meal-planner-planner")

    PlannerHandler(
        repo,
        api,
        llm,
        lambda_client=lambda_client,
    ).generate_plan(
        "user",
        1,
        week_start=week,
        preference="Three egg breakfasts",
        request_id=state.request_id,
        state_revision=state.revision,
    )

    llm.chat_json_sync.assert_called_once()
    lambda_client.invoke.assert_called_once()
    repo.save_generated_draft.assert_not_called()
    repo.mark_conversation_retry_ready.assert_not_called()
    api.send_plan.assert_not_called()
    api.send_message.assert_not_called()
    repair_event = json.loads(lambda_client.invoke.call_args.kwargs["Payload"])
    assert repair_event["attempt"] == 2
    assert (
        "code=missing location=week_start_date"
        in (repair_event["repair_feedback"])
    )


@pytest.mark.parametrize("failure_kind", ["structural", "compliance"])
def test_repair_ownership_read_failure_recovers_request(
    mocker: Any,
    caplog: pytest.LogCaptureFixture,
    failure_kind: str,
) -> None:
    """Ownership read failures enter the existing retry recovery path."""
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    state = _plan_request_state(week)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    if failure_kind == "structural":
        repo.get_conversation_state.side_effect = [
            state,
            RuntimeError("secret user preference and chat data"),
            state,
        ]
    else:
        repo.get_conversation_state.side_effect = [
            state,
            state,
            RuntimeError("secret user preference and chat data"),
            state,
        ]
    repo.mark_conversation_retry_ready.return_value = True
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    lambda_client = mocker.MagicMock()
    if failure_kind == "structural":
        llm.chat_json_sync.return_value = {}
        requirements: list[PreferenceRequirement] = []
    else:
        payload = _complete_plan_payload(week)
        payload["days"][0]["meals"][0]["name"] = "Egg breakfast"
        payload["days"][0]["meals"][0]["ingredients"] = [{"item": "egg"}]
        llm.chat_json_sync.return_value = payload
        requirements = [_preference_requirement(["eggs"], 3)]

    with caplog.at_level(
        logging.WARNING, logger="meal_planner.planner_handler"
    ):
        PlannerHandler(
            repo, api, llm, lambda_client=lambda_client
        ).generate_plan(
            "user-secret",
            987654,
            week_start=week,
            preference="secret preference",
            requirements=requirements,
            request_id=state.request_id,
            state_revision=state.revision,
        )

    repo.mark_conversation_retry_ready.assert_called_once()
    expected_reads = 3 if failure_kind == "structural" else 4
    assert repo.get_conversation_state.call_count == expected_reads
    lambda_client.invoke.assert_not_called()
    api.send_plan.assert_not_called()
    assert api.send_message.call_count == 1
    assert "No draft was saved" in api.send_message.call_args.args[1]
    assert "user-secret" not in caplog.text
    assert "987654" not in caplog.text
    assert "secret preference" not in caplog.text
    assert "secret user preference" not in caplog.text


def test_direct_invalid_call_with_repair_id_queues_without_notification(
    mocker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy direct calls follow the same no-publication first boundary."""
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    payload = _complete_plan_payload(week)
    payload["days"][0]["meals"][0]["name"] = "Egg breakfast"
    payload["days"][0]["meals"][0]["ingredients"] = [{"item": "egg"}]
    llm.chat_json_sync.return_value = payload
    lambda_client = mocker.MagicMock()
    lambda_client.invoke.return_value = {"StatusCode": 202}
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "meal-planner-planner")

    PlannerHandler(
        repo,
        api,
        llm,
        lambda_client=lambda_client,
    ).generate_plan(
        "user",
        1,
        week_start=week,
        preference="Three egg breakfasts",
        requirements=[_preference_requirement(["egg"], 3)],
        repair_id="repair-123",
    )

    llm.chat_json_sync.assert_called_once()
    lambda_client.invoke.assert_called_once()
    repo.save_generated_draft.assert_not_called()
    api.send_plan.assert_not_called()
    api.send_message.assert_not_called()


@pytest.mark.parametrize(
    ("failure", "category", "message_fragment"),
    [
        (
            LLMResponseFormatError("raw response and preference"),
            "response_format",
            "invalid response format",
        ),
        (
            LLMTimeoutError("raw preference and credentials"),
            "timeout",
            "timed out",
        ),
        (
            LLMTransientError("raw meal and ingredient data"),
            "transient",
            "temporarily unavailable",
        ),
        (
            LLMPermanentError("raw user and chat identifiers"),
            "permanent",
            "rejected the request",
        ),
    ],
)
def test_terminal_provider_failures_are_specific_and_privacy_safe(
    mocker: Any,
    caplog: pytest.LogCaptureFixture,
    failure: Exception,
    category: str,
    message_fragment: str,
) -> None:
    """Provider failure categories do not expose exception content."""
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.model = "gpt-5.6-luna"
    llm.chat_json_sync.side_effect = failure

    with caplog.at_level(
        logging.WARNING, logger="meal_planner.planner_handler"
    ):
        PlannerHandler(repo, api, llm).generate_plan(
            "user-secret",
            987654,
            week_start=date(2026, 8, 10),
            preference="secret preference",
        )

    message = api.send_message.call_args.args[1]
    assert message_fragment in message
    assert "No draft was saved" in message
    assert "/plan" in message
    assert "raw response" not in caplog.text
    assert "secret preference" not in caplog.text
    assert "user-secret" not in caplog.text
    assert "987654" not in caplog.text
    record = next(
        record
        for record in caplog.records
        if record.message.startswith("Planner LLM attempt failed")
    )
    assert record.category == category
    assert record.attempt == 1
    assert record.model == "gpt-5.6-luna"
    assert record.validation == ""


@pytest.mark.parametrize(
    ("case", "message_fragment"),
    [
        ("structural", "invalid meal plan structure"),
        ("completeness", "because it was incomplete"),
        ("compliance", "preference clauses were not met"),
    ],
)
def test_terminal_validation_failures_are_distinct_and_recoverable(
    mocker: Any,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    message_fragment: str,
) -> None:
    """Validation failures explain why no draft was saved and retain /plan."""
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    state = _plan_request_state(week).model_copy(
        update={"preference": "Three egg breakfasts"}
    )
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.get_conversation_state.return_value = state
    repo.mark_conversation_retry_ready.return_value = True
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    requirements: list[PreferenceRequirement] = []
    payload = _complete_plan_payload(week)
    if case == "structural":
        response: object = {}
    elif case == "completeness":
        response = _complete_plan_payload(week)
        response["days"][0]["meals"] = response["days"][0]["meals"][:2]
    else:
        response = payload
        requirements = [_preference_requirement(["eggs"], 3)]
    llm.chat_json_sync.return_value = response

    PlannerHandler(repo, api, llm).generate_plan(
        "user-secret",
        987654,
        week_start=week,
        preference="Three egg breakfasts",
        requirements=requirements,
        request_id=state.request_id,
        state_revision=state.revision,
    )

    message = api.send_message.call_args.args[1]
    assert message_fragment in message
    assert "No draft was saved" in message
    assert "Your preference is retained" in message
    assert "/plan" in message
    if case == "compliance":
        assert "eggs" in message
    repo.save_generated_draft.assert_not_called()
    retry_state = repo.mark_conversation_retry_ready.call_args.args[1]
    assert retry_state.preference == "Three egg breakfasts"


@pytest.mark.parametrize(
    ("case", "expected_category"),
    [
        ("structural", "structural"),
        ("completeness", "completeness"),
        ("compliance", "compliance"),
    ],
)
def test_terminal_validation_failures_measure_from_generation_start(
    mocker: Any,
    caplog: pytest.LogCaptureFixture,
    case: str,
    expected_category: str,
) -> None:
    """Terminal validation logs cover the whole generation operation."""
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.model = "planner-model"
    week = date(2026, 8, 10)
    requirements: list[PreferenceRequirement] = []
    if case == "structural":
        response: object = {}
    elif case == "completeness":
        response = _complete_plan_payload(week)
        response["days"][0]["meals"] = response["days"][0]["meals"][:2]
    else:
        response = _complete_plan_payload(week)
        for plan_day in response["days"][:3]:
            plan_day["meals"][0]["name"] = "Egg breakfast"
            plan_day["meals"][0]["ingredients"] = [{"item": "egg"}]
        requirements = [_preference_requirement(["salmon"], 3)]
    llm.chat_json_sync.return_value = response
    mocker.patch(
        "meal_planner.planner_handler.time.monotonic",
        side_effect=[100.0, 100.1, 100.4, 100.4, 100.5],
    )

    with caplog.at_level(
        logging.WARNING, logger="meal_planner.planner_handler"
    ):
        PlannerHandler(repo, api, llm).generate_plan(
            "secret-user",
            987654,
            week_start=week,
            preference="secret preference",
            requirements=requirements,
            attempt=2,
            repair_feedback="bounded repair feedback",
            repair_id="repair-1",
        )

    record = next(
        record
        for record in caplog.records
        if record.message.startswith("Planner LLM attempt failed")
    )
    assert record.category == expected_category
    assert record.elapsed_ms == pytest.approx(400.0)
    assert 0.0 < record.elapsed_ms < 1_000.0
    assert len(record.validation) <= 400
    assert "secret" not in caplog.text
    assert "987654" not in caplog.text


def test_attempt_two_recovery_read_failure_logs_attempt_two(
    mocker: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Attempt-two recovery read failures retain the active attempt."""
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    state = _plan_request_state(
        date(2026, 8, 10), request_id="secret-request", revision=7
    )
    repo.get_conversation_state.side_effect = [
        state,
        RuntimeError("secret recovery details"),
    ]
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.model = "planner-model"
    llm.chat_json_sync.return_value = {}
    mocker.patch(
        "meal_planner.planner_handler.time.monotonic",
        side_effect=[100.0, 100.1, 100.2, 100.3, 100.4, 100.5],
    )

    with caplog.at_level(
        logging.WARNING, logger="meal_planner.planner_handler"
    ):
        PlannerHandler(repo, api, llm).generate_plan(
            "secret-user",
            987654,
            week_start=date(2026, 8, 10),
            attempt=2,
            repair_feedback="bounded repair feedback",
            request_id="secret-request",
            state_revision=7,
        )

    record = next(
        record
        for record in caplog.records
        if record.category == "state_recovery"
    )
    assert record.attempt == 2
    assert record.elapsed_ms == pytest.approx(100.0)
    assert 0.0 <= record.elapsed_ms < 1_000.0
    assert "secret" not in caplog.text
    assert "987654" not in caplog.text


def test_attempt_two_recovery_write_failure_logs_attempt_two(
    mocker: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Attempt-two recovery write failures retain the active attempt."""
    repo = mocker.MagicMock()
    state = _plan_request_state(date(2026, 8, 10))
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.get_conversation_state.return_value = state
    repo.mark_conversation_retry_ready.side_effect = RuntimeError(
        "secret recovery details"
    )
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.model = "planner-model"
    llm.chat_json_sync.return_value = {}
    mocker.patch(
        "meal_planner.planner_handler.time.monotonic",
        side_effect=[200.0, 200.1, 200.2, 200.3, 200.4, 200.5, 200.6],
    )

    with caplog.at_level(
        logging.WARNING, logger="meal_planner.planner_handler"
    ):
        PlannerHandler(repo, api, llm).generate_plan(
            "secret-user",
            987654,
            week_start=date(2026, 8, 10),
            attempt=2,
            repair_feedback="bounded repair feedback",
            request_id=state.request_id,
            state_revision=state.revision,
        )

    record = next(
        record
        for record in caplog.records
        if record.category == "state_recovery"
    )
    assert record.attempt == 2
    assert record.elapsed_ms == pytest.approx(100.0)
    assert 0.0 <= record.elapsed_ms < 1_000.0
    assert "secret" not in caplog.text
    assert "987654" not in caplog.text


def test_attempt_two_notification_failure_logs_attempt_two(
    mocker: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Attempt-two failure notifications retain the active attempt."""
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.side_effect = RuntimeError("secret generation details")
    api = mocker.MagicMock()
    api.send_message.side_effect = RuntimeError("secret notification details")
    llm = mocker.MagicMock()
    llm.model = "planner-model"
    mocker.patch(
        "meal_planner.planner_handler.time.monotonic",
        side_effect=[300.0, 300.1, 300.2, 300.3],
    )

    with caplog.at_level(
        logging.WARNING, logger="meal_planner.planner_handler"
    ):
        PlannerHandler(repo, api, llm).generate_plan(
            "secret-user",
            987654,
            week_start=date(2026, 8, 10),
            attempt=2,
            repair_feedback="bounded repair feedback",
        )

    record = next(
        record for record in caplog.records if record.category == "notification"
    )
    assert record.attempt == 2
    assert record.elapsed_ms == pytest.approx(100.0)
    assert 0.0 <= record.elapsed_ms < 1_000.0
    assert "secret" not in caplog.text
    assert "987654" not in caplog.text


def test_second_attempt_success_publishes_once_without_repair(
    mocker: Any,
) -> None:
    """A valid repair result is persisted and displayed exactly once."""
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    state = _plan_request_state(week)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.get_conversation_state.return_value = state
    repo.save_generated_draft_and_clear_conversation_state.return_value = True
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    week_payload = _complete_plan_payload(week)
    for plan_day in week_payload["days"][:3]:
        plan_day["meals"][0]["name"] = "Egg breakfast"
        plan_day["meals"][0]["ingredients"] = [{"item": "egg"}]
    llm.chat_json_sync.return_value = week_payload
    lambda_client = mocker.MagicMock()
    requirement = _preference_requirement(["egg"], 3)

    PlannerHandler(
        repo,
        api,
        llm,
        lambda_client=lambda_client,
    ).generate_plan(
        "user",
        1,
        week_start=week,
        preference="Three egg breakfasts",
        requirements=[requirement],
        attempt=2,
        repair_feedback="r1 matched 1; expected exactly 3",
        request_id=state.request_id,
        state_revision=state.revision,
    )

    llm.chat_json_sync.assert_called_once()
    lambda_client.invoke.assert_not_called()
    repo.save_generated_draft_and_clear_conversation_state.assert_called_once()
    repo.clear_conversation_state_if_matches.assert_not_called()
    api.send_plan.assert_called_once()
    assert api.send_message.call_count == 2


def test_second_attempt_failure_is_retry_ready_without_third_attempt(
    mocker: Any,
) -> None:
    """An invalid repair is terminal and cannot queue another repair."""
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    state = _plan_request_state(week)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.get_conversation_state.return_value = state
    repo.mark_conversation_retry_ready.return_value = True
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.chat_json_sync.return_value = {}
    lambda_client = mocker.MagicMock()

    PlannerHandler(
        repo,
        api,
        llm,
        lambda_client=lambda_client,
    ).generate_plan(
        "user",
        1,
        week_start=week,
        preference="Three egg breakfasts",
        requirements=[_preference_requirement(["egg"], 3)],
        attempt=2,
        repair_feedback="return one complete JSON plan object",
        request_id=state.request_id,
        state_revision=state.revision,
    )

    llm.chat_json_sync.assert_called_once()
    lambda_client.invoke.assert_not_called()
    repo.save_generated_draft.assert_not_called()
    repo.mark_conversation_retry_ready.assert_called_once()
    api.send_plan.assert_not_called()
    assert api.send_message.call_count == 1


def test_repair_invocation_failure_becomes_retry_ready_without_publication(
    mocker: Any,
) -> None:
    """A failed asynchronous handoff reports one recoverable failure."""
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    state = _plan_request_state(week)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.get_conversation_state.return_value = state
    repo.mark_conversation_retry_ready.return_value = True
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    payload = _complete_plan_payload(week)
    payload["days"][0]["meals"][0]["name"] = "Egg breakfast"
    payload["days"][0]["meals"][0]["ingredients"] = [{"item": "egg"}]
    llm.chat_json_sync.return_value = payload
    lambda_client = mocker.MagicMock()
    lambda_client.invoke.side_effect = RuntimeError("invoke unavailable")

    PlannerHandler(
        repo,
        api,
        llm,
        lambda_client=lambda_client,
    ).generate_plan(
        "user",
        1,
        week_start=week,
        preference="Three egg breakfasts",
        requirements=[_preference_requirement(["egg"], 3)],
        request_id=state.request_id,
        state_revision=state.revision,
    )

    repo.save_generated_draft.assert_not_called()
    repo.mark_conversation_retry_ready.assert_called_once()
    api.send_plan.assert_not_called()
    assert api.send_message.call_count == 1


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("private connect timeout"),
        TimeoutError("private read timeout"),
        {"StatusCode": 500},
    ],
)
def test_repair_dispatch_failures_recover_after_tracked_generation(
    mocker: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: TimeoutError | dict[str, int],
) -> None:
    """Every bounded dispatch failure reaches the existing recovery path."""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "planner-function")
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    state = _plan_request_state(week)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.get_conversation_state.return_value = state
    repo.mark_conversation_retry_ready.return_value = True
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.chat_json_sync.return_value = {}
    lambda_client = mocker.MagicMock()
    if isinstance(failure, dict):
        lambda_client.invoke.return_value = failure
    else:
        lambda_client.invoke.side_effect = failure

    handler = PlannerHandler(
        repo,
        api,
        llm,
        lambda_client=lambda_client,
    )

    with caplog.at_level(
        logging.WARNING, logger="meal_planner.planner_handler"
    ):
        handler.generate_plan(
            "user",
            1,
            week_start=week,
            preference="Three egg breakfasts",
            requirements=[_preference_requirement(["egg"], 3)],
            request_id=state.request_id,
            state_revision=state.revision,
        )

    records = [
        record
        for record in caplog.records
        if record.category == "repair_dispatch"
    ]
    assert len(records) == 1
    assert "private" not in caplog.text
    assert "timeout" not in caplog.text
    repo.mark_conversation_retry_ready.assert_called_once()
    assert lambda_client.invoke.call_count == 1
    assert api.send_message.call_count == 1


def test_default_repair_client_has_bounded_botocore_policy(
    mocker: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default asynchronous repair client has no unbudgeted retry."""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "planner-function")
    boto_client = mocker.patch("meal_planner.planner_handler.boto3.client")
    boto_client.return_value.invoke.return_value = {"StatusCode": 202}
    handler = PlannerHandler(
        mocker.MagicMock(), mocker.MagicMock(), mocker.MagicMock()
    )
    context = PlanGenerationContext(
        preference="eggs", attempt=1, repair_id="repair-123"
    )

    assert handler._schedule_repair(
        "user", 1, date(2026, 8, 10), context, "safe feedback"
    )

    config = boto_client.call_args.kwargs["config"]
    assert isinstance(config, Config)
    assert config.connect_timeout == 3.0
    assert config.read_timeout == 10.0
    assert config.retries == {
        "mode": "standard",
        "total_max_attempts": 1,
    }


def test_repair_ownership_failure_logs_operation_elapsed_time(
    mocker: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Ownership-read failure timing covers only the ownership read."""
    repo = mocker.MagicMock()
    repo.get_conversation_state.side_effect = RuntimeError(
        "secret preference and user data"
    )
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.model = "planner-model"
    handler = PlannerHandler(repo, api, llm)
    context = PlanGenerationContext(
        preference="secret preference",
        attempt=1,
        request_id="secret-request",
        state_revision=7,
    )
    mocker.patch(
        "meal_planner.planner_handler.time.monotonic",
        side_effect=[10.0, 10.125],
    )

    with caplog.at_level(
        logging.WARNING, logger="meal_planner.planner_handler"
    ):
        result = handler._schedule_repair(
            "secret-user",
            987654,
            date(2026, 8, 10),
            context,
            "secret feedback",
        )

    assert result is False
    record = next(
        record
        for record in caplog.records
        if record.category == "repair_ownership"
    )
    assert record.elapsed_ms == pytest.approx(125.0)
    assert 0.0 <= record.elapsed_ms < 1_000.0
    assert "secret" not in caplog.text
    assert "987654" not in caplog.text


@pytest.mark.parametrize(
    ("dispatch_mode", "expected_elapsed_ms"),
    [
        ("missing", 50.0),
        ("exception", 200.0),
        ("non_202", 75.0),
    ],
)
def test_repair_dispatch_failures_log_operation_elapsed_time(
    mocker: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    dispatch_mode: str,
    expected_elapsed_ms: float,
) -> None:
    """Repair dispatch failures report bounded, privacy-safe durations."""
    repo = mocker.MagicMock()
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.model = "planner-model"
    lambda_client = mocker.MagicMock()
    if dispatch_mode == "missing":
        monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    else:
        monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "planner-function")
        if dispatch_mode == "exception":
            lambda_client.invoke.side_effect = RuntimeError(
                "secret dispatch details"
            )
        else:
            lambda_client.invoke.return_value = {"StatusCode": 500}
    handler = PlannerHandler(
        repo,
        api,
        llm,
        lambda_client=lambda_client,
    )
    context = PlanGenerationContext(preference="secret preference", attempt=1)
    mocker.patch(
        "meal_planner.planner_handler.time.monotonic",
        side_effect=[20.0, 20.0 + expected_elapsed_ms / 1000.0],
    )

    with caplog.at_level(
        logging.WARNING, logger="meal_planner.planner_handler"
    ):
        result = handler._schedule_repair(
            "secret-user",
            987654,
            date(2026, 8, 10),
            context,
            "secret feedback",
        )

    assert result is False
    record = next(
        record
        for record in caplog.records
        if record.category == "repair_dispatch"
    )
    assert record.elapsed_ms == pytest.approx(expected_elapsed_ms)
    assert 0.0 <= record.elapsed_ms < 1_000.0
    assert "secret" not in caplog.text
    assert "987654" not in caplog.text


def test_retry_state_recovery_failure_logs_operation_elapsed_time(
    mocker: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Retry-state write failure timing covers the recovery operation."""
    repo = mocker.MagicMock()
    state = _plan_request_state(date(2026, 8, 10))
    repo.get_conversation_state.return_value = state
    repo.mark_conversation_retry_ready.side_effect = RuntimeError(
        "secret recovery details"
    )
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.model = "planner-model"
    handler = PlannerHandler(repo, api, llm)
    mocker.patch(
        "meal_planner.planner_handler.time.monotonic",
        side_effect=[30.0, 30.3, 30.31],
    )

    with caplog.at_level(
        logging.WARNING, logger="meal_planner.planner_handler"
    ):
        result = handler._retain_retry_state(
            "secret-user",
            request_id=state.request_id,
            state_revision=state.revision,
        )

    assert result is None
    record = next(
        record
        for record in caplog.records
        if record.category == "state_recovery"
    )
    assert record.elapsed_ms == pytest.approx(10.0)
    assert 0.0 <= record.elapsed_ms < 1_000.0
    assert "secret" not in caplog.text
    assert "987654" not in caplog.text


def test_retry_state_recovery_read_failure_logs_operation_elapsed_time(
    mocker: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Retry-state read failure timing covers only the recovery read."""
    repo = mocker.MagicMock()
    repo.get_conversation_state.side_effect = RuntimeError(
        "secret recovery details"
    )
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.model = "planner-model"
    handler = PlannerHandler(repo, api, llm)
    mocker.patch(
        "meal_planner.planner_handler.time.monotonic",
        side_effect=[40.0, 40.04],
    )

    with caplog.at_level(
        logging.WARNING, logger="meal_planner.planner_handler"
    ):
        result = handler._retain_retry_state(
            "secret-user",
            request_id="secret-request",
            state_revision=7,
        )

    assert result is None
    record = next(
        record
        for record in caplog.records
        if record.category == "state_recovery"
    )
    assert record.elapsed_ms == pytest.approx(40.0)
    assert 0.0 <= record.elapsed_ms < 1_000.0
    assert "secret" not in caplog.text


def test_cancelled_request_does_not_schedule_repair_or_notify(
    mocker: Any,
) -> None:
    """A cancelled or stale first worker cannot enqueue a repair."""
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    state = _plan_request_state(week).model_copy(
        update={"step": ConversationWorkflowStep.RETRY_READY}
    )
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.get_conversation_state.return_value = state
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    payload = _complete_plan_payload(week)
    payload["days"][0]["meals"][0]["name"] = "Egg breakfast"
    payload["days"][0]["meals"][0]["ingredients"] = [{"item": "egg"}]
    llm.chat_json_sync.return_value = payload
    lambda_client = mocker.MagicMock()

    PlannerHandler(
        repo,
        api,
        llm,
        lambda_client=lambda_client,
    ).generate_plan(
        "user",
        1,
        week_start=week,
        preference="Three egg breakfasts",
        requirements=[_preference_requirement(["egg"], 3)],
        request_id=state.request_id,
        state_revision=state.revision,
    )

    lambda_client.invoke.assert_not_called()
    repo.save_generated_draft.assert_not_called()
    repo.mark_conversation_retry_ready.assert_not_called()
    api.send_message.assert_not_called()


def test_stale_second_attempt_cannot_publish_or_notify(mocker: Any) -> None:
    """A duplicate repair that loses state ownership is fully suppressed."""
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    state = _plan_request_state(week).model_copy(
        update={"step": ConversationWorkflowStep.RETRY_READY}
    )
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.get_conversation_state.return_value = state
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.chat_json_sync.return_value = _complete_plan_payload(week)

    PlannerHandler(repo, api, llm).generate_plan(
        "user",
        1,
        week_start=week,
        attempt=2,
        repair_feedback="repair",
        request_id=state.request_id,
        state_revision=state.revision,
    )

    repo.save_generated_draft.assert_not_called()
    api.send_plan.assert_not_called()
    api.send_message.assert_not_called()


@pytest.mark.parametrize("attempt", [1, 2])
@pytest.mark.parametrize(
    "stale_kind", ["absent", "replaced", "revision", "completed"]
)
def test_tracked_preflight_suppresses_stale_events_before_provider(
    mocker: Any, attempt: int, stale_kind: str
) -> None:
    """Every stale tracked event exits before profile or LLM access."""
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    expected_request_id = "request-1"
    expected_revision = 3
    current_state = _plan_request_state(
        week, request_id=expected_request_id, revision=expected_revision
    )
    if stale_kind == "absent":
        durable_state = None
    elif stale_kind == "replaced":
        durable_state = current_state.model_copy(
            update={"request_id": "replacement-request"}
        )
    elif stale_kind == "revision":
        durable_state = current_state.model_copy(update={"revision": 4})
    else:
        durable_state = current_state.model_copy(
            update={"step": ConversationWorkflowStep.RETRY_READY}
        )
    repo.get_conversation_state.return_value = durable_state
    api = mocker.MagicMock()
    llm = mocker.MagicMock()

    PlannerHandler(repo, api, llm).generate_plan(
        "user",
        1,
        week_start=week,
        preference="private preference",
        attempt=attempt,
        repair_feedback="bounded feedback" if attempt == 2 else None,
        request_id=expected_request_id,
        state_revision=expected_revision,
    )

    repo.get_conversation_state.assert_called_once_with(
        "user", consistent_read=True
    )
    repo.get_profile.assert_not_called()
    repo.get_plan.assert_not_called()
    repo.get_meal_history.assert_not_called()
    repo.get_latest_plan.assert_not_called()
    llm.chat_json_sync.assert_not_called()
    repo.save_generated_draft.assert_not_called()
    repo.save_generated_draft_and_clear_conversation_state.assert_not_called()
    api.send_plan.assert_not_called()
    api.send_message.assert_not_called()


def test_generate_plan_does_not_send_summary_when_draft_delivery_fails(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.save_generated_draft.return_value = True
    api = mocker.MagicMock()
    api.send_plan.side_effect = RuntimeError("offline")
    llm = mocker.MagicMock()
    week = date(2026, 8, 10)
    payload = _complete_plan_payload(week)
    for plan_day in payload["days"][:3]:
        plan_day["meals"][0]["name"] = "Egg breakfast"
        plan_day["meals"][0]["ingredients"] = [{"item": "egg"}]
    llm.chat_json_sync.return_value = payload

    PlannerHandler(repo, api, llm).generate_plan(
        "user",
        1,
        week_start=week,
        preference="Egg breakfasts",
        requirements=[_preference_requirement(["egg"], 3)],
    )

    repo.save_generated_draft.assert_called_once()
    api.send_plan.assert_called_once()
    api.send_message.assert_not_called()


def test_generate_plan_no_preference_keeps_existing_follow_up_only(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.save_generated_draft.return_value = True
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    week = date(2026, 8, 10)
    llm.chat_json_sync.return_value = _complete_plan_payload(week)

    PlannerHandler(repo, api, llm).generate_plan("user", 1, week_start=week)

    repo.save_generated_draft.assert_called_once()
    api.send_plan.assert_called_once()
    api.send_message.assert_called_once_with(
        1, "Review this draft, request edits, then tell me to confirm it."
    )


def test_generate_plan_does_not_publish_summary_for_stale_request(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    requirement = _preference_requirement(["egg"], 3)
    stale_state = _plan_request_state(week).model_copy(
        update={
            "step": ConversationWorkflowStep.RETRY_READY,
            "requirements": [requirement],
        }
    )
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.get_conversation_state.return_value = stale_state
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.chat_json_sync.return_value = _complete_plan_payload(week)

    PlannerHandler(repo, api, llm).generate_plan(
        "user",
        1,
        week_start=week,
        preference="Egg breakfasts",
        requirements=[requirement],
        request_id=stale_state.request_id,
        state_revision=stale_state.revision,
    )

    repo.save_generated_draft.assert_not_called()
    api.send_plan.assert_not_called()
    api.send_message.assert_not_called()


def test_generate_plan_does_not_send_summary_after_persistence_conflict(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.save_generated_draft.return_value = False
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    week = date(2026, 8, 10)
    payload = _complete_plan_payload(week)
    for plan_day in payload["days"][:3]:
        plan_day["meals"][0]["name"] = "Egg breakfast"
        plan_day["meals"][0]["ingredients"] = [{"item": "egg"}]
    llm.chat_json_sync.return_value = payload

    PlannerHandler(repo, api, llm).generate_plan(
        "user",
        1,
        week_start=week,
        preference="Egg breakfasts",
        requirements=[_preference_requirement(["egg"], 3)],
    )

    repo.save_generated_draft.assert_called_once()
    api.send_plan.assert_not_called()
    assert api.send_message.call_count == 1
    assert "discarded the stale result" in api.send_message.call_args.args[1]


def test_tracked_generation_losing_ownership_before_publication_is_silent(
    mocker: Any,
) -> None:
    """A publication race cannot display a draft from a stale worker."""
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    state = _plan_request_state(week)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.get_conversation_state.return_value = state
    repo.save_generated_draft.return_value = True
    repo.clear_conversation_state_if_matches.return_value = False
    repo.save_generated_draft_and_clear_conversation_state.return_value = False
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    payload = _complete_plan_payload(week)
    for plan_day in payload["days"][:3]:
        plan_day["meals"][0]["name"] = "Egg breakfast"
        plan_day["meals"][0]["ingredients"] = [{"item": "egg"}]
    llm.chat_json_sync.return_value = payload

    PlannerHandler(repo, api, llm).generate_plan(
        "user",
        1,
        week_start=week,
        preference="Three egg breakfasts",
        requirements=[_preference_requirement(["egg"], 3)],
        request_id=state.request_id,
        state_revision=state.revision,
    )

    repo.save_generated_draft_and_clear_conversation_state.assert_called_once()
    repo.save_generated_draft.assert_not_called()
    api.send_plan.assert_not_called()
    api.send_message.assert_not_called()


def test_revise_plan_publishes_normalized_replacement_before_delivery(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    week = date.today()
    current = make_plan(
        week_start=week,
        revision=4,
        planning_instructions=["Three egg breakfasts"],
    )
    now = datetime.now(timezone.utc)
    state = ConversationState(
        workflow_kind=ConversationWorkflowKind.PLAN_REVISION,
        step=ConversationWorkflowStep.GENERATING,
        amendment="Avoid cauliflower",
        target_week=week,
        expected_plan_revision=4,
        request_id="revision-1",
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
    )
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = current
    repo.get_conversation_state.return_value = state
    repo.replace_draft_and_clear_revision_state.return_value = True
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    payload = make_plan_payload(week)
    payload["status"] = PlanStatus.CONFIRMED.value
    payload["revision"] = 99
    payload["grocery_status"] = GroceryStatus.READY.value
    payload["grocery_list"] = [{"name": "Produce", "items": ["Apples"]}]
    llm.chat_json_sync.return_value = payload

    PlannerHandler(repo, api, llm).revise_plan(
        "user",
        1,
        PlanRevisionContext(
            amendment="Avoid cauliflower",
            request_id="revision-1",
            state_revision=0,
            expected_plan_revision=4,
            week_start=week,
        ),
    )

    replacement = repo.replace_draft_and_clear_revision_state.call_args.args[1]
    assert replacement.revision == 5
    assert replacement.status is PlanStatus.DRAFT
    assert replacement.grocery_status is GroceryStatus.NOT_REQUESTED
    assert replacement.grocery_list == []
    assert replacement.planning_instructions == [
        "Three egg breakfasts",
        "Avoid cauliflower",
    ]
    assert all(
        meal.outcome is MealOutcome.UNREPORTED
        for plan_day in replacement.days
        for meal in plan_day.meals
    )
    repo.replace_draft_and_clear_revision_state.assert_called_once()
    assert api.send_plan.call_count == 1
    assert "revised draft" in api.send_message.call_args.args[1]


def test_revision_unexpected_failure_retains_matching_retry_state(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    week = date.today()
    state = _revision_state(week)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = make_plan(week_start=week, revision=4)
    repo.get_conversation_state.return_value = state
    repo.mark_conversation_retry_ready.return_value = True
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.chat_json_sync.side_effect = RuntimeError("worker failure")

    PlannerHandler(repo, api, llm).revise_plan(
        "user",
        1,
        PlanRevisionContext(
            amendment=state.amendment or "",
            request_id=state.request_id or "",
            state_revision=state.revision,
            expected_plan_revision=state.expected_plan_revision or 0,
            week_start=week,
        ),
    )

    recovered = repo.mark_conversation_retry_ready.call_args.args[1]
    assert recovered.step is ConversationWorkflowStep.RETRY_READY
    assert recovered.revision == state.revision + 1
    assert repo.mark_conversation_retry_ready.call_args.kwargs == {
        "expected_revision": state.revision
    }
    assert all(
        call.kwargs == {"consistent_read": True}
        for call in repo.get_conversation_state.call_args_list
    )
    assert "reply retry" in api.send_message.call_args.args[1]


def test_revision_recovery_failure_does_not_mask_worker_failure(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    week = date.today()
    state = _revision_state(week)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = make_plan(week_start=week, revision=4)
    repo.get_conversation_state.return_value = state
    repo.mark_conversation_retry_ready.side_effect = RuntimeError(
        "write failed"
    )
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.chat_json_sync.side_effect = RuntimeError("worker failure")

    PlannerHandler(repo, api, llm).revise_plan(
        "user",
        1,
        PlanRevisionContext(
            amendment="Avoid cauliflower",
            request_id="revision-1",
            state_revision=0,
            expected_plan_revision=4,
            week_start=week,
        ),
    )

    assert "reply retry" in api.send_message.call_args.args[1]


def test_revision_failure_does_not_overwrite_newer_workflow_or_notify(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    week = date.today()
    state = _revision_state(week)
    newer_state = state.model_copy(
        update={
            "step": ConversationWorkflowStep.RETRY_READY,
            "revision": 1,
        }
    )
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = make_plan(week_start=week, revision=4)
    repo.get_conversation_state.side_effect = [state, newer_state]
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.chat_json_sync.side_effect = RuntimeError("late worker failure")

    PlannerHandler(repo, api, llm).revise_plan(
        "user",
        1,
        PlanRevisionContext(
            amendment="Avoid cauliflower",
            request_id="revision-1",
            state_revision=0,
            expected_plan_revision=4,
            week_start=week,
        ),
    )

    repo.mark_conversation_retry_ready.assert_not_called()
    api.send_message.assert_not_called()


def test_revision_conflict_clears_and_reports_only_current_owner(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    week = date.today()
    state = _revision_state(week)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = make_plan(week_start=week, revision=5)
    repo.get_conversation_state.side_effect = [state, state]
    repo.clear_conversation_state_if_matches.return_value = True
    api = mocker.MagicMock()

    PlannerHandler(repo, api).revise_plan(
        "user",
        1,
        PlanRevisionContext(
            amendment="Avoid cauliflower",
            request_id="revision-1",
            state_revision=0,
            expected_plan_revision=4,
            week_start=week,
        ),
    )

    repo.clear_conversation_state_if_matches.assert_called_once_with(
        "user", request_id="revision-1", expected_revision=0
    )
    assert "discarded the stale result" in api.send_message.call_args.args[1]


def test_revision_conflict_cleanup_failure_retains_retry_state(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    week = date.today()
    state = _revision_state(week)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = make_plan(week_start=week, revision=5)
    repo.get_conversation_state.return_value = state
    repo.clear_conversation_state_if_matches.side_effect = RuntimeError(
        "delete failed"
    )
    repo.mark_conversation_retry_ready.return_value = True
    api = mocker.MagicMock()

    PlannerHandler(repo, api).revise_plan(
        "user",
        1,
        PlanRevisionContext(
            amendment="Avoid cauliflower",
            request_id="revision-1",
            state_revision=0,
            expected_plan_revision=4,
            week_start=week,
        ),
    )

    retry_state = repo.mark_conversation_retry_ready.call_args.args[1]
    assert retry_state.step is ConversationWorkflowStep.RETRY_READY
    assert retry_state.revision == state.revision + 1
    assert "reply retry" in api.send_message.call_args.args[1]


def test_duplicate_revision_worker_suppresses_losing_conflict_message(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    week = date.today()
    state = _revision_state(week)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = make_plan(week_start=week, revision=4)
    repo.get_conversation_state.side_effect = [state, None]
    repo.replace_draft_and_clear_revision_state.return_value = False
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.chat_json_sync.return_value = make_plan_payload(week)

    PlannerHandler(repo, api, llm).revise_plan(
        "user",
        1,
        PlanRevisionContext(
            amendment="Avoid cauliflower",
            request_id="revision-1",
            state_revision=0,
            expected_plan_revision=4,
            week_start=week,
        ),
    )

    api.send_message.assert_not_called()
    api.send_plan.assert_not_called()


def test_generate_plan_delivery_failure_keeps_persisted_draft(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.save_generated_draft.return_value = True
    api = mocker.MagicMock()
    api.send_plan.side_effect = RuntimeError("offline")
    llm = mocker.MagicMock()
    week = date(2026, 8, 10)
    llm.chat_json_sync.return_value = _complete_plan_payload(week)

    PlannerHandler(repo, api, llm).generate_plan("user", 1, week_start=week)

    repo.save_generated_draft.assert_called_once()
    api.send_plan.assert_called_once()
    api.send_message.assert_not_called()


def test_generate_plan_follow_up_delivery_failure_keeps_persisted_draft(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.save_generated_draft.return_value = True
    api = mocker.MagicMock()
    api.send_message.side_effect = RuntimeError("offline")
    llm = mocker.MagicMock()
    week = date(2026, 8, 10)
    llm.chat_json_sync.return_value = _complete_plan_payload(week)

    PlannerHandler(repo, api, llm).generate_plan("user", 1, week_start=week)

    repo.save_generated_draft.assert_called_once()
    api.send_plan.assert_called_once()
    api.send_message.assert_called_once_with(
        1, "Review this draft, request edits, then tell me to confirm it."
    )


def test_generate_plan_normalizes_provider_lifecycle_fields(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    week = date(2026, 8, 10)
    payload = _complete_plan_payload(week)
    payload["status"] = PlanStatus.CONFIRMED.value
    payload["revision"] = 9
    payload["grocery_status"] = GroceryStatus.READY.value
    payload["grocery_list"] = [{"name": "Produce", "items": ["Apples"]}]
    payload["days"][0]["meals"][0]["outcome"] = MealOutcome.COOKED.value
    payload["days"][1]["meals"][0]["outcome"] = MealOutcome.SKIPPED.value
    payload["days"][2]["meals"][0]["outcome"] = MealOutcome.SWAPPED.value
    llm.chat_json_sync.return_value = payload
    repo.save_generated_draft.return_value = True

    PlannerHandler(repo, api, llm).generate_plan("user", 1, week_start=week)

    saved = repo.save_generated_draft.call_args.args[1]
    sent = api.send_plan.call_args.args[1]
    assert saved.status is PlanStatus.DRAFT
    assert saved.revision == 0
    assert saved.grocery_status is GroceryStatus.NOT_REQUESTED
    assert saved.grocery_list == []
    assert all(
        meal.outcome is MealOutcome.UNREPORTED
        for plan_day in saved.days
        for meal in plan_day.meals
    )
    assert sent is saved


def test_generate_plan_rejects_missing_profile_and_malformed_plan(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    api = mocker.MagicMock()
    repo.get_profile.return_value = None
    PlannerHandler(repo, api).generate_plan("user", 1)
    repo.save_plan.assert_not_called()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    llm = mocker.MagicMock()
    llm.chat_json_sync.return_value = {}
    PlannerHandler(repo, api, llm).generate_plan(
        "user", 1, week_start=date(2026, 8, 10)
    )
    repo.save_generated_draft.assert_not_called()


def test_generate_plan_rejects_ambiguous_daily_meals(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    payload = make_plan_payload(date(2026, 8, 10))
    payload["days"][0]["meals"].append(payload["days"][0]["meals"][0])
    llm.chat_json_sync.return_value = payload

    PlannerHandler(repo, api, llm).generate_plan(
        "user", 1, week_start=date(2026, 8, 10)
    )

    repo.save_generated_draft.assert_not_called()
    api.send_plan.assert_not_called()
    assert "valid meal plan" in api.send_message.call_args.args[1]


def test_late_generation_result_does_not_replace_confirmed_week(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.save_generated_draft.return_value = False
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.chat_json_sync.return_value = _complete_plan_payload(date(2026, 8, 10))
    PlannerHandler(repo, api, llm).generate_plan(
        "user", 1, week_start=date(2026, 8, 10)
    )
    repo.save_generated_draft.assert_called_once()
    api.send_plan.assert_not_called()
    assert "discarded the stale result" in api.send_message.call_args.args[1]


def test_generate_plan_uses_draft_revision_snapshot(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    existing = make_plan(week_start=week, revision=4)
    events: list[str] = []
    repo.get_profile.return_value = make_profile()
    repo.get_plan.side_effect = lambda *_args, **_kwargs: (
        events.append("snapshot") or existing
    )
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = existing
    repo.save_generated_draft.return_value = True
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.chat_json_sync.side_effect = lambda *_args: (
        events.append("llm") or _complete_plan_payload(week)
    )

    PlannerHandler(repo, api, llm).generate_plan("user", 1, week_start=week)

    saved = repo.save_generated_draft.call_args.args[1]
    assert saved.revision == 5
    assert events == ["snapshot", "llm"]
    assert repo.get_plan.call_args.kwargs == {"consistent_read": True}
    repo.save_generated_draft.assert_called_once_with(
        "user", saved, expected_revision=4
    )


def test_generate_plan_skips_llm_for_confirmed_exact_week(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = make_plan(
        week_start=week, status=PlanStatus.CONFIRMED
    )
    api = mocker.MagicMock()
    llm = mocker.MagicMock()

    PlannerHandler(repo, api, llm).generate_plan("user", 1, week_start=week)

    llm.chat_json_sync.assert_not_called()
    repo.get_plan.assert_called_once_with("user", week, consistent_read=True)
    repo.get_meal_history.assert_not_called()
    repo.save_generated_draft.assert_not_called()
    api.send_plan.assert_not_called()
    repo.clear_conversation_state_if_matches.assert_not_called()


def test_confirmed_stateful_plan_clears_matching_request(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    repo.get_profile.return_value = make_profile()
    repo.get_conversation_state.return_value = _plan_request_state(
        week, request_id="request-1", revision=3
    )
    repo.get_plan.return_value = make_plan(
        week_start=week, status=PlanStatus.CONFIRMED
    )
    api = mocker.MagicMock()

    PlannerHandler(repo, api).generate_plan(
        "user",
        1,
        week_start=week,
        request_id="request-1",
        state_revision=3,
    )

    repo.clear_conversation_state_if_matches.assert_called_once_with(
        "user", request_id="request-1", expected_revision=3
    )
    assert "already confirmed" in api.send_message.call_args.args[1]


def test_confirmed_stateful_plan_does_not_delete_when_cleanup_loses_race(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    repo.get_profile.return_value = make_profile()
    repo.get_conversation_state.return_value = _plan_request_state(
        week, request_id="request-1", revision=3
    )
    repo.get_plan.return_value = make_plan(
        week_start=week, status=PlanStatus.CONFIRMED
    )
    repo.clear_conversation_state_if_matches.return_value = False
    api = mocker.MagicMock()

    PlannerHandler(repo, api).generate_plan(
        "user",
        1,
        week_start=week,
        request_id="request-1",
        state_revision=3,
    )

    repo.clear_conversation_state_if_matches.assert_called_once()
    assert "already confirmed" in api.send_message.call_args.args[1]


def test_planner_attempt_limit_one_stops_after_transient_failure(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.chat_json_sync.side_effect = LLMTransientError("temporary")

    PlannerHandler(repo, api, llm, max_attempts=1).generate_plan(
        "user", 1, week_start=date(2026, 8, 10)
    )

    assert llm.chat_json_sync.call_count == 1
    repo.save_generated_draft.assert_not_called()
    assert "temporarily unavailable" in api.send_message.call_args.args[1]


def test_planner_timeout_makes_one_call_and_retains_retry_state(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    state = _plan_request_state(week)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.get_conversation_state.return_value = state
    repo.mark_conversation_retry_ready.return_value = True
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.chat_json_sync.side_effect = LLMTimeoutError(
        "preference=low sodium prompt content"
    )

    PlannerHandler(repo, api, llm).generate_plan(
        "user",
        1,
        week_start=week,
        preference="low sodium",
        request_id=state.request_id,
        state_revision=state.revision,
    )

    assert llm.chat_json_sync.call_count == 1
    retry_state = repo.mark_conversation_retry_ready.call_args.args[1]
    assert retry_state.step is ConversationWorkflowStep.RETRY_READY
    assert retry_state.revision == state.revision + 1
    repo.mark_conversation_retry_ready.assert_called_once_with(
        "user", retry_state, expected_revision=state.revision
    )
    assert "/plan to retry" in api.send_message.call_args.args[1]


def test_planner_attempt_limit_one_does_not_repair_invalid_output(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.chat_json_sync.return_value = {}

    PlannerHandler(repo, api, llm, max_attempts=1).generate_plan(
        "user", 1, week_start=date(2026, 8, 10)
    )

    assert llm.chat_json_sync.call_count == 1
    assert "invalid meal plan" in api.send_message.call_args.args[1]


def test_planner_default_attempt_limit_does_not_repair_invalid_output(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.save_generated_draft.return_value = True
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.chat_json_sync.side_effect = [{}, make_plan_payload(week)]

    PlannerHandler(repo, api, llm).generate_plan("user", 1, week_start=week)

    assert llm.chat_json_sync.call_count == 1
    repo.save_generated_draft.assert_not_called()
    assert "invalid meal plan" in api.send_message.call_args.args[1]


def test_planner_attempt_limit_does_not_add_in_invocation_provider_calls(
    mocker: Any,
) -> None:
    """The configured attempt limit cannot create an in-process retry."""
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.chat_json_sync.side_effect = [
        {},
        _complete_plan_payload(date(2026, 8, 10)),
    ]

    PlannerHandler(repo, api, llm, max_attempts=2).generate_plan(
        "user", 1, week_start=date(2026, 8, 10)
    )

    llm.chat_json_sync.assert_called_once()
    repo.save_generated_draft.assert_not_called()


def test_planner_failure_log_contains_only_operational_context(
    mocker: Any, caplog: pytest.LogCaptureFixture
) -> None:
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.model = "gpt-5.6-luna"
    llm.chat_json_sync.side_effect = LLMTimeoutError(
        "system prompt, preference, plan, credential, chat 42, user 7"
    )
    mocker.patch(
        "meal_planner.planner_handler.time.monotonic",
        side_effect=[1.0, 10.0, 12.345, 13.0],
    )

    with caplog.at_level(
        logging.WARNING, logger="meal_planner.planner_handler"
    ):
        PlannerHandler(repo, api, llm).generate_plan(
            "user-7",
            42,
            week_start=date(2026, 8, 10),
            preference="secret preference",
        )

    records = [
        record
        for record in caplog.records
        if record.name == "meal_planner.planner_handler"
        and record.message.startswith("Planner LLM attempt failed")
    ]
    assert len(records) == 1
    record = records[0]
    assert record.attempt == 1
    assert record.elapsed_ms == pytest.approx(2345.0)
    assert record.model == "gpt-5.6-luna"
    assert record.category == "timeout"
    assert "system prompt" not in caplog.text
    assert "secret preference" not in caplog.text
    assert "chat 42" not in caplog.text
    assert "user 7" not in caplog.text


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gpt-5.6-luna", "gpt-5.6-luna"),
        ("a" * 64, "a" * 64),
        ("a" * 65, "unknown"),
        ("", "unknown"),
        ("   ", "unknown"),
        (None, "unknown"),
        ("gpt 5.6", "unknown"),
        ("gpt\t5.6", "unknown"),
        ("gpt\r5.6", "unknown"),
        ("gpt\n5.6", "unknown"),
        ("gpt\x005.6", "unknown"),
    ],
)
def test_model_name_accepts_only_bounded_safe_labels(
    mocker: Any, model: object, expected: str
) -> None:
    client = mocker.MagicMock()
    client.model = model

    assert PlannerHandler._model_name(client) == expected


@pytest.mark.parametrize(
    ("unsafe_model", "unsafe_marker"),
    [
        ("a" * 65 + "UNSAFE_SUFFIX", "UNSAFE_SUFFIX"),
        ("gpt-5.6-luna\nINJECTED", "INJECTED"),
    ],
)
def test_failure_log_sanitizes_unsafe_model_labels(
    mocker: Any,
    caplog: pytest.LogCaptureFixture,
    unsafe_model: str,
    unsafe_marker: str,
) -> None:
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.model = unsafe_model
    llm.chat_json_sync.side_effect = LLMTimeoutError("provider failed")

    with caplog.at_level(
        logging.WARNING, logger="meal_planner.planner_handler"
    ):
        PlannerHandler(repo, api, llm).generate_plan(
            "user-7",
            42,
            week_start=date(2026, 8, 10),
            preference="secret preference",
        )

    records = [
        record
        for record in caplog.records
        if record.name == "meal_planner.planner_handler"
        and record.message.startswith("Planner LLM attempt failed")
    ]
    assert len(records) == 1
    record = records[0]
    assert record.model == "unknown"
    assert len(record.message) <= 256
    assert unsafe_marker not in record.message
    assert unsafe_marker not in str(record.__dict__)


def test_planner_success_does_not_emit_failure_log(
    mocker: Any, caplog: pytest.LogCaptureFixture
) -> None:
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.save_generated_draft.return_value = True
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.model = "gpt-5.6-luna"
    llm.chat_json_sync.return_value = _complete_plan_payload(date(2026, 8, 10))

    with caplog.at_level(
        logging.WARNING, logger="meal_planner.planner_handler"
    ):
        PlannerHandler(repo, api, llm).generate_plan(
            "user", 1, week_start=date(2026, 8, 10)
        )

    assert not any(
        record.message.startswith("Planner LLM attempt failed")
        for record in caplog.records
    )


def test_planner_rejects_non_positive_attempt_limit(mocker: Any) -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        PlannerHandler(mocker.MagicMock(), mocker.MagicMock(), max_attempts=0)


def test_generate_plan_notifies_when_snapshot_read_fails(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.side_effect = RuntimeError("database unavailable")
    api = mocker.MagicMock()
    llm = mocker.MagicMock()

    PlannerHandler(repo, api, llm).generate_plan(
        "user", 1, week_start=date(2026, 8, 10)
    )

    repo.get_plan.assert_called_once_with(
        "user", date(2026, 8, 10), consistent_read=True
    )
    llm.chat_json_sync.assert_not_called()
    repo.save_generated_draft.assert_not_called()
    assert (
        api.send_message.call_args.args[1]
        == "Sorry, an error occurred while generating your plan."
    )


def test_generate_plan_does_not_send_rejected_stale_result(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = make_plan(week_start=week, revision=2)
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.save_generated_draft.return_value = False
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.chat_json_sync.return_value = _complete_plan_payload(week)

    PlannerHandler(repo, api, llm).generate_plan("user", 1, week_start=week)

    api.send_plan.assert_not_called()
    assert "discarded the stale result" in api.send_message.call_args.args[1]


def test_finalize_grocery_success(mocker: Any) -> None:
    repo = mocker.MagicMock()
    plan = make_plan(
        status=PlanStatus.CONFIRMED, grocery_status=GroceryStatus.PENDING
    )
    repo.get_plan.return_value = plan
    repo.get_profile.return_value = make_profile()
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.chat_json_sync.return_value = {
        "sections": [{"name": "Produce", "items": ["Apples"]}]
    }
    repo.complete_grocery.return_value = True
    PlannerHandler(repo, api, llm).finalize_grocery(
        "user", 1, plan.week_start_date
    )
    repo.get_plan.assert_called_once_with(
        "user", plan.week_start_date, consistent_read=True
    )
    repo.complete_grocery.assert_called_once()
    assert repo.complete_grocery.call_args.args[2] == plan.revision
    assert repo.complete_grocery.call_args.args[3][0].name == "Produce"


def test_finalize_grocery_uses_dedicated_llm_client(mocker: Any) -> None:
    repo = mocker.MagicMock()
    plan = make_plan(
        status=PlanStatus.CONFIRMED, grocery_status=GroceryStatus.PENDING
    )
    repo.get_plan.return_value = plan
    repo.get_profile.return_value = make_profile()
    repo.complete_grocery.return_value = True
    api = mocker.MagicMock()
    plan_llm = mocker.MagicMock()
    grocery_llm = mocker.MagicMock()
    grocery_llm.chat_json_sync.return_value = {
        "sections": [{"name": "Produce", "items": ["Apples"]}]
    }

    PlannerHandler(
        repo, api, plan_llm, grocery_llm_client=grocery_llm
    ).finalize_grocery("user", 1, plan.week_start_date)

    plan_llm.chat_json_sync.assert_not_called()
    grocery_llm.chat_json_sync.assert_called_once()


@pytest.mark.parametrize(
    "grocery_status", [GroceryStatus.READY, GroceryStatus.ERROR]
)
def test_duplicate_non_pending_grocery_event_is_silent(
    mocker: Any, grocery_status: GroceryStatus
) -> None:
    repo = mocker.MagicMock()
    plan = make_plan(status=PlanStatus.CONFIRMED, grocery_status=grocery_status)
    repo.get_plan.return_value = plan
    api = mocker.MagicMock()
    llm = mocker.MagicMock()

    PlannerHandler(repo, api, llm).finalize_grocery(
        "user", 1, plan.week_start_date
    )

    repo.get_profile.assert_not_called()
    llm.chat_json_sync.assert_not_called()
    repo.complete_grocery.assert_not_called()
    repo.fail_grocery.assert_not_called()
    api.send_message.assert_not_called()


def test_finalize_grocery_marks_errors_and_rejects_stale_week(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    api = mocker.MagicMock()
    repo.get_plan.return_value = None
    PlannerHandler(repo, api).finalize_grocery("user", 1, "2026-08-10")
    repo.save_plan.assert_not_called()
    plan = make_plan(
        status=PlanStatus.CONFIRMED, grocery_status=GroceryStatus.PENDING
    )
    repo.get_plan.return_value = plan
    repo.get_profile.return_value = make_profile()
    llm = mocker.MagicMock()
    llm.chat_json_sync.return_value = {"sections": []}
    repo.fail_grocery.return_value = True
    PlannerHandler(repo, api, llm).finalize_grocery(
        "user", 1, plan.week_start_date
    )
    repo.fail_grocery.assert_called_once_with(
        "user", plan.week_start_date, plan.revision
    )


def test_ready_groceries_survive_notification_failure(mocker: Any) -> None:
    repo = mocker.MagicMock()
    plan = make_plan(
        status=PlanStatus.CONFIRMED, grocery_status=GroceryStatus.PENDING
    )
    repo.get_plan.return_value = plan
    repo.get_profile.return_value = make_profile()
    repo.complete_grocery.return_value = True
    api = mocker.MagicMock()
    api.send_message.side_effect = RuntimeError("offline")
    llm = mocker.MagicMock()
    llm.chat_json_sync.return_value = {
        "sections": [{"name": "Produce", "items": ["Apples"]}]
    }
    PlannerHandler(repo, api, llm).finalize_grocery(
        "user", 1, plan.week_start_date
    )
    repo.complete_grocery.assert_called_once()
    repo.fail_grocery.assert_not_called()


def test_handle_event_validates_actions(mocker: Any) -> None:
    planner = PlannerHandler(mocker.MagicMock(), mocker.MagicMock())
    generate = mocker.patch.object(planner, "generate_plan")
    finalize = mocker.patch.object(planner, "finalize_grocery")
    assert planner.handle_event(
        {
            "action": "generate_plan",
            "user_id": "user",
            "chat_id": 1,
            "week_start": "2026-08-10",
        }
    )
    generate.assert_called_once()
    assert planner.handle_event(
        {
            "action": "finalize_grocery",
            "user_id": "user",
            "chat_id": 1,
            "week_start": "2026-08-10",
        }
    )
    finalize.assert_called_once()
    assert not planner.handle_event({"action": "unknown"})


def test_handle_event_forwards_requirements_and_repair_metadata(
    mocker: Any,
) -> None:
    """Planner events validate and forward typed generation metadata."""
    planner = PlannerHandler(mocker.MagicMock(), mocker.MagicMock())
    generate = mocker.patch.object(planner, "generate_plan")
    event = {
        "action": "generate_plan",
        "user_id": "user",
        "chat_id": 1,
        "week_start": "2026-08-10",
        "preference": "eggs three times",
        "requirements": [
            {
                "id": "r1",
                "source_text": "eggs three times",
                "foods_any_of": ["eggs"],
                "meal_type": None,
                "exact_count": 3,
            }
        ],
        "attempt": 2,
        "repair_feedback": "r1 matched 2; expected exactly 3",
        "repair_id": "repair-123",
    }

    assert planner.handle_event(event)

    assert generate.call_args.kwargs == {
        "week_start": date(2026, 8, 10),
        "preference": "eggs three times",
        "requirements": [
            PreferenceRequirement(
                id="r1",
                source_text="eggs three times",
                foods_any_of=["eggs"],
                exact_count=3,
            )
        ],
        "stored_rules": [],
        "current_rules": [],
        "effective_rules": [],
        "constraint_rules": [],
        "attempt": 2,
        "repair_feedback": "r1 matched 2; expected exactly 3",
        "request_id": None,
        "state_revision": None,
        "repair_id": "repair-123",
    }


def test_handle_event_preserves_distinct_effective_rule_ids(
    mocker: Any,
) -> None:
    """The planner accepts the ordered, unique partial-scope snapshot."""
    planner = PlannerHandler(mocker.MagicMock(), mocker.MagicMock())
    generate = mocker.patch.object(planner, "generate_plan")
    event = {
        "action": "generate_plan",
        "user_id": "user",
        "chat_id": 1,
        "week_start": "2026-08-10",
        "preference": "eggs",
        "effective_rules": [
            {
                "id": "r-current-wednesday",
                "source_text": "eggs at most once Wednesday",
                "foods_any_of": ["egg"],
                "weekdays": [3],
                "operator": "at_most",
                "count": 1,
                "strength": "strict",
            },
            {
                "id": "r-stored-weekdays",
                "source_text": "eggs weekdays",
                "foods_any_of": ["egg"],
                "weekdays": [1, 2, 3, 4, 5],
                "operator": "exactly",
                "count": 4,
                "strength": "strict",
            },
        ],
    }

    assert planner.handle_event(event)

    forwarded = generate.call_args.kwargs["effective_rules"]
    assert [rule.id for rule in forwarded] == [
        "r-current-wednesday",
        "r-stored-weekdays",
    ]


@pytest.mark.parametrize(
    "field_value",
    [
        {"attempt": 0},
        {"attempt": 3},
        {"repair_feedback": "feedback"},
        {"repair_feedback": "x" * 801},
        {"attempt": 2},
        {"attempt": 2, "repair_feedback": None},
        {"attempt": 2, "repair_feedback": "   "},
        {
            "requirements": [
                {
                    "id": "bad id",
                    "source_text": "eggs once",
                    "foods_any_of": ["eggs"],
                    "exact_count": 1,
                }
            ]
        },
    ],
)
def test_handle_event_rejects_malformed_generation_metadata(
    mocker: Any, field_value: dict[str, object]
) -> None:
    """Malformed nested rules and repair metadata never reach generation."""
    planner = PlannerHandler(mocker.MagicMock(), mocker.MagicMock())
    generate = mocker.patch.object(planner, "generate_plan")
    event: dict[str, object] = {
        "action": "generate_plan",
        "user_id": "user",
        "chat_id": 1,
        "week_start": "2026-08-10",
    }
    event.update(field_value)

    assert not planner.handle_event(event)
    generate.assert_not_called()


@pytest.mark.parametrize(
    "repair_feedback",
    [None, "   "],
)
def test_handle_event_rejects_malformed_attempt_two_before_side_effects(
    mocker: Any, repair_feedback: str | None
) -> None:
    """Malformed repair events do not reach provider or delivery paths."""
    repo = mocker.MagicMock()
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    planner = PlannerHandler(repo, api, llm)
    event: dict[str, object] = {
        "action": "generate_plan",
        "user_id": "user",
        "chat_id": 1,
        "week_start": "2026-08-10",
        "attempt": 2,
        "repair_feedback": repair_feedback,
    }

    assert not planner.handle_event(event)
    llm.chat_json_sync.assert_not_called()
    repo.assert_not_called()
    api.assert_not_called()


def test_legacy_generation_event_remains_compatible(mocker: Any) -> None:
    """Old events may omit new optional generation metadata."""
    planner = PlannerHandler(mocker.MagicMock(), mocker.MagicMock())
    generate = mocker.patch.object(planner, "generate_plan")

    assert planner.handle_event(
        {
            "action": "generate_plan",
            "user_id": "user",
            "chat_id": 1,
            "week_start": "2026-08-10",
            "preference": "low sodium",
        }
    )
    assert generate.call_args.kwargs["requirements"] == []
    assert generate.call_args.kwargs["attempt"] == 1
    assert generate.call_args.kwargs["repair_feedback"] is None


def test_lambda_handler_dispatches_and_rejects_invalid_event(
    mocker: Any, mock_env: None
) -> None:
    mocker.patch("boto3.resource")
    planner_class = mocker.patch("meal_planner.planner_handler.PlannerHandler")
    planner_class.return_value.handle_event.return_value = True
    assert (
        lambda_handler({"user_id": "user", "chat_id": 1}, None)["statusCode"]
        == 200
    )
    assert planner_class.call_args.kwargs["max_attempts"] == 1
    planner_class.return_value.handle_event.return_value = False
    assert lambda_handler({}, None)["statusCode"] == 400


def test_lambda_handler_configures_independent_grocery_client(
    mocker: Any, mock_env: None
) -> None:
    mocker.patch("boto3.resource")
    client_class = mocker.patch("meal_planner.planner_handler.LLMClient")
    plan_client = mocker.MagicMock()
    grocery_client = mocker.MagicMock()
    client_class.side_effect = [plan_client, grocery_client]
    planner_class = mocker.patch("meal_planner.planner_handler.PlannerHandler")
    planner_class.return_value.handle_event.return_value = True

    assert (
        lambda_handler({"user_id": "user", "chat_id": 1}, None)["statusCode"]
        == 200
    )

    assert client_class.call_args_list[0].kwargs["max_retries"] == 1
    assert client_class.call_args_list[0].kwargs["request_timeout"] == 240.0
    assert client_class.call_args_list[1].kwargs["max_retries"] == 2
    assert client_class.call_args_list[1].kwargs["request_timeout"] == 120.0
    assert planner_class.call_args.args[2] is plan_client
    assert (
        planner_class.call_args.kwargs["grocery_llm_client"] is grocery_client
    )


def test_planner_deadline_raises_when_the_alarm_fires(mocker: Any) -> None:
    mocker.patch(
        "meal_planner.planner_handler.signal.getsignal",
        return_value=signal.SIG_DFL,
    )
    set_signal = mocker.patch("meal_planner.planner_handler.signal.signal")
    set_timer = mocker.patch("meal_planner.planner_handler.signal.setitimer")

    with pytest.raises(PlannerDeadlineExceeded):
        with planner_deadline(300.0):
            deadline_handler = set_signal.call_args.args[1]
            deadline_handler(signal.SIGALRM, None)

    assert set_timer.call_args_list[0].args == (signal.ITIMER_REAL, 300.0)
    assert set_timer.call_args_list[1].args == (signal.ITIMER_REAL, 0.0)
    assert set_signal.call_args_list[-1].args == (
        signal.SIGALRM,
        signal.SIG_DFL,
    )


def test_lambda_handler_returns_timeout_after_planner_deadline(
    mocker: Any, mock_env: None
) -> None:
    mocker.patch("boto3.resource")
    deadline = mocker.patch("meal_planner.planner_handler.planner_deadline")
    planner_class = mocker.patch("meal_planner.planner_handler.PlannerHandler")
    planner_class.return_value.handle_event.side_effect = (
        PlannerDeadlineExceeded
    )

    result = lambda_handler({"user_id": "user", "chat_id": 1}, None)

    assert result == {"statusCode": 504, "body": "planner deadline exceeded"}
    deadline.assert_called_once_with(300.0)
