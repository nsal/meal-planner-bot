"""DynamoDB repository integration tests."""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from threading import Barrier
from typing import Any, Generator
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from pydantic import ValidationError

from meal_planner.bot_handler import BotHandler
from meal_planner.db.dynamo import DynamoRepository
from meal_planner.models.schemas import (
    ConversationState,
    ConversationWorkflowKind,
    ConversationWorkflowStep,
    FamilyMember,
    MealLogDraft,
    MealLogEntry,
    ProfileDraft,
    ProfileEditCategory,
    ProfileEditOperation,
    UserProfile,
)
from meal_planner.router import RouteResult, RouteType
from tests.factories import (
    make_legacy_profile_item,
    make_profile,
)


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
    draft = ProfileDraft(name="Alex", people_count=2)
    repo.save_profile_draft("user", draft)
    assert repo.get_profile_draft("user").name == "Alex"
    profile = make_profile()
    repo.save_profile("user", profile, expected_revision=None)
    assert repo.get_profile("user") == profile
    repo.delete_profile_draft("user")
    assert repo.get_profile_draft("user") is None


def test_profile_round_trip_writes_only_raw_dietary_text(
    repo: DynamoRepository,
) -> None:
    """Legacy profile mappings are simplified on read and write."""
    repo.table.put_item(
        Item={
            "PK": "USER#user",
            "SK": "PROFILE",
            "name": "Alex",
            "people_count": 1,
            "family_members": [{"name": "Alex", "calorie_target": 2000}],
            "dietary_constraints": [
                {"source_text": "Peanuts", "forbidden_terms": ["peanut"]},
                {"source_text": "PEANUTS", "rule": {}},
                {"source_text": None},
            ],
            "dietary_preferences": [
                {"source_text": "More vegetables", "rule": None},
                "no preferences",
            ],
            "batch_rules": [{"source_text": "cook once"}],
        }
    )

    loaded = repo.get_profile("user")

    assert loaded is not None
    assert loaded.dietary_constraints == ["Peanuts"]
    assert loaded.dietary_preferences == ["More vegetables"]
    assert repo.save_profile("user", loaded, expected_revision=0)
    saved = repo.table.get_item(Key={"PK": "USER#user", "SK": "PROFILE"})[
        "Item"
    ]
    assert saved["dietary_constraints"] == ["Peanuts"]
    assert saved["dietary_preferences"] == ["More vegetables"]
    assert "batch_rules" not in saved


def test_profile_read_rejects_malformed_raw_dietary_shape(
    repo: DynamoRepository,
) -> None:
    """A saved dietary field with the wrong top-level shape is unsafe."""
    repo.table.put_item(
        Item={
            "PK": "USER#user",
            "SK": "PROFILE",
            "name": "Alex",
            "people_count": 1,
            "family_members": [],
            "dietary_constraints": {"source_text": "peanuts"},
            "dietary_preferences": [],
        }
    )

    with pytest.raises(ValidationError):
        repo.get_profile("user")


def test_guarded_profile_update_preserves_raw_lists_without_rule_merge(
    repo: DynamoRepository,
) -> None:
    """A guarded edit commits the candidate lists exactly as raw text."""
    original = make_profile().model_copy(
        update={
            "dietary_constraints": ["Peanuts"],
            "dietary_preferences": ["More vegetables"],
        }
    )
    observed = _profile_edit_state(
        operation=ProfileEditOperation.ADD,
    )
    next_state = _profile_menu_state(observed)
    updated = original.model_copy(
        update={
            "dietary_constraints": ["Peanuts", "No shellfish"],
            "dietary_preferences": ["More vegetables", "Pasta"],
        }
    )
    assert repo.save_profile("user", original, expected_revision=None)
    assert repo.save_conversation_state("user", observed)

    assert repo.save_profile_and_transition_state(
        "user", updated, next_state, observed
    )

    saved = repo.get_profile("user", consistent_read=True)
    assert saved is not None
    assert saved.dietary_constraints == ["Peanuts", "No shellfish"]
    assert saved.dietary_preferences == ["More vegetables", "Pasta"]


def test_canonical_profile_round_trip_discards_goals_on_read_and_write(
    repo: DynamoRepository,
) -> None:
    """Raw dietary text survives a round trip and goals never persist."""
    profile = make_profile(
        dietary_constraints=["peanuts"],
        dietary_preferences=["eggs for breakfast"],
    )

    repo.table.put_item(
        Item={
            "PK": "USER#user",
            "SK": "PROFILE",
            **profile.model_dump(mode="json"),
            "goals": ["lose weight"],
        }
    )

    loaded = repo.get_profile("user")
    assert loaded == profile
    repo.save_profile("user", loaded, expected_revision=loaded.profile_revision)

    item = repo.table.get_item(Key={"PK": "USER#user", "SK": "PROFILE"})["Item"]
    assert item["dietary_constraints"] == ["peanuts"]
    assert item["dietary_preferences"] == ["eggs for breakfast"]
    assert "goals" not in item


