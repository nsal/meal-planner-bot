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
from meal_planner.db.dynamo import (
    DynamoRepository,
    RepairPublicationOutcome,
    TransactionConflictKind,
)
from meal_planner.models.schemas import (
    BatchLedgerEntry,
    BatchLedgerState,
    BatchMealRole,
    ConversationState,
    ConversationWorkflowKind,
    ConversationWorkflowStep,
    DietaryRule,
    GrocerySection,
    GroceryStatus,
    MealLogDraft,
    MealLogEntry,
    MealOutcome,
    MealType,
    PlannedBatchLink,
    PlanStatus,
    ProfileEditCategory,
    ProfileEditOperation,
    ProfileUpdateEntities,
    RuleOperator,
    SubmittedMealBatchLink,
    UserProfile,
    WeeklyBatchLedger,
    canonicalize_profile_rule_ids,
)
from meal_planner.router import RouteResult, RouteType
from tests.factories import (
    make_batch_rule,
    make_constraint,
    make_legacy_profile_item,
    make_plan,
    make_preference,
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
    draft = ProfileUpdateEntities(name="Alex", people_count=2)
    repo.save_profile_draft("user", draft)
    assert repo.get_profile_draft("user").name == "Alex"
    profile = make_profile()
    repo.save_profile("user", profile, expected_revision=None)
    assert repo.get_profile("user") == canonicalize_profile_rule_ids(profile)
    repo.delete_profile_draft("user")
    assert repo.get_profile_draft("user") is None


def test_canonical_profile_round_trip_discards_goals_on_read_and_write(
    repo: DynamoRepository,
) -> None:
    """Structured entries survive a round trip and goals never persist."""
    profile = UserProfile(
        name="Alex",
        dietary_constraints=[make_constraint()],
        dietary_preferences=[make_preference()],
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
    assert loaded == canonicalize_profile_rule_ids(profile)
    repo.save_profile("user", loaded, expected_revision=loaded.profile_revision)

    item = repo.table.get_item(Key={"PK": "USER#user", "SK": "PROFILE"})["Item"]
    expected = canonicalize_profile_rule_ids(profile)
    assert item["dietary_constraints"] == [
        expected.dietary_constraints[0].model_dump(mode="json")
    ]
    assert item["dietary_preferences"] == [
        expected.dietary_preferences[0].model_dump(mode="json")
    ]
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
    assert profile.dietary_preferences == []
    assert [entry.source_text for entry in profile.dietary_constraints] == [
        "Peanuts"
    ]


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
    assert caplog.records[-1].message == (
        "Loaded profile with discarded legacy preferences "
        "reason_code=legacy_preference count=3"
    )


def test_profile_transaction_rejects_conflicting_preference_atomically(
    repo: DynamoRepository,
) -> None:
    """A preference conflicting with a constraint cannot consume state."""
    original = UserProfile(
        name="Alex",
        dietary_constraints=[make_constraint()],
        dietary_preferences=[],
    )
    conflicting = original.model_copy(
        update={
            "dietary_preferences": [
                make_preference(
                    "peanuts for breakfast",
                    rule=DietaryRule(
                        id="preference-peanuts",
                        source_text="peanuts for breakfast",
                        foods_any_of=["peanuts"],
                        meal_type="breakfast",
                        operator=RuleOperator.AT_LEAST,
                        count=1,
                    ),
                )
            ]
        }
    )
    observed = _profile_edit_state(
        operation=ProfileEditOperation.ADD,
    ).model_copy(
        update={"profile_category": ProfileEditCategory.DIETARY_PREFERENCES}
    )
    repo.save_profile("user", original, expected_revision=None)
    assert repo.save_conversation_state("user", observed)

    assert not repo.save_profile_and_transition_state(
        "user", conflicting, _profile_menu_state(observed), observed
    )
    assert repo.get_profile("user") == canonicalize_profile_rule_ids(original)
    assert repo.get_conversation_state("user") == observed


def test_constraint_transaction_removes_only_conflicting_preferences(
    repo: DynamoRepository,
) -> None:
    """A new constraint atomically removes conflicting stored rules only."""
    original = UserProfile(
        name="Alex",
        dietary_constraints=[],
        dietary_preferences=[
            make_preference(
                "peanuts for breakfast",
                identifier="preference-peanuts",
                rule=DietaryRule(
                    id="preference-peanuts",
                    source_text="peanuts for breakfast",
                    foods_any_of=["peanuts"],
                    meal_type="breakfast",
                    count=1,
                ),
            ),
            make_preference(
                "eggs for breakfast",
                identifier="preference-eggs",
            ),
        ],
    )
    updated = original.model_copy(
        update={"dietary_constraints": [make_constraint()]}
    )
    observed = _profile_edit_state()
    repo.save_profile("user", original, expected_revision=None)
    assert repo.save_conversation_state("user", observed)

    assert repo.save_profile_and_transition_state(
        "user", updated, _profile_menu_state(observed), observed
    )
    saved = repo.get_profile("user")
    assert saved is not None
    expected = canonicalize_profile_rule_ids(
        original.model_copy(
            update={"dietary_preferences": [original.dietary_preferences[1]]}
        )
    )
    assert saved.dietary_preferences == expected.dietary_preferences
    assert (
        saved.dietary_constraints
        == canonicalize_profile_rule_ids(updated).dietary_constraints
    )


def test_profile_write_deduplicates_structured_entries_deterministically(
    repo: DynamoRepository,
) -> None:
    """The first canonical entry wins when source text repeats."""
    profile = UserProfile(
        name="Alex",
        dietary_constraints=[
            make_constraint("Peanuts", identifier="constraint-first"),
            make_constraint("peanuts", identifier="constraint-second"),
        ],
        dietary_preferences=[
            make_preference("Eggs", identifier="preference-first"),
            make_preference(" eggs ", identifier="preference-second"),
        ],
    )

    repo.save_profile("user", profile, expected_revision=None)
    saved = repo.get_profile("user")
    assert saved is not None
    expected = canonicalize_profile_rule_ids(
        profile.model_copy(
            update={
                "dietary_constraints": [profile.dietary_constraints[0]],
                "dietary_preferences": [profile.dietary_preferences[0]],
            }
        )
    )
    assert saved.dietary_constraints == expected.dietary_constraints
    assert saved.dietary_preferences == expected.dietary_preferences


def test_profile_canonicalization_repairs_duplicate_provider_rule_ids(
    repo: DynamoRepository,
) -> None:
    """Distinct profile rules do not retain a shared provider ID."""
    profile = UserProfile(
        name="Alex",
        dietary_preferences=[
            make_preference(
                "eggs for breakfast",
                identifier="r1",
                rule=DietaryRule(
                    id="r1",
                    source_text="eggs for breakfast",
                    foods_any_of=["eggs"],
                    meal_type="breakfast",
                    count=1,
                ),
            ),
            make_preference(
                "tofu for dinner",
                identifier="r1",
                rule=DietaryRule(
                    id="r1",
                    source_text="tofu for dinner",
                    foods_any_of=["tofu"],
                    meal_type="dinner",
                    count=1,
                ),
            ),
        ],
    )

    canonical = repo._canonical_profile(profile)
    reread = repo._canonical_profile(canonical)

    ids = [entry.rule.id for entry in canonical.dietary_preferences]
    assert len(ids) == len(set(ids))
    assert all(identifier != "r1" for identifier in ids)
    assert reread == canonical


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

    assert repo.get_profile("user") == canonicalize_profile_rule_ids(profile)
    assert table.get_item.call_args.kwargs == {
        "Key": {"PK": "USER#user", "SK": "PROFILE"}
    }

    assert repo.get_profile("user", consistent_read=True) == (
        canonicalize_profile_rule_ids(profile)
    )
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

    assert repo.get_profile("user") == canonicalize_profile_rule_ids(
        stale_profile
    )
    assert repo.get_profile("user", consistent_read=True) == (
        canonicalize_profile_rule_ids(current_profile)
    )


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
        update={"dietary_constraints": [make_constraint("peanuts")]}
    )

    concurrent_update = snapshot.model_copy(
        update={
            "dietary_constraints": [
                make_constraint("shellfish", identifier="constraint-shellfish")
            ],
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
        canonicalize_profile_rule_ids(
            concurrent_update.model_copy(update={"profile_revision": 1})
        )
    )
    assert repo.get_conversation_state("user", consistent_read=True) == observed


def test_profile_confirmation_rejects_missing_profile(
    repo: DynamoRepository,
) -> None:
    """A confirmation cannot create a profile that disappeared meanwhile."""
    original = make_profile()
    updated = original.model_copy(
        update={"dietary_constraints": [make_constraint("peanuts")]}
    )
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
    updated = original.model_copy(
        update={"dietary_constraints": [make_constraint()]}
    )
    observed = _profile_edit_state()
    next_state = _profile_menu_state(observed)
    repo.save_profile("user", original, expected_revision=None)
    assert repo.save_conversation_state("user", observed)

    assert repo.save_profile_and_transition_state(
        "user", updated, next_state, observed
    )

    assert repo.get_profile("user") == canonicalize_profile_rule_ids(
        updated.model_copy(update={"profile_revision": 1})
    )
    assert repo.get_conversation_state("user") == next_state


def test_numbered_profile_removal_transaction_retains_remove_mode(
    repo: DynamoRepository,
) -> None:
    """A numbered removal advances both guarded revisions atomically."""
    original = make_profile().model_copy(
        update={"dietary_constraints": [make_constraint()]}
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
    assert repo.get_profile("user", consistent_read=True) == (
        canonicalize_profile_rule_ids(original)
    )
    assert repo.get_conversation_state("user", consistent_read=True) == observed


def test_numbered_constraint_removal_preserves_unrelated_profile_data(
    repo: DynamoRepository,
) -> None:
    """Removing one constraint does not rewrite unrelated profile fields."""
    profile = make_profile(with_nutrient_targets=True).model_copy(
        update={
            "dietary_constraints": [
                make_constraint("peanuts", identifier="constraint-peanuts"),
                make_constraint("shellfish", identifier="constraint-shellfish"),
            ],
            "dietary_preferences": [
                make_preference(
                    "same preference",
                    identifier="preference-eggs",
                    rule=DietaryRule(
                        id="preference-eggs",
                        source_text="same preference",
                        foods_any_of=["eggs"],
                        meal_type="breakfast",
                        operator=RuleOperator.AT_LEAST,
                        count=1,
                    ),
                ),
                make_preference(
                    "same preference",
                    identifier="preference-oats",
                    rule=DietaryRule(
                        id="preference-oats",
                        source_text="same preference",
                        foods_any_of=["oats"],
                        meal_type="breakfast",
                        operator=RuleOperator.AT_LEAST,
                        count=1,
                    ),
                ),
                make_preference(
                    "shellfish for breakfast",
                    identifier="preference-shellfish",
                    rule=DietaryRule(
                        id="preference-shellfish",
                        source_text="shellfish for breakfast",
                        foods_any_of=["shellfish"],
                        meal_type="breakfast",
                        operator=RuleOperator.AT_LEAST,
                        count=1,
                    ),
                ),
            ],
            "batch_rules": [
                make_batch_rule("first batch", identifier="batch-first"),
                make_batch_rule("second batch", identifier="batch-second"),
            ],
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
    assert expected.batch_rules == observed_profile.batch_rules
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

    assert repo.get_profile("user") == canonicalize_profile_rule_ids(original)
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

    assert repo.get_profile("user") == canonicalize_profile_rule_ids(original)
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
    repo.save_profile("user", original, expected_revision=None)
    assert repo.save_conversation_state("user", observed)
    assert repo.save_conversation_state(
        "user", replacement, expected_revision=observed.revision
    )

    assert not repo.save_profile_and_transition_state(
        "user", updated, _profile_menu_state(observed), observed
    )

    assert repo.get_profile("user") == canonicalize_profile_rule_ids(original)
    assert repo.get_conversation_state("user") == replacement


def test_profile_amendment_transaction_duplicate_input_is_idempotently_rejected(
    repo: DynamoRepository,
) -> None:
    """A second submission using the consumed state changes nothing."""
    original = make_profile()
    first_update = original.model_copy(
        update={"dietary_constraints": [make_constraint()]}
    )
    duplicate_update = original.model_copy(
        update={
            "dietary_constraints": [
                make_constraint("shellfish", identifier="constraint-shellfish")
            ]
        }
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

    assert repo.get_profile("user") == canonicalize_profile_rule_ids(
        first_update.model_copy(update={"profile_revision": 1})
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
    assert [entry.source_text for entry in profile.dietary_constraints] == [
        "Peanuts",
        "Vegan",
    ]
    assert [entry["source_text"] for entry in item["dietary_constraints"]] == [
        "Peanuts",
        "Vegan",
    ]
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
    assert item["dietary_constraints"][0]["source_text"] == "peanuts"
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
    assert [entry.source_text for entry in saved.dietary_constraints] == [
        "dairy-free"
    ]
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
    stale_update = stale.model_copy(
        update={"dietary_constraints": [make_constraint("dairy")]}
    )
    winner = stale.model_copy(
        update={"dietary_constraints": [make_constraint("shellfish")]}
    )
    assert repo.save_profile_and_transition_state(
        "user", winner, next_state, observed
    )
    assert not repo.save_profile(
        "user", stale_update, expected_revision=stale.profile_revision
    )

    saved = repo.get_profile("user", consistent_read=True)
    assert saved is not None
    assert [entry.source_text for entry in saved.dietary_constraints] == [
        "shellfish"
    ]
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
    assert [entry["source_text"] for entry in item["dietary_constraints"]] == [
        "Peanuts",
        "vegan",
    ]
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


def _batch_submission_fixture(
    *,
    state: ConversationState,
    role: str,
    ledger_state: BatchLedgerState,
    remaining: int,
    ledger_revision: int = 0,
    portion: int = 2,
    total_portions: int = 2,
) -> tuple[ConversationState, MealLogEntry, WeeklyBatchLedger]:
    if role == "preparation":
        meal_date = date(2026, 8, 8)
        meal_type = MealType.DINNER
        batch_link = SubmittedMealBatchLink(
            batch_id="batch-1", role=role, portion=1
        )
    else:
        meal_date = date(2026, 8, 9)
        meal_type = MealType.DINNER
        batch_link = SubmittedMealBatchLink(
            batch_id="batch-1",
            role=role,
            portion=portion,
            source_date=date(2026, 8, 8),
            source_meal_type=MealType.DINNER,
        )
    entry = MealLogEntry(
        date=meal_date,
        meal_type=meal_type,
        description="roast chicken",
        created_at=datetime(2026, 8, 8, 12, tzinfo=timezone.utc),
        batch_link=batch_link,
    )
    continuation = state.model_copy(
        update={
            "step": ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION,
            "revision": state.revision + 1,
            "updated_at": state.updated_at + timedelta(seconds=1),
            "pending_batch_link": None,
        }
    )
    ledger = WeeklyBatchLedger(
        iso_week="2026-W32",
        revision=ledger_revision,
        entries=[
            BatchLedgerEntry(
                batch_id="batch-1",
                source_plan_id="plan-2026-08-03",
                source_request_id="request-1",
                source_revision=1,
                preparation_date=date(2026, 8, 8),
                preparation_meal_type=MealType.DINNER,
                food="chicken",
                meal_name="Roast chicken",
                total_portions=total_portions,
                remaining_portions=remaining,
                state=ledger_state,
                week_end=date(2026, 8, 9),
            )
        ],
    )
    return continuation, entry, ledger


def test_confirm_meal_activates_preparation_and_writes_marker_atomically(
    repo: DynamoRepository,
) -> None:
    review = _review_meal_state().model_copy(
        update={
            "meal_draft": MealLogDraft(
                date=date(2026, 8, 8),
                meal_type=MealType.DINNER,
                description="roast chicken",
            ),
            "pending_batch_link": PlannedBatchLink(
                batch_id="batch-1", role="preparation", total_yield=2
            ),
        }
    )
    continuation, entry, ledger = _batch_submission_fixture(
        state=review,
        role="preparation",
        ledger_state=BatchLedgerState.PROVISIONAL,
        remaining=1,
    )
    assert repo.save_conversation_state("user", review)
    repo.put_weekly_batch_ledger("user", ledger)

    assert repo.confirm_meal_and_transition(
        "user",
        entry,
        continuation,
        expected_revision=review.revision,
        submission_id=review.request_id,
    )

    saved = repo.get_weekly_batch_ledger("user", "2026-W32")
    assert saved.revision == 1
    assert saved.entries[0].state is BatchLedgerState.AVAILABLE
    assert saved.entries[0].remaining_portions == 1
    assert repo.table.get_item(
        Key={"PK": "USER#user", "SK": f"MEAL_UPDATE#{review.request_id}"}
    ).get("Item")


@pytest.mark.parametrize("materialize_expiry", [False, True])
def test_late_preparation_activation_is_rejected_before_or_after_expiry_read(
    repo: DynamoRepository, materialize_expiry: bool
) -> None:
    """A Friday source cannot activate during Saturday processing."""
    preparation_date = date(2026, 8, 7)
    processing_date = date(2026, 8, 8)
    review = _review_meal_state().model_copy(
        update={
            "meal_draft": MealLogDraft(
                date=preparation_date,
                meal_type=MealType.DINNER,
                description="roast chicken",
            ),
            "pending_batch_link": PlannedBatchLink(
                batch_id="batch-1", role="preparation", total_yield=2
            ),
        }
    )
    continuation, entry, ledger = _batch_submission_fixture(
        state=review,
        role="preparation",
        ledger_state=BatchLedgerState.PROVISIONAL,
        remaining=1,
    )
    entry = entry.model_copy(
        update={
            "date": preparation_date,
            "created_at": datetime(2026, 8, 7, 12, tzinfo=timezone.utc),
        }
    )
    ledger = ledger.model_copy(
        update={
            "iso_week": "2026-W32",
            "entries": [
                ledger.entries[0].model_copy(
                    update={
                        "preparation_date": preparation_date,
                        "week_end": date(2026, 8, 9),
                    }
                )
            ],
        }
    )
    assert repo.save_conversation_state("user", review)
    repo.put_weekly_batch_ledger("user", ledger)

    if materialize_expiry:
        expired = repo.get_weekly_batch_ledger(
            "user", "2026-W32", as_of=processing_date
        )
        assert expired.entries[0].state is BatchLedgerState.EXPIRED

    assert not repo.confirm_meal_and_transition(
        "user",
        entry,
        continuation,
        expected_revision=review.revision,
        submission_id=review.request_id,
        processing_date=processing_date,
    )
    assert repo.get_conversation_state("user") == review
    assert repo.get_meal_history("user", days=2, on_date=processing_date) == []
    saved = repo.get_weekly_batch_ledger("user", "2026-W32").entries[0]
    assert saved.state is (
        BatchLedgerState.EXPIRED
        if materialize_expiry
        else BatchLedgerState.PROVISIONAL
    )
    assert saved.remaining_portions == (0 if materialize_expiry else 1)


@pytest.mark.parametrize(
    ("preparation_date", "processing_date", "expected"),
    [
        (date(2026, 8, 7), date(2026, 8, 7), True),
        (date(2026, 8, 7), date(2026, 8, 8), False),
        (date(2026, 8, 9), date(2026, 8, 9), True),
        (date(2026, 8, 9), date(2026, 8, 10), False),
    ],
)
def test_preparation_activation_requires_processing_on_source_day_and_week(
    repo: DynamoRepository,
    preparation_date: date,
    processing_date: date,
    expected: bool,
) -> None:
    """Activation is bounded to the source preparation day and ISO week."""
    iso = preparation_date.isocalendar()
    iso_week = f"{iso.year:04d}-W{iso.week:02d}"
    week_end = date.fromisocalendar(iso.year, iso.week, 7)
    review = _review_meal_state().model_copy(
        update={
            "meal_draft": MealLogDraft(
                date=preparation_date,
                meal_type=MealType.DINNER,
                description="roast chicken",
            ),
            "pending_batch_link": PlannedBatchLink(
                batch_id="batch-1", role="preparation", total_yield=2
            ),
        }
    )
    continuation = _continuation_meal_state(review)
    entry = MealLogEntry(
        date=preparation_date,
        meal_type=MealType.DINNER,
        description="roast chicken",
        created_at=datetime.combine(
            preparation_date, datetime.min.time(), tzinfo=timezone.utc
        ),
        batch_link=SubmittedMealBatchLink(
            batch_id="batch-1", role=BatchMealRole.PREPARATION
        ),
    )
    ledger = WeeklyBatchLedger(
        iso_week=iso_week,
        entries=[
            BatchLedgerEntry(
                batch_id="batch-1",
                source_plan_id="plan-1",
                source_request_id="request-1",
                source_revision=1,
                preparation_date=preparation_date,
                preparation_meal_type=MealType.DINNER,
                food="chicken",
                total_portions=2,
                remaining_portions=1,
                state=BatchLedgerState.PROVISIONAL,
                week_end=week_end,
            )
        ],
    )
    assert repo.save_conversation_state("user", review)
    repo.put_weekly_batch_ledger("user", ledger)

    result = repo.confirm_meal_and_transition(
        "user",
        entry,
        continuation,
        expected_revision=review.revision,
        submission_id=review.request_id,
        processing_date=processing_date,
    )
    assert result is expected


def test_confirm_meal_consumes_one_leftover_portion_atomically(
    repo: DynamoRepository,
) -> None:
    review = _review_meal_state().model_copy(
        update={
            "meal_draft": MealLogDraft(
                date=date(2026, 8, 9),
                meal_type=MealType.DINNER,
                description="roast chicken",
            ),
            "pending_batch_link": PlannedBatchLink(
                batch_id="batch-1",
                role="leftover",
                source_date=date(2026, 8, 8),
                source_meal_type=MealType.DINNER,
                portion=2,
            ),
        }
    )
    continuation, entry, ledger = _batch_submission_fixture(
        state=review,
        role="leftover",
        ledger_state=BatchLedgerState.AVAILABLE,
        remaining=1,
    )
    assert repo.save_conversation_state("user", review)
    repo.put_weekly_batch_ledger("user", ledger)

    assert repo.confirm_meal_and_transition(
        "user",
        entry,
        continuation,
        expected_revision=review.revision,
        submission_id=review.request_id,
    )

    consumed = repo.get_weekly_batch_ledger("user", "2026-W32").entries[0]
    assert consumed.state is BatchLedgerState.EXHAUSTED
    assert consumed.remaining_portions == 0


@pytest.mark.parametrize(
    ("total", "remaining", "portion"),
    [(2, 1, 2), (3, 2, 2), (3, 1, 3)],
)
def test_confirm_meal_accepts_each_canonical_available_portion(
    repo: DynamoRepository, total: int, remaining: int, portion: int
) -> None:
    """Submission accepts only the next canonical available ordinal."""
    review = _review_meal_state().model_copy(
        update={
            "meal_draft": MealLogDraft(
                date=date(2026, 8, 9),
                meal_type=MealType.DINNER,
                description="roast chicken",
            ),
            "pending_batch_link": PlannedBatchLink(
                batch_id="batch-1",
                role="leftover",
                source_date=date(2026, 8, 8),
                source_meal_type=MealType.DINNER,
                portion=portion,
            ),
        }
    )
    continuation, entry, ledger = _batch_submission_fixture(
        state=review,
        role="leftover",
        ledger_state=BatchLedgerState.AVAILABLE,
        remaining=remaining,
        portion=portion,
        total_portions=total,
    )
    assert repo.save_conversation_state("user", review)
    repo.put_weekly_batch_ledger("user", ledger)

    assert repo.confirm_meal_and_transition(
        "user",
        entry,
        continuation,
        expected_revision=review.revision,
        submission_id=review.request_id,
    )

    saved = repo.get_weekly_batch_ledger("user", "2026-W32").entries[0]
    assert saved.remaining_portions == remaining - 1


def test_confirm_meal_consumes_three_portions_in_order_without_replay(
    repo: DynamoRepository,
) -> None:
    """Distinct submissions claim leftover ordinals in durable order."""
    batch = BatchLedgerEntry(
        batch_id="ordered-batch",
        source_plan_id="plan-2026-08-03",
        source_request_id="request-1",
        source_revision=1,
        preparation_date=date(2026, 8, 8),
        preparation_meal_type=MealType.DINNER,
        food="chicken",
        meal_name="Roast chicken",
        total_portions=3,
        remaining_portions=2,
        state=BatchLedgerState.AVAILABLE,
        week_end=date(2026, 8, 9),
    )
    repo.put_weekly_batch_ledger(
        "user",
        WeeklyBatchLedger(iso_week="2026-W32", revision=7, entries=[batch]),
    )

    def review(
        request_id: str, revision: int, portion: int
    ) -> ConversationState:
        state = _review_meal_state(request_id=request_id, revision=revision)
        return state.model_copy(
            update={
                "meal_draft": MealLogDraft(
                    date=date(2026, 8, 9),
                    meal_type=MealType.DINNER,
                    description=f"portion {portion}",
                )
            }
        )

    def submitted(
        review_state: ConversationState, portion: int
    ) -> MealLogEntry:
        return MealLogEntry(
            date=date(2026, 8, 9),
            meal_type=MealType.DINNER,
            description=f"portion {portion}",
            created_at=datetime(2026, 8, 9, 12, tzinfo=timezone.utc),
            batch_link=SubmittedMealBatchLink(
                batch_id="ordered-batch",
                role=BatchMealRole.LEFTOVER,
                source_date=date(2026, 8, 8),
                source_meal_type=MealType.DINNER,
                portion=portion,
            ),
        )

    def marker(request_id: str) -> dict[str, Any] | None:
        return repo.table.get_item(
            Key={"PK": "USER#user", "SK": f"MEAL_UPDATE#{request_id}"}
        ).get("Item")

    def assert_snapshot(
        expected_state: ConversationState,
        *,
        revision: int,
        remaining: int,
        meal_count: int,
        request_id: str,
        marker_present: bool,
    ) -> None:
        saved = repo.get_weekly_batch_ledger("user", "2026-W32")
        assert saved.revision == revision
        assert saved.entries[0].remaining_portions == remaining
        assert saved.entries[0].state is (
            BatchLedgerState.EXHAUSTED
            if remaining == 0
            else BatchLedgerState.AVAILABLE
        )
        assert (
            len(
                repo.get_submitted_meals(
                    "user",
                    start_date=date(2026, 8, 8),
                    end_date=date(2026, 8, 9),
                )
            )
            == meal_count
        )
        assert repo.get_conversation_state("user") == expected_state
        assert (marker(request_id) is not None) is marker_present

    early = review("123e4567-e89b-12d3-a456-426614174001", 0, 3)
    assert repo.save_conversation_state("user", early)
    assert not repo.confirm_meal_and_transition(
        "user",
        submitted(early, 3),
        _continuation_meal_state(early),
        expected_revision=early.revision,
        submission_id=early.request_id or "",
    )
    assert_snapshot(
        early,
        revision=7,
        remaining=2,
        meal_count=0,
        request_id=early.request_id or "",
        marker_present=False,
    )

    second = review("123e4567-e89b-12d3-a456-426614174002", 0, 2)
    assert repo.save_conversation_state(
        "user", second, expected_revision=early.revision
    )
    assert repo.confirm_meal_and_transition(
        "user",
        submitted(second, 2),
        _continuation_meal_state(second),
        expected_revision=second.revision,
        submission_id=second.request_id or "",
    )
    second_saved = _continuation_meal_state(second)
    assert_snapshot(
        second_saved,
        revision=8,
        remaining=1,
        meal_count=1,
        request_id=second.request_id or "",
        marker_present=True,
    )

    third = review("123e4567-e89b-12d3-a456-426614174003", 1, 3)
    assert repo.save_conversation_state(
        "user", third, expected_revision=second_saved.revision
    )
    assert repo.confirm_meal_and_transition(
        "user",
        submitted(third, 3),
        _continuation_meal_state(third),
        expected_revision=third.revision,
        submission_id=third.request_id or "",
    )
    third_saved = _continuation_meal_state(third)
    assert_snapshot(
        third_saved,
        revision=9,
        remaining=0,
        meal_count=2,
        request_id=third.request_id or "",
        marker_present=True,
    )

    replay = review("123e4567-e89b-12d3-a456-426614174004", 2, 3)
    assert repo.save_conversation_state(
        "user", replay, expected_revision=third_saved.revision
    )
    assert not repo.confirm_meal_and_transition(
        "user",
        submitted(replay, 3),
        _continuation_meal_state(replay),
        expected_revision=replay.revision,
        submission_id=replay.request_id or "",
    )
    assert_snapshot(
        replay,
        revision=9,
        remaining=0,
        meal_count=2,
        request_id=replay.request_id or "",
        marker_present=False,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_date", date(2026, 8, 7)),
        ("source_meal_type", MealType.LUNCH),
    ],
)
def test_leftover_rejects_wrong_source_metadata_without_mutation(
    repo: DynamoRepository, field: str, value: Any
) -> None:
    """Wrong source metadata cannot consume inventory or workflow state."""
    review, entry, ledger = _batch_submission_fixture(
        state=_review_meal_state(),
        role="leftover",
        ledger_state=BatchLedgerState.AVAILABLE,
        remaining=1,
    )
    assert repo.save_conversation_state("user", review)
    repo.put_weekly_batch_ledger("user", ledger)
    assert entry.batch_link is not None
    entry = entry.model_copy(
        update={
            "batch_link": entry.batch_link.model_copy(update={field: value})
        }
    )

    assert not repo.confirm_meal_and_transition(
        "user",
        entry,
        _continuation_meal_state(review),
        expected_revision=review.revision,
        submission_id=review.request_id or "",
    )
    assert repo.get_conversation_state("user") == review
    assert repo.get_meal_history("user", days=2, on_date=date(2026, 8, 9)) == []
    saved = repo.get_weekly_batch_ledger("user", "2026-W32")
    assert saved.revision == ledger.revision
    assert saved.entries == ledger.entries
    assert (
        repo.table.get_item(
            Key={"PK": "USER#user", "SK": f"MEAL_UPDATE#{review.request_id}"}
        ).get("Item")
        is None
    )


@pytest.mark.parametrize(
    ("total", "remaining", "portion"),
    [(3, 1, 2), (2, 1, 3)],
)
def test_confirm_meal_rejects_portions_outside_canonical_range(
    repo: DynamoRepository, total: int, remaining: int, portion: int
) -> None:
    """Submission rejects consumed and out-of-range portion ordinals."""
    review = _review_meal_state().model_copy(
        update={
            "meal_draft": MealLogDraft(
                date=date(2026, 8, 9),
                meal_type=MealType.DINNER,
                description="roast chicken",
            ),
            "pending_batch_link": PlannedBatchLink(
                batch_id="batch-1",
                role="leftover",
                source_date=date(2026, 8, 8),
                source_meal_type=MealType.DINNER,
                portion=portion,
            ),
        }
    )
    continuation, entry, ledger = _batch_submission_fixture(
        state=review,
        role="leftover",
        ledger_state=BatchLedgerState.AVAILABLE,
        remaining=remaining,
        portion=portion,
        total_portions=total,
    )
    assert repo.save_conversation_state("user", review)
    repo.put_weekly_batch_ledger("user", ledger)

    assert not repo.confirm_meal_and_transition(
        "user",
        entry,
        continuation,
        expected_revision=review.revision,
        submission_id=review.request_id,
    )

    assert repo.get_conversation_state("user") == review
    saved = repo.get_weekly_batch_ledger("user", "2026-W32").entries[0]
    assert saved.remaining_portions == remaining


@pytest.mark.parametrize(
    ("role", "ledger_state", "remaining", "expected_ledger_revision"),
    [
        ("preparation", BatchLedgerState.AVAILABLE, 1, None),
        ("leftover", BatchLedgerState.EXHAUSTED, 0, None),
        ("leftover", BatchLedgerState.PROVISIONAL, 1, None),
        ("preparation", BatchLedgerState.PROVISIONAL, 1, 1),
    ],
)
def test_batch_confirmation_rejects_insufficient_wrong_or_stale_inventory(
    repo: DynamoRepository,
    role: str,
    ledger_state: BatchLedgerState,
    remaining: int,
    expected_ledger_revision: int | None,
) -> None:
    review = _review_meal_state().model_copy(
        update={
            "meal_draft": MealLogDraft(
                date=date(2026, 8, 8 if role == "preparation" else 9),
                meal_type=MealType.DINNER,
                description="roast chicken",
            ),
            "pending_batch_link": PlannedBatchLink(
                batch_id="batch-1",
                role=role,
                **(
                    {}
                    if role == "preparation"
                    else {
                        "source_date": date(2026, 8, 8),
                        "source_meal_type": MealType.DINNER,
                        "portion": 2,
                    }
                ),
            ),
        }
    )
    continuation, entry, ledger = _batch_submission_fixture(
        state=review,
        role=role,
        ledger_state=ledger_state,
        remaining=remaining,
    )
    assert repo.save_conversation_state("user", review)
    repo.put_weekly_batch_ledger("user", ledger)

    assert not repo.confirm_meal_and_transition(
        "user",
        entry,
        continuation,
        expected_revision=review.revision,
        submission_id=review.request_id,
        expected_ledger_revision=expected_ledger_revision,
    )
    assert repo.get_conversation_state("user") == review
    assert repo.get_meal_history("user", days=2, on_date=date(2026, 8, 9)) == []
    assert (
        repo.get_weekly_batch_ledger("user", "2026-W32").entries[0].state
        is ledger_state
    )


def test_duplicate_batch_confirmation_does_not_consume_another_portion(
    repo: DynamoRepository,
) -> None:
    review = _review_meal_state().model_copy(
        update={
            "meal_draft": MealLogDraft(
                date=date(2026, 8, 8),
                meal_type=MealType.DINNER,
                description="roast chicken",
            ),
            "pending_batch_link": PlannedBatchLink(
                batch_id="batch-1", role="preparation"
            ),
        }
    )
    continuation, entry, ledger = _batch_submission_fixture(
        state=review,
        role="preparation",
        ledger_state=BatchLedgerState.PROVISIONAL,
        remaining=1,
    )
    assert repo.save_conversation_state("user", review)
    repo.put_weekly_batch_ledger("user", ledger)
    assert repo.confirm_meal_and_transition(
        "user",
        entry,
        continuation,
        expected_revision=review.revision,
        submission_id=review.request_id,
    )

    assert not repo.confirm_meal_and_transition(
        "user",
        entry,
        continuation,
        expected_revision=review.revision,
        submission_id=review.request_id,
    )
    assert (
        repo.get_weekly_batch_ledger("user", "2026-W32")
        .entries[0]
        .remaining_portions
        == 1
    )


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


def test_bounded_week_range_query_returns_only_submitted_meals_and_is_inclusive(
    repo: DynamoRepository,
) -> None:
    """The scheduler query cannot accidentally read plans or other weeks."""
    before = _meal(2, 8, "before")
    inside = _meal(3, 8, "inside")
    after = _meal(10, 8, "after")
    repo.log_meal("user", before)
    repo.log_meal("user", inside)
    repo.log_meal("user", after)
    repo.save_plan("user", make_plan(week_start=date(2026, 8, 3)))

    history = repo.get_submitted_meals(
        "user", start_date=date(2026, 8, 3), end_date=date(2026, 8, 9)
    )

    assert [entry.description for entry in history] == ["inside"]


def test_bounded_week_range_query_rejects_unbounded_ranges(
    repo: DynamoRepository,
) -> None:
    """Range reads stay bounded to the two ISO weeks a seven-day plan needs."""
    with pytest.raises(ValueError, match="at most 14 days"):
        repo.get_submitted_meals(
            "user",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 20),
        )


def test_weekly_batch_ledger_empty_read_is_exact_and_bounded(
    mocker: Any,
) -> None:
    """An absent week returns an empty ledger without a partition scan."""
    table = mocker.MagicMock()
    table.get_item.return_value = {}
    repo = DynamoRepository(table)
    ledger = repo.get_weekly_batch_ledger("user", "2026-W34")

    assert ledger == WeeklyBatchLedger(iso_week="2026-W34")
    table.get_item.assert_called_once_with(
        Key={"PK": "USER#user", "SK": "BATCH_LEDGER#2026-W34"}
    )
    table.scan.assert_not_called()
    table.query.assert_not_called()


def test_weekly_batch_ledger_round_trip_is_bounded_and_selects_available(
    repo: DynamoRepository,
) -> None:
    """Weekly entries round-trip and only eligible available portions select."""
    available = BatchLedgerEntry(
        batch_id="available-batch",
        source_plan_id="plan-1",
        source_request_id="request-1",
        source_revision=1,
        preparation_date=date(2026, 8, 19),
        preparation_meal_type=MealType.DINNER,
        food="chicken",
        meal_name="Roast chicken",
        total_portions=3,
        remaining_portions=2,
        state=BatchLedgerState.AVAILABLE,
        week_end=date(2026, 8, 23),
    )
    not_ready = available.model_copy(
        update={
            "batch_id": "provisional-batch",
            "state": BatchLedgerState.PROVISIONAL,
        }
    )
    repo.put_weekly_batch_ledger(
        "user",
        WeeklyBatchLedger(iso_week="2026-W34", entries=[available, not_ready]),
    )

    loaded = repo.get_weekly_batch_ledger("user", "2026-W34")
    selected = repo.get_available_batch_portions(
        "user", date(2026, 8, 21), meal_type=MealType.LUNCH
    )

    assert loaded.entries == [available, not_ready]
    assert [entry.batch_id for entry in selected] == ["available-batch"]


def test_weekly_batch_ledger_expires_provisional_sources_and_week_remainders(
    repo: DynamoRepository,
) -> None:
    """Preparation-day and Sunday expiry do not leave usable portions."""
    provisional = BatchLedgerEntry(
        batch_id="unsubmitted",
        source_plan_id="plan-1",
        source_request_id="request-1",
        source_revision=1,
        preparation_date=date(2026, 8, 21),
        preparation_meal_type=MealType.DINNER,
        food="beans",
        total_portions=2,
        remaining_portions=1,
        state=BatchLedgerState.PROVISIONAL,
        week_end=date(2026, 8, 23),
    )
    available = provisional.model_copy(
        update={
            "batch_id": "submitted-source",
            "preparation_date": date(2026, 8, 20),
            "state": BatchLedgerState.AVAILABLE,
        }
    )
    repo.put_weekly_batch_ledger(
        "user",
        WeeklyBatchLedger(
            iso_week="2026-W34", entries=[provisional, available]
        ),
    )

    before_week_end = repo.get_weekly_batch_ledger(
        "user", "2026-W34", as_of=date(2026, 8, 22)
    )
    after_week_end = repo.get_weekly_batch_ledger(
        "user", "2026-W34", as_of=date(2026, 8, 24)
    )

    assert before_week_end.entries[0].state is BatchLedgerState.EXPIRED
    assert before_week_end.entries[0].remaining_portions == 0
    assert after_week_end.entries[0].state is BatchLedgerState.EXPIRED
    assert after_week_end.entries[0].remaining_portions == 0
    assert after_week_end.entries[1].state is BatchLedgerState.EXPIRED
    assert after_week_end.entries[1].remaining_portions == 0


def test_expiry_materialization_advances_the_ledger_revision(
    repo: DynamoRepository,
) -> None:
    """Materialized expiry must never reset a nonzero ledger revision."""
    entry = _batch_entry_for_test(
        "expiring",
        date(2026, 8, 3),
        0,
        state=BatchLedgerState.AVAILABLE,
    )
    original = WeeklyBatchLedger(
        iso_week="2026-W32", revision=7, entries=[entry]
    )
    repo.put_weekly_batch_ledger("user", original)

    materialized = repo.get_weekly_batch_ledger(
        "user", "2026-W32", as_of=date(2026, 8, 10)
    )

    assert materialized.revision == 8
    assert repo.get_weekly_batch_ledger("user", "2026-W32").revision == 8


def test_concurrent_expiry_loser_reloads_the_winning_ledger(
    repo: DynamoRepository, mocker: Any
) -> None:
    """A conditional expiry loser returns the winner's materialized state."""
    entry = _batch_entry_for_test(
        "expiring",
        date(2026, 8, 3),
        0,
        state=BatchLedgerState.AVAILABLE,
    )
    repo.put_weekly_batch_ledger(
        "user",
        WeeklyBatchLedger(iso_week="2026-W32", revision=4, entries=[entry]),
    )
    barrier = Barrier(2)
    original_put = repo._put_weekly_batch_ledger_conditionally

    def synchronized_put(*args: Any, **kwargs: Any) -> None:
        barrier.wait()
        original_put(*args, **kwargs)

    mocker.patch.object(
        repo,
        "_put_weekly_batch_ledger_conditionally",
        side_effect=synchronized_put,
    )

    def materialize() -> WeeklyBatchLedger:
        return repo.get_weekly_batch_ledger(
            "user", "2026-W32", as_of=date(2026, 8, 10)
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: materialize(), range(2)))

    assert results[0] == results[1]
    assert results[0].revision == 5
    assert results[0].entries[0].state is BatchLedgerState.EXPIRED
    saved = repo.get_weekly_batch_ledger("user", "2026-W32")
    assert saved == results[0]


def test_expiry_retry_preserves_unrelated_winner_and_original_as_of(
    repo: DynamoRepository, mocker: Any
) -> None:
    """A losing expiry CAS reapplies the same date to the new revision."""
    expiring = _batch_entry_for_test(
        "expiring", date(2026, 8, 3), 0, state=BatchLedgerState.AVAILABLE
    )
    unrelated = _batch_entry_for_test(
        "unrelated", date(2026, 8, 3), 0, state=BatchLedgerState.AVAILABLE
    )
    original = WeeklyBatchLedger(
        iso_week="2026-W32", revision=10, entries=[expiring, unrelated]
    )
    competing = original.model_copy(
        update={
            "revision": 11,
            "entries": [
                expiring,
                unrelated.model_copy(
                    update={
                        "remaining_portions": 0,
                        "state": BatchLedgerState.EXHAUSTED,
                    }
                ),
            ],
        }
    )
    repo.put_weekly_batch_ledger("user", original)
    original_put = repo._put_weekly_batch_ledger_conditionally
    calls = 0

    def race_then_put(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            repo.put_weekly_batch_ledger("user", competing)
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}},
                "PutItem",
            )
        original_put(*args, **kwargs)

    mocker.patch.object(
        repo,
        "_put_weekly_batch_ledger_conditionally",
        side_effect=race_then_put,
    )

    result = repo.get_weekly_batch_ledger(
        "user", "2026-W32", as_of=date(2026, 8, 10)
    )

    assert result.revision == 12
    assert result.entries[0].state is BatchLedgerState.EXPIRED
    assert result.entries[0].remaining_portions == 0
    assert result.entries[1] == competing.entries[1]
    assert calls == 2
    assert repo.get_weekly_batch_ledger("user", "2026-W32") == result


def test_expiry_loser_strongly_reloads_expiry_winner_without_rewrite(
    repo: DynamoRepository, mocker: Any
) -> None:
    """An already-expired winner is returned without another revision."""
    entry = _batch_entry_for_test(
        "expiring", date(2026, 8, 3), 0, state=BatchLedgerState.AVAILABLE
    )
    original = WeeklyBatchLedger(
        iso_week="2026-W32", revision=4, entries=[entry]
    )
    winner = original.model_copy(
        update={
            "revision": 5,
            "entries": [
                entry.model_copy(
                    update={
                        "state": BatchLedgerState.EXPIRED,
                        "remaining_portions": 0,
                    }
                )
            ],
        }
    )
    repo.put_weekly_batch_ledger("user", original)

    def publish_winner_then_fail(*args: Any, **kwargs: Any) -> None:
        repo.put_weekly_batch_ledger("user", winner)
        raise ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}},
            "PutItem",
        )

    put = mocker.patch.object(
        repo,
        "_put_weekly_batch_ledger_conditionally",
        side_effect=publish_winner_then_fail,
    )
    get_item = mocker.spy(repo.table, "get_item")

    result = repo.get_weekly_batch_ledger(
        "user", "2026-W32", as_of=date(2026, 8, 10)
    )

    assert result == winner
    put.assert_called_once()
    assert get_item.call_args_list[1].kwargs == {
        "Key": repo._batch_ledger_key("user", "2026-W32"),
        "ConsistentRead": True,
    }


