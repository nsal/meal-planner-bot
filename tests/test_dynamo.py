"""DynamoDB repository integration tests."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from threading import Barrier
from typing import Any, Generator
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from meal_planner.bot_handler import BotHandler
from meal_planner.db.dynamo import (
    DynamoRepository,
    RepairPublicationOutcome,
)
from meal_planner.models.schemas import (
    ConversationState,
    ConversationWorkflowKind,
    ConversationWorkflowStep,
    GrocerySection,
    GroceryStatus,
    MealLogDraft,
    MealLogEntry,
    MealOutcome,
    PlanStatus,
    ProfileEditCategory,
    ProfileEditOperation,
    ProfileUpdateEntities,
    UserProfile,
)
from meal_planner.router import RouteResult, RouteType
from tests.factories import make_plan, make_profile


@pytest.fixture
def dynamodb_table() -> Generator[Any, None, None]:
    with mock_aws():
        table = boto3.resource(
            "dynamodb", region_name="us-east-1"
        ).create_table(
            TableName="test-meal-planner",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield table


@pytest.fixture
def repo(dynamodb_table: Any) -> DynamoRepository:
    return DynamoRepository(dynamodb_table)


def test_profile_and_onboarding_draft_round_trip(
    repo: DynamoRepository,
) -> None:
    assert repo.get_profile("user") is None
    draft = ProfileUpdateEntities(name="Alex", people_count=2)
    repo.save_profile_draft("user", draft)
    assert repo.get_profile_draft("user").name == "Alex"
    profile = make_profile()
    repo.save_profile("user", profile)
    assert repo.get_profile("user") == profile
    repo.delete_profile_draft("user")
    assert repo.get_profile_draft("user") is None


def test_profile_read_consistency_is_opt_in(mocker: Any) -> None:
    """Profile reads add ConsistentRead only when explicitly requested."""
    table = mocker.MagicMock()
    profile = make_profile()
    table.get_item.return_value = {
        "Item": {
            "PK": "USER#user",
            "SK": "PROFILE",
            **profile.model_dump(mode="json"),
        }
    }
    repo = DynamoRepository(table)

    assert repo.get_profile("user") == profile
    assert table.get_item.call_args.kwargs == {
        "Key": {"PK": "USER#user", "SK": "PROFILE"}
    }

    assert repo.get_profile("user", consistent_read=True) == profile
    assert table.get_item.call_args.kwargs == {
        "Key": {"PK": "USER#user", "SK": "PROFILE"},
        "ConsistentRead": True,
    }


def test_profile_read_consistency_selects_current_table_response(
    mocker: Any,
) -> None:
    """A simulated strong read returns current data instead of stale data."""
    table = mocker.MagicMock()
    stale_profile = make_profile().model_copy(update={"name": "Stale"})
    current_profile = make_profile().model_copy(update={"name": "Current"})

    def get_item(**kwargs: Any) -> dict[str, Any]:
        profile = (
            current_profile
            if kwargs.get("ConsistentRead") is True
            else stale_profile
        )
        return {
            "Item": {
                "PK": "USER#user",
                "SK": "PROFILE",
                **profile.model_dump(mode="json"),
            }
        }

    table.get_item.side_effect = get_item
    repo = DynamoRepository(table)

    assert repo.get_profile("user") == stale_profile
    assert repo.get_profile("user", consistent_read=True) == current_profile


def _profile_edit_state(
    *,
    revision: int = 3,
    created_at: datetime | None = None,
    operation: ProfileEditOperation = ProfileEditOperation.ADD,
) -> ConversationState:
    """Build a persisted state that owns one profile text amendment."""
    current = created_at or datetime.now(timezone.utc)
    return ConversationState(
        workflow_kind=ConversationWorkflowKind.PROFILE_EDIT,
        step=ConversationWorkflowStep.AWAITING_PROFILE_INPUT,
        profile_category=ProfileEditCategory.DIETARY_CONSTRAINTS,
        profile_operation=operation,
        revision=revision,
        created_at=current,
        updated_at=current,
        expires_at=int((current + timedelta(hours=24)).timestamp()),
    )


def _profile_menu_state(state: ConversationState) -> ConversationState:
    """Build the next profile menu state for an observed edit state."""
    return state.model_copy(
        update={
            "step": ConversationWorkflowStep.PROFILE_MENU,
            "profile_category": None,
            "profile_operation": None,
            "revision": state.revision + 1,
            "updated_at": state.updated_at + timedelta(seconds=1),
        }
    )


def test_profile_amendment_transaction_commits_matching_profile_and_state(
    repo: DynamoRepository,
) -> None:
    """A matching profile edit commits both documents atomically."""
    original = make_profile()
    updated = original.model_copy(update={"dietary_constraints": ["peanuts"]})
    observed = _profile_edit_state()
    next_state = _profile_menu_state(observed)
    repo.save_profile("user", original)
    assert repo.save_conversation_state("user", observed)

    assert repo.save_profile_and_transition_state(
        "user", updated, next_state, observed
    )

    assert repo.get_profile("user") == updated
    assert repo.get_conversation_state("user") == next_state


@pytest.mark.parametrize("conflict", ["deleted", "changed_operation"])
def test_profile_amendment_transaction_conflicts_leave_documents_unchanged(
    repo: DynamoRepository,
    conflict: str,
) -> None:
    """Cancellation and changed operation cannot commit a profile write."""
    original = make_profile()
    updated = original.model_copy(update={"dietary_constraints": ["peanuts"]})
    observed = _profile_edit_state()
    repo.save_profile("user", original)
    assert repo.save_conversation_state("user", observed)
    if conflict == "deleted":
        assert repo.delete_conversation_state("user")
        replacement = None
    else:
        replacement = observed.model_copy(
            update={"profile_operation": ProfileEditOperation.REMOVE}
        )
        repo.table.put_item(
            Item={
                "PK": "USER#user",
                "SK": "CONVERSATION_STATE",
                **replacement.model_dump(mode="json"),
            }
        )

    assert not repo.save_profile_and_transition_state(
        "user", updated, _profile_menu_state(observed), observed
    )

    assert repo.get_profile("user") == original
    assert repo.get_conversation_state("user") == replacement


def test_profile_amendment_transaction_replacement_reuses_revision_safely(
    repo: DynamoRepository,
) -> None:
    """A replacement workflow with the same revision cannot authorize input."""
    original = make_profile()
    updated = original.model_copy(update={"goals": ["eat well", "save time"]})
    observed = _profile_edit_state()
    replacement = ConversationState(
        workflow_kind=ConversationWorkflowKind.PLAN_REQUEST,
        step=ConversationWorkflowStep.AWAITING_PREFERENCE,
        request_id="replacement",
        revision=observed.revision,
        created_at=observed.created_at + timedelta(seconds=1),
        updated_at=observed.updated_at + timedelta(seconds=1),
        expires_at=observed.expires_at,
    )
    repo.save_profile("user", original)
    assert repo.save_conversation_state("user", observed)
    repo.table.put_item(
        Item={
            "PK": "USER#user",
            "SK": "CONVERSATION_STATE",
            **replacement.model_dump(mode="json"),
        }
    )

    assert not repo.save_profile_and_transition_state(
        "user", updated, _profile_menu_state(observed), observed
    )

    assert repo.get_profile("user") == original
    assert repo.get_conversation_state("user") == replacement


@pytest.mark.parametrize("replacement_kind", ["plan", "meal", "profile"])
def test_profile_amendment_transaction_rejects_replacement_workflows(
    repo: DynamoRepository,
    replacement_kind: str,
) -> None:
    """Commands replacing an edit cannot be overwritten by stale input."""
    original = make_profile()
    updated = original.model_copy(update={"goals": ["eat well", "save time"]})
    observed = _profile_edit_state()
    replacement_time = observed.created_at + timedelta(seconds=1)
    if replacement_kind == "plan":
        replacement = ConversationState(
            workflow_kind=ConversationWorkflowKind.PLAN_REQUEST,
            step=ConversationWorkflowStep.AWAITING_PREFERENCE,
            request_id="replacement-plan",
            revision=observed.revision,
            created_at=replacement_time,
            updated_at=replacement_time,
            expires_at=observed.expires_at,
        )
    elif replacement_kind == "meal":
        replacement = ConversationState(
            workflow_kind=ConversationWorkflowKind.MEAL_LOG,
            step=ConversationWorkflowStep.AWAITING_DATE,
            meal_draft=MealLogDraft(),
            revision=observed.revision,
            created_at=replacement_time,
            updated_at=replacement_time,
            expires_at=observed.expires_at,
        )
    else:
        replacement = _profile_edit_state(
            revision=observed.revision, created_at=replacement_time
        )
    repo.save_profile("user", original)
    assert repo.save_conversation_state("user", observed)
    assert repo.save_conversation_state(
        "user", replacement, expected_revision=observed.revision
    )

    assert not repo.save_profile_and_transition_state(
        "user", updated, _profile_menu_state(observed), observed
    )

    assert repo.get_profile("user") == original
    assert repo.get_conversation_state("user") == replacement


def test_profile_amendment_transaction_duplicate_input_is_idempotently_rejected(
    repo: DynamoRepository,
) -> None:
    """A second submission using the consumed state changes nothing."""
    original = make_profile()
    first_update = original.model_copy(
        update={"dietary_constraints": ["peanuts"]}
    )
    duplicate_update = original.model_copy(
        update={"dietary_constraints": ["shellfish"]}
    )
    observed = _profile_edit_state()
    repo.save_profile("user", original)
    assert repo.save_conversation_state("user", observed)
    next_state = _profile_menu_state(observed)
    assert repo.save_profile_and_transition_state(
        "user", first_update, next_state, observed
    )

    assert not repo.save_profile_and_transition_state(
        "user", duplicate_update, next_state, observed
    )

    assert repo.get_profile("user") == first_update
    assert repo.get_conversation_state("user") == next_state


def test_profile_amendment_transaction_reraises_unrelated_failure(
    repo: DynamoRepository,
    mocker: Any,
) -> None:
    """Unexpected DynamoDB failures remain visible to the handler."""
    observed = _profile_edit_state()
    error = ClientError(
        {
            "Error": {
                "Code": "TransactionCanceledException",
                "Message": "transaction conflict",
            },
            "CancellationReasons": [
                {"Code": "None"},
                {"Code": "TransactionConflict"},
            ],
        },
        "TransactWriteItems",
    )
    mocker.patch.object(
        repo.table.meta.client,
        "transact_write_items",
        side_effect=error,
    )

    with pytest.raises(ClientError) as raised:
        repo.save_profile_and_transition_state(
            "user",
            make_profile(),
            _profile_menu_state(observed),
            observed,
        )

    assert raised.value is error


def test_legacy_profile_draft_round_trip_preserves_unanswered_constraints(
    repo: DynamoRepository,
) -> None:
    """Legacy drafts with null constraint fields remain unanswered."""
    repo.table.put_item(
        Item={
            "PK": "USER#user",
            "SK": "PROFILE_DRAFT",
            "name": "Alex",
            "people_count": 2,
            "allergies": None,
            "restrictions": None,
        }
    )

    draft = repo.get_profile_draft("user")

    assert draft is not None
    assert draft.dietary_constraints is None
    repo.save_profile_draft("user", draft)
    saved_item = repo.table.get_item(
        Key={"PK": "USER#user", "SK": "PROFILE_DRAFT"}
    )["Item"]
    assert saved_item["dietary_constraints"] is None
    assert "allergies" not in saved_item
    assert "restrictions" not in saved_item


def test_legacy_profile_draft_round_trip_normalizes_explicit_empty(
    repo: DynamoRepository,
) -> None:
    """Legacy no-value draft answers round-trip as an explicit empty list."""
    repo.table.put_item(
        Item={
            "PK": "USER#user",
            "SK": "PROFILE_DRAFT",
            "name": "Alex",
            "people_count": 2,
            "allergies": "no allergies",
            "restrictions": None,
        }
    )

    draft = repo.get_profile_draft("user")

    assert draft is not None
    assert draft.dietary_constraints == []
    repo.save_profile_draft("user", draft)
    saved_item = repo.table.get_item(
        Key={"PK": "USER#user", "SK": "PROFILE_DRAFT"}
    )["Item"]
    assert saved_item["dietary_constraints"] == []
    assert "allergies" not in saved_item
    assert "restrictions" not in saved_item


def test_legacy_profile_resave_removes_aliases_and_keeps_constraints(
    repo: DynamoRepository,
) -> None:
    """Canonical profile re-saves retain real legacy constraints only."""
    repo.table.put_item(
        Item={
            "PK": "USER#user",
            "SK": "PROFILE",
            "name": "Alex",
            "family_members": [],
            "allergies": ["Peanuts", "no allergies"],
            "restrictions": ["Vegan", "NO RESTRICTIONS", "peanuts"],
            "dietary_preferences": [],
            "goals": [],
            "people_count": 1,
        }
    )

    profile = repo.get_profile("user")
    assert profile is not None
    repo.save_profile("user", profile)

    item = repo.table.get_item(Key={"PK": "USER#user", "SK": "PROFILE"})["Item"]
    assert profile.dietary_constraints == ["Peanuts", "Vegan"]
    assert item["dietary_constraints"] == ["Peanuts", "Vegan"]
    assert "allergies" not in item
    assert "restrictions" not in item


def test_save_profile_creates_canonical_revisionless_item(
    repo: DynamoRepository,
) -> None:
    """Onboarding saves create a canonical profile without a revision."""
    profile = UserProfile(
        name="Alex",
        dietary_constraints=["peanuts"],
    )

    repo.save_profile("user", profile)

    item = repo.table.get_item(Key={"PK": "USER#user", "SK": "PROFILE"})["Item"]
    assert "revision" not in item
    assert item["dietary_constraints"] == ["peanuts"]
    assert "allergies" not in item
    assert "restrictions" not in item


def test_save_profile_replaces_existing_document_without_revision(
    repo: DynamoRepository,
) -> None:
    """A normal profile save directly replaces the existing document."""
    initial = UserProfile(name="Alex", dietary_constraints=["peanuts"])
    updated = UserProfile(name="Alex", dietary_constraints=["dairy-free"])
    repo.save_profile("user", initial)

    repo.save_profile("user", updated)

    saved = repo.get_profile("user")
    assert saved is not None
    assert saved.dietary_constraints == ["dairy-free"]


def test_save_profile_omits_legacy_revision_on_first_write(
    repo: DynamoRepository,
) -> None:
    """A legacy revision is removed by the first canonical profile save."""
    repo.table.put_item(
        Item={
            "PK": "USER#user",
            "SK": "PROFILE",
            "name": "Alex",
            "family_members": [],
            "allergies": ["Peanuts"],
            "restrictions": ["vegetarian"],
            "dietary_preferences": [],
            "goals": [],
            "people_count": 1,
            "revision": 7,
        }
    )
    legacy_read = repo.get_profile("user")
    assert legacy_read is not None
    canonical = legacy_read.model_copy(
        update={"dietary_constraints": ["Peanuts", "vegan"]}
    )

    repo.save_profile("user", canonical)

    item = repo.table.get_item(Key={"PK": "USER#user", "SK": "PROFILE"})["Item"]
    assert "revision" not in item
    assert item["dietary_constraints"] == ["Peanuts", "vegan"]
    assert "allergies" not in item
    assert "restrictions" not in item


def test_conversation_state_round_trip_and_revision_guard(
    repo: DynamoRepository,
) -> None:
    """Conversation state is isolated and stale revisions cannot replace it."""
    now = datetime.now(timezone.utc)
    state = ConversationState(
        workflow_kind=ConversationWorkflowKind.MEAL_LOG,
        step=ConversationWorkflowStep.AWAITING_DATE,
        meal_draft=MealLogDraft(),
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
    )
    assert repo.save_conversation_state("user", state)
    assert repo.get_conversation_state("user") == state
    newer = state.model_copy(
        update={"revision": 1, "updated_at": now + timedelta(seconds=1)}
    )
    assert repo.transition_conversation_state(
        "user", newer, expected_revision=state.revision
    )
    assert not repo.transition_conversation_state(
        "user", state, expected_revision=state.revision
    )
    assert repo.delete_conversation_state("user", expected_revision=1)
    assert repo.get_conversation_state("user") is None


def test_conversation_state_read_can_be_strongly_consistent(
    repo: DynamoRepository, mocker: Any
) -> None:
    now = datetime.now(timezone.utc)
    state = ConversationState(
        workflow_kind=ConversationWorkflowKind.MEAL_LOG,
        step=ConversationWorkflowStep.AWAITING_DATE,
        meal_draft=MealLogDraft(),
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
    )
    get_item = mocker.patch.object(
        repo.table,
        "get_item",
        return_value={
            "Item": {
                "PK": "USER#user",
                "SK": "CONVERSATION_STATE",
                **state.model_dump(mode="json"),
            }
        },
    )

    assert repo.get_conversation_state("user") == state
    assert get_item.call_args.kwargs == {
        "Key": {"PK": "USER#user", "SK": "CONVERSATION_STATE"}
    }
    assert repo.get_conversation_state("user", consistent_read=True) == state
    assert get_item.call_args.kwargs == {
        "Key": {"PK": "USER#user", "SK": "CONVERSATION_STATE"},
        "ConsistentRead": True,
    }


def test_revision_start_marker_is_atomic_and_survives_state_cleanup(
    repo: DynamoRepository,
) -> None:
    now = datetime.now(timezone.utc)
    state = ConversationState(
        workflow_kind=ConversationWorkflowKind.PLAN_REVISION,
        step=ConversationWorkflowStep.GENERATING,
        amendment="Avoid cauliflower",
        target_week=date.today(),
        expected_plan_revision=0,
        request_id="revision-1",
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
    )

    assert repo.start_plan_revision("user", state, source_update_id="42")
    assert repo.has_plan_revision_update_marker("user", "42")
    assert repo.delete_conversation_state("user", expected_revision=0)
    assert repo.get_conversation_state("user") is None
    assert repo.has_plan_revision_update_marker("user", "42")
    assert not repo.start_plan_revision("user", state, source_update_id="42")


def test_revision_start_rolls_back_marker_when_state_condition_fails(
    repo: DynamoRepository,
) -> None:
    now = datetime.now(timezone.utc)
    existing = ConversationState(
        workflow_kind=ConversationWorkflowKind.MEAL_LOG,
        step=ConversationWorkflowStep.AWAITING_DATE,
        meal_draft=MealLogDraft(),
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
    )
    assert repo.save_conversation_state("user", existing)
    revision = existing.model_copy(
        update={
            "workflow_kind": ConversationWorkflowKind.PLAN_REVISION,
            "step": ConversationWorkflowStep.GENERATING,
            "amendment": "Avoid cauliflower",
            "target_week": date.today(),
            "expected_plan_revision": 0,
            "request_id": "revision-1",
        }
    )

    assert not repo.start_plan_revision("user", revision, source_update_id="42")
    assert repo.get_conversation_state("user") == existing
    assert not repo.has_plan_revision_update_marker("user", "42")


def test_revision_replacement_and_state_cleanup_are_atomic(
    repo: DynamoRepository,
) -> None:
    plan = make_plan(revision=3, planning_instructions=["Egg breakfasts"])
    repo.save_plan("user", plan)
    now = datetime.now(timezone.utc)
    state = ConversationState(
        workflow_kind=ConversationWorkflowKind.PLAN_REVISION,
        step=ConversationWorkflowStep.GENERATING,
        amendment="Avoid cauliflower",
        target_week=plan.week_start,
        expected_plan_revision=plan.revision,
        request_id="revision-1",
        revision=0,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
    )
    assert repo.save_conversation_state("user", state)
    replacement = make_plan(
        week_start=plan.week_start,
        revision=4,
        planning_instructions=["Egg breakfasts", "Avoid cauliflower"],
    )

    assert repo.replace_draft_and_clear_revision_state(
        "user",
        replacement,
        expected_plan_revision=3,
        request_id="revision-1",
        expected_state_revision=0,
    )
    assert repo.get_plan("user", plan.week_start) == replacement
    assert repo.get_conversation_state("user") is None


def test_revision_replacement_rejects_stale_plan_or_state(
    repo: DynamoRepository,
) -> None:
    plan = make_plan(revision=1)
    repo.save_plan("user", plan)
    now = datetime.now(timezone.utc)
    state = ConversationState(
        workflow_kind=ConversationWorkflowKind.PLAN_REVISION,
        step=ConversationWorkflowStep.GENERATING,
        amendment="Avoid cauliflower",
        target_week=plan.week_start,
        expected_plan_revision=1,
        request_id="revision-1",
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
    )
    assert repo.save_conversation_state("user", state)
    replacement = make_plan(week_start=plan.week_start, revision=2)
    assert not repo.replace_draft_and_clear_revision_state(
        "user",
        replacement,
        expected_plan_revision=0,
        request_id="wrong-request",
        expected_state_revision=0,
    )
    assert repo.get_plan("user", plan.week_start) == plan
    assert repo.get_conversation_state("user") == state


def test_replacements_from_one_snapshot_have_one_winner(
    repo: DynamoRepository,
) -> None:
    """A delayed transition cannot overwrite the winning replacement."""
    now = datetime.now(timezone.utc)
    initial = ConversationState(
        workflow_kind=ConversationWorkflowKind.MEAL_LOG,
        step=ConversationWorkflowStep.AWAITING_DATE,
        meal_draft=MealLogDraft(),
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
    )
    assert repo.save_conversation_state("user", initial)
    first = initial.model_copy(
        update={
            "revision": initial.revision + 1,
            "updated_at": now + timedelta(seconds=1),
        }
    )
    second = first.model_copy(update={"updated_at": now + timedelta(seconds=2)})

    assert repo.save_conversation_state(
        "user", first, expected_revision=initial.revision
    )
    assert not repo.save_conversation_state(
        "user", second, expected_revision=initial.revision
    )
    assert not repo.transition_conversation_state(
        "user", initial, expected_revision=initial.revision
    )
    assert repo.get_conversation_state("user") == first


def _meal(day: int, hour: int, description: str) -> MealLogEntry:
    return MealLogEntry(
        date=date(2026, 8, day),
        meal_type="lunch",
        description=description,
        created_at=datetime(2026, 8, day, hour, tzinfo=timezone.utc),
    )


def _completed_meal_state(
    *, revision: int = 0, now: datetime | None = None
) -> ConversationState:
    current = now or datetime.now(timezone.utc)
    return ConversationState(
        workflow_kind=ConversationWorkflowKind.MEAL_LOG,
        step=ConversationWorkflowStep.AWAITING_ANOTHER_MEAL,
        meal_draft=MealLogDraft(
            date=date(2026, 8, 8),
            meal_type="lunch",
            description="Soup",
        ),
        revision=revision,
        created_at=current,
        updated_at=current,
        expires_at=current + timedelta(hours=24),
    )


def _review_meal_state(
    *,
    request_id: str = "123e4567-e89b-12d3-a456-426614174000",
    revision: int = 0,
    now: datetime | None = None,
) -> ConversationState:
    current = now or datetime.now(timezone.utc)
    return ConversationState(
        workflow_kind=ConversationWorkflowKind.MEAL_LOG,
        step=ConversationWorkflowStep.AWAITING_MEAL_CONFIRMATION,
        meal_draft=MealLogDraft(
            date=date(2026, 8, 8),
            meal_type="lunch",
            description="Soup",
        ),
        request_id=request_id,
        revision=revision,
        created_at=current,
        updated_at=current,
        expires_at=current + timedelta(hours=24),
    )


def _continuation_meal_state(
    review: ConversationState,
) -> ConversationState:
    return review.model_copy(
        update={
            "step": ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION,
            "revision": review.revision + 1,
            "updated_at": review.updated_at + timedelta(seconds=1),
        }
    )


def test_transition_rejects_stale_same_revision_request_id(
    repo: DynamoRepository,
) -> None:
    original = _review_meal_state(request_id="original-request")
    replacement = _review_meal_state(
        request_id="replacement-request",
        revision=original.revision,
        now=original.updated_at + timedelta(seconds=1),
    )
    assert repo.save_conversation_state("user", original)
    assert repo.save_conversation_state(
        "user", replacement, expected_revision=original.revision
    )

    assert not repo.transition_conversation_state(
        "user",
        _continuation_meal_state(original),
        expected_revision=original.revision,
        expected_request_id=original.request_id,
        expected_step=ConversationWorkflowStep.AWAITING_MEAL_CONFIRMATION,
    )
    assert repo.get_conversation_state("user") == replacement


def test_delete_rejects_stale_same_revision_request_id(
    repo: DynamoRepository,
) -> None:
    original = _review_meal_state(request_id="original-request")
    replacement = _review_meal_state(
        request_id="replacement-request",
        revision=original.revision,
        now=original.updated_at + timedelta(seconds=1),
    )
    assert repo.save_conversation_state("user", original)
    assert repo.save_conversation_state(
        "user", replacement, expected_revision=original.revision
    )

    assert not repo.delete_conversation_state(
        "user",
        expected_revision=original.revision,
        expected_request_id=original.request_id,
        expected_step=ConversationWorkflowStep.AWAITING_MEAL_CONFIRMATION,
    )
    assert repo.get_conversation_state("user") == replacement


def test_transition_accepts_matching_state_preconditions(
    repo: DynamoRepository,
) -> None:
    review = _review_meal_state()
    continuation = _continuation_meal_state(review)
    assert repo.save_conversation_state("user", review)

    assert repo.transition_conversation_state(
        "user",
        continuation,
        expected_revision=review.revision,
        expected_request_id=review.request_id,
        expected_step=review.step,
    )
    assert repo.get_conversation_state("user") == continuation


def test_delete_accepts_matching_state_preconditions(
    repo: DynamoRepository,
) -> None:
    review = _review_meal_state()
    assert repo.save_conversation_state("user", review)

    assert repo.delete_conversation_state(
        "user",
        expected_revision=review.revision,
        expected_request_id=review.request_id,
        expected_step=review.step,
    )
    assert repo.get_conversation_state("user") is None


def test_handler_add_more_round_trip_preserves_state_invariants(
    repo: DynamoRepository,
) -> None:
    """Add more writes a state that the repository can deserialize."""
    telegram_api = MagicMock()
    handler = BotHandler(repo, telegram_api)
    review = _review_meal_state(
        request_id="123e4567-e89b-12d3-a456-426614174000", revision=4
    )
    continuation = _continuation_meal_state(review)
    assert repo.save_conversation_state("user", continuation)

    route = RouteResult(
        route_type=RouteType.CALLBACK,
        chat_id=1,
        user_id="user",
        callback_query_id="meal-query",
        callback_data=f"meal:add:{continuation.request_id}",
    )

    handler.handle_callback(route)

    next_state = repo.get_conversation_state("user")
    assert next_state is not None
    assert next_state.created_at <= next_state.updated_at
    assert next_state.request_id not in {
        None,
        continuation.request_id,
    }
    assert next_state.meal_draft == MealLogDraft()
    assert next_state.step is ConversationWorkflowStep.AWAITING_MEAL_INPUT
    assert next_state.revision == continuation.revision + 1
    telegram_api.answer_callback_query.assert_called_once_with(
        "meal-query", "Add more"
    )


@pytest.mark.parametrize(
    "expected_request_id, expected_step",
    [
        (
            "wrong-request",
            ConversationWorkflowStep.AWAITING_MEAL_CONFIRMATION,
        ),
        (
            "123e4567-e89b-12d3-a456-426614174000",
            ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION,
        ),
    ],
    ids=["request-id-mismatch", "step-mismatch"],
)
def test_transition_rejects_request_id_or_step_mismatch(
    repo: DynamoRepository,
    expected_request_id: str,
    expected_step: ConversationWorkflowStep,
) -> None:
    review = _review_meal_state()
    assert repo.save_conversation_state("user", review)

    assert not repo.transition_conversation_state(
        "user",
        _continuation_meal_state(review),
        expected_revision=review.revision,
        expected_request_id=expected_request_id,
        expected_step=expected_step,
    )
    assert repo.get_conversation_state("user") == review


@pytest.mark.parametrize(
    "expected_request_id, expected_step",
    [
        (
            "wrong-request",
            ConversationWorkflowStep.AWAITING_MEAL_CONFIRMATION,
        ),
        (
            "123e4567-e89b-12d3-a456-426614174000",
            ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION,
        ),
    ],
    ids=["request-id-mismatch", "step-mismatch"],
)
def test_delete_rejects_request_id_or_step_mismatch(
    repo: DynamoRepository,
    expected_request_id: str,
    expected_step: ConversationWorkflowStep,
) -> None:
    review = _review_meal_state()
    assert repo.save_conversation_state("user", review)

    assert not repo.delete_conversation_state(
        "user",
        expected_revision=review.revision,
        expected_request_id=expected_request_id,
        expected_step=expected_step,
    )
    assert repo.get_conversation_state("user") == review


def test_transition_propagates_nonconditional_error(
    repo: DynamoRepository, mocker: Any
) -> None:
    review = _review_meal_state()
    error = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException"}},
        "PutItem",
    )
    mocker.patch.object(repo.table, "put_item", side_effect=error)

    with pytest.raises(ClientError) as raised:
        repo.transition_conversation_state(
            "user",
            _continuation_meal_state(review),
            expected_revision=review.revision,
            expected_request_id=review.request_id,
            expected_step=review.step,
        )

    assert raised.value is error


def test_delete_propagates_nonconditional_error(
    repo: DynamoRepository, mocker: Any
) -> None:
    error = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException"}},
        "DeleteItem",
    )
    mocker.patch.object(repo.table, "delete_item", side_effect=error)

    with pytest.raises(ClientError) as raised:
        repo.delete_conversation_state(
            "user",
            expected_revision=0,
            expected_request_id="request-id",
            expected_step=ConversationWorkflowStep.AWAITING_MEAL_CONFIRMATION,
        )

    assert raised.value is error


def test_confirm_meal_atomically_writes_stable_key_and_continuation_state(
    repo: DynamoRepository,
) -> None:
    review = _review_meal_state()
    continuation = _continuation_meal_state(review)
    assert repo.save_conversation_state("user", review)
    entry = _meal(8, 12, "Soup")

    assert repo.confirm_meal_and_transition(
        "user",
        entry,
        continuation,
        expected_revision=review.revision,
        submission_id=review.request_id,
    )

    assert repo.get_conversation_state("user") == continuation
    saved = repo.table.get_item(
        Key={
            "PK": "USER#user",
            "SK": (
                "MEAL#2026-08-08#SUBMISSION#"
                "123e4567-e89b-12d3-a456-426614174000"
            ),
        }
    )
    assert saved["Item"]["description"] == "Soup"


def test_confirm_meal_rejects_duplicate_submission_without_state_change(
    repo: DynamoRepository,
) -> None:
    review = _review_meal_state()
    continuation = _continuation_meal_state(review)
    assert repo.save_conversation_state("user", review)
    entry = _meal(8, 12, "Soup")
    assert repo.confirm_meal_and_transition(
        "user",
        entry,
        continuation,
        expected_revision=review.revision,
        submission_id=review.request_id,
    )

    duplicate_review = _review_meal_state(
        request_id=review.request_id,
        revision=2,
    )
    assert repo.save_conversation_state(
        "user", duplicate_review, expected_revision=continuation.revision
    )
    duplicate_continuation = _continuation_meal_state(duplicate_review)

    assert not repo.confirm_meal_and_transition(
        "user",
        entry,
        duplicate_continuation,
        expected_revision=duplicate_review.revision,
        submission_id=review.request_id,
    )
    assert repo.get_conversation_state("user") == duplicate_review
    assert repo.get_meal_history("user", days=1, on_date=date(2026, 8, 8)) == [
        entry
    ]


def test_confirm_meal_rejects_stale_revision_without_partial_write(
    repo: DynamoRepository,
) -> None:
    review = _review_meal_state()
    assert repo.save_conversation_state("user", review)
    competing = review.model_copy(
        update={
            "revision": 1,
            "updated_at": review.updated_at + timedelta(seconds=1),
        }
    )
    assert repo.transition_conversation_state(
        "user", competing, expected_revision=review.revision
    )

    assert not repo.confirm_meal_and_transition(
        "user",
        _meal(8, 12, "Stale meal"),
        _continuation_meal_state(review),
        expected_revision=review.revision,
        submission_id=review.request_id,
    )
    assert repo.get_conversation_state("user") == competing
    assert repo.get_meal_history("user", days=1, on_date=date(2026, 8, 8)) == []


@pytest.mark.parametrize(
    "current_state, expected_revision, submission_id",
    [
        (
            _review_meal_state(
                request_id="123e4567-e89b-12d3-a456-426614174002"
            ),
            0,
            "123e4567-e89b-12d3-a456-426614174000",
        ),
        (_completed_meal_state(), 0, "123e4567-e89b-12d3-a456-426614174000"),
    ],
)
def test_confirm_meal_rejects_wrong_submission_or_step(
    repo: DynamoRepository,
    current_state: ConversationState,
    expected_revision: int,
    submission_id: str,
) -> None:
    assert repo.save_conversation_state("user", current_state)
    continuation = _continuation_meal_state(
        _review_meal_state(
            request_id="123e4567-e89b-12d3-a456-426614174000",
            revision=current_state.revision,
            now=current_state.updated_at,
        )
    )

    assert not repo.confirm_meal_and_transition(
        "user",
        _meal(8, 12, "Invalid confirmation"),
        continuation,
        expected_revision=expected_revision,
        submission_id=submission_id,
    )
    assert repo.get_conversation_state("user") == current_state
    assert repo.get_meal_history("user", days=1, on_date=date(2026, 8, 8)) == []


def test_confirm_meal_propagates_transaction_contention(
    repo: DynamoRepository, mocker: Any
) -> None:
    review = _review_meal_state()
    assert repo.save_conversation_state("user", review)
    error = ClientError(
        {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [
                {"Code": "TransactionConflict"},
                {"Code": "None"},
            ],
        },
        "TransactWriteItems",
    )
    mocker.patch.object(
        repo.table.meta.client, "transact_write_items", side_effect=error
    )

    with pytest.raises(ClientError) as raised:
        repo.confirm_meal_and_transition(
            "user",
            _meal(8, 12, "Soup"),
            _continuation_meal_state(review),
            expected_revision=review.revision,
            submission_id=review.request_id,
        )

    assert raised.value is error
    assert repo.get_conversation_state("user") == review
    assert repo.get_meal_history("user", days=1, on_date=date(2026, 8, 8)) == []


def test_atomic_meal_and_state_transition_writes_one_meal_and_marker(
    repo: DynamoRepository,
) -> None:
    state = _completed_meal_state()
    assert repo.save_conversation_state("user", state)
    entry = _meal(8, 12, "Soup")
    next_state = state.model_copy(
        update={
            "revision": 1,
            "updated_at": state.updated_at + timedelta(seconds=1),
        }
    )

    assert repo.log_meal_and_transition(
        "user",
        entry,
        next_state,
        expected_revision=state.revision,
        source_update_id="100",
    )

    assert repo.get_conversation_state("user") == next_state
    assert repo.get_meal_history("user", days=1, on_date=date(2026, 8, 8)) == [
        entry
    ]
    marker = repo.table.get_item(
        Key={"PK": "USER#user", "SK": "MEAL_UPDATE#100"}
    )
    assert marker["Item"]["SK"] == "MEAL_UPDATE#100"


def test_atomic_meal_and_state_transition_rejects_stale_revision(
    repo: DynamoRepository,
) -> None:
    state = _completed_meal_state()
    assert repo.save_conversation_state("user", state)
    competing = state.model_copy(
        update={
            "revision": 1,
            "updated_at": state.updated_at + timedelta(seconds=1),
        }
    )
    assert repo.transition_conversation_state(
        "user", competing, expected_revision=state.revision
    )
    next_state = state.model_copy(
        update={
            "revision": 1,
            "updated_at": state.updated_at + timedelta(seconds=2),
        }
    )

    assert not repo.log_meal_and_transition(
        "user",
        _meal(8, 12, "Stale meal"),
        next_state,
        expected_revision=state.revision,
        source_update_id="101",
    )
    assert repo.get_conversation_state("user") == competing
    assert repo.get_meal_history("user", days=1, on_date=date(2026, 8, 8)) == []
    assert (
        repo.table.get_item(
            Key={"PK": "USER#user", "SK": "MEAL_UPDATE#101"}
        ).get("Item")
        is None
    )


def test_atomic_meal_and_state_transition_propagates_transaction_error(
    repo: DynamoRepository, mocker: Any
) -> None:
    state = _completed_meal_state()
    assert repo.save_conversation_state("user", state)
    error = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException"}},
        "TransactWriteItems",
    )
    mocker.patch.object(
        repo.table.meta.client, "transact_write_items", side_effect=error
    )
    next_state = state.model_copy(
        update={
            "revision": 1,
            "updated_at": state.updated_at + timedelta(seconds=1),
        }
    )

    with pytest.raises(ClientError) as raised:
        repo.log_meal_and_transition(
            "user",
            _meal(8, 12, "Failed meal"),
            next_state,
            expected_revision=state.revision,
            source_update_id="102",
        )

    assert raised.value is error
    assert repo.get_conversation_state("user") == state
    assert repo.get_meal_history("user", days=1, on_date=date(2026, 8, 8)) == []


def test_meal_history_includes_multiple_meals_and_date_boundaries(
    repo: DynamoRepository,
) -> None:
    for entry in (
        _meal(1, 8, "too old"),
        _meal(2, 8, "start boundary"),
        _meal(2, 12, "same day second meal"),
        _meal(8, 8, "end boundary"),
    ):
        repo.log_meal("user", entry)
    history = repo.get_meal_history("user", days=7, on_date=date(2026, 8, 8))
    assert [entry.description for entry in history] == [
        "end boundary",
        "same day second meal",
        "start boundary",
    ]


def test_meal_log_retries_are_idempotent_per_source_update(
    repo: DynamoRepository,
) -> None:
    first = _meal(8, 12, "first description")
    retry = MealLogEntry(
        date=date(2026, 8, 9),
        meal_type="dinner",
        description="retry description",
        created_at=datetime(2026, 8, 8, 13, tzinfo=timezone.utc),
    )
    distinct = MealLogEntry(
        date=date(2026, 8, 8),
        meal_type="dinner",
        description="distinct update",
        created_at=datetime(2026, 8, 8, 14, tzinfo=timezone.utc),
    )

    repo.log_meal("user", first, source_update_id="100")
    repo.log_meal("user", retry, source_update_id="100")
    repo.log_meal("user", distinct, source_update_id="101")

    saved = repo.table.get_item(
        Key={
            "PK": "USER#user",
            "SK": "MEAL#2026-08-08#UPDATE#100#lunch",
        }
    )["Item"]
    assert saved["description"] == "first description"
    assert (
        repo.table.get_item(Key={"PK": "USER#user", "SK": "MEAL_UPDATE#100"})[
            "Item"
        ]["SK"]
        == "MEAL_UPDATE#100"
    )
    history = repo.get_meal_history("user", days=1, on_date=date(2026, 8, 8))
    assert [entry.description for entry in history] == [
        "distinct update",
        "first description",
    ]


def test_source_meal_transaction_contains_stable_marker_after_meal(
    mocker: Any,
) -> None:
    """Greenfield writes make markerless source updates unreachable."""
    table = mocker.MagicMock()
    table.name = "test-meal-planner"

    DynamoRepository(table).log_meal(
        "user", _meal(8, 12, "meal"), source_update_id="100"
    )

    request = table.meta.client.transact_write_items.call_args.kwargs
    meal_put, marker_put = request["TransactItems"]
    assert list(meal_put) == ["Put"]
    assert list(marker_put) == ["Put"]
    assert meal_put["Put"]["Item"]["SK"].startswith(
        "MEAL#2026-08-08#UPDATE#100#"
    )
    assert marker_put["Put"] == {
        "TableName": "test-meal-planner",
        "Item": {"PK": "USER#user", "SK": "MEAL_UPDATE#100"},
        "ConditionExpression": "attribute_not_exists(PK)",
    }


def test_marker_only_transaction_cancellation_is_distinguishable(
    mocker: Any,
) -> None:
    """A marker conflict is distinct from an unexpected transaction failure."""
    table = mocker.MagicMock()
    table.name = "test-meal-planner"
    error = ClientError(
        {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [
                {"Code": "None"},
                {"Code": "ConditionalCheckFailed"},
            ],
        },
        "TransactWriteItems",
    )
    table.meta.client.transact_write_items.side_effect = error

    DynamoRepository(table).log_meal(
        "user", _meal(8, 12, "retry"), source_update_id="100"
    )


def test_meal_marker_conflict_is_an_idempotent_success(mocker: Any) -> None:
    table = mocker.MagicMock()
    table.name = "test-meal-planner"
    table.meta.client.transact_write_items.side_effect = ClientError(
        {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [
                {"Code": "None"},
                {"Code": "ConditionalCheckFailed"},
            ],
        },
        "TransactWriteItems",
    )

    DynamoRepository(table).log_meal(
        "user", _meal(8, 12, "duplicate"), source_update_id="100"
    )


@pytest.mark.parametrize(
    "error",
    [
        ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}},
            "TransactWriteItems",
        ),
        ClientError(
            {
                "Error": {"Code": "TransactionCanceledException"},
                "CancellationReasons": [
                    {"Code": "None"},
                    {"Code": "TransactionConflict"},
                ],
            },
            "TransactWriteItems",
        ),
    ],
)
def test_meal_transaction_unexpected_failures_propagate(
    mocker: Any, error: ClientError
) -> None:
    table = mocker.MagicMock()
    table.name = "test-meal-planner"
    table.meta.client.transact_write_items.side_effect = error

    with pytest.raises(ClientError) as raised:
        DynamoRepository(table).log_meal(
            "user", _meal(8, 12, "meal"), source_update_id="100"
        )

    assert raised.value is error


def test_meal_log_without_source_id_and_legacy_keys_are_queryable(
    repo: DynamoRepository,
) -> None:
    timestamped = _meal(8, 12, "timestamped")
    legacy = _meal(8, 13, "legacy")

    repo.log_meal("user", timestamped)
    repo.table.put_item(
        Item={
            "PK": "USER#user",
            "SK": "MEAL#2026-08-08#2026-08-08T13:00:00+00:00#lunch",
            **legacy.model_dump(mode="json"),
        }
    )

    saved = repo.table.get_item(
        Key={
            "PK": "USER#user",
            "SK": "MEAL#2026-08-08#TIME#2026-08-08T12:00:00+00:00#lunch",
        }
    )["Item"]
    assert saved["description"] == "timestamped"
    history = repo.get_meal_history("user", days=1, on_date=date(2026, 8, 8))
    assert [entry.description for entry in history] == [
        "legacy",
        "timestamped",
    ]


def test_meal_history_paginates_and_skips_malformed_items(mocker: Any) -> None:
    table = mocker.MagicMock()
    valid = {
        "PK": "USER#user",
        "SK": "MEAL#2026-08-08#x",
        **_meal(8, 8, "valid").model_dump(mode="json"),
    }
    table.query.side_effect = [
        {
            "Items": [{"PK": "USER#user", "SK": "bad"}],
            "LastEvaluatedKey": {"PK": "x"},
        },
        {"Items": [valid]},
    ]
    history = DynamoRepository(table).get_meal_history(
        "user", days=1, on_date=date(2026, 8, 8)
    )
    assert [entry.description for entry in history] == ["valid"]
    assert table.query.call_count == 2


def test_plan_selection_distinguishes_latest_exact_and_active(
    repo: DynamoRepository,
) -> None:
    active = make_plan(week_start=date(2026, 8, 4), status=PlanStatus.CONFIRMED)
    future = make_plan(week_start=date(2026, 8, 18))
    repo.save_plan("user", active)
    repo.save_plan("user", future)
    assert repo.get_latest_plan("user") == future
    assert repo.get_plan("user", "2026-08-04") == active
    assert repo.get_active_plan("user", date(2026, 8, 10)) == active
    assert repo.get_active_plan("user", date(2026, 8, 11)) is None


def test_active_plan_snapshot_returns_legacy_absent_epoch(
    repo: DynamoRepository,
) -> None:
    plan = make_plan(week_start=date(2026, 8, 10), status=PlanStatus.CONFIRMED)
    repo.save_plan("user", plan)

    snapshot = repo.get_active_plan_snapshot("user", on_date=date(2026, 8, 13))

    assert snapshot is not None
    assert snapshot.plan == plan
    assert snapshot.active_epoch is None


def test_active_callback_write_rejects_older_overlapping_plan(
    repo: DynamoRepository,
) -> None:
    older = make_plan(week_start=date(2026, 8, 10), status=PlanStatus.CONFIRMED)
    repo.save_plan("user", older)
    snapshot = repo.get_active_plan_snapshot("user", on_date=date(2026, 8, 13))
    assert snapshot is not None
    assert snapshot.active_epoch is None

    newer = make_plan(week_start=date(2026, 8, 12))
    repo.save_plan("user", newer)
    assert repo.confirm_plan("user", newer.week_start_date, 0)

    assert not repo.update_meal_outcome(
        "user",
        older.week_start_date,
        1,
        "lunch",
        MealOutcome.COOKED,
        expected_epoch=snapshot.active_epoch,
    )
    saved_older = repo.get_plan("user", older.week_start_date)
    assert saved_older is not None
    assert saved_older.days[0].meals[0].outcome is MealOutcome.UNREPORTED


def test_active_callback_write_accepts_present_epoch(
    repo: DynamoRepository,
) -> None:
    plan = make_plan(week_start=date(2026, 8, 10))
    repo.save_plan("user", plan)
    assert repo.confirm_plan("user", plan.week_start_date, 0)

    snapshot = repo.get_active_plan_snapshot("user", on_date=date(2026, 8, 13))
    assert snapshot is not None
    assert snapshot.active_epoch == 1
    assert repo.update_meal_outcome(
        "user",
        plan.week_start_date,
        1,
        "lunch",
        MealOutcome.COOKED,
        expected_epoch=snapshot.active_epoch,
    )
    saved = repo.get_plan("user", plan.week_start_date)
    assert saved is not None
    assert saved.days[0].meals[0].outcome is MealOutcome.COOKED


def test_present_epoch_values_are_scoped_to_condition_check(
    mocker: Any,
) -> None:
    table = mocker.MagicMock()
    table.name = "test-meal-planner"
    plan = make_plan(status=PlanStatus.CONFIRMED)
    repo = DynamoRepository(table)
    mocker.patch.object(repo, "get_plan", return_value=plan)

    assert repo.update_meal_outcome(
        "user",
        plan.week_start_date,
        1,
        "lunch",
        MealOutcome.COOKED,
        expected_epoch=7,
    )

    request = table.meta.client.transact_write_items.call_args.kwargs
    update, condition_check = request["TransactItems"]
    update_values = update["Update"]["ExpressionAttributeValues"]
    condition_values = condition_check["ConditionCheck"][
        "ExpressionAttributeValues"
    ]
    assert ":expected_epoch" not in update_values
    assert condition_values == {":expected_epoch": 7}


def test_get_plan_consistency_is_opt_in(mocker: Any) -> None:
    table = mocker.MagicMock()
    plan = make_plan()
    table.get_item.return_value = {
        "Item": {
            "PK": "USER#user",
            "SK": f"PLAN#{plan.week_start_date}",
            **plan.model_dump(by_alias=True, mode="json"),
        }
    }
    repo = DynamoRepository(table)

    assert repo.get_plan("user", plan.week_start_date) == plan
    assert "ConsistentRead" not in table.get_item.call_args.kwargs

    assert (
        repo.get_plan("user", plan.week_start_date, consistent_read=True)
        == plan
    )
    assert table.get_item.call_args.kwargs["ConsistentRead"] is True


def test_transaction_conditional_conflict_returns_false(
    mocker: Any,
) -> None:
    table = mocker.MagicMock()
    table.name = "test-meal-planner"
    plan = make_plan(status=PlanStatus.CONFIRMED)
    repo = DynamoRepository(table)
    mocker.patch.object(repo, "get_plan", return_value=plan)
    table.meta.client.transact_write_items.side_effect = ClientError(
        {
            "Error": {
                "Code": "TransactionCanceledException",
                "Message": "conditional conflict",
            },
            "CancellationReasons": [
                {"Code": "None"},
                {"Code": "ConditionalCheckFailed"},
            ],
        },
        "TransactWriteItems",
    )

    assert not repo.update_meal_outcome(
        "user",
        plan.week_start_date,
        1,
        "lunch",
        MealOutcome.COOKED,
        expected_epoch=1,
    )


def test_transaction_service_failure_is_reraised(mocker: Any) -> None:
    table = mocker.MagicMock()
    table.name = "test-meal-planner"
    plan = make_plan(status=PlanStatus.CONFIRMED)
    repo = DynamoRepository(table)
    mocker.patch.object(repo, "get_plan", return_value=plan)
    error = ClientError(
        {
            "Error": {
                "Code": "TransactionCanceledException",
                "Message": "transaction conflict",
            },
            "CancellationReasons": [
                {"Code": "None"},
                {"Code": "TransactionConflict"},
            ],
        },
        "TransactWriteItems",
    )
    table.meta.client.transact_write_items.side_effect = error

    with pytest.raises(ClientError) as raised:
        repo.update_meal_outcome(
            "user",
            plan.week_start_date,
            1,
            "lunch",
            MealOutcome.COOKED,
            expected_epoch=1,
        )

    assert raised.value is error


def test_atomic_outcome_updates_preserve_independent_meals(
    repo: DynamoRepository,
) -> None:
    plan = make_plan(status=PlanStatus.CONFIRMED)
    repo.save_plan("user", plan)
    assert repo.update_meal_outcome(
        "user", plan.week_start_date, 1, "lunch", MealOutcome.COOKED
    )
    assert repo.update_meal_outcome(
        "user", plan.week_start_date, 2, "lunch", MealOutcome.SWAPPED
    )
    saved = repo.get_plan("user", plan.week_start_date)
    assert saved is not None
    assert saved.days[0].meals[0].outcome is MealOutcome.COOKED
    assert saved.days[1].meals[0].outcome is MealOutcome.SWAPPED
    assert not repo.update_meal_outcome(
        "user", plan.week_start_date, 1, "dinner", MealOutcome.SKIPPED
    )


def test_atomic_outcome_rejects_draft(repo: DynamoRepository) -> None:
    plan = make_plan()
    repo.save_plan("user", plan)
    assert not repo.update_meal_outcome(
        "user", plan.week_start_date, 1, "lunch", MealOutcome.COOKED
    )


def test_generated_draft_cannot_replace_confirmed_plan(
    repo: DynamoRepository,
) -> None:
    week = date(2026, 8, 10)
    draft = make_plan(week_start=week)
    assert repo.save_generated_draft("user", draft, expected_revision=None)
    replacement = make_plan(week_start=week, revision=1)
    replacement.days[0].meals[0].name = "Replacement"
    assert repo.save_generated_draft("user", replacement, expected_revision=0)
    assert repo.confirm_plan("user", draft.week_start_date, 1)
    assert not repo.save_generated_draft(
        "user", replacement, expected_revision=1
    )
    saved = repo.get_plan("user", week)
    assert saved is not None
    assert saved.status is PlanStatus.CONFIRMED


def test_generated_draft_rejects_stale_edit_and_duplicate_worker(
    repo: DynamoRepository,
) -> None:
    week = date(2026, 8, 10)
    draft = make_plan(week_start=week)
    assert repo.save_generated_draft("user", draft, expected_revision=None)
    edited_meal = draft.days[0].meals[0].model_copy(update={"name": "Edit"})
    assert repo.update_meal(
        "user",
        draft.week_start_date,
        1,
        "lunch",
        edited_meal,
        expected_revision=0,
        expected_status=PlanStatus.DRAFT,
    )

    replacement = make_plan(week_start=week, revision=1)
    assert not repo.save_generated_draft(
        "user", replacement, expected_revision=0
    )
    assert not repo.save_generated_draft(
        "user", replacement, expected_revision=None
    )


def test_tracked_generated_draft_publishes_and_clears_state_atomically(
    repo: DynamoRepository,
) -> None:
    week = date(2026, 8, 10)
    draft = make_plan(week_start=week)
    now = datetime.now(timezone.utc)
    state = ConversationState(
        workflow_kind=ConversationWorkflowKind.PLAN_REQUEST,
        step=ConversationWorkflowStep.GENERATING,
        preference="balanced",
        request_id="request-1",
        revision=4,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
    )
    assert repo.save_conversation_state("user", state)

    assert repo.save_generated_draft_and_clear_conversation_state(
        "user",
        draft,
        expected_revision=None,
        request_id="request-1",
        expected_state_revision=4,
    )

    assert repo.get_plan("user", week) == draft
    assert repo.get_conversation_state("user") is None


def test_tracked_generated_draft_rejects_plan_revision_conflict(
    repo: DynamoRepository,
) -> None:
    week = date(2026, 8, 10)
    current = make_plan(week_start=week, revision=1)
    repo.save_plan("user", current)
    now = datetime.now(timezone.utc)
    state = ConversationState(
        workflow_kind=ConversationWorkflowKind.PLAN_REQUEST,
        step=ConversationWorkflowStep.GENERATING,
        request_id="request-1",
        revision=0,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
    )
    assert repo.save_conversation_state("user", state)
    replacement = make_plan(week_start=week, revision=2)

    assert not repo.save_generated_draft_and_clear_conversation_state(
        "user",
        replacement,
        expected_revision=0,
        request_id="request-1",
        expected_state_revision=0,
    )

    assert repo.get_plan("user", week) == current
    assert repo.get_conversation_state("user") == state


def test_tracked_generated_draft_rejects_state_ownership_conflict(
    repo: DynamoRepository,
) -> None:
    week = date(2026, 8, 10)
    current = make_plan(week_start=week, revision=1)
    repo.save_plan("user", current)
    now = datetime.now(timezone.utc)
    state = ConversationState(
        workflow_kind=ConversationWorkflowKind.PLAN_REQUEST,
        step=ConversationWorkflowStep.GENERATING,
        request_id="new-owner",
        revision=1,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
    )
    assert repo.save_conversation_state("user", state)
    replacement = make_plan(week_start=week, revision=2)

    assert not repo.save_generated_draft_and_clear_conversation_state(
        "user",
        replacement,
        expected_revision=1,
        request_id="old-owner",
        expected_state_revision=0,
    )

    assert repo.get_plan("user", week) == current
    assert repo.get_conversation_state("user") == state


def test_tracked_generated_draft_reraises_nonconditional_transaction_error(
    mocker: Any,
) -> None:
    table = mocker.MagicMock()
    table.name = "test-meal-planner"
    error = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException"}},
        "TransactWriteItems",
    )
    table.meta.client.transact_write_items.side_effect = error
    repo = DynamoRepository(table)

    with pytest.raises(ClientError) as raised:
        repo.save_generated_draft_and_clear_conversation_state(
            "user",
            make_plan(),
            expected_revision=None,
            request_id="request-1",
            expected_state_revision=0,
        )

    assert raised.value is error


def test_repaired_draft_publishes_with_atomic_repair_marker(
    repo: DynamoRepository,
) -> None:
    """An untracked repair writes its draft and marker in one transaction."""
    draft = make_plan(week_start=date(2026, 8, 10))

    outcome = repo.save_repaired_draft_once(
        "user", draft, expected_revision=None, repair_id="repair-123"
    )

    assert outcome is RepairPublicationOutcome.PUBLISHED
    assert repo.get_plan("user", draft.week_start) == draft
    marker = repo.table.get_item(
        Key={"PK": "USER#user", "SK": "PLAN_REPAIR#repair-123"}
    )["Item"]
    assert marker == {
        "PK": "USER#user",
        "SK": "PLAN_REPAIR#repair-123",
    }


def test_repaired_draft_replay_is_duplicate_and_silent_at_repository_boundary(
    repo: DynamoRepository,
) -> None:
    """A second transaction with one token cannot replace the first draft."""
    first = make_plan(week_start=date(2026, 8, 10))
    second = make_plan(week_start=first.week_start, revision=0)
    second.days[0].meals[0].name = "Different worker"

    assert (
        repo.save_repaired_draft_once(
            "user", first, expected_revision=None, repair_id="repair-123"
        )
        is RepairPublicationOutcome.PUBLISHED
    )
    assert (
        repo.save_repaired_draft_once(
            "user", second, expected_revision=None, repair_id="repair-123"
        )
        is RepairPublicationOutcome.DUPLICATE
    )
    assert repo.get_plan("user", first.week_start) == first


def test_repaired_draft_plan_revision_conflict_does_not_leave_marker(
    repo: DynamoRepository,
) -> None:
    """A stale plan condition rolls back the marker put."""
    current = make_plan(week_start=date(2026, 8, 10), revision=1)
    replacement = make_plan(week_start=current.week_start, revision=2)
    repo.save_plan("user", current)

    outcome = repo.save_repaired_draft_once(
        "user", replacement, expected_revision=0, repair_id="repair-123"
    )

    assert outcome is RepairPublicationOutcome.STALE
    assert repo.get_plan("user", current.week_start) == current
    assert not repo.table.get_item(
        Key={"PK": "USER#user", "SK": "PLAN_REPAIR#repair-123"}
    ).get("Item")


def test_repaired_draft_marker_conflict_does_not_write_plan(
    repo: DynamoRepository,
) -> None:
    """An existing marker rolls back a new-plan write."""
    repo.table.put_item(
        Item={"PK": "USER#user", "SK": "PLAN_REPAIR#repair-123"}
    )
    draft = make_plan(week_start=date(2026, 8, 10))

    outcome = repo.save_repaired_draft_once(
        "user", draft, expected_revision=None, repair_id="repair-123"
    )

    assert outcome is RepairPublicationOutcome.DUPLICATE
    assert repo.get_plan("user", draft.week_start) is None


@pytest.mark.parametrize(
    ("reasons", "expected"),
    [
        (
            [{"Code": "None"}, {"Code": "ConditionalCheckFailed"}],
            RepairPublicationOutcome.DUPLICATE,
        ),
        (
            [{"Code": "ConditionalCheckFailed"}, {"Code": "None"}],
            RepairPublicationOutcome.STALE,
        ),
    ],
)
def test_repaired_draft_classifies_exact_cancellation_reasons(
    mocker: Any,
    reasons: list[dict[str, str]],
    expected: RepairPublicationOutcome,
) -> None:
    """Only the documented plan/marker condition failures are classified."""
    table = mocker.MagicMock()
    table.name = "test-meal-planner"
    table.meta.client.transact_write_items.side_effect = ClientError(
        {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": reasons,
        },
        "TransactWriteItems",
    )

    outcome = DynamoRepository(table).save_repaired_draft_once(
        "user", make_plan(), expected_revision=None, repair_id="repair-123"
    )

    assert outcome is expected


def test_repaired_draft_reraises_unexpected_transaction_failure(
    mocker: Any,
) -> None:
    """Nonconditional transaction failures stay visible to Planner handling."""
    table = mocker.MagicMock()
    table.name = "test-meal-planner"
    error = ClientError(
        {
            "Error": {
                "Code": "TransactionCanceledException",
            },
            "CancellationReasons": [
                {"Code": "None"},
                {"Code": "TransactionConflict"},
            ],
        },
        "TransactWriteItems",
    )
    table.meta.client.transact_write_items.side_effect = error

    with pytest.raises(ClientError) as raised:
        DynamoRepository(table).save_repaired_draft_once(
            "user", make_plan(), expected_revision=None, repair_id="repair-123"
        )

    assert raised.value is error


def test_repaired_draft_concurrent_replays_publish_once(
    repo: DynamoRepository,
) -> None:
    """Concurrent workers sharing a token produce one durable publication."""
    barrier = Barrier(2)
    drafts = [make_plan(week_start=date(2026, 8, 10)) for _ in range(2)]

    def publish(draft: Any) -> RepairPublicationOutcome:
        barrier.wait()
        return repo.save_repaired_draft_once(
            "user", draft, expected_revision=None, repair_id="repair-123"
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(publish, drafts))

    assert sorted(outcome.value for outcome in outcomes) == [
        "duplicate",
        "published",
    ]


def test_generated_draft_reraises_nonconditional_dynamodb_errors(
    mocker: Any,
) -> None:
    table = mocker.MagicMock()
    error = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException"}},
        "PutItem",
    )
    table.put_item.side_effect = error
    repo = DynamoRepository(table)

    with pytest.raises(ClientError) as raised:
        repo.save_generated_draft("user", make_plan(), expected_revision=None)

    assert raised.value is error


def test_plan_lifecycle_writes_are_revision_checked(
    repo: DynamoRepository,
) -> None:
    plan = make_plan(
        status=PlanStatus.CONFIRMED, grocery_status=GroceryStatus.PENDING
    )
    repo.save_plan("user", plan)
    edited_meal = (
        plan.days[0].meals[0].model_copy(update={"name": "Edited lunch"})
    )
    assert repo.update_meal(
        "user",
        plan.week_start_date,
        1,
        "lunch",
        edited_meal,
        expected_revision=0,
        expected_status=PlanStatus.CONFIRMED,
    )
    assert not repo.update_meal(
        "user",
        plan.week_start_date,
        1,
        "lunch",
        edited_meal,
        expected_revision=0,
        expected_status=PlanStatus.CONFIRMED,
    )
    saved = repo.get_plan("user", plan.week_start_date)
    assert saved is not None
    assert saved.revision == 1
    assert saved.grocery_status.value == "pending"
    assert repo.complete_grocery(
        "user",
        plan.week_start_date,
        1,
        [GrocerySection(name="Produce", items=["Apples"])],
    )
    assert not repo.fail_grocery("user", plan.week_start_date, 1)


def test_stale_draft_edit_is_rejected_after_confirmation(
    repo: DynamoRepository,
) -> None:
    plan = make_plan()
    repo.save_plan("user", plan)
    edited_meal = (
        plan.days[0].meals[0].model_copy(update={"name": "Stale edit"})
    )
    assert repo.confirm_plan("user", plan.week_start_date, 0)

    assert not repo.update_meal(
        "user",
        plan.week_start_date,
        1,
        "lunch",
        edited_meal,
        expected_revision=0,
        expected_status=PlanStatus.DRAFT,
    )
    saved = repo.get_plan("user", plan.week_start_date)
    assert saved is not None
    assert saved.status is PlanStatus.CONFIRMED
    assert saved.revision == 0
    assert saved.grocery_status is GroceryStatus.PENDING
    assert saved.days[0].meals[0].name != "Stale edit"


def test_confirm_and_grocery_retry_are_conditional(
    repo: DynamoRepository,
) -> None:
    plan = make_plan()
    repo.save_plan("user", plan)
    assert repo.confirm_plan("user", plan.week_start_date, 0)
    assert not repo.confirm_plan("user", plan.week_start_date, 0)
    assert repo.fail_grocery("user", plan.week_start_date, 0)
    assert repo.retry_grocery("user", plan.week_start_date, 0)
    assert not repo.retry_grocery("user", plan.week_start_date, 0)


def test_grocery_worker_races_preserve_outcomes_and_reject_stale_edits(
    repo: DynamoRepository,
) -> None:
    plan = make_plan(
        status=PlanStatus.CONFIRMED, grocery_status=GroceryStatus.PENDING
    )
    repo.save_plan("user", plan)
    worker_revision = plan.revision
    assert repo.update_meal_outcome(
        "user", plan.week_start_date, 1, "lunch", MealOutcome.COOKED
    )
    assert repo.complete_grocery(
        "user",
        plan.week_start_date,
        worker_revision,
        [GrocerySection(name="Produce", items=["Apples"])],
    )
    ready = repo.get_plan("user", plan.week_start_date)
    assert ready is not None
    assert ready.days[0].meals[0].outcome is MealOutcome.COOKED
    assert ready.grocery_status is GroceryStatus.READY

    updated_meal = (
        ready.days[0].meals[0].model_copy(update={"name": "New lunch"})
    )
    assert repo.update_meal(
        "user",
        plan.week_start_date,
        1,
        "lunch",
        updated_meal,
        expected_revision=worker_revision,
        expected_status=PlanStatus.CONFIRMED,
    )
    assert not repo.complete_grocery(
        "user",
        plan.week_start_date,
        worker_revision,
        [GrocerySection(name="Stale", items=["Old item"])],
    )
    edited = repo.get_plan("user", plan.week_start_date)
    assert edited is not None
    assert edited.days[0].meals[0].name == "New lunch"
    assert edited.grocery_status is GroceryStatus.PENDING