def test_legacy_profile_preferences_are_discarded_on_read(
    repo: DynamoRepository,
) -> None:
    """Saved reads discard malformed preferences while retaining constraints."""
    legacy = make_legacy_profile_item()
    legacy["family_members"] = [{"name": "Alex", "calorie_target": 2000}]
    repo.table.put_item(
        Item={
            "PK": "USER#user",
            "SK": "PROFILE",
            **legacy,
        }
    )

    profile = repo.get_profile("user")

    assert profile is not None
    assert profile.dietary_preferences == ["More vegetables"]
    assert profile.dietary_constraints == ["Peanuts"]


def test_compatible_profile_read_logs_bounded_non_content_diagnostic(
    repo: DynamoRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """Discard diagnostics contain only a category and bounded count."""
    profile = make_profile().model_dump(mode="json")
    profile["dietary_preferences"] = [
        "secret preference text",
        {"id": "missing", "source_text": "secret missing"},
        {"id": "null", "source_text": "secret null", "rule": None},
    ]
    repo.table.put_item(
        Item={"PK": "USER#secret-user", "SK": "PROFILE", **profile}
    )

    with caplog.at_level(logging.WARNING, logger="meal_planner.db.dynamo"):
        loaded = repo.get_profile("secret-user")

    assert loaded is not None
    assert "secret" not in caplog.text
    assert loaded.dietary_preferences == [
        "secret preference text",
        "secret missing",
        "secret null",
    ]
    assert not caplog.records


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


def _profile_setup_state(
    *,
    revision: int = 0,
    step: ConversationWorkflowStep = (
        ConversationWorkflowStep.AWAITING_PROFILE_FAMILY_NAME
    ),
    now: datetime | None = None,
    last_update_id: str | None = None,
) -> ConversationState:
    """Build a valid persisted state for deterministic profile setup."""
    current = now or datetime.now(timezone.utc)
    return ConversationState(
        workflow_kind=ConversationWorkflowKind.PROFILE_SETUP,
        step=step,
        revision=revision,
        created_at=current,
        updated_at=current,
        expires_at=int((current + timedelta(hours=24)).timestamp()),
        last_update_id=last_update_id,
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


def test_competing_profile_setup_answers_commit_as_one_transaction(
    repo: DynamoRepository,
) -> None:
    """Only one answer can advance one observed setup state."""
    observed = _profile_setup_state()
    first = ProfileDraft(name="First")
    second = ProfileDraft(name="Second")
    first_state = observed.model_copy(
        update={
            "step": ConversationWorkflowStep.AWAITING_PROFILE_HOUSEHOLD_SIZE,
            "revision": observed.revision + 1,
            "last_update_id": "first-update",
        }
    )
    second_state = first_state.model_copy(
        update={"last_update_id": "second-update"}
    )
    assert repo.save_conversation_state("user", observed)
    writes = Barrier(2)

    def commit_answer(
        candidate: tuple[ProfileDraft, ConversationState],
    ) -> bool:
        writes.wait(timeout=5)
        draft, next_state = candidate
        return repo.save_profile_draft_and_transition_state(
            "user", draft, next_state, observed
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                commit_answer, [(first, first_state), (second, second_state)]
            )
        )

    assert sorted(outcomes) == [False, True]
    saved_draft = repo.get_profile_draft("user")
    assert saved_draft.name in {"First", "Second"}
    saved_state = repo.get_conversation_state("user")
    assert saved_state is not None
    expected_state = (
        first_state if saved_draft.name == "First" else second_state
    )
    assert saved_state == expected_state


def test_profile_setup_completion_rejects_replaced_state_without_writes(
    repo: DynamoRepository,
) -> None:
    """A stale completion cannot consume a replacement setup workflow."""
    draft = ProfileDraft(
        name="Smith",
        people_count=1,
        family_members=[FamilyMember(name="Alex", calorie_target=2_000)],
        dietary_constraints=[],
        dietary_preferences=[],
    )
    observed = _profile_setup_state(
        revision=5,
        step=ConversationWorkflowStep.AWAITING_PROFILE_PREFERENCES,
    )
    replacement = observed.model_copy(
        update={
            "revision": observed.revision + 1,
            "step": ConversationWorkflowStep.AWAITING_PROFILE_FAMILY_NAME,
            "last_update_id": "replacement-update",
        }
    )
    candidate = UserProfile(
        name=draft.name or "",
        people_count=draft.people_count or 0,
        family_members=draft.family_members or [],
        dietary_constraints=draft.dietary_constraints or [],
        dietary_preferences=draft.dietary_preferences or [],
    )
    repo.save_profile_draft("user", draft)
    assert repo.save_conversation_state("user", observed)
    assert repo.save_conversation_state(
        "user",
        replacement,
        expected_revision=observed.revision,
        expected_step=observed.step,
    )

    assert not repo.complete_profile_setup(
        "user",
        candidate,
        observed,
        expected_profile_revision=None,
    )

    assert repo.get_profile("user", consistent_read=True) is None
    assert repo.get_profile_draft("user") == draft
    assert repo.get_conversation_state("user", consistent_read=True) == (
        replacement
    )


@pytest.mark.parametrize("existing_profile", [False, True])
def test_profile_setup_completion_commits_profile_and_cleanup_atomically(
    repo: DynamoRepository,
    existing_profile: bool,
) -> None:
    """Completion supports creation and optimistic profile replacement."""
    draft = ProfileDraft(
        name="Smith",
        people_count=1,
        family_members=[FamilyMember(name="Alex", calorie_target=2_000)],
        dietary_constraints=[],
        dietary_preferences=[],
    )
    observed = _profile_setup_state(
        revision=5,
        step=ConversationWorkflowStep.AWAITING_PROFILE_PREFERENCES,
    )
    original = make_profile() if existing_profile else None
    if original is not None:
        assert repo.save_profile("user", original, expected_revision=None)
    repo.save_profile_draft("user", draft)
    assert repo.save_conversation_state("user", observed)
    candidate = UserProfile(
        name=draft.name or "",
        people_count=draft.people_count or 0,
        family_members=draft.family_members or [],
        dietary_constraints=draft.dietary_constraints or [],
        dietary_preferences=draft.dietary_preferences or [],
        profile_revision=original.profile_revision if original else 0,
    )

    assert repo.complete_profile_setup(
        "user",
        candidate,
        observed,
        expected_profile_revision=(
            original.profile_revision if original else None
        ),
    )

    expected_revision = (original.profile_revision + 1) if original else 0
    assert repo.get_profile(
        "user", consistent_read=True
    ) == candidate.model_copy(update={"profile_revision": expected_revision})
    assert repo.get_profile_draft("user") is None
    assert repo.get_conversation_state("user", consistent_read=True) is None


def test_profile_setup_draft_retry_reuses_token_after_commit_then_error(
    repo: DynamoRepository,
    mocker: Any,
) -> None:
    """A lost draft response is recovered by DynamoDB idempotency."""
    observed = _profile_setup_state()
    next_state = observed.model_copy(
        update={
            "step": ConversationWorkflowStep.AWAITING_PROFILE_HOUSEHOLD_SIZE,
            "revision": observed.revision + 1,
        }
    )
    draft = ProfileDraft(name="Smith")
    assert repo.save_conversation_state("user", observed)
    real_transact_write_items = repo.table.meta.client.transact_write_items
    calls: list[dict[str, Any]] = []
    committed_token: str | None = None
    cached_response: dict[str, Any] = {}
    error = ClientError(
        {
            "Error": {
                "Code": "InternalServerError",
                "Message": "response lost after commit",
            }
        },
        "TransactWriteItems",
    )

    def commit_then_lose_response(**kwargs: Any) -> dict[str, Any]:
        nonlocal committed_token
        calls.append(kwargs)
        if len(calls) == 1:
            real_transact_write_items(**kwargs)
            committed_token = kwargs.get("ClientRequestToken")
            raise error
        if committed_token and kwargs.get("ClientRequestToken") == (
            committed_token
        ):
            return cached_response
        return real_transact_write_items(**kwargs)

    mocker.patch.object(
        repo.table.meta.client,
        "transact_write_items",
        side_effect=commit_then_lose_response,
    )

    assert repo.save_profile_draft_and_transition_state(
        "user", draft, next_state, observed
    )

    assert len(calls) == 2
    first_token = calls[0].get("ClientRequestToken")
    second_token = calls[1].get("ClientRequestToken")
    assert isinstance(first_token, str) and first_token
    assert first_token == second_token
    assert repo.get_profile_draft("user") == draft
    assert repo.get_conversation_state("user", consistent_read=True) == (
        next_state
    )


def test_profile_setup_completion_retry_reuses_token_after_commit_then_error(
    repo: DynamoRepository,
    mocker: Any,
) -> None:
    """A lost completion response is recovered by DynamoDB idempotency."""
    draft = ProfileDraft(
        name="Smith",
        people_count=1,
        family_members=[FamilyMember(name="Alex", calorie_target=2_000)],
        dietary_constraints=[],
        dietary_preferences=[],
    )
    observed = _profile_setup_state(
        step=ConversationWorkflowStep.AWAITING_PROFILE_PREFERENCES
    )
    candidate = UserProfile(
        name=draft.name or "",
        people_count=draft.people_count or 0,
        family_members=draft.family_members or [],
        dietary_constraints=draft.dietary_constraints or [],
        dietary_preferences=draft.dietary_preferences or [],
    )
    repo.save_profile_draft("user", draft)
    assert repo.save_conversation_state("user", observed)
    real_transact_write_items = repo.table.meta.client.transact_write_items
    calls: list[dict[str, Any]] = []
    committed_token: str | None = None
    cached_response: dict[str, Any] = {}
    error = ClientError(
        {
            "Error": {
                "Code": "InternalServerError",
                "Message": "response lost after commit",
            }
        },
        "TransactWriteItems",
    )

    def commit_then_lose_response(**kwargs: Any) -> dict[str, Any]:
        nonlocal committed_token
        calls.append(kwargs)
        if len(calls) == 1:
            real_transact_write_items(**kwargs)
            committed_token = kwargs.get("ClientRequestToken")
            raise error
        if committed_token and kwargs.get("ClientRequestToken") == (
            committed_token
        ):
            return cached_response
        return real_transact_write_items(**kwargs)

    mocker.patch.object(
        repo.table.meta.client,
        "transact_write_items",
        side_effect=commit_then_lose_response,
    )

    assert repo.complete_profile_setup(
        "user",
        candidate,
        observed,
        expected_profile_revision=None,
    )

    assert len(calls) == 2
    first_token = calls[0].get("ClientRequestToken")
    second_token = calls[1].get("ClientRequestToken")
    assert isinstance(first_token, str) and first_token
    assert first_token == second_token
    assert repo.get_profile("user", consistent_read=True) == candidate
    assert repo.get_profile_draft("user") is None
    assert repo.get_conversation_state("user", consistent_read=True) is None


def test_profile_setup_transaction_tokens_are_scoped_per_invocation(
    mocker: Any,
) -> None:
    """Separate setup transactions receive separate idempotency tokens."""
    table = mocker.MagicMock()
    table.name = "test-meal-planner"
    repo = DynamoRepository(table)
    observed = _profile_setup_state()
    first_next_state = observed.model_copy(
        update={
            "step": ConversationWorkflowStep.AWAITING_PROFILE_HOUSEHOLD_SIZE,
            "revision": 1,
        }
    )
    second_next_state = first_next_state.model_copy(
        update={
            "step": ConversationWorkflowStep.AWAITING_PROFILE_MEMBERS,
            "revision": 2,
        }
    )

    assert repo.save_profile_draft_and_transition_state(
        "user", ProfileDraft(name="First"), first_next_state, observed
    )
    assert repo.save_profile_draft_and_transition_state(
        "user", ProfileDraft(name="Second"), second_next_state, first_next_state
    )

    requests = table.meta.client.transact_write_items.call_args_list
    first_token = requests[0].kwargs.get("ClientRequestToken")
    second_token = requests[1].kwargs.get("ClientRequestToken")
    assert isinstance(first_token, str) and first_token
    assert isinstance(second_token, str) and second_token
    assert first_token != second_token


@pytest.mark.parametrize("existing_profile", [False, True])
def test_profile_setup_completion_propagates_operational_errors(
    repo: DynamoRepository,
    existing_profile: bool,
    mocker: Any,
) -> None:
    """Unexpected finalization failures remain visible to the handler."""
    observed = _profile_setup_state(
        step=ConversationWorkflowStep.AWAITING_PROFILE_PREFERENCES
    )
    original = make_profile() if existing_profile else None
    if original is not None:
        assert repo.save_profile("user", original, expected_revision=None)
    error = ClientError(
        {
            "Error": {
                "Code": "InternalServerError",
                "Message": "database unavailable",
            }
        },
        "TransactWriteItems",
    )
    mocker.patch.object(
        repo.table.meta.client,
        "transact_write_items",
        side_effect=error,
    )

    with pytest.raises(ClientError) as raised:
        repo.complete_profile_setup(
            "user",
            make_profile(),
            observed,
            expected_profile_revision=(
                original.profile_revision if original else None
            ),
        )

    assert raised.value is error


def test_profile_confirmation_rejects_concurrent_profile_mutation(
    repo: DynamoRepository,
) -> None:
    """A stale confirmation cannot overwrite a profile changed after read."""
    original = make_profile()
    observed = _profile_edit_state()
    next_state = _profile_menu_state(observed)
    repo.save_profile("user", original, expected_revision=None)
    assert repo.save_conversation_state("user", observed)

    snapshot = repo.get_profile("user", consistent_read=True)
    assert snapshot is not None
    stale_update = snapshot.model_copy(
        update={"dietary_constraints": ["peanuts"]}
    )

    concurrent_update = snapshot.model_copy(
        update={
            "dietary_constraints": ["shellfish"],
            "dietary_preferences": [],
        }
    )
    repo.save_profile(
        "user", concurrent_update, expected_revision=snapshot.profile_revision
    )
    assert not repo.save_profile_and_transition_state(
        "user", stale_update, next_state, observed
    )
    assert repo.get_profile("user", consistent_read=True) == (
        concurrent_update.model_copy(update={"profile_revision": 1})
    )
    assert repo.get_conversation_state("user", consistent_read=True) == observed


def test_profile_confirmation_rejects_missing_profile(
    repo: DynamoRepository,
) -> None:
    """A confirmation cannot create a profile that disappeared meanwhile."""
    original = make_profile()
    updated = original.model_copy(update={"dietary_constraints": ["peanuts"]})
    observed = _profile_edit_state()
    repo.save_conversation_state("user", observed)

    assert not repo.save_profile_and_transition_state(
        "user", updated, _profile_menu_state(observed), observed
    )
    assert repo.get_profile("user", consistent_read=True) is None
    assert repo.get_conversation_state("user", consistent_read=True) == observed


def test_profile_amendment_transaction_commits_matching_profile_and_state(
    repo: DynamoRepository,
) -> None:
    """A matching profile edit commits both documents atomically."""
    original = make_profile()
    updated = original.model_copy(update={"dietary_constraints": ["peanuts"]})
    observed = _profile_edit_state()
    next_state = _profile_menu_state(observed)
    repo.save_profile("user", original, expected_revision=None)
    assert repo.save_conversation_state("user", observed)

    assert repo.save_profile_and_transition_state(
        "user", updated, next_state, observed
    )

    assert repo.get_profile("user") == updated.model_copy(
        update={"profile_revision": 1}
    )
    assert repo.get_conversation_state("user") == next_state


def test_numbered_profile_removal_transaction_retains_remove_mode(
    repo: DynamoRepository,
) -> None:
    """A numbered removal advances both guarded revisions atomically."""
    original = make_profile().model_copy(
        update={"dietary_constraints": ["peanuts"]}
    )
    observed = _profile_edit_state(operation=ProfileEditOperation.REMOVE)
    next_state = observed.model_copy(update={"revision": observed.revision + 1})
    repo.save_profile("user", original, expected_revision=None)
    assert repo.save_conversation_state("user", observed)

    updated = original.model_copy(update={"dietary_constraints": []})
    committed = repo.remove_profile_item_and_transition_state(
        "user",
        updated,
        next_state,
        observed,
        expected_profile_revision=original.profile_revision,
    )

    saved_profile = repo.get_profile("user", consistent_read=True)
    assert saved_profile is not None
    assert committed == saved_profile
    assert saved_profile.dietary_constraints == []
    assert saved_profile.profile_revision == original.profile_revision + 1
    saved_state = repo.get_conversation_state("user", consistent_read=True)
    assert saved_state == next_state
    assert saved_state.step is ConversationWorkflowStep.AWAITING_PROFILE_INPUT
    assert saved_state.profile_operation is ProfileEditOperation.REMOVE


def test_numbered_profile_removal_transaction_rejects_stale_profile_revision(
    repo: DynamoRepository,
) -> None:
    """A stale numbered profile snapshot cannot mutate either document."""
    original = make_profile()
    observed = _profile_edit_state(operation=ProfileEditOperation.REMOVE)
    next_state = observed.model_copy(update={"revision": observed.revision + 1})
    repo.save_profile("user", original, expected_revision=None)
    assert repo.save_conversation_state("user", observed)
    updated = original.model_copy(update={"family_members": []})

    assert not repo.remove_profile_item_and_transition_state(
        "user",
        updated,
        next_state,
        observed,
        expected_profile_revision=1,
    )
    assert repo.get_profile("user", consistent_read=True) == original
    assert repo.get_conversation_state("user", consistent_read=True) == observed


def test_numbered_constraint_removal_preserves_unrelated_profile_data(
    repo: DynamoRepository,
) -> None:
    """Removing one constraint does not rewrite unrelated profile fields."""
    profile = make_profile(with_nutrient_targets=True).model_copy(
        update={
            "dietary_constraints": ["peanuts", "shellfish"],
            "dietary_preferences": ["eggs", "oats", "shellfish"],
        }
    )
    repo.table.put_item(
        Item={
            "PK": "USER#user",
            "SK": "PROFILE",
            **profile.model_dump(mode="json"),
        }
    )
    observed_profile = repo.get_profile("user", consistent_read=True)
    assert observed_profile is not None
    observed = _profile_edit_state(operation=ProfileEditOperation.REMOVE)
    next_state = observed.model_copy(update={"revision": observed.revision + 1})
    repo.save_conversation_state("user", observed)

    updated = observed_profile.model_copy(
        update={"dietary_constraints": observed_profile.dietary_constraints[1:]}
    )
    expected = updated.model_copy(
        update={"profile_revision": observed_profile.profile_revision + 1}
    )

    committed = repo.remove_profile_item_and_transition_state(
        "user",
        updated,
        next_state,
        observed,
        expected_profile_revision=observed_profile.profile_revision,
    )

    assert committed == expected
    assert repo.get_profile("user", consistent_read=True) == expected
    assert (
        repo.get_conversation_state("user", consistent_read=True) == next_state
    )
    assert expected.dietary_constraints == [
        observed_profile.dietary_constraints[1]
    ]
    assert expected.dietary_preferences == observed_profile.dietary_preferences
    assert expected.family_members == observed_profile.family_members
    assert expected.people_count == observed_profile.people_count


@pytest.mark.parametrize("conflict", ["deleted", "changed_operation"])
def test_profile_amendment_transaction_conflicts_leave_documents_unchanged(
    repo: DynamoRepository,
    conflict: str,
) -> None:
    """Cancellation and changed operation cannot commit a profile write."""
    original = make_profile()
    updated = original.model_copy(update={"dietary_constraints": ["peanuts"]})
    observed = _profile_edit_state()
    repo.save_profile("user", original, expected_revision=None)
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
    replacement = _profile_edit_state(
        revision=observed.revision,
        created_at=observed.created_at + timedelta(seconds=1),
        operation=ProfileEditOperation.REMOVE,
    )
    repo.save_profile("user", original, expected_revision=None)
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


@pytest.mark.parametrize("replacement_kind", ["meal", "profile"])
def test_profile_amendment_transaction_rejects_replacement_workflows(
    repo: DynamoRepository,
    replacement_kind: str,
) -> None:
    """Commands replacing an edit cannot be overwritten by stale input."""
    original = make_profile()
    updated = original.model_copy(update={"goals": ["eat well", "save time"]})
    observed = _profile_edit_state()
    replacement_time = observed.created_at + timedelta(seconds=1)
    if replacement_kind == "meal":
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
    repo.save_profile("user", original, expected_revision=None)
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
    repo.save_profile("user", original, expected_revision=None)
    assert repo.save_conversation_state("user", observed)
    next_state = _profile_menu_state(observed)
    assert repo.save_profile_and_transition_state(
        "user", first_update, next_state, observed
    )

    assert not repo.save_profile_and_transition_state(
        "user", duplicate_update, next_state, observed
    )

    assert repo.get_profile("user") == first_update.model_copy(
        update={"profile_revision": 1}
    )
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
    repo.save_profile("user", profile, expected_revision=0)

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

    repo.save_profile("user", profile, expected_revision=None)

    item = repo.table.get_item(Key={"PK": "USER#user", "SK": "PROFILE"})["Item"]
    assert "revision" not in item
    assert item["dietary_constraints"] == ["peanuts"]
    assert "allergies" not in item
    assert "restrictions" not in item


def test_save_profile_replaces_existing_document_without_revision(
    repo: DynamoRepository,
) -> None:
    """A normal profile save advances the observed profile revision."""
    initial = UserProfile(name="Alex", dietary_constraints=["peanuts"])
    assert repo.save_profile("user", initial, expected_revision=None)

    saved_initial = repo.get_profile("user", consistent_read=True)
    assert saved_initial is not None
    assert saved_initial.profile_revision == 0
    updated = saved_initial.model_copy(
        update={"dietary_constraints": ["dairy-free"]}
    )
    assert repo.save_profile(
        "user", updated, expected_revision=saved_initial.profile_revision
    )

    saved = repo.get_profile("user")
    assert saved is not None
    assert saved.dietary_constraints == ["dairy-free"]
    assert saved.profile_revision == 1


def test_stale_ordinary_save_cannot_overwrite_confirmation_winner(
    repo: DynamoRepository,
) -> None:
    """An ordinary stale writer loses to a later profile transaction."""
    initial = UserProfile(name="Alex", dietary_constraints=["peanuts"])
    assert repo.save_profile("user", initial, expected_revision=None)
    stale = repo.get_profile("user", consistent_read=True)
    assert stale is not None

    observed = _profile_edit_state()
    next_state = _profile_menu_state(observed)
    assert repo.save_conversation_state("user", observed)
    stale_update = stale.model_copy(update={"dietary_constraints": ["dairy"]})
    winner = stale.model_copy(update={"dietary_constraints": ["shellfish"]})
    assert repo.save_profile_and_transition_state(
        "user", winner, next_state, observed
    )
    assert not repo.save_profile(
        "user", stale_update, expected_revision=stale.profile_revision
    )

    saved = repo.get_profile("user", consistent_read=True)
    assert saved is not None
    assert saved.dietary_constraints == ["shellfish"]
    assert saved.profile_revision == 1


def test_competing_ordinary_saves_allow_only_one_revision_owner(
    repo: DynamoRepository,
) -> None:
    """Two writers from one snapshot cannot both commit."""
    initial = UserProfile(name="Alex", dietary_constraints=["peanuts"])
    assert repo.save_profile("user", initial, expected_revision=None)
    snapshot = repo.get_profile("user", consistent_read=True)
    assert snapshot is not None

    first = snapshot.model_copy(update={"name": "First"})
    second = snapshot.model_copy(update={"name": "Second"})
    writes = Barrier(2)

    def save_competing(profile: UserProfile) -> bool:
        writes.wait(timeout=5)
        return repo.save_profile(
            "user", profile, expected_revision=snapshot.profile_revision
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(save_competing, [first, second]))

    assert sorted(outcomes) == [False, True]
    saved = repo.get_profile("user", consistent_read=True)
    assert saved is not None
    assert saved.name in {"First", "Second"}
    assert saved.profile_revision == 1


def test_new_profile_creation_is_race_safe(repo: DynamoRepository) -> None:
    """Only one writer can create a missing profile item."""
    first = UserProfile(name="First")
    second = UserProfile(name="Second")
    writes = Barrier(2)

    def save_competing(profile: UserProfile) -> bool:
        writes.wait(timeout=5)
        return repo.save_profile("user", profile, expected_revision=None)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(save_competing, [first, second]))

    assert sorted(outcomes) == [False, True]

    saved = repo.get_profile("user", consistent_read=True)
    assert saved is not None
    assert saved.name in {"First", "Second"}
    assert saved.profile_revision == 0