def test_final_expiry_conflict_returns_strongly_reloaded_winner(
    mocker: Any,
) -> None:
    """The final expiry conflict evaluates its already-expired winner."""
    table = mocker.MagicMock()
    repo = DynamoRepository(table)
    expiring = _batch_entry_for_test(
        "expiring", date(2026, 8, 3), 0, state=BatchLedgerState.AVAILABLE
    )
    unrelated = _batch_entry_for_test(
        "unrelated", date(2026, 8, 3), 0, state=BatchLedgerState.AVAILABLE
    )
    original = WeeklyBatchLedger(
        iso_week="2026-W32", revision=10, entries=[expiring, unrelated]
    )
    winner = original.model_copy(
        update={
            "revision": 42,
            "entries": [
                expiring.model_copy(
                    update={
                        "state": BatchLedgerState.EXPIRED,
                        "remaining_portions": 0,
                    }
                ),
                unrelated.model_copy(
                    update={
                        "state": BatchLedgerState.EXHAUSTED,
                        "remaining_portions": 0,
                    }
                ),
            ],
        }
    )

    def raw(ledger: WeeklyBatchLedger) -> dict[str, Any]:
        return {
            "Item": {
                **repo._batch_ledger_key("user", "2026-W32"),
                **ledger.model_dump(mode="json"),
            }
        }

    table.get_item.side_effect = [
        raw(original),
        raw(original),
        raw(original),
        raw(winner),
    ]
    conflict = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}},
        "PutItem",
    )
    put = mocker.patch.object(
        repo,
        "_put_weekly_batch_ledger_conditionally",
        side_effect=conflict,
    )

    result = repo.get_weekly_batch_ledger(
        "user", "2026-W32", as_of=date(2026, 8, 10)
    )

    assert result == winner
    assert put.call_count == 3
    assert table.get_item.call_count == 4
    assert all(
        call.kwargs
        == {
            "Key": repo._batch_ledger_key("user", "2026-W32"),
            "ConsistentRead": True,
        }
        for call in table.get_item.call_args_list[1:]
    )


