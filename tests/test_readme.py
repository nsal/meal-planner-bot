"""Boundary tests for the retained product and deployment surface."""

import ast
from datetime import timedelta
from pathlib import Path
from typing import Iterable

from meal_planner.llm.prompts import build_plan_chat_prompt
from meal_planner.models import MealType
from meal_planner.telegram.commands import BOT_COMMANDS, render_help
from tests.factories import make_meal, make_profile

PROJECT_ROOT = Path(__file__).parents[1]
README_PATH = PROJECT_ROOT / "README.md"
SOURCE_ROOT = PROJECT_ROOT / "src" / "meal_planner"
SCRIPT_ROOT = PROJECT_ROOT / "scripts"
TEST_ROOT = PROJECT_ROOT / "tests"
ORIGINAL_ACTIVE_PLAN_PATH = (
    PROJECT_ROOT
    / "docs"
    / "plans"
    / "2026-08-28-simplify-meal-planning-to-conversational-drafts.md"
)
ORIGINAL_COMPLETED_PLAN_PATH = (
    ORIGINAL_ACTIVE_PLAN_PATH.parent
    / "completed"
    / ORIGINAL_ACTIVE_PLAN_PATH.name
)
REMEDIATION_ACTIVE_PLAN_PATH = (
    PROJECT_ROOT
    / "docs"
    / "plans"
    / "2026-08-31-conversational-simplification-review-remediation.md"
)
REMEDIATION_COMPLETED_PLAN_PATH = (
    REMEDIATION_ACTIVE_PLAN_PATH.parent
    / "completed"
    / REMEDIATION_ACTIVE_PLAN_PATH.name
)