def test_observed_absence_cannot_overwrite_staggered_creator(
    repo: DynamoRepository,
) -> None:
    """A delayed creator loses after another observed-absence save wins."""
    first = UserProfile(name="First")
    second = UserProfile(name="Second")

    assert repo.get_profile("user", consistent_read=True) is None
    assert repo.get_profile("user", consistent_read=True) is None
    assert repo.save_profile("user", first, expected_revision=None)
    assert not repo.save_profile("user", second, expected_revision=None)

    saved = repo.get_profile("user", consistent_read=True)
    assert saved is not None
    assert saved.name == "First"
    assert saved.profile_revision == 0


def test_legacy_revision_zero_profile_can_be_updated(
    repo: DynamoRepository,
) -> None:
    """A legacy item without a revision is treated as revision zero."""
    profile = UserProfile(name="Alex")
    repo.table.put_item(
        Item={
            "PK": "USER#user",
            "SK": "PROFILE",
            **profile.model_dump(mode="json", exclude={"profile_revision"}),
        }
    )
    observed = repo.get_profile("user", consistent_read=True)
    assert observed is not None
    assert observed.profile_revision == 0

    updated = observed.model_copy(update={"name": "Updated"})
    assert repo.save_profile("user", updated, expected_revision=0)
    saved = repo.get_profile("user", consistent_read=True)
    assert saved is not None
    assert saved.name == "Updated"
    assert saved.profile_revision == 1