def test_expiry_retry_keeps_original_as_of_after_stale_reload(
    mocker: Any,
) -> None:
    """Stale post-conflict data cannot be returned as a false success."""
    table = mocker.MagicMock()
    repo = DynamoRepository(table)
    entry = _batch_entry_for_test(
        "expiring", date(2026, 8, 3), 0, state=BatchLedgerState.AVAILABLE
    )
    original = WeeklyBatchLedger(
        iso_week="2026-W32", revision=7, entries=[entry]
    )
    current = original.model_copy(update={"revision": 8})
    responses = [original, original, current]
    table.get_item.side_effect = [
        {
            "Item": {
                **repo._batch_ledger_key("user", "2026-W32"),
                **ledger.model_dump(mode="json"),
            }
        }
        for ledger in responses
    ]
    conflict = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}},
        "PutItem",
    )
    put = mocker.patch.object(
        repo,
        "_put_weekly_batch_ledger_conditionally",
        side_effect=[conflict, conflict, None],
    )

    result = repo.get_weekly_batch_ledger(
        "user", "2026-W32", as_of=date(2026, 8, 10)
    )

    assert result.revision == 9
    assert result.entries[0].state is BatchLedgerState.EXPIRED
    assert put.call_count == 3
    assert all(
        call.kwargs.get("ConsistentRead") is True
        for call in repo.table.get_item.call_args_list[1:]
    )