RETAINED_COMMANDS = ("start", "help", "profile", "plan", "submit_meals")
DELETED_MODULES = (
    "dietary_rules",
    "preferences",
    "normalization",
    "llm.parser",
    "planner_handler",
)
DELETED_TESTS = (
    "test_dietary_rules.py",
    "test_preferences.py",
    "test_parser.py",
    "test_planner_handler.py",
    "test_reset_profile_dietary_fields.py",
)
DELETED_SCRIPTS = ("reset_profile_dietary_fields.py",)
DELETED_SYMBOLS = frozenset(
    {
        "PlanStatus",
        "GroceryStatus",
        "PlanDays",
        "PlanInstruction",
        "PlanningInstruction",
        "PlanGenerationContext",
        "PlanRevisionContext",
        "RevisionEventContext",
        "Ingredient",
        "PlannedMeal",
        "PlanDay",
        "GrocerySection",
        "WeeklyPlan",
        "DietaryRule",
        "ConstraintEntry",
        "DietaryPreferenceEntry",
        "DietaryObligation",
        "PreferenceRequirement",
        "RuleOperator",
        "RuleStrength",
        "RuleCadence",
        "ScheduleKind",
        "Weekday",
        "BatchRule",
        "PlannedBatchLink",
        "SubmittedMealBatchLink",
        "BatchLedgerEntry",
        "WeeklyBatchLedger",
        "BatchMealRole",
        "BatchLedgerState",
        "ConversationIntent",
        "LLMResponseMetadata",
        "ProfileUpdateEntities",
        "MealOutcome",
        "WorkflowKind",
        "WorkflowStep",
        "PartialMealLog",
        "RulePeriod",
        "ScheduleSource",
        "BatchRole",
        "BatchState",
        "BatchReuseRule",
        "ProjectedDietaryObligation",
        "BatchLedger",
        "MealBatchLink",
        "RequestId",
        "RequirementId",
        "PreferenceFood",
        "RepairFeedback",
        "MAX_PLAN_REQUIREMENTS",
        "MAX_PLAN_OBLIGATIONS",
        "MAX_MEALS_PER_DAY",
        "MAX_BATCH_LEDGER_ENTRIES",
        "ISO_WEEK",
        "daily_meal_capacity",
        "application_owned_dietary_rule_id",
        "application_owned_constraint_id",
        "application_owned_text_id",
        "canonicalize_dietary_rule",
        "canonicalize_constraint_entry",
        "canonicalize_profile_rule_ids",
        "_cmd_grocery",
        "_cmd_today",
        "_cmd_checkin",
        "_cmd_cancel",
        "_get_todays_plan_day",
        "_handle_plan_preference",
        "_parse_initial_plan_response",
        "_plan_progress_message",
        "_collect_stored_preference_rules",
        "_collect_stored_batch_rules",
        "_snapshot_effective_rules",
        "_retry_plan_request",
        "_confirm_plan",
        "_edit_plan",
        "_handle_plan_revision_state",
        "_start_plan_revision",
        "_retry_plan_revision",
        "_is_eligible_draft",
        "_is_active_confirmed_plan",
        "_apply_intent_metadata",
        "_update_profile",
        "mark_conversation_retry_ready",
        "start_plan_revision",
        "has_plan_revision_update_marker",
        "get_submitted_meals",
        "get_meal_history_between",
        "get_meal_history_for_range",
        "save_plan",
        "save_generated_draft",
        "save_generated_draft_and_clear_conversation_state",
        "save_repaired_draft_once",
        "replace_draft_and_clear_revision_state",
        "confirm_plan",
        "get_plan",
        "get_latest_plan",
        "get_active_plan",
        "get_active_plan_snapshot",
        "update_meal",
        "update_meal_outcome",
        "retry_grocery",
        "complete_grocery",
        "fail_grocery",
        "_update_grocery_state",
        "_batch_ledger_key",
        "_iso_week_bounds",
        "get_weekly_batch_ledger",
        "_materialize_weekly_batch_expiry",
        "put_weekly_batch_ledger",
        "save_weekly_batch_ledger",
        "_put_weekly_batch_ledger_conditionally",
        "_batch_ledger_transaction_items",
        "get_available_batch_portions",
        "_batch_submission_ledger_item",
        "_repair_marker_key",
        "_repair_publication_outcome",
        "get_planned_batch_link",
        "CheckinCallback",
        "parse_checkin_callback",
        "send_plan",
        "send_grocery_list",
        "send_meal_checkin",
        "send_profile_rule_review",
        "normalize_legacy_batch_fields",
    }
)
ACTIVE_DOCUMENTATION = (
    PROJECT_ROOT / "README.md",
    PROJECT_ROOT / "docs" / "prompt.md",
    PROJECT_ROOT / "docs" / "plan-chat-migration-prework.md",
)
RETIRED_DOCUMENTATION_MARKERS = (
    "Planner Lambda",
    "/grocery",
    "/today",
    "/checkin",
    "/cancel",
    "PLANNER_LLM_MODEL",
    "CONVERSATIONAL_LLM_MODEL",
    "BOT_LLM_REQUEST_TIMEOUT_SECONDS",
    "PLANNER_GROCERY_LLM_REQUEST_TIMEOUT_SECONDS",
    "Batch cooking",
    "Repair generation",
    "automatic repair",
    '"batch_link"',
    "planner_handler.py",
    "application validates constraints",
)


def _read_readme() -> str:
    """Load the user-facing README for retained-contract assertions."""
    return README_PATH.read_text(encoding="utf-8")


def _active_python_files() -> tuple[Path, ...]:
    """Return active implementation, operator, and test Python files."""
    return tuple(
        sorted(
            (
                *SOURCE_ROOT.rglob("*.py"),
                *SCRIPT_ROOT.glob("*.py"),
                *TEST_ROOT.glob("*.py"),
            )
        )
    )


def _defined_and_imported_names(tree: ast.AST) -> Iterable[str]:
    """Yield exact names defined or imported by one Python module."""
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            yield node.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.asname or alias.name.rsplit(".", maxsplit=1)[-1]
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                yield alias.asname or alias.name


def test_retained_command_set_is_exact_and_documented_by_help() -> None:
    """The command menu and help expose exactly the retained five commands."""
    command_names = tuple(command.name for command in BOT_COMMANDS)

    assert command_names == RETAINED_COMMANDS
    help_text = render_help()
    documented_names = tuple(
        line.split(" — ", maxsplit=1)[0] for line in help_text.splitlines()
    )
    assert documented_names == tuple(f"/{name}" for name in RETAINED_COMMANDS)
    assert "/cancel" not in help_text
    assert "/grocery" not in help_text
    assert "/today" not in help_text
    assert "/checkin" not in help_text