def test_save_profile_propagates_nonconditional_client_error(
    mocker: Any,
) -> None:
    """Unexpected DynamoDB failures remain visible to application callers."""
    table = mocker.MagicMock()
    table.put_item.side_effect = ClientError(
        {
            "Error": {
                "Code": "ProvisionedThroughputExceededException",
                "Message": "capacity exceeded",
            }
        },
        "PutItem",
    )
    repo = DynamoRepository(table)

    with pytest.raises(ClientError, match="capacity exceeded"):
        repo.save_profile(
            "user", UserProfile(name="Alex"), expected_revision=None
        )


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

    repo.save_profile("user", canonical, expected_revision=0)

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


def _legacy_conversation_item(workflow_kind: str) -> dict[str, Any]:
    """Return one representative incompatible conversation item."""
    item: dict[str, Any] = {
        "PK": "USER#user",
        "SK": "CONVERSATION_STATE",
        "workflow_kind": workflow_kind,
        "step": "awaiting_preference",
        "revision": 4,
        "created_at": "2026-08-31T10:00:00+00:00",
        "updated_at": "2026-08-31T10:01:00+00:00",
        "expires_at": 1_798_763_400,
        "request_id": "legacy-request",
        "private_text": "do not include this persisted content in logs",
    }
    if workflow_kind == "meal_log":
        item["meal"] = {"description": "private legacy meal"}
    elif workflow_kind == "profile_edit":
        item["profile_update"] = {"name": "private legacy profile"}
    return item