def test_expiry_conflict_exhaustion_raises_last_retryable_conflict(
    mocker: Any,
) -> None:
    """Repeated expiry conflicts never return known-unexpired state."""
    table = mocker.MagicMock()
    repo = DynamoRepository(table)
    entry = _batch_entry_for_test(
        "expiring", date(2026, 8, 3), 0, state=BatchLedgerState.AVAILABLE
    )
    ledger = WeeklyBatchLedger(iso_week="2026-W32", entries=[entry])
    raw = {
        "Item": {
            **repo._batch_ledger_key("user", "2026-W32"),
            **ledger.model_dump(mode="json"),
        }
    }
    table.get_item.return_value = raw
    conflict = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}},
        "PutItem",
    )
    put = mocker.patch.object(
        repo,
        "_put_weekly_batch_ledger_conditionally",
        side_effect=conflict,
    )

    with pytest.raises(ClientError) as raised:
        repo.get_weekly_batch_ledger(
            "user", "2026-W32", as_of=date(2026, 8, 10)
        )

    assert raised.value is conflict
    assert put.call_count == 3
    assert table.get_item.call_count == 4
    assert all(
        call.kwargs.get("ConsistentRead") is True
        for call in table.get_item.call_args_list[1:]
    )


def test_expiry_malformed_strong_reload_is_not_reported_as_success(
    mocker: Any,
) -> None:
    """Malformed reloads fail validation instead of returning stale data."""
    table = mocker.MagicMock()
    repo = DynamoRepository(table)
    entry = _batch_entry_for_test(
        "expiring", date(2026, 8, 3), 0, state=BatchLedgerState.AVAILABLE
    )
    ledger = WeeklyBatchLedger(iso_week="2026-W32", entries=[entry])
    table.get_item.side_effect = [
        {
            "Item": {
                **repo._batch_ledger_key("user", "2026-W32"),
                **ledger.model_dump(mode="json"),
            }
        },
        {
            "Item": {
                **repo._batch_ledger_key("user", "2026-W32"),
                "iso_week": "not-an-iso-week",
            }
        },
    ]
    mocker.patch.object(
        repo,
        "_put_weekly_batch_ledger_conditionally",
        side_effect=ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}},
            "PutItem",
        ),
    )

    with pytest.raises(ValidationError):
        repo.get_weekly_batch_ledger(
            "user", "2026-W32", as_of=date(2026, 8, 10)
        )