def test_active_surface_keeps_draft_disclaimer_and_retained_workflows() -> None:
    """The active surface retains the draft disclaimer and workflows."""
    readme = " ".join(_read_readme().split())
    bot = (SOURCE_ROOT / "bot_handler.py").read_text(encoding="utf-8")

    assert "variable-length meal plan" in readme
    assert "/plan" in readme
    assert "/submit_meals" in readme
    assert "Drafts are suggestions, not confirmed plans." in bot


def test_readme_documents_inclusive_submitted_meal_date_window() -> None:
    """Document all eight accepted UTC calendar dates explicitly."""
    readme = " ".join(_read_readme().split()).casefold()

    assert (
        "from utc today through the previous seven dates, inclusive "
        "(eight calendar dates)"
    ) in readme


def test_plan_chat_prompt_contract_has_raw_context_and_21_day_history() -> None:
    """The documented prompt boundary is reflected in the live prompt."""
    context_date = make_meal().date
    prompt = build_plan_chat_prompt(
        profile=make_profile(
            dietary_constraints=["No peanuts"],
            dietary_preferences=["Simple meals"],
        ),
        meal_history=[
            make_meal(context_date, MealType.DINNER, "Pasta"),
            make_meal(
                context_date - timedelta(days=21),
                MealType.LUNCH,
                "Old enough",
            ),
        ],
        initial_request="Plan three dinners",
        latest_response="Previous draft",
        pending_message="Make one vegetarian",
        context_date=context_date,
    )

    assert "No peanuts" in prompt
    assert "Simple meals" in prompt
    assert "Pasta" in prompt
    assert "Old enough" not in prompt
    assert "2026-08-08 through 2026-08-28" in prompt
    assert "Original request:" in prompt
    assert "Previous draft response:" in prompt
    assert "Current instruction:" in prompt
    assert "editable draft" in prompt
    assert "preference evidence, not an obligation" in prompt


def test_prompt_documentation_describes_dietary_delimiter_normalization() -> (
    None
):
    """Prompt docs distinguish uninterpreted dietary text from delimiters."""
    prompt = " ".join(
        (PROJECT_ROOT / "docs" / "prompt.md")
        .read_text(encoding="utf-8")
        .split()
    )

    assert (
        "semantically uninterpreted but delimiter-normalized for section safety"
        in prompt
    )
    assert "`---`" in prompt
    assert "`===`" in prompt


def test_plan_chat_worker_and_transport_have_no_plan_lifecycle() -> None:
    """Draft output is text transport, not a parsed or persisted plan."""
    worker = (SOURCE_ROOT / "plan_chat_handler.py").read_text(encoding="utf-8")
    telegram_api = (SOURCE_ROOT / "telegram" / "api.py").read_text(
        encoding="utf-8"
    )

    assert "days=21" in worker
    assert "chat_text_strict_sync" in worker
    assert "response_format" not in worker
    assert "save_plan" not in worker
    assert "validate_plan" not in worker
    assert "repair" not in worker.casefold()
    assert "send_plan_chat" in telegram_api
    assert "parse_mode" not in telegram_api
    assert "reply_markup" in telegram_api


def test_workflow_controls_are_scoped_and_stale_safe() -> None:
    """Only workflow-owned controls can end, cancel, or close a session."""
    router = (SOURCE_ROOT / "router.py").read_text(encoding="utf-8")
    bot = (SOURCE_ROOT / "bot_handler.py").read_text(encoding="utf-8")
    telegram_api = (SOURCE_ROOT / "telegram" / "api.py").read_text(
        encoding="utf-8"
    )

    assert "plan_chat:end:" in telegram_api
    assert "expected_revision=state.revision" in bot
    assert "ProfileCallbackAction.CLOSE" in bot
    assert "_cancel_meal_callback" in bot
    assert "parse_checkin_callback" not in router
    assert "def _cmd_cancel" not in bot
    assert 'command="cancel"' not in bot


def test_deleted_files_and_import_boundaries_are_absent() -> None:
    """The deletion inventory is absent from active implementation surfaces."""
    for module_name in DELETED_MODULES:
        module_path = SOURCE_ROOT.joinpath(*module_name.split("."))
        assert not module_path.with_suffix(".py").exists()
    for filename in DELETED_TESTS:
        assert not (PROJECT_ROOT / "tests" / filename).exists()
    for filename in DELETED_SCRIPTS:
        assert not (SCRIPT_ROOT / filename).exists()

    for path in _active_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                imported = [f"{module}.{alias.name}" for alias in node.names]
            else:
                continue
            assert all(
                not any(
                    name == module_name or name.endswith(f".{module_name}")
                    for module_name in DELETED_MODULES
                )
                for name in imported
            ), f"deleted import in {path}"