@pytest.mark.parametrize(
    "workflow_kind",
    ["plan_request", "plan_revision", "meal_log", "profile_edit"],
)
def test_incompatible_legacy_conversation_items_are_expired(
    repo: DynamoRepository, workflow_kind: str
) -> None:
    """Retired or old-shaped conversation items behave as absent state."""
    repo.table.put_item(Item=_legacy_conversation_item(workflow_kind))

    assert repo.get_conversation_state("user") is None
    assert (
        repo.table.get_item(
            Key={"PK": "USER#user", "SK": "CONVERSATION_STATE"}
        ).get("Item")
        is None
    )


def test_legacy_cleanup_does_not_delete_concurrent_replacement(
    repo: DynamoRepository, mocker: Any
) -> None:
    """Cleanup loses safely when a new workflow replaces the bad item."""
    legacy = _legacy_conversation_item("plan_request")
    repo.table.put_item(Item=legacy)
    original_delete = repo.table.delete_item
    replacement = ConversationState(
        workflow_kind=ConversationWorkflowKind.MEAL_LOG,
        step=ConversationWorkflowStep.AWAITING_DATE,
        meal_draft=MealLogDraft(),
        revision=5,
        created_at=datetime(2026, 8, 31, 11, tzinfo=timezone.utc),
        updated_at=datetime(2026, 8, 31, 11, tzinfo=timezone.utc),
        expires_at=1_798_763_400,
    )

    def replace_before_delete(**kwargs: Any) -> Any:
        repo.table.put_item(
            Item={
                "PK": "USER#user",
                "SK": "CONVERSATION_STATE",
                **replacement.model_dump(mode="json"),
            }
        )
        return original_delete(**kwargs)

    mocker.patch.object(
        repo.table, "delete_item", side_effect=replace_before_delete
    )

    assert repo.get_conversation_state("user") is None
    assert repo.get_conversation_state("user") == replacement