def test_weekly_batch_ledger_cas_requires_revision_and_exact_entries(
    repo: DynamoRepository,
) -> None:
    """Stale revisions or changed entries cannot overwrite a ledger."""
    entry = _batch_entry_for_test(
        "expiring",
        date(2026, 8, 3),
        0,
        state=BatchLedgerState.AVAILABLE,
    )
    original = WeeklyBatchLedger(
        iso_week="2026-W32", revision=6, entries=[entry]
    )
    repo.put_weekly_batch_ledger("user", original)
    expired = original.model_copy(
        update={
            "revision": 7,
            "entries": [
                entry.model_copy(
                    update={
                        "state": BatchLedgerState.EXPIRED,
                        "remaining_portions": 0,
                    }
                )
            ],
        }
    )

    assert not repo.save_weekly_batch_ledger(
        "user",
        expired,
        expected_revision=5,
        expected_entries=original.entries,
    )
    assert not repo.save_weekly_batch_ledger(
        "user",
        expired,
        expected_revision=6,
        expected_entries=[
            entry.model_copy(
                update={
                    "state": BatchLedgerState.EXPIRED,
                    "remaining_portions": 0,
                }
            )
        ],
    )
    assert repo.get_weekly_batch_ledger("user", "2026-W32") == original


def test_repeated_current_expiry_reads_are_no_ops(
    repo: DynamoRepository, mocker: Any
) -> None:
    """Reads without a new expiry transition do not write or increment."""
    entry = _batch_entry_for_test(
        "expiring",
        date(2026, 8, 3),
        0,
        state=BatchLedgerState.AVAILABLE,
    )
    repo.put_weekly_batch_ledger(
        "user",
        WeeklyBatchLedger(iso_week="2026-W32", revision=3, entries=[entry]),
    )
    put = mocker.spy(repo, "_put_weekly_batch_ledger_conditionally")

    before_expiry = repo.get_weekly_batch_ledger(
        "user", "2026-W32", as_of=date(2026, 8, 8)
    )
    assert before_expiry.revision == 3
    assert put.call_count == 0

    first_expired = repo.get_weekly_batch_ledger(
        "user", "2026-W32", as_of=date(2026, 8, 10)
    )
    repeated_expired = repo.get_weekly_batch_ledger(
        "user", "2026-W32", as_of=date(2026, 8, 10)
    )
    assert first_expired.revision == 4
    assert repeated_expired == first_expired
    assert put.call_count == 1


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