def test_deleted_symbol_inventory_is_absent_from_active_python() -> None:
    """Removed models, helpers, and workflow APIs are not retained."""
    for path in _active_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        retained_names = set(_defined_and_imported_names(tree))
        assert not retained_names.intersection(DELETED_SYMBOLS), path


def test_deployment_deletion_inventory_has_exact_retained_resources() -> None:
    """Settings, outputs, and IAM grants contain only retained concepts."""
    template = (PROJECT_ROOT / "template.yaml").read_text(encoding="utf-8")
    config = (SOURCE_ROOT / "config.py").read_text(encoding="utf-8")
    deployment = (SCRIPT_ROOT / "deploy.py").read_text(encoding="utf-8")
    verifier = (SCRIPT_ROOT / "verify_transaction_permission.py").read_text(
        encoding="utf-8"
    )

    legacy_infrastructure_names = (
        "PlannerFunction",
        "PlannerFunctionName",
        "PLANNER_",
        "CONVERSATIONAL_",
        "BOT_LLM_",
        "PLANNER_GROCERY_",
        "PLANNER_HANDLER_",
        "GROCERY_LLM_",
        "HANDLER_SAFETY_MARGIN",
    )
    for path, content in (
        (PROJECT_ROOT / "template.yaml", template),
        (SOURCE_ROOT / "config.py", config),
        (SCRIPT_ROOT / "deploy.py", deployment),
        (SCRIPT_ROOT / "verify_transaction_permission.py", verifier),
    ):
        for name in legacy_infrastructure_names:
            assert name not in content, f"{name} retained in {path}"

    resources = template.split("Resources:", maxsplit=1)[1].split(
        "Outputs:", maxsplit=1
    )[0]
    assert "  BotFunction:" in resources
    assert "  PlanChatFunction:" in resources
    assert "  PlannerFunction:" not in resources
    assert (
        "  LLM_API_KEY:"
        not in resources.split("  PlanChatFunction:", maxsplit=1)[0]
    )
    plan_chat_resource = resources.split("  PlanChatFunction:", maxsplit=1)[1]
    assert "LLM_API_KEY" in plan_chat_resource
    assert "lambda:InvokeFunction" not in plan_chat_resource
    assert "dynamodb:TransactWriteItems" not in plan_chat_resource

    output_section = template.split("Outputs:", maxsplit=1)[1]
    for output_name in (
        "WebhookUrl",
        "MealPlannerTableName",
        "BotFunctionName",
        "PlanChatFunctionName",
    ):
        assert f"  {output_name}:" in output_section
    assert "  PlannerFunctionName:" not in output_section
    bot_resource = resources.split("  BotFunction:", maxsplit=1)[1].split(
        "  PlanChatFunction:", maxsplit=1
    )[0]
    assert "dynamodb:TransactWriteItems" in bot_resource


def test_active_documentation_has_no_retired_workflow_contracts() -> None:
    """Active docs describe only the retained Plan Chat product surface."""
    for path in ACTIVE_DOCUMENTATION:
        content = path.read_text(encoding="utf-8")
        for marker in RETIRED_DOCUMENTATION_MARKERS:
            assert marker not in content, f"stale marker in {path}: {marker}"


def test_active_documentation_describes_the_plan_chat_contract() -> None:
    """README and prompt docs cover the implemented draft-only behavior."""
    readme = _read_readme()
    prompt = " ".join(
        (PROJECT_ROOT / "docs" / "prompt.md")
        .read_text(encoding="utf-8")
        .split()
    )

    for marker in (
        "temporary, conversational meal-plan drafts",
        "Plan Chat Lambda",
        "inclusive 21-day window",
        "End planning",
        "PLAN_CHAT_LLM_MODEL",
        "PlanChatFunctionName",
        "Privacy and failure behavior",
    ):
        assert marker in readme
    for marker in (
        "Five prompt sections",
        "Raw dietary constraints",
        "Raw dietary preferences",
        "Submitted meals",
        "Planning conversation",
        "21 calendar dates",
        "one focused clarification question",
        "does not parse the response",
    ):
        assert marker in prompt