def test_legacy_cleanup_conditional_conflict_is_harmless(
    mocker: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A conditional cleanup race does not surface as an application error."""
    table = mocker.MagicMock()
    table.get_item.return_value = {
        "Item": _legacy_conversation_item("plan_revision")
    }
    conflict = ClientError(
        {
            "Error": {
                "Code": "ConditionalCheckFailedException",
                "Message": "condition failed",
            }
        },
        "DeleteItem",
    )
    table.delete_item.side_effect = conflict

    with caplog.at_level(logging.WARNING, logger="meal_planner.db.dynamo"):
        assert DynamoRepository(table).get_conversation_state("user") is None

    assert "private" not in caplog.text
    assert "legacy-request" not in caplog.text


def test_legacy_cleanup_propagates_nonconditional_failure(
    mocker: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Operational cleanup failures remain visible to callers."""
    table = mocker.MagicMock()
    table.get_item.return_value = {
        "Item": _legacy_conversation_item("profile_edit")
    }
    failure = ClientError(
        {
            "Error": {
                "Code": "ProvisionedThroughputExceededException",
                "Message": "capacity exceeded",
            }
        },
        "DeleteItem",
    )
    table.delete_item.side_effect = failure

    with caplog.at_level(logging.WARNING, logger="meal_planner.db.dynamo"):
        with pytest.raises(ClientError) as raised:
            DynamoRepository(table).get_conversation_state("user")

    assert raised.value is failure
    assert "private" not in caplog.text
    assert "legacy-request" not in caplog.text


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


def test_meal_history_reads_legacy_batch_link_items(
    repo: DynamoRepository,
) -> None:
    """Stored meals with retired batch metadata remain readable."""
    for index, batch_link in enumerate(
        (
            None,
            {
                "batch_id": "batch-2026-08-28",
                "role": "preparation",
                "portion": 1,
            },
        ),
        start=1,
    ):
        repo.table.put_item(
            Item={
                "PK": "USER#user",
                "SK": f"MEAL#2026-08-08#LEGACY#{index}",
                "date": "2026-08-08",
                "meal_type": "lunch",
                "description": f"legacy meal {index}",
                "created_at": f"2026-08-08T0{index}:00:00+00:00",
                "batch_link": batch_link,
            }
        )

    history = repo.get_meal_history("user", days=1, on_date=date(2026, 8, 8))

    assert [entry.description for entry in history] == [
        "legacy meal 2",
        "legacy meal 1",
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


def test_malformed_meal_history_warning_does_not_log_meal_content(
    mocker: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Malformed history is skipped with a bounded reason code only."""
    table = mocker.MagicMock()
    table.query.return_value = {
        "Items": [
            {
                "PK": "USER#user",
                "SK": "MEAL#2026-08-26#secret-raw-payload",
                "date": "2026-08-26",
                "meal_type": "lunch",
                "description": "secret meal foods source text secret-batch-id",
                "created_at": "not-a-timestamp",
            }
        ]
    }

    with caplog.at_level(logging.WARNING, logger="meal_planner.db.dynamo"):
        history = DynamoRepository(table).get_meal_history(
            "secret-user", days=1, on_date=date(2026, 8, 26)
        )

    assert history == []
    assert "secret" not in caplog.text
    assert caplog.records[0].message == (
        "Skipping malformed meal history item reason_code=malformed"
    )


def test_meal_history_uses_exact_inclusive_21_day_window_and_order(
    repo: DynamoRepository,
) -> None:
    """The plan-chat history includes exactly 21 calendar days."""
    for entry in (
        _meal(7, 8, "outside before window"),
        _meal(8, 8, "start boundary"),
        _meal(28, 12, "latest"),
    ):
        repo.log_meal("user", entry)

    history = repo.get_meal_history("user", days=21, on_date=date(2026, 8, 28))

    assert [entry.description for entry in history] == [
        "latest",
        "start boundary",
    ]


def test_empty_meal_history_returns_empty_list(repo: DynamoRepository) -> None:
    assert (
        repo.get_meal_history("user", days=21, on_date=date(2026, 8, 28)) == []
    )