def test_replacement_cleanup_preserves_other_provisional_owner(
    repo: DynamoRepository,
) -> None:
    """Replacing one draft cannot remove another draft's reservation."""
    week = date(2026, 8, 17)
    replaced = _batch_entry_for_test(
        "replaced", week, 0, request_id="replaced-request"
    )
    other = _batch_entry_for_test("other", week, 0, request_id="other-request")
    repo.put_weekly_batch_ledger(
        "user",
        WeeklyBatchLedger(iso_week="2026-W34", entries=[replaced, other]),
    )
    repo.save_plan("user", make_plan(week_start=week, revision=0))
    now = datetime.now(timezone.utc)
    state = ConversationState(
        workflow_kind=ConversationWorkflowKind.PLAN_REVISION,
        step=ConversationWorkflowStep.GENERATING,
        amendment="change",
        target_week=week,
        expected_plan_revision=0,
        request_id="replacement-request",
        revision=2,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
    )
    assert repo.save_conversation_state("user", state)

    replacement = _batch_entry_for_test(
        "new", week, 1, request_id="replacement-request"
    )
    assert repo.replace_draft_and_clear_revision_state(
        "user",
        make_plan(week_start=week, revision=1),
        expected_plan_revision=0,
        request_id=state.request_id,
        expected_state_revision=state.revision,
        batch_entries=[replacement],
        replaced_request_id="replaced-request",
    )

    entries = repo.get_weekly_batch_ledger("user", "2026-W34").entries
    assert [entry.batch_id for entry in entries] == ["new", "other"]


def test_simultaneous_fresh_plan_state_starts_have_one_winner(
    repo: DynamoRepository,
) -> None:
    """Two fresh workflows cannot both acquire the conversation slot."""
    barrier = Barrier(2)
    states = [
        ConversationState(
            workflow_kind=ConversationWorkflowKind.PLAN_REQUEST,
            step=ConversationWorkflowStep.AWAITING_PREFERENCE,
            request_id=f"123e4567-e89b-12d3-a456-42661417400{index}",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )
        for index in range(2)
    ]

    def start(state: ConversationState) -> bool:
        barrier.wait()
        return repo.save_conversation_state("user", state)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(start, states))

    assert sorted(results) == [False, True]


def test_two_consumers_cannot_decrement_the_last_batch_portion(
    repo: DynamoRepository,
) -> None:
    """Exact ledger CAS permits one simultaneous last-portion consumer."""
    entry = _batch_entry_for_test(
        "last-portion", date(2026, 8, 3), 0, state=BatchLedgerState.AVAILABLE
    )
    repo.put_weekly_batch_ledger(
        "user", WeeklyBatchLedger(iso_week="2026-W32", entries=[entry])
    )
    submitted = MealLogEntry(
        date=date(2026, 8, 9),
        meal_type=MealType.DINNER,
        description="chicken",
        created_at=datetime(2026, 8, 9, 12, tzinfo=timezone.utc),
        batch_link=SubmittedMealBatchLink(
            batch_id=entry.batch_id,
            role=BatchMealRole.LEFTOVER,
            source_date=entry.preparation_date,
            source_meal_type=entry.preparation_meal_type,
            portion=2,
        ),
    )
    puts = [
        repo._batch_submission_ledger_item(
            "user", submitted, expected_ledger_revision=None
        )
        for _ in range(2)
    ]
    assert all(put is not None for put in puts)
    barrier = Barrier(2)

    def consume(put: dict[str, Any]) -> bool:
        barrier.wait()
        try:
            repo.table.meta.client.transact_write_items(TransactItems=[put])
        except ClientError as exc:
            assert (
                DynamoRepository._classify_transaction_conflict(
                    exc, operation="ledger_mutation"
                )
                is TransactionConflictKind.INVENTORY_CHANGED
            )
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(consume, puts))  # type: ignore[arg-type]

    assert sorted(results) == [False, True]
    saved = repo.get_weekly_batch_ledger("user", "2026-W32").entries[0]
    assert saved.remaining_portions == 0
    assert saved.state is BatchLedgerState.EXHAUSTED