def test_readme_documents_bounded_failure_recovery_contract() -> None:
    """Both user-facing failure sections match worker recovery behavior."""
    readme = " ".join(_read_readme().split()).casefold()
    workflow = readme.split("### plan chat", maxsplit=1)[1].split(
        "### submitted meals", maxsplit=1
    )[0]
    privacy = readme.split("## privacy and failure behavior", maxsplit=1)[
        1
    ].split("## development guidance", maxsplit=1)[0]

    for section in (workflow, privacy):
        assert (
            "provider failures yield bounded retry guidance only if state "
            "recovery and telegram delivery succeed"
        ) in section
        assert (
            "persistence or telegram delivery failures can be silent to the "
            "user and are logged with bounded metadata"
        ) in section


def test_completed_plans_preserve_external_post_completion_gates() -> None:
    """Archive local work while retaining external follow-up instructions."""
    assert not ORIGINAL_ACTIVE_PLAN_PATH.exists()
    assert ORIGINAL_COMPLETED_PLAN_PATH.exists()
    assert not REMEDIATION_ACTIVE_PLAN_PATH.exists()
    assert REMEDIATION_COMPLETED_PLAN_PATH.exists()

    plan = ORIGINAL_COMPLETED_PLAN_PATH.read_text(encoding="utf-8")
    implementation = plan.split("## Post-Completion", maxsplit=1)[0]
    post_completion = " ".join(
        plan.split("## Post-Completion", maxsplit=1)[1].split()
    )

    assert "- [x] move this fully completed plan" in implementation
    assert "### Task 15: Finalize active documentation" in plan
    assert "Task 15 verification completed." in plan
    assert "Historical completed plan documents remain" in plan
    assert "Deploy through a feature branch and pull request" in post_completion
    assert "add the required issue comment linking the commit or PR" in (
        post_completion
    )
    assert (
        "After completion, comment on the tracking GitHub issue"
        in post_completion
    )

    remediation = REMEDIATION_COMPLETED_PLAN_PATH.read_text(encoding="utf-8")
    remediation_implementation = remediation.split(
        "## Post-Completion", maxsplit=1
    )[0]
    remediation_post_completion = " ".join(
        remediation.split("## Post-Completion", maxsplit=1)[1].split()
    )
    assert "- [x] move the original simplification plan" in (
        remediation_implementation
    )
    assert "Deploy through a dedicated branch and pull request" in (
        remediation_post_completion
    )
    assert "Comment on the associated GitHub issue" in (
        remediation_post_completion
    )


def test_deployment_surface_has_plan_chat_names_and_scoped_settings() -> None:
    """SAM and settings contain no retired worker or model configuration."""
    template = (PROJECT_ROOT / "template.yaml").read_text(encoding="utf-8")
    config = (SOURCE_ROOT / "config.py").read_text(encoding="utf-8")
    deployment = (SCRIPT_ROOT / "deploy.py").read_text(encoding="utf-8")

    for content in (template, config, deployment):
        assert "PlannerFunction" not in content
        assert "PlannerFunctionName" not in content
        assert "PLANNER_" not in content
        assert "CONVERSATIONAL_" not in content
    assert "PlanChatFunction" in template
    assert "PlanChatFunctionName" in template
    assert "PLAN_CHAT_" in template
    assert "LLM_API_KEY" in template
    assert "TransactWriteItems" in template


def test_plan_chat_retry_surface_is_absent() -> None:
    """The inert retry setting is absent from active configuration surfaces."""
    contents = [
        _read_readme(),
        (SOURCE_ROOT / "config.py").read_text(encoding="utf-8"),
        (SOURCE_ROOT / "llm" / "client.py").read_text(encoding="utf-8"),
        (SOURCE_ROOT / "plan_chat_handler.py").read_text(encoding="utf-8"),
        (PROJECT_ROOT / "template.yaml").read_text(encoding="utf-8"),
    ]
    for content in contents:
        assert "PLAN_CHAT_LLM_MAX_RETRIES" not in content
        assert "plan_chat_llm_max_retries" not in content
        assert "self.max_retries" not in content
