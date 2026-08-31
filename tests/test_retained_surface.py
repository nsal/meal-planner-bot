"""Import-smoke tests for the intentionally small public surface."""

import inspect
from importlib import import_module

import meal_planner.llm as llm
import meal_planner.models as models
from meal_planner.bot_handler import BotHandler
from meal_planner.db.dynamo import DynamoRepository


def test_retained_modules_import_without_legacy_dependencies() -> None:
    """All active workflow modules import after deletion cleanup."""
    for module_name in (
        "meal_planner.bot_handler",
        "meal_planner.db.dynamo",
        "meal_planner.llm.client",
        "meal_planner.llm.prompts",
        "meal_planner.plan_chat_handler",
        "meal_planner.router",
        "meal_planner.telegram.api",
        "meal_planner.telegram.commands",
    ):
        assert import_module(module_name)


def test_model_and_llm_exports_are_explicit() -> None:
    """Exports enumerate only models and client classes still in use."""
    assert models.__all__ == [
        "ConversationState",
        "ConversationWorkflowKind",
        "ConversationWorkflowStep",
        "FamilyMember",
        "MAX_PLAN_CHAT_MESSAGE_LENGTH",
        "MAX_PLAN_CHAT_REQUEST_LENGTH",
        "MAX_PLAN_CHAT_RESPONSE_LENGTH",
        "MealCallbackAction",
        "MealLogDraft",
        "MealLogEntry",
        "MealType",
        "PlanChatAction",
        "PlanChatEvent",
        "ProfileDraft",
        "ProfileEditCategory",
        "ProfileEditOperation",
        "UserProfile",
    ]
    assert llm.__all__ == [
        "LLMClient",
        "LLMFailure",
        "LLMPermanentError",
        "LLMTextResponseError",
        "LLMTimeoutError",
        "LLMTransientError",
    ]


def test_dead_field_by_field_meal_workflow_is_not_retained() -> None:
    """Retired field-by-field meal symbols stay outside the public surface."""
    assert not hasattr(BotHandler, "_handle_meal_workflow")
    assert not hasattr(DynamoRepository, "log_meal_and_transition")


def test_structured_meal_submission_is_the_only_writable_meal_path() -> None:
    """The handler writes meals through structured confirmation only."""
    source = inspect.getsource(BotHandler)
    assert "_handle_structured_meal_input" in source
    assert "confirm_meal_and_transition" in source
    assert "log_meal_and_transition" not in source