def test_concurrent_leftover_candidates_require_next_ordinal(
    repo: DynamoRepository,
) -> None:
    """Only the next ordinal is eligible, even when candidates race."""
    batch = BatchLedgerEntry(
        batch_id="racing-batch",
        source_plan_id="plan-2026-08-03",
        source_request_id="request-1",
        source_revision=1,
        preparation_date=date(2026, 8, 8),
        preparation_meal_type=MealType.DINNER,
        food="chicken",
        meal_name="Chicken batch",
        total_portions=3,
        remaining_portions=2,
        state=BatchLedgerState.AVAILABLE,
        week_end=date(2026, 8, 9),
    )
    repo.put_weekly_batch_ledger(
        "user", WeeklyBatchLedger(iso_week="2026-W32", entries=[batch])
    )

    def submitted(portion: int, request_id: str) -> MealLogEntry:
        return MealLogEntry(
            date=date(2026, 8, 9),
            meal_type=MealType.DINNER,
            description=f"portion {portion}",
            created_at=datetime(2026, 8, 9, 12, tzinfo=timezone.utc),
            batch_link=SubmittedMealBatchLink(
                batch_id="racing-batch",
                role=BatchMealRole.LEFTOVER,
                source_date=date(2026, 8, 8),
                source_meal_type=MealType.DINNER,
                portion=portion,
            ),
        ).model_copy(update={"created_at": datetime.fromisoformat(request_id)})

    barrier = Barrier(2)
    candidates = [
        submitted(2, "2026-08-09T12:00:01+00:00"),
        submitted(3, "2026-08-09T12:00:02+00:00"),
    ]

    def build_candidate(entry: MealLogEntry) -> dict[str, Any] | None:
        barrier.wait()
        return repo._batch_submission_ledger_item(
            "user", entry, expected_ledger_revision=None
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        puts = list(executor.map(build_candidate, candidates))

    assert puts[0] is not None
    assert puts[1] is None
    repo.table.meta.client.transact_write_items(TransactItems=[puts[0]])
    after_portion_two = repo.get_weekly_batch_ledger("user", "2026-W32")
    assert after_portion_two.revision == 1
    assert after_portion_two.entries[0].remaining_portions == 1

    portion_three = submitted(3, "2026-08-09T12:00:03+00:00")
    refreshed_put = repo._batch_submission_ledger_item(
        "user", portion_three, expected_ledger_revision=1
    )
    assert refreshed_put is not None
    replay_entry = portion_three.model_copy(
        update={
            "created_at": datetime(2026, 8, 9, 12, 0, 4, tzinfo=timezone.utc)
        }
    )
    replay_put = repo._batch_submission_ledger_item(
        "user", replay_entry, expected_ledger_revision=1
    )
    assert replay_put is not None

    def atomic_submission(
        ledger_put: dict[str, Any],
        entry: MealLogEntry,
        submission_id: str,
    ) -> list[dict[str, Any]]:
        """Add distinct workflow records to the shared ledger CAS race."""
        return [
            {
                "Put": {
                    "TableName": repo.table.name,
                    "Item": {
                        "PK": "USER#user",
                        "SK": (
                            f"MEAL#{entry.date_key}#SUBMISSION#{submission_id}"
                        ),
                        **entry.model_dump(mode="json"),
                    },
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            },
            {
                "Put": {
                    "TableName": repo.table.name,
                    "Item": {
                        "PK": "USER#user",
                        "SK": f"MEAL_UPDATE#{submission_id}",
                    },
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            },
            ledger_put,
        ]

    submissions = [
        atomic_submission(
            refreshed_put,
            portion_three,
            "123e4567-e89b-12d3-a456-426614174005",
        ),
        atomic_submission(
            replay_put,
            replay_entry,
            "123e4567-e89b-12d3-a456-426614174006",
        ),
    ]
    barrier = Barrier(2)

    def consume(items: list[dict[str, Any]]) -> bool:
        barrier.wait()
        try:
            repo.table.meta.client.transact_write_items(TransactItems=items)
        except ClientError as exc:
            assert (
                DynamoRepository._classify_transaction_conflict(
                    exc, operation="ledger_mutation"
                )
                is TransactionConflictKind.INVENTORY_CHANGED
            )
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(consume, submissions))

    assert sorted(results) == [False, True]
    saved = repo.get_weekly_batch_ledger("user", "2026-W32")
    assert saved.revision == 2
    assert saved.entries[0].remaining_portions == 0
    assert saved.entries[0].state is BatchLedgerState.EXHAUSTED


def test_draft_replacement_and_batch_activation_have_one_ledger_winner(
    repo: DynamoRepository,
) -> None:
    """Replacement cannot overwrite an activation based on stale inventory."""
    week = date(2026, 8, 3)
    old = _batch_entry_for_test("old", week, 0)
    repo.put_weekly_batch_ledger(
        "user", WeeklyBatchLedger(iso_week="2026-W32", entries=[old])
    )
    preparation = MealLogEntry(
        date=week,
        meal_type=MealType.DINNER,
        description="chicken",
        created_at=datetime(2026, 8, 3, 12, tzinfo=timezone.utc),
        batch_link=SubmittedMealBatchLink(
            batch_id=old.batch_id, role=BatchMealRole.PREPARATION
        ),
    )
    activation = repo._batch_submission_ledger_item(
        "user", preparation, expected_ledger_revision=None
    )
    replacement = _batch_entry_for_test("replacement", week, 1)
    replacement_items = repo._batch_ledger_transaction_items(
        "user",
        make_plan(week_start=week, revision=1),
        [replacement],
        expected_revision=0,
    )
    assert activation is not None
    assert len(replacement_items) == 1
    barrier = Barrier(2)

    def write(items: list[dict[str, Any]]) -> bool:
        barrier.wait()
        try:
            repo.table.meta.client.transact_write_items(TransactItems=items)
        except ClientError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(write, [[activation], replacement_items]))

    assert sorted(results) == [False, True]
    ledger = repo.get_weekly_batch_ledger("user", "2026-W32")
    assert len(ledger.entries) == 1
    assert ledger.entries[0].batch_id in {"old", "replacement"}


@pytest.mark.parametrize(
    ("as_of", "state", "remaining"),
    [
        (date(2026, 8, 7), BatchLedgerState.PROVISIONAL, 1),
        (date(2026, 8, 8), BatchLedgerState.EXPIRED, 0),
        (date(2026, 8, 9), BatchLedgerState.AVAILABLE, 1),
        (date(2026, 8, 10), BatchLedgerState.EXPIRED, 0),
    ],
)
def test_batch_expiry_is_exact_at_preparation_and_iso_week_boundaries(
    repo: DynamoRepository,
    as_of: date,
    state: BatchLedgerState,
    remaining: int,
) -> None:
    """Provisional stock expires after preparation day and after Sunday."""
    provisional = _batch_entry_for_test(
        "provisional", date(2026, 8, 3), 0
    ).model_copy(update={"preparation_date": date(2026, 8, 7)})
    available = _batch_entry_for_test(
        "available", date(2026, 8, 3), 0, state=BatchLedgerState.AVAILABLE
    )
    repo.put_weekly_batch_ledger(
        "user",
        WeeklyBatchLedger(
            iso_week="2026-W32", entries=[provisional, available]
        ),
    )

    loaded = repo.get_weekly_batch_ledger("user", "2026-W32", as_of=as_of)
    selected = {entry.batch_id: entry for entry in loaded.entries}
    target_id = "provisional" if as_of <= date(2026, 8, 8) else "available"
    assert selected[target_id].state is state
    assert selected[target_id].remaining_portions == remaining


@pytest.mark.parametrize(
    ("reasons", "operation", "expected"),
    [
        (
            [
                {"Code": "None"},
                {"Code": "None"},
                {"Code": "None"},
                {"Code": "ConditionalCheckFailed"},
            ],
            "meal_confirmation",
            TransactionConflictKind.INVENTORY_CHANGED,
        ),
        (
            [
                {"Code": "None"},
                {"Code": "ConditionalCheckFailed"},
                {"Code": "None"},
                {"Code": "None"},
            ],
            "meal_confirmation",
            TransactionConflictKind.STALE_WORK,
        ),
        (
            [{"Code": "None"}, {"Code": "TransactionConflict"}],
            None,
            TransactionConflictKind.RETRYABLE,
        ),
    ],
)
def test_transaction_conflicts_have_bounded_classification(
    reasons: list[dict[str, str]],
    operation: str | None,
    expected: TransactionConflictKind,
) -> None:
    """Cancellation positions distinguish stale work from live inventory."""
    error = ClientError(
        {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": reasons,
        },
        "TransactWriteItems",
    )

    assert (
        DynamoRepository._classify_transaction_conflict(
            error, operation=operation
        )
        is expected
    )


def test_retryable_meal_transaction_is_retried_once_without_relaxing_guards(
    mocker: Any,
) -> None:
    """A service conflict retries the same exact conditional transaction."""
    table = mocker.MagicMock()
    table.name = "test-meal-planner"
    error = ClientError(
        {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [
                {"Code": "None"},
                {"Code": "TransactionConflict"},
            ],
        },
        "TransactWriteItems",
    )
    table.meta.client.transact_write_items.side_effect = error
    review = _review_meal_state()
    continuation = _continuation_meal_state(review)
    entry = _meal(8, 12, "Soup")

    with pytest.raises(ClientError) as raised:
        DynamoRepository(table).confirm_meal_and_transition(
            "user",
            entry,
            continuation,
            expected_revision=review.revision,
            submission_id=review.request_id,
        )

    assert raised.value is error
    assert table.meta.client.transact_write_items.call_count == 2


def test_tracked_draft_publication_writes_plan_ledger_and_state_atomically(
    repo: DynamoRepository,
) -> None:
    """A tracked draft and provisional source share one transaction."""
    week = date(2026, 8, 17)
    now = datetime.now(timezone.utc)
    state = ConversationState(
        workflow_kind=ConversationWorkflowKind.PLAN_REQUEST,
        step=ConversationWorkflowStep.GENERATING,
        request_id="request-1",
        revision=4,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
    )
    repo.save_conversation_state("user", state)
    entry = _batch_entry_for_test("provisional-1", week, state.revision)

    assert repo.save_generated_draft_and_clear_conversation_state(
        "user",
        make_plan(week_start=week),
        expected_revision=None,
        request_id=state.request_id,
        expected_state_revision=state.revision,
        batch_entries=[entry],
    )

    saved = repo.get_weekly_batch_ledger("user", "2026-W34")
    assert saved.entries == [entry]
    assert repo.get_plan("user", week) is not None
    assert repo.get_conversation_state("user") is None


def test_untracked_draft_publication_writes_provisional_ledger_atomically(
    repo: DynamoRepository,
) -> None:
    """An untracked draft cannot leave its plan without its reservation."""
    week = date(2026, 8, 17)
    entry = _batch_entry_for_test("provisional-1", week, 0)

    assert repo.save_generated_draft(
        "user",
        make_plan(week_start=week),
        expected_revision=None,
        batch_entries=[entry],
    )

    assert repo.get_plan("user", week) is not None
    assert repo.get_weekly_batch_ledger("user", "2026-W34").entries == [entry]


def test_replacing_draft_removes_only_owned_provisional_entries(
    repo: DynamoRepository,
) -> None:
    """Replacement cleanup retains available inventory and other owners."""
    week = date(2026, 8, 17)
    available = _batch_entry_for_test(
        "available-1", week, 0, state=BatchLedgerState.AVAILABLE
    )
    old = _batch_entry_for_test("old-provisional", week, 0)
    repo.put_weekly_batch_ledger(
        "user", WeeklyBatchLedger(iso_week="2026-W34", entries=[available, old])
    )
    repo.save_plan("user", make_plan(week_start=week, revision=0))
    now = datetime.now(timezone.utc)
    state = ConversationState(
        workflow_kind=ConversationWorkflowKind.PLAN_REVISION,
        step=ConversationWorkflowStep.GENERATING,
        amendment="change",
        target_week=week,
        expected_plan_revision=0,
        request_id="replacement-request",
        revision=2,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
    )
    repo.save_conversation_state("user", state)
    replacement = _batch_entry_for_test(
        "new-provisional", week, 1, request_id=state.request_id
    )

    assert repo.replace_draft_and_clear_revision_state(
        "user",
        make_plan(week_start=week, revision=1),
        expected_plan_revision=0,
        request_id=state.request_id,
        expected_state_revision=state.revision,
        batch_entries=[replacement],
    )

    assert [
        entry.batch_id
        for entry in repo.get_weekly_batch_ledger("user", "2026-W34").entries
    ] == ["available-1", "new-provisional"]


def _batch_entry_for_test(
    batch_id: str,
    week: date,
    revision: int,
    *,
    state: BatchLedgerState = BatchLedgerState.PROVISIONAL,
    request_id: str = "request-1",
) -> BatchLedgerEntry:
    """Build a valid ledger entry for repository transaction tests."""
    return BatchLedgerEntry(
        batch_id=batch_id,
        source_plan_id=f"plan-{week.isoformat()}",
        source_request_id=request_id,
        source_revision=revision,
        preparation_date=week,
        preparation_meal_type=MealType.DINNER,
        food="chicken",
        meal_name="Chicken batch",
        total_portions=2,
        remaining_portions=1,
        state=state,
        week_end=week + timedelta(days=6),
    )


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


def _plan_with_batch_link(
    *,
    week_start: date,
    target_date: date,
    batch_id: str,
    revision: int = 0,
    status: PlanStatus = PlanStatus.DRAFT,
    role: BatchMealRole = BatchMealRole.PREPARATION,
) -> Any:
    """Build one plan containing a link at a selected date."""
    plan = make_plan(
        week_start=week_start,
        status=status,
        revision=revision,
        plan_days=(target_date - week_start).days + 1,
    )
    link = PlannedBatchLink(batch_id=batch_id, role=role)
    if role is BatchMealRole.LEFTOVER:
        link = PlannedBatchLink(
            batch_id=batch_id,
            role=role,
            source_date=week_start,
            source_meal_type=MealType.LUNCH,
            portion=2,
        )
    plan.days[-1].meals[0].batch_link = link
    return plan


def test_planned_batch_link_uses_covering_plan_when_newer_plan_does_not(
    repo: DynamoRepository,
) -> None:
    """A newer non-covering plan cannot hide an older matching plan."""
    target = date(2026, 8, 20)
    covering = _plan_with_batch_link(
        week_start=date(2026, 8, 17),
        target_date=target,
        batch_id="covering-batch",
    )
    newer = make_plan(week_start=date(2026, 8, 24), revision=4)
    repo.save_plan("user", covering)
    repo.save_plan("user", newer)

    link = repo.get_planned_batch_link("user", target, MealType.LUNCH)

    assert link is not None
    assert link.batch_id == "covering-batch"


def test_planned_batch_link_precedence_skips_stale_malformed_and_ambiguous(
    repo: DynamoRepository,
) -> None:
    """Overlapping candidates use status, revision, and date precedence."""
    target = date(2026, 8, 20)
    stale = _plan_with_batch_link(
        week_start=date(2026, 8, 17),
        target_date=target,
        batch_id="stale-batch",
        revision=1,
    )
    current_draft = _plan_with_batch_link(
        week_start=date(2026, 8, 18),
        target_date=target,
        batch_id="draft-batch",
        revision=3,
    )
    current_confirmed = _plan_with_batch_link(
        week_start=date(2026, 8, 19),
        target_date=target,
        batch_id="confirmed-batch",
        revision=4,
        status=PlanStatus.CONFIRMED,
    )
    repo.save_plan("user", stale)
    repo.save_plan("user", current_draft)
    repo.save_plan("user", current_confirmed)
    repo.table.put_item(
        Item={
            "PK": "USER#user",
            "SK": "PLAN#2026-08-20",
            "week_start_date": "2026-08-20",
            "status": "not-a-plan-status",
            "revision": 99,
            "days": [],
        }
    )

    link = repo.get_planned_batch_link("user", target, MealType.LUNCH)

    assert link is not None
    assert link.batch_id == "confirmed-batch"


def test_planned_batch_link_returns_none_for_an_ordinary_meal_slot(
    repo: DynamoRepository,
) -> None:
    """A covered ordinary meal does not inherit an unrelated batch link."""
    target = date(2026, 8, 20)
    plan = _plan_with_batch_link(
        week_start=date(2026, 8, 17),
        target_date=target,
        batch_id="planned-batch",
    )
    repo.save_plan("user", plan)

    assert repo.get_planned_batch_link("user", target, MealType.DINNER) is None


def test_planned_batch_link_query_is_bounded_to_user_partition(
    mocker: Any,
) -> None:
    """Lookup uses one bounded key query and never scans the table."""
    table = mocker.MagicMock()
    table.query.return_value = {"Items": []}

    assert (
        DynamoRepository(table).get_planned_batch_link(
            "user", date(2026, 8, 20), MealType.LUNCH
        )
        is None
    )

    query = table.query.call_args.kwargs
    assert query["ScanIndexForward"] is False
    assert query["Limit"] == 32
    expression = query["KeyConditionExpression"].get_expression()
    partition_condition = expression["values"][0].get_expression()
    sort_condition = expression["values"][1].get_expression()
    assert partition_condition["values"][1] == "USER#user"
    assert sort_condition["values"][1] == "PLAN#"
    table.scan.assert_not_called()


@pytest.mark.parametrize("plan_days", [1, 3])
def test_short_confirmed_plan_is_active_only_through_dynamic_end(
    repo: DynamoRepository, plan_days: int
) -> None:
    """Active-plan reads include the final persisted day and no later date."""
    start = date(2026, 8, 10)
    draft = make_plan(
        week_start=start, plan_days=plan_days, status=PlanStatus.DRAFT
    )
    repo.save_plan("user", draft)
    assert repo.confirm_plan("user", draft.week_start_date, draft.revision)

    confirmed = repo.get_plan("user", start)
    assert confirmed is not None
    final_date = start + timedelta(days=plan_days - 1)
    assert repo.get_active_plan("user", start) == confirmed
    assert repo.get_active_plan("user", final_date) == confirmed
    assert repo.get_active_plan("user", final_date + timedelta(days=1)) is None


@pytest.mark.parametrize("plan_days", [1, 3])
def test_short_plan_lifecycle_bounds_edits_outcomes_and_groceries(
    repo: DynamoRepository, plan_days: int
) -> None:
    """Short plans retain CAS lifecycle behavior at their actual last day."""
    plan = make_plan(
        plan_days=plan_days,
        status=PlanStatus.DRAFT,
    )
    repo.save_plan("user", plan)
    assert repo.confirm_plan("user", plan.week_start_date, plan.revision)
    confirmed = repo.get_plan("user", plan.week_start_date)
    assert confirmed is not None

    last_day = confirmed.days[-1]
    edited = last_day.meals[0].model_copy(update={"name": "Edited lunch"})
    assert repo.update_meal(
        "user",
        confirmed.week_start_date,
        plan_days,
        "lunch",
        edited,
        expected_revision=confirmed.revision,
        expected_status=PlanStatus.CONFIRMED,
    )
    assert not repo.update_meal(
        "user",
        confirmed.week_start_date,
        plan_days + 1,
        "lunch",
        edited,
        expected_revision=confirmed.revision + 1,
        expected_status=PlanStatus.CONFIRMED,
    )
    snapshot = repo.get_active_plan_snapshot("user")
    assert snapshot is not None
    assert repo.update_meal_outcome(
        "user",
        confirmed.week_start_date,
        plan_days,
        "lunch",
        MealOutcome.COOKED,
        expected_epoch=snapshot.active_epoch,
    )
    assert repo.complete_grocery(
        "user",
        confirmed.week_start_date,
        confirmed.revision + 1,
        [GrocerySection(name="Produce", items=["Apples"])],
    )

    saved = repo.get_plan("user", confirmed.week_start_date)
    assert saved is not None
    assert saved.days[-1].meals[0].name == "Edited lunch"
    assert saved.days[-1].meals[0].outcome is MealOutcome.COOKED
    assert saved.grocery_status is GroceryStatus.READY


@pytest.mark.parametrize("plan_days", [1, 3])
def test_short_plan_outcome_rejects_day_after_persisted_end(
    repo: DynamoRepository, plan_days: int
) -> None:
    """Out-of-range outcome writes leave the persisted plan unchanged."""
    plan = make_plan(plan_days=plan_days, status=PlanStatus.DRAFT)
    repo.save_plan("user", plan)
    assert repo.confirm_plan("user", plan.week_start_date, plan.revision)
    confirmed = repo.get_plan("user", plan.week_start_date)
    assert confirmed is not None
    before_snapshot = repo.get_active_plan_snapshot("user")
    assert before_snapshot is not None

    assert not repo.update_meal_outcome(
        "user",
        confirmed.week_start_date,
        plan_days + 1,
        "lunch",
        MealOutcome.COOKED,
        expected_epoch=before_snapshot.active_epoch,
    )

    saved = repo.get_plan("user", confirmed.week_start_date)
    after_snapshot = repo.get_active_plan_snapshot("user")
    assert saved == confirmed
    assert saved is not None
    assert all(
        meal.outcome is MealOutcome.UNREPORTED
        for plan_day in saved.days
        for meal in plan_day.meals
    )
    assert saved.revision == confirmed.revision
    assert saved.status is PlanStatus.CONFIRMED
    assert saved.grocery_status is confirmed.grocery_status
    assert after_snapshot is not None
    assert after_snapshot.active_epoch == before_snapshot.active_epoch


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
