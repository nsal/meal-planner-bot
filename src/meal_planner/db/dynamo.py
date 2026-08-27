"""DynamoDB repository for profiles, meal history, and weekly plans."""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

from boto3.dynamodb.conditions import Key  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from pydantic import ValidationError

from meal_planner.dietary_rules import has_constraint_conflict
from meal_planner.models.schemas import (
    BatchLedgerEntry,
    BatchLedgerState,
    BatchMealRole,
    ConversationState,
    ConversationWorkflowKind,
    ConversationWorkflowStep,
    MealLogEntry,
    MealOutcome,
    MealType,
    PlannedBatchLink,
    PlanStatus,
    ProfileEditCategory,
    ProfileEditOperation,
    ProfileUpdateEntities,
    UserProfile,
    WeeklyBatchLedger,
    WeeklyPlan,
    _normalize_saved_profile,
    canonicalize_profile_rule_ids,
)

logger = logging.getLogger(__name__)
_PLANNED_BATCH_PLAN_QUERY_LIMIT = 32
_WEEKLY_BATCH_EXPIRY_MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class ActivePlanSnapshot:
    """Active plan and the activity epoch observed with it."""

    plan: WeeklyPlan
    active_epoch: int | None


class RepairPublicationOutcome(str, Enum):
    """Result of attempting an untracked repair publication."""

    PUBLISHED = "published"
    STALE = "stale"
    DUPLICATE = "duplicate"


class TransactionConflictKind(str, Enum):
    """Bounded classification for conditional transaction cancellations."""

    STALE_WORK = "stale_work"
    DUPLICATE_SUBMISSION = "duplicate_submission"
    INVENTORY_CHANGED = "inventory_changed"
    RETRYABLE = "retryable"
    UNKNOWN = "unknown"


class DynamoRepository:
    """Repository handling CRUD operations on a single DynamoDB table."""

    def __init__(self, table: Any) -> None:
        self.table = table

    @staticmethod
    def _data(item: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value for key, value in item.items() if key not in {"PK", "SK"}
        }

    def _transact_write_with_retry(
        self, transact_items: list[dict[str, Any]]
    ) -> None:
        """Retry one service-level transaction conflict with the same guards."""
        for attempt in range(2):
            try:
                self.table.meta.client.transact_write_items(
                    TransactItems=transact_items
                )
            except ClientError as exc:
                if (
                    attempt == 0
                    and self._classify_transaction_conflict(exc)
                    is TransactionConflictKind.RETRYABLE
                ):
                    continue
                raise
            return

    def get_profile(
        self, user_id: str, *, consistent_read: bool = False
    ) -> Optional[UserProfile]:
        """Return the user's canonical profile, if it exists."""
        get_kwargs: dict[str, Any] = {
            "Key": {"PK": f"USER#{user_id}", "SK": "PROFILE"}
        }
        if consistent_read:
            get_kwargs["ConsistentRead"] = True
        response = self.table.get_item(**get_kwargs)
        item = response.get("Item")
        if not item:
            return None

        data, discarded_count = _normalize_saved_profile(self._data(item))
        profile = canonicalize_profile_rule_ids(
            UserProfile.model_validate(data, context={"saved_profile": True})
        )
        if discarded_count:
            logger.warning(
                "Loaded profile with discarded legacy preferences "
                "reason_code=legacy_preference count=%d",
                discarded_count,
            )
        return profile

    @staticmethod
    def _canonical_profile(profile: UserProfile) -> UserProfile:
        """Validate and deterministically normalize a profile before saving."""
        canonical = canonicalize_profile_rule_ids(profile)
        data = canonical.model_dump(mode="json", warnings=False)
        preferences = data.get("dietary_preferences", [])
        seen_sources: set[str] = set()
        unique_preferences: list[dict[str, Any]] = []
        for preference in preferences:
            source_text = str(preference["source_text"])
            source_key = source_text.casefold()
            if source_key in seen_sources:
                continue
            seen_sources.add(source_key)
            unique_preferences.append(preference)
        data["dietary_preferences"] = unique_preferences
        return UserProfile.model_validate(data)

    @classmethod
    def _prepare_guarded_profile(
        cls,
        profile: UserProfile,
        category: ProfileEditCategory,
    ) -> UserProfile | None:
        """Apply profile-rule guards before a state-guarded transaction."""
        canonical = cls._canonical_profile(profile)
        if category is ProfileEditCategory.DIETARY_PREFERENCES:
            if any(
                preference.rule is not None
                and has_constraint_conflict(
                    preference.rule, canonical.dietary_constraints
                )
                for preference in canonical.dietary_preferences
            ):
                return None
            return canonical

        if category is ProfileEditCategory.DIETARY_CONSTRAINTS:
            retained = [
                preference
                for preference in canonical.dietary_preferences
                if preference.rule is None
                or not has_constraint_conflict(
                    preference.rule, canonical.dietary_constraints
                )
            ]
            if len(retained) != len(canonical.dietary_preferences):
                return canonical.model_copy(
                    update={"dietary_preferences": retained}
                )
        return canonical

    def save_profile(
        self,
        user_id: str,
        profile: UserProfile,
        *,
        expected_revision: int | None,
    ) -> bool:
        """Save a profile only when its caller-observed state is current."""
        profile = self._canonical_profile(profile)
        profile = profile.model_copy(
            update={
                "profile_revision": (
                    0 if expected_revision is None else expected_revision + 1
                )
            }
        )
        item = {
            "PK": f"USER#{user_id}",
            "SK": "PROFILE",
            **profile.model_dump(mode="json"),
        }
        kwargs: dict[str, Any] = {"Item": item}
        if expected_revision is None:
            kwargs["ConditionExpression"] = "attribute_not_exists(#pk)"
            kwargs["ExpressionAttributeNames"] = {"#pk": "PK"}
        else:
            revision_condition = (
                "(attribute_not_exists(#profile_revision) OR "
                "#profile_revision = :expected_revision)"
                if expected_revision == 0
                else "attribute_exists(#profile_revision) AND "
                "#profile_revision = :expected_revision"
            )
            kwargs["ConditionExpression"] = (
                "attribute_exists(#pk) AND " + revision_condition
            )
            kwargs["ExpressionAttributeNames"] = {
                "#pk": "PK",
                "#profile_revision": "profile_revision",
            }
            kwargs["ExpressionAttributeValues"] = {
                ":expected_revision": expected_revision,
            }
        try:
            self.table.put_item(**kwargs)
        except ClientError as exc:
            if self._is_conditional_failure(exc):
                return False
            raise
        return True

    def save_profile_and_transition_state(
        self,
        user_id: str,
        profile: UserProfile,
        next_state: ConversationState,
        observed_state: ConversationState,
    ) -> bool:
        """Atomically save a profile and consume its observed edit state."""
        if (
            observed_state.workflow_kind
            is not ConversationWorkflowKind.PROFILE_EDIT
            or observed_state.step
            is not ConversationWorkflowStep.AWAITING_PROFILE_INPUT
            or observed_state.profile_category is None
            or observed_state.profile_operation is None
        ):
            return False
        prepared_profile = self._prepare_guarded_profile(
            profile, observed_state.profile_category
        )
        if prepared_profile is None:
            return False
        committed_profile = prepared_profile.model_copy(
            update={
                "profile_revision": prepared_profile.profile_revision + 1,
            }
        )
        profile_item = {
            "PK": f"USER#{user_id}",
            "SK": "PROFILE",
            **committed_profile.model_dump(mode="json"),
        }
        state_item = {
            **self._conversation_key(user_id),
            **next_state.model_dump(mode="json"),
        }
        profile_condition = (
            "attribute_exists(PK) AND "
            "(attribute_not_exists(#profile_revision) OR "
            "#profile_revision = :observed_profile_revision)"
        )
        state_condition = (
            "#revision = :observed_revision AND "
            "#created_at = :observed_created_at AND "
            "#workflow_kind = :observed_workflow_kind AND "
            "#step = :observed_step AND "
            "#profile_category = :observed_profile_category AND "
            "#profile_operation = :observed_profile_operation"
        )
        profile_names = {"#profile_revision": "profile_revision"}
        state_names = {
            "#revision": "revision",
            "#created_at": "created_at",
            "#workflow_kind": "workflow_kind",
            "#step": "step",
            "#profile_category": "profile_category",
            "#profile_operation": "profile_operation",
        }
        observed_data = observed_state.model_dump(mode="json")
        profile_values = {
            ":observed_profile_revision": profile.profile_revision,
        }
        state_values = {
            ":observed_revision": observed_data["revision"],
            ":observed_created_at": observed_data["created_at"],
            ":observed_workflow_kind": observed_data["workflow_kind"],
            ":observed_step": observed_data["step"],
            ":observed_profile_category": observed_data["profile_category"],
            ":observed_profile_operation": observed_data["profile_operation"],
        }
        try:
            self.table.meta.client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.table.name,
                            "Item": profile_item,
                            "ConditionExpression": profile_condition,
                            "ExpressionAttributeNames": profile_names,
                            "ExpressionAttributeValues": profile_values,
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.table.name,
                            "Item": state_item,
                            "ConditionExpression": state_condition,
                            "ExpressionAttributeNames": state_names,
                            "ExpressionAttributeValues": state_values,
                        }
                    },
                ]
            )
        except ClientError as exc:
            if self._is_transaction_conditional_failure(exc):
                return False
            raise
        return True

    def remove_profile_item_and_transition_state(
        self,
        user_id: str,
        profile: UserProfile,
        next_state: ConversationState,
        observed_state: ConversationState,
        *,
        expected_profile_revision: int,
    ) -> UserProfile | None:
        """Atomically apply a numbered removal and retain removal mode."""
        if (
            observed_state.profile_operation is not ProfileEditOperation.REMOVE
            or observed_state.profile_category is None
            or next_state.step
            is not ConversationWorkflowStep.AWAITING_PROFILE_INPUT
            or next_state.profile_category
            is not observed_state.profile_category
            or next_state.profile_operation is not ProfileEditOperation.REMOVE
            or next_state.revision != observed_state.revision + 1
            or profile.profile_revision != expected_profile_revision
        ):
            return None
        committed_profile = profile.model_copy(
            update={
                "profile_revision": profile.profile_revision + 1,
            }
        )
        profile_item = {
            "PK": f"USER#{user_id}",
            "SK": "PROFILE",
            **committed_profile.model_dump(mode="json"),
        }
        state_item = {
            **self._conversation_key(user_id),
            **next_state.model_dump(mode="json"),
        }
        profile_condition = (
            "attribute_exists(PK) AND "
            "(attribute_not_exists(#profile_revision) OR "
            "#profile_revision = :observed_profile_revision)"
        )
        state_condition = (
            "#revision = :observed_revision AND "
            "#created_at = :observed_created_at AND "
            "#workflow_kind = :observed_workflow_kind AND "
            "#step = :observed_step AND "
            "#profile_category = :observed_profile_category AND "
            "#profile_operation = :observed_profile_operation"
        )
        profile_names = {"#profile_revision": "profile_revision"}
        state_names = {
            "#revision": "revision",
            "#created_at": "created_at",
            "#workflow_kind": "workflow_kind",
            "#step": "step",
            "#profile_category": "profile_category",
            "#profile_operation": "profile_operation",
        }
        observed_data = observed_state.model_dump(mode="json")
        profile_values = {
            ":observed_profile_revision": expected_profile_revision,
        }
        state_values = {
            ":observed_revision": observed_data["revision"],
            ":observed_created_at": observed_data["created_at"],
            ":observed_workflow_kind": observed_data["workflow_kind"],
            ":observed_step": observed_data["step"],
            ":observed_profile_category": observed_data["profile_category"],
            ":observed_profile_operation": observed_data["profile_operation"],
        }
        try:
            self.table.meta.client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.table.name,
                            "Item": profile_item,
                            "ConditionExpression": profile_condition,
                            "ExpressionAttributeNames": profile_names,
                            "ExpressionAttributeValues": profile_values,
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.table.name,
                            "Item": state_item,
                            "ConditionExpression": state_condition,
                            "ExpressionAttributeNames": state_names,
                            "ExpressionAttributeValues": state_values,
                        }
                    },
                ]
            )
        except ClientError as exc:
            if self._is_transaction_conditional_failure(exc):
                return None
            raise
        return committed_profile

    def get_profile_draft(
        self, user_id: str
    ) -> Optional[ProfileUpdateEntities]:
        """Return accumulated onboarding fields, if a draft exists."""
        response = self.table.get_item(
            Key={"PK": f"USER#{user_id}", "SK": "PROFILE_DRAFT"}
        )
        item = response.get("Item")
        if not item:
            return None
        return ProfileUpdateEntities.model_validate(self._data(item))

    def save_profile_draft(
        self, user_id: str, draft: ProfileUpdateEntities
    ) -> None:
        self.table.put_item(
            Item={
                "PK": f"USER#{user_id}",
                "SK": "PROFILE_DRAFT",
                **draft.model_dump(mode="json"),
            }
        )

    def delete_profile_draft(self, user_id: str) -> None:
        self.table.delete_item(
            Key={"PK": f"USER#{user_id}", "SK": "PROFILE_DRAFT"}
        )

    @staticmethod
    def _conversation_key(user_id: str) -> dict[str, str]:
        return {"PK": f"USER#{user_id}", "SK": "CONVERSATION_STATE"}

    def get_conversation_state(
        self,
        user_id: str,
        *,
        now: datetime | None = None,
        consistent_read: bool = False,
    ) -> ConversationState | None:
        """Return non-expired conversation state, if present."""
        get_kwargs: dict[str, Any] = {"Key": self._conversation_key(user_id)}
        if consistent_read:
            get_kwargs["ConsistentRead"] = True
        response = self.table.get_item(**get_kwargs)
        item = response.get("Item")
        if not item:
            return None
        state = ConversationState.model_validate(self._data(item))
        current = now or datetime.now(timezone.utc)
        if state.expires_at <= int(current.timestamp()):
            try:
                self.table.delete_item(
                    Key=self._conversation_key(user_id),
                    ConditionExpression="#expires_at = :expires_at",
                    ExpressionAttributeNames={"#expires_at": "expires_at"},
                    ExpressionAttributeValues={":expires_at": state.expires_at},
                )
            except ClientError as exc:
                if not self._is_conditional_failure(exc):
                    raise
            return None
        return state

    def save_conversation_state(
        self,
        user_id: str,
        state: ConversationState,
        *,
        expected_revision: int | None = None,
        expected_request_id: str | None = None,
        expected_step: ConversationWorkflowStep | None = None,
    ) -> bool:
        """Create or replace state only when its revision is current."""
        item = {
            **self._conversation_key(user_id),
            **state.model_dump(mode="json"),
        }
        condition, names, values = self._conversation_state_condition(
            expected_revision=expected_revision,
            expected_request_id=expected_request_id,
            expected_step=expected_step,
            require_absent=expected_revision is None
            and expected_request_id is None
            and expected_step is None,
        )
        kwargs: dict[str, Any] = {
            "Item": item,
            "ConditionExpression": condition,
            "ExpressionAttributeNames": names,
        }
        if values:
            kwargs["ExpressionAttributeValues"] = values
        try:
            self.table.put_item(**kwargs)
        except ClientError as exc:
            if self._is_conditional_failure(exc):
                return False
            raise
        return True

    def transition_conversation_state(
        self,
        user_id: str,
        state: ConversationState,
        *,
        expected_revision: int,
        expected_request_id: str | None = None,
        expected_step: ConversationWorkflowStep | None = None,
    ) -> bool:
        """Atomically persist a state transition from one revision."""
        return self.save_conversation_state(
            user_id,
            state,
            expected_revision=expected_revision,
            expected_request_id=expected_request_id,
            expected_step=expected_step,
        )

    def delete_conversation_state(
        self,
        user_id: str,
        *,
        expected_revision: int | None = None,
        expected_request_id: str | None = None,
        expected_step: ConversationWorkflowStep | None = None,
    ) -> bool:
        """Delete state conditionally, preventing stale workflow cleanup."""
        kwargs: dict[str, Any] = {"Key": self._conversation_key(user_id)}
        if (
            expected_revision is not None
            or expected_request_id is not None
            or expected_step is not None
        ):
            condition, names, values = self._conversation_state_condition(
                expected_revision=expected_revision,
                expected_request_id=expected_request_id,
                expected_step=expected_step,
                require_absent=False,
            )
            kwargs.update(
                {
                    "ConditionExpression": condition,
                    "ExpressionAttributeNames": names,
                    "ExpressionAttributeValues": values,
                }
            )
        try:
            self.table.delete_item(**kwargs)
        except ClientError as exc:
            if self._is_conditional_failure(exc):
                return False
            raise
        return True

    @staticmethod
    def _conversation_state_condition(
        *,
        expected_revision: int | None,
        expected_request_id: str | None,
        expected_step: ConversationWorkflowStep | None,
        require_absent: bool,
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        """Build one condition for the conversation-state mutation."""
        if require_absent:
            return "attribute_not_exists(#pk)", {"#pk": "PK"}, {}

        conditions: list[str] = []
        names: dict[str, str] = {}
        values: dict[str, Any] = {}
        if expected_revision is not None:
            conditions.append("#revision = :expected_revision")
            names["#revision"] = "revision"
            values[":expected_revision"] = expected_revision
        if expected_request_id is not None:
            conditions.append("#request_id = :expected_request_id")
            names["#request_id"] = "request_id"
            values[":expected_request_id"] = expected_request_id
        if expected_step is not None:
            conditions.append("#step = :expected_step")
            names["#step"] = "step"
            values[":expected_step"] = expected_step.value
        return " AND ".join(conditions), names, values

    def clear_conversation_state_if_matches(
        self,
        user_id: str,
        *,
        request_id: str,
        expected_revision: int,
    ) -> bool:
        """Clear only the planner request that produced a persisted draft."""
        try:
            self.table.delete_item(
                Key=self._conversation_key(user_id),
                ConditionExpression=(
                    "#revision = :revision AND #request_id = :request_id"
                ),
                ExpressionAttributeNames={
                    "#revision": "revision",
                    "#request_id": "request_id",
                },
                ExpressionAttributeValues={
                    ":revision": expected_revision,
                    ":request_id": request_id,
                },
            )
        except ClientError as exc:
            if self._is_conditional_failure(exc):
                return False
            raise
        return True

    def mark_conversation_retry_ready(
        self,
        user_id: str,
        state: ConversationState,
        *,
        expected_revision: int,
    ) -> bool:
        """Move a matching planner request into recoverable retry state."""
        try:
            self.table.put_item(
                Item={
                    **self._conversation_key(user_id),
                    **state.model_dump(mode="json"),
                },
                ConditionExpression=(
                    "#revision = :revision AND #request_id = :request_id "
                    "AND #step = :generating"
                ),
                ExpressionAttributeNames={
                    "#revision": "revision",
                    "#request_id": "request_id",
                    "#step": "step",
                },
                ExpressionAttributeValues={
                    ":revision": expected_revision,
                    ":request_id": state.request_id,
                    ":generating": ConversationWorkflowStep.GENERATING.value,
                },
            )
        except ClientError as exc:
            if self._is_conditional_failure(exc):
                return False
            raise
        return True

    def start_plan_revision(
        self,
        user_id: str,
        state: ConversationState,
        *,
        source_update_id: str,
    ) -> bool:
        """Atomically create a revision lock and its Telegram marker."""
        state_item = {
            **self._conversation_key(user_id),
            **state.model_dump(mode="json"),
        }
        marker_item = {
            "PK": f"USER#{user_id}",
            "SK": f"PLAN_REVISION_UPDATE#{source_update_id}",
        }
        try:
            self.table.meta.client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.table.name,
                            "Item": state_item,
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.table.name,
                            "Item": marker_item,
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                ]
            )
        except ClientError as exc:
            if self._is_transaction_conditional_failure(exc):
                return False
            raise
        return True

    def has_plan_revision_update_marker(
        self, user_id: str, source_update_id: str
    ) -> bool:
        """Return whether a Telegram update already started a revision."""
        response = self.table.get_item(
            Key={
                "PK": f"USER#{user_id}",
                "SK": f"PLAN_REVISION_UPDATE#{source_update_id}",
            },
            ConsistentRead=True,
        )
        return bool(response.get("Item"))

    def log_meal(
        self,
        user_id: str,
        entry: MealLogEntry,
        *,
        source_update_id: str | None = None,
    ) -> None:
        """Store a meal, retaining the first result for a source update."""
        meal_item = {
            "PK": f"USER#{user_id}",
            "SK": (
                f"MEAL#{entry.date_key}#UPDATE#{source_update_id}#"
                f"{entry.meal_type.value}"
            )
            if source_update_id is not None
            else (
                f"MEAL#{entry.date_key}#TIME#{entry.created_at.isoformat()}#"
                f"{entry.meal_type.value}"
            ),
            **entry.model_dump(mode="json"),
        }
        if source_update_id is None:
            self.table.put_item(Item=meal_item)
            return

        try:
            self.table.meta.client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.table.name,
                            "Item": meal_item,
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.table.name,
                            "Item": {
                                "PK": f"USER#{user_id}",
                                "SK": f"MEAL_UPDATE#{source_update_id}",
                            },
                            "ConditionExpression": ("attribute_not_exists(PK)"),
                        }
                    },
                ]
            )
        except ClientError as exc:
            if self._is_meal_duplicate_transaction(exc):
                return
            raise

    def log_meal_and_transition(
        self,
        user_id: str,
        entry: MealLogEntry,
        state: ConversationState,
        *,
        expected_revision: int,
        source_update_id: str | None = None,
    ) -> bool:
        """Atomically record a meal and advance its guided workflow state."""
        meal_item = {
            "PK": f"USER#{user_id}",
            "SK": (
                f"MEAL#{entry.date_key}#UPDATE#{source_update_id}#"
                f"{entry.meal_type.value}"
            )
            if source_update_id is not None
            else (
                f"MEAL#{entry.date_key}#TIME#{entry.created_at.isoformat()}#"
                f"{entry.meal_type.value}"
            ),
            **entry.model_dump(mode="json"),
        }
        state_item = {
            **self._conversation_key(user_id),
            **state.model_dump(mode="json"),
        }
        transact_items: list[dict[str, Any]] = [
            {
                "Put": {
                    "TableName": self.table.name,
                    "Item": meal_item,
                }
            },
            {
                "Put": {
                    "TableName": self.table.name,
                    "Item": state_item,
                    "ConditionExpression": "#revision = :revision",
                    "ExpressionAttributeNames": {"#revision": "revision"},
                    "ExpressionAttributeValues": {
                        ":revision": expected_revision
                    },
                }
            },
        ]
        if source_update_id is not None:
            transact_items.append(
                {
                    "Put": {
                        "TableName": self.table.name,
                        "Item": {
                            "PK": f"USER#{user_id}",
                            "SK": f"MEAL_UPDATE#{source_update_id}",
                        },
                        "ConditionExpression": "attribute_not_exists(PK)",
                    }
                }
            )
        for transaction_attempt in range(2):
            try:
                self.table.meta.client.transact_write_items(
                    TransactItems=transact_items
                )
            except ClientError as exc:
                conflict = self._classify_transaction_conflict(exc)
                if conflict is TransactionConflictKind.RETRYABLE:
                    if transaction_attempt == 0:
                        continue
                    raise
                if conflict in {
                    TransactionConflictKind.STALE_WORK,
                    TransactionConflictKind.DUPLICATE_SUBMISSION,
                    TransactionConflictKind.INVENTORY_CHANGED,
                }:
                    return False
                raise
            return True
        return False

    def confirm_meal_and_transition(
        self,
        user_id: str,
        entry: MealLogEntry,
        state: ConversationState,
        *,
        expected_revision: int,
        submission_id: str,
        expected_ledger_revision: int | None = None,
        processing_date: date | None = None,
    ) -> bool:
        """Save one reviewed meal and advance its workflow atomically.

        ``state`` is the state that should exist after confirmation.  The
        transaction conditions its replacement on the prior state still
        being the matching review step, revision, and submission.  The meal
        key is derived from the submission ID so a retry cannot overwrite a
        different meal or create a second item.
        """
        draft = state.meal_draft
        if (
            state.workflow_kind is not ConversationWorkflowKind.MEAL_LOG
            or state.step
            is not ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION
            or draft is None
            or state.request_id != submission_id
            or state.revision != expected_revision + 1
            or draft.date != entry.date
            or draft.meal_type != entry.meal_type
            or draft.description != entry.description
        ):
            return False

        ledger_item = self._batch_submission_ledger_item(
            user_id,
            entry,
            expected_ledger_revision=expected_ledger_revision,
            processing_date=processing_date,
        )
        if entry.batch_link is not None and ledger_item is None:
            return False

        meal_item = {
            "PK": f"USER#{user_id}",
            "SK": (f"MEAL#{entry.date_key}#SUBMISSION#{submission_id}"),
            **entry.model_dump(mode="json"),
        }
        state_item = {
            **self._conversation_key(user_id),
            **state.model_dump(mode="json"),
        }
        transact_items: list[dict[str, Any]] = [
            {
                "Put": {
                    "TableName": self.table.name,
                    "Item": meal_item,
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            },
            {
                "Put": {
                    "TableName": self.table.name,
                    "Item": state_item,
                    "ConditionExpression": (
                        "#workflow_kind = :meal_log "
                        "AND #step = :awaiting_confirmation "
                        "AND #revision = :expected_revision "
                        "AND #request_id = :request_id"
                    ),
                    "ExpressionAttributeNames": {
                        "#workflow_kind": "workflow_kind",
                        "#step": "step",
                        "#revision": "revision",
                        "#request_id": "request_id",
                    },
                    "ExpressionAttributeValues": {
                        ":meal_log": ConversationWorkflowKind.MEAL_LOG.value,
                        ":awaiting_confirmation": (
                            ConversationWorkflowStep.AWAITING_MEAL_CONFIRMATION.value
                        ),
                        ":expected_revision": expected_revision,
                        ":request_id": submission_id,
                    },
                }
            },
            {
                "Put": {
                    "TableName": self.table.name,
                    "Item": {
                        "PK": f"USER#{user_id}",
                        "SK": f"MEAL_UPDATE#{submission_id}",
                    },
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            },
        ]
        if ledger_item is not None:
            transact_items.append(ledger_item)
        for transaction_attempt in range(2):
            try:
                self.table.meta.client.transact_write_items(
                    TransactItems=transact_items
                )
            except ClientError as exc:
                conflict = self._classify_transaction_conflict(
                    exc, operation="meal_confirmation"
                )
                if conflict is TransactionConflictKind.RETRYABLE:
                    if transaction_attempt == 0:
                        continue
                    raise
                if conflict in {
                    TransactionConflictKind.STALE_WORK,
                    TransactionConflictKind.DUPLICATE_SUBMISSION,
                    TransactionConflictKind.INVENTORY_CHANGED,
                }:
                    return False
                raise
            return True
        return False

    def _batch_submission_ledger_item(
        self,
        user_id: str,
        entry: MealLogEntry,
        *,
        expected_ledger_revision: int | None,
        processing_date: date | None = None,
    ) -> dict[str, Any] | None:
        """Build a conditional ledger mutation for one confirmed meal."""
        link = entry.batch_link
        if link is None:
            return None
        source_date = link.source_date or entry.date
        iso = source_date.isocalendar()
        iso_week = f"{iso.year:04d}-W{iso.week:02d}"
        response = self.table.get_item(
            Key=self._batch_ledger_key(user_id, iso_week)
        )
        raw_item = response.get("Item")
        if raw_item is None:
            return None
        ledger = WeeklyBatchLedger.model_validate(self._data(raw_item))
        if (
            expected_ledger_revision is not None
            and ledger.revision != expected_ledger_revision
        ):
            return None
        target = next(
            (item for item in ledger.entries if item.batch_id == link.batch_id),
            None,
        )
        if target is None:
            return None

        if link.role is BatchMealRole.PREPARATION:
            effective_processing_date = processing_date or entry.date
            if (
                target.state is not BatchLedgerState.PROVISIONAL
                or target.remaining_portions != target.total_portions - 1
                or target.preparation_date != entry.date
                or target.preparation_meal_type is not entry.meal_type
                or link.portion != 1
                or effective_processing_date != target.preparation_date
                or effective_processing_date > target.week_end
                or effective_processing_date.isocalendar()[:2]
                != target.preparation_date.isocalendar()[:2]
            ):
                return None
            remaining = target.total_portions - 1
            next_state = (
                BatchLedgerState.AVAILABLE
                if remaining
                else BatchLedgerState.EXHAUSTED
            )
        else:
            if target.state is not BatchLedgerState.AVAILABLE:
                return None
            if target.remaining_portions <= 0:
                return None
            if link.source_date != target.preparation_date:
                return None
            if link.source_meal_type not in {
                None,
                target.preparation_meal_type,
            }:
                return None
            if entry.date <= target.preparation_date:
                return None
            if (
                entry.date.isocalendar()[:2]
                != target.preparation_date.isocalendar()[:2]
            ):
                return None
            next_portion = target.total_portions - target.remaining_portions + 1
            if link.portion != next_portion:
                return None
            remaining = target.remaining_portions - 1
            next_state = (
                BatchLedgerState.AVAILABLE
                if remaining
                else BatchLedgerState.EXHAUSTED
            )

        updated = target.model_copy(
            update={"remaining_portions": remaining, "state": next_state}
        )
        updated_entries = [
            updated if item.batch_id == target.batch_id else item
            for item in ledger.entries
        ]
        next_ledger = WeeklyBatchLedger(
            iso_week=ledger.iso_week,
            revision=ledger.revision + 1,
            entries=updated_entries,
        )
        old_entries = [item.model_dump(mode="json") for item in ledger.entries]
        names = {
            "#pk": "PK",
            "#revision": "revision",
            "#entries": "entries",
        }
        values = {
            ":revision": ledger.revision,
            ":entries": old_entries,
        }
        condition = (
            "attribute_exists(#pk) AND #revision = :revision "
            "AND #entries = :entries"
        )
        return {
            "Put": {
                "TableName": self.table.name,
                "Item": {
                    **self._batch_ledger_key(user_id, iso_week),
                    **next_ledger.model_dump(mode="json"),
                },
                "ConditionExpression": condition,
                "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": values,
            }
        }

    def get_meal_history(
        self,
        user_id: str,
        days: int = 14,
        *,
        on_date: date | None = None,
    ) -> list[MealLogEntry]:
        """Return every valid meal in the inclusive requested date window."""
        end_date = on_date or date.today()
        start_date = end_date - timedelta(days=max(days, 1) - 1)
        key_condition = Key("PK").eq(f"USER#{user_id}") & Key("SK").between(
            f"MEAL#{start_date.isoformat()}",
            f"MEAL#{end_date.isoformat()}~",
        )
        query_kwargs: dict[str, Any] = {
            "KeyConditionExpression": key_condition,
            "ScanIndexForward": False,
        }
        entries: list[MealLogEntry] = []
        while True:
            response = self.table.query(**query_kwargs)
            for item in response.get("Items", []):
                try:
                    entries.append(
                        MealLogEntry.model_validate(self._data(item))
                    )
                except ValidationError:
                    logger.warning(
                        "Skipping malformed meal history item "
                        "reason_code=malformed"
                    )
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            query_kwargs["ExclusiveStartKey"] = last_key
        entries.sort(key=lambda entry: entry.created_at, reverse=True)
        return entries

    def get_submitted_meals(
        self,
        user_id: str,
        *,
        start_date: date,
        end_date: date,
    ) -> list[MealLogEntry]:
        """Read submitted meal evidence from one bounded date range.

        Meal history uses a dedicated ``MEAL#`` sort-key namespace, so plan
        records (draft or confirmed) cannot be returned by this query.  The
        scheduler only needs one seven-day horizon plus a possible adjacent
        ISO week, therefore larger reads are rejected at this boundary.
        """
        if end_date < start_date:
            raise ValueError("end_date must not precede start_date")
        if (end_date - start_date).days + 1 > 14:
            raise ValueError("submitted meal ranges may cover at most 14 days")
        key_condition = Key("PK").eq(f"USER#{user_id}") & Key("SK").between(
            f"MEAL#{start_date.isoformat()}",
            f"MEAL#{end_date.isoformat()}~",
        )
        query_kwargs: dict[str, Any] = {
            "KeyConditionExpression": key_condition,
            "ScanIndexForward": False,
        }
        entries: list[MealLogEntry] = []
        while True:
            response = self.table.query(**query_kwargs)
            for item in response.get("Items", []):
                try:
                    entries.append(
                        MealLogEntry.model_validate(self._data(item))
                    )
                except ValidationError:
                    logger.warning(
                        "Skipping malformed meal history item "
                        "reason_code=malformed"
                    )
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            query_kwargs["ExclusiveStartKey"] = last_key
        entries.sort(
            key=lambda entry: (entry.date, entry.created_at), reverse=True
        )
        return entries

    def get_meal_history_between(
        self,
        user_id: str,
        start_date: date,
        end_date: date,
    ) -> list[MealLogEntry]:
        """Compatibility spelling for a bounded submitted-meal range read."""
        return self.get_submitted_meals(
            user_id, start_date=start_date, end_date=end_date
        )

    def get_meal_history_for_range(
        self,
        user_id: str,
        start_date: date,
        end_date: date,
    ) -> list[MealLogEntry]:
        """Return submitted history for an inclusive bounded date range."""
        return self.get_submitted_meals(
            user_id, start_date=start_date, end_date=end_date
        )

    def save_plan(self, user_id: str, plan: WeeklyPlan) -> None:
        self.table.put_item(
            Item={
                "PK": f"USER#{user_id}",
                "SK": f"PLAN#{plan.week_start_date}",
                **plan.model_dump(by_alias=True, mode="json"),
            }
        )

    @staticmethod
    def _batch_ledger_key(user_id: str, iso_week: str) -> dict[str, str]:
        """Return the exact key for one user's ISO-week batch ledger."""
        return {
            "PK": f"USER#{user_id}",
            "SK": f"BATCH_LEDGER#{iso_week}",
        }

    @staticmethod
    def _iso_week_bounds(iso_week: str) -> tuple[date, date]:
        """Return validated Monday/Sunday bounds for a persisted ISO week."""
        try:
            year_text, week_text = iso_week.split("-W", 1)
            start = date.fromisocalendar(int(year_text), int(week_text), 1)
        except AttributeError, TypeError, ValueError:
            raise ValueError(
                "iso_week must identify a valid ISO week"
            ) from None
        return start, start + timedelta(days=6)

    def get_weekly_batch_ledger(
        self,
        user_id: str,
        iso_week: str,
        *,
        as_of: date | None = None,
    ) -> WeeklyBatchLedger:
        """Read one bounded weekly ledger and expire unusable sources."""
        self._iso_week_bounds(iso_week)
        key = self._batch_ledger_key(user_id, iso_week)
        response = self.table.get_item(Key=key)
        item = response.get("Item")
        if not item:
            return WeeklyBatchLedger(iso_week=iso_week)
        ledger = WeeklyBatchLedger.model_validate(self._data(item))
        if as_of is None:
            return ledger

        last_conflict: ClientError | None = None
        expiry_writes = 0
        while True:
            expired_ledger = self._materialize_weekly_batch_expiry(
                ledger, as_of
            )
            if expired_ledger is None:
                return ledger
            if expiry_writes >= _WEEKLY_BATCH_EXPIRY_MAX_ATTEMPTS:
                assert last_conflict is not None
                raise last_conflict
            try:
                expiry_writes += 1
                self._put_weekly_batch_ledger_conditionally(
                    user_id,
                    expired_ledger,
                    expected_revision=ledger.revision,
                    expected_entries=ledger.entries,
                )
            except ClientError as exc:
                if not self._is_conditional_failure(exc):
                    raise
                last_conflict = exc
                response = self.table.get_item(
                    Key=key,
                    ConsistentRead=True,
                )
                item = response.get("Item")
                if not item:
                    raise
                ledger = WeeklyBatchLedger.model_validate(self._data(item))
                continue
            return expired_ledger

    @staticmethod
    def _materialize_weekly_batch_expiry(
        ledger: WeeklyBatchLedger, as_of: date
    ) -> WeeklyBatchLedger | None:
        """Return an expired revision, or ``None`` when no change is needed."""
        expired_entries: list[BatchLedgerEntry] = []
        changed = False
        for entry in ledger.entries:
            expired = as_of > entry.week_end or (
                entry.state is BatchLedgerState.PROVISIONAL
                and as_of > entry.preparation_date
            )
            if expired and entry.state in {
                BatchLedgerState.PROVISIONAL,
                BatchLedgerState.AVAILABLE,
            }:
                expired_entries.append(
                    entry.model_copy(
                        update={
                            "state": BatchLedgerState.EXPIRED,
                            "remaining_portions": 0,
                        }
                    )
                )
                changed = True
            else:
                expired_entries.append(entry)
        if not changed:
            return None
        return WeeklyBatchLedger(
            iso_week=ledger.iso_week,
            revision=ledger.revision + 1,
            entries=expired_entries,
        )

    def put_weekly_batch_ledger(
        self, user_id: str, ledger: WeeklyBatchLedger
    ) -> None:
        """Write one validated weekly ledger without reading the partition."""
        self._iso_week_bounds(ledger.iso_week)
        self.table.put_item(
            Item={
                **self._batch_ledger_key(user_id, ledger.iso_week),
                **ledger.model_dump(mode="json"),
            }
        )

    def save_weekly_batch_ledger(
        self,
        user_id: str,
        ledger: WeeklyBatchLedger,
        *,
        expected_revision: int | None = None,
        expected_entries: list[BatchLedgerEntry] | None = None,
    ) -> bool:
        """Conditionally replace one weekly ledger without a table scan."""
        try:
            self._put_weekly_batch_ledger_conditionally(
                user_id,
                ledger,
                expected_revision=expected_revision,
                expected_entries=expected_entries,
            )
        except ClientError as exc:
            if self._is_conditional_failure(exc):
                return False
            raise
        return True

    def _put_weekly_batch_ledger_conditionally(
        self,
        user_id: str,
        ledger: WeeklyBatchLedger,
        *,
        expected_revision: int | None = None,
        expected_entries: list[BatchLedgerEntry] | None,
    ) -> None:
        """Issue a CAS ledger put."""
        item = {
            **self._batch_ledger_key(user_id, ledger.iso_week),
            **ledger.model_dump(mode="json"),
        }
        names: dict[str, str] = {}
        values: dict[str, Any] = {}
        conditions: list[str] = []
        if expected_entries is None:
            if expected_revision is None:
                conditions.append("attribute_not_exists(#pk)")
                names["#pk"] = "PK"
            else:
                conditions.append("#revision = :expected_revision")
                names["#revision"] = "revision"
                values[":expected_revision"] = expected_revision
        else:
            conditions.append("#entries = :expected_entries")
            names["#entries"] = "entries"
            values[":expected_entries"] = [
                entry.model_dump(mode="json") for entry in expected_entries
            ]
            if expected_revision is not None:
                conditions.append("#revision = :expected_revision")
                names["#revision"] = "revision"
                values[":expected_revision"] = expected_revision
        kwargs = {
            "Item": item,
            "ConditionExpression": " AND ".join(conditions),
            "ExpressionAttributeNames": names,
        }
        if values:
            kwargs["ExpressionAttributeValues"] = values
        self.table.put_item(**kwargs)

    def _batch_ledger_transaction_items(
        self,
        user_id: str,
        plan: WeeklyPlan,
        batch_entries: list[BatchLedgerEntry],
        *,
        expected_revision: int | None,
        replaced_request_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Build conditional ledger puts for one atomic plan publication.

        A replacement may provide the exact request owner whose provisional
        reservations are being superseded.  When it does, reservations from
        other in-flight requests are retained.  The compatibility fallback
        keeps the existing plan/revision ownership boundary for callers that
        do not have an owner snapshot.
        """
        dates = {
            plan.week_start + timedelta(days=offset) for offset in range(7)
        }
        weeks = {
            f"{value.isocalendar().year:04d}-W{value.isocalendar().week:02d}"
            for value in dates
        }
        weeks.update(
            (
                f"{entry.preparation_date.isocalendar().year:04d}-W"
                f"{entry.preparation_date.isocalendar().week:02d}"
            )
            for entry in batch_entries
        )
        incoming_by_week: dict[str, list[BatchLedgerEntry]] = {}
        for entry in batch_entries:
            iso = entry.preparation_date.isocalendar()
            key = f"{iso.year:04d}-W{iso.week:02d}"
            incoming_by_week.setdefault(key, []).append(entry)

        plan_id = f"plan-{plan.week_start.isoformat()}"
        items: list[dict[str, Any]] = []
        for iso_week in sorted(weeks):
            response = self.table.get_item(
                Key=self._batch_ledger_key(user_id, iso_week)
            )
            raw_item = response.get("Item")
            existing = (
                WeeklyBatchLedger.model_validate(self._data(raw_item))
                if raw_item
                else WeeklyBatchLedger(iso_week=iso_week)
            )
            retained = list(existing.entries)
            if expected_revision is not None:
                retained = [
                    entry
                    for entry in retained
                    if not (
                        entry.state is BatchLedgerState.PROVISIONAL
                        and entry.source_plan_id == plan_id
                        and entry.source_revision == expected_revision
                        and (
                            replaced_request_id is None
                            or entry.source_request_id == replaced_request_id
                        )
                    )
                ]

            by_id = {entry.batch_id: entry for entry in retained}
            for entry in incoming_by_week.get(iso_week, []):
                previous = by_id.get(entry.batch_id)
                if previous is not None:
                    if previous.state is BatchLedgerState.AVAILABLE:
                        continue
                    if previous != entry:
                        raise ValueError(
                            "batch reservation conflicts with existing ledger"
                        )
                    continue
                by_id[entry.batch_id] = entry
            merged = WeeklyBatchLedger(
                iso_week=iso_week,
                revision=existing.revision + 1,
                entries=sorted(by_id.values(), key=lambda item: item.batch_id),
            )
            old_entries = list(existing.entries)
            if merged.entries == old_entries:
                continue
            put: dict[str, Any] = {
                "TableName": self.table.name,
                "Item": {
                    **self._batch_ledger_key(user_id, iso_week),
                    **merged.model_dump(mode="json"),
                },
            }
            if raw_item is None:
                put["ConditionExpression"] = "attribute_not_exists(#pk)"
                put["ExpressionAttributeNames"] = {"#pk": "PK"}
            else:
                put["ConditionExpression"] = "#entries = :old_entries"
                put["ExpressionAttributeNames"] = {"#entries": "entries"}
                put["ExpressionAttributeValues"] = {
                    ":old_entries": [
                        entry.model_dump(mode="json") for entry in old_entries
                    ]
                }
            items.append({"Put": put})
        return items

    def get_available_batch_portions(
        self,
        user_id: str,
        target_date: date,
        *,
        meal_type: Any | None = None,
    ) -> list[BatchLedgerEntry]:
        """Return available portions from the target date's ISO-week ledger."""
        del (
            meal_type
        )  # The ledger stores preparation type; rules own reuse scope.
        iso = target_date.isocalendar()
        iso_week = f"{iso.year:04d}-W{iso.week:02d}"
        ledger = self.get_weekly_batch_ledger(
            user_id, iso_week, as_of=target_date
        )
        return sorted(
            (
                entry
                for entry in ledger.entries
                if entry.state is BatchLedgerState.AVAILABLE
                and entry.remaining_portions > 0
                and entry.preparation_date < target_date
            ),
            key=lambda entry: (entry.preparation_date, entry.batch_id),
        )

    @staticmethod
    def _is_conditional_failure(exc: ClientError) -> bool:
        error = exc.response.get("Error", {})
        code = error.get("Code") if isinstance(error, dict) else None
        return code == "ConditionalCheckFailedException"

    @staticmethod
    def _is_transaction_conditional_failure(exc: ClientError) -> bool:
        """Return whether a transaction failed only on an expected condition."""
        return DynamoRepository._classify_transaction_conflict(exc) in {
            TransactionConflictKind.STALE_WORK,
            TransactionConflictKind.DUPLICATE_SUBMISSION,
            TransactionConflictKind.INVENTORY_CHANGED,
        }

    @staticmethod
    def _classify_transaction_conflict(
        exc: ClientError, *, operation: str | None = None
    ) -> TransactionConflictKind:
        """Classify a DynamoDB cancellation without inspecting payload data."""
        error = exc.response.get("Error", {})
        code = error.get("Code") if isinstance(error, dict) else None
        if code in {
            "ProvisionedThroughputExceededException",
            "ThrottlingException",
            "InternalServerError",
            "RequestLimitExceeded",
        }:
            return TransactionConflictKind.RETRYABLE
        if code != "TransactionCanceledException":
            return TransactionConflictKind.UNKNOWN
        reasons = exc.response.get("CancellationReasons")
        if not isinstance(reasons, list):
            return TransactionConflictKind.UNKNOWN
        reason_codes = [
            reason.get("Code") if isinstance(reason, dict) else None
            for reason in reasons
        ]
        if "TransactionConflict" in reason_codes:
            return TransactionConflictKind.RETRYABLE
        if set(reason_codes) - {"ConditionalCheckFailed", "None"}:
            return TransactionConflictKind.UNKNOWN
        if "ConditionalCheckFailed" not in reason_codes:
            return TransactionConflictKind.UNKNOWN
        if operation == "ledger_mutation":
            return TransactionConflictKind.INVENTORY_CHANGED
        if operation == "meal_confirmation" and len(reason_codes) >= 4:
            if reason_codes[3] == "ConditionalCheckFailed":
                return TransactionConflictKind.INVENTORY_CHANGED
            if reason_codes[2] == "ConditionalCheckFailed":
                return TransactionConflictKind.DUPLICATE_SUBMISSION
            if reason_codes[0] == "ConditionalCheckFailed":
                return TransactionConflictKind.DUPLICATE_SUBMISSION
        return TransactionConflictKind.STALE_WORK

    @staticmethod
    def _is_meal_duplicate_transaction(exc: ClientError) -> bool:
        """Return whether only the source-update marker already exists."""
        error = exc.response.get("Error", {})
        code = error.get("Code") if isinstance(error, dict) else None
        if code != "TransactionCanceledException":
            return False
        reasons = exc.response.get("CancellationReasons")
        if not isinstance(reasons, list) or len(reasons) != 2:
            return False
        first = reasons[0] if isinstance(reasons[0], dict) else {}
        second = reasons[1] if isinstance(reasons[1], dict) else {}
        return (
            first.get("Code") == "None"
            and second.get("Code") == "ConditionalCheckFailed"
        )

    @staticmethod
    def _repair_marker_key(user_id: str, repair_id: str) -> dict[str, str]:
        """Return the durable marker key for one untracked repair."""
        return {
            "PK": f"USER#{user_id}",
            "SK": f"PLAN_REPAIR#{repair_id}",
        }

    def save_generated_draft(
        self,
        user_id: str,
        plan: WeeklyPlan,
        *,
        expected_revision: int | None,
        batch_entries: list[BatchLedgerEntry] | None = None,
    ) -> bool:
        """Save a generated draft only when its snapshot is still current."""
        if expected_revision is None:
            condition_expression = "attribute_not_exists(#pk)"
            expression_attribute_names = {"#pk": "PK"}
            expression_attribute_values: dict[str, Any] = {}
        else:
            condition_expression = (
                "#status = :draft AND #revision = :expected_revision"
            )
            expression_attribute_names = {
                "#status": "status",
                "#revision": "revision",
            }
            expression_attribute_values = {
                ":draft": PlanStatus.DRAFT.value,
                ":expected_revision": expected_revision,
            }
        put_kwargs: dict[str, Any] = {
            "Item": {
                "PK": f"USER#{user_id}",
                "SK": f"PLAN#{plan.week_start_date}",
                **plan.model_dump(by_alias=True, mode="json"),
            },
            "ConditionExpression": condition_expression,
            "ExpressionAttributeNames": expression_attribute_names,
        }
        if expression_attribute_values:
            put_kwargs["ExpressionAttributeValues"] = (
                expression_attribute_values
            )
        if batch_entries is not None:
            ledger_items = self._batch_ledger_transaction_items(
                user_id,
                plan,
                batch_entries,
                expected_revision=expected_revision,
            )
            try:
                self._transact_write_with_retry(
                    [
                        {
                            "Put": {
                                "TableName": self.table.name,
                                **put_kwargs,
                            }
                        }
                    ]
                    + ledger_items
                )
            except ClientError as exc:
                if self._is_transaction_conditional_failure(exc):
                    return False
                raise
            return True
        try:
            self.table.put_item(**put_kwargs)
        except ClientError as exc:
            if self._is_conditional_failure(exc):
                return False
            raise
        return True

    def save_generated_draft_and_clear_conversation_state(
        self,
        user_id: str,
        plan: WeeklyPlan,
        *,
        expected_revision: int | None,
        request_id: str,
        expected_state_revision: int,
        batch_entries: list[BatchLedgerEntry] | None = None,
    ) -> bool:
        """Publish a tracked draft and release its request atomically."""
        plan_item = {
            "PK": f"USER#{user_id}",
            "SK": f"PLAN#{plan.week_start_date}",
            **plan.model_dump(by_alias=True, mode="json"),
        }
        if expected_revision is None:
            plan_condition = "attribute_not_exists(#pk)"
            plan_names = {"#pk": "PK"}
            plan_values: dict[str, Any] = {}
        else:
            plan_condition = (
                "#status = :draft AND #revision = :expected_revision"
            )
            plan_names = {
                "#status": "status",
                "#revision": "revision",
            }
            plan_values = {
                ":draft": PlanStatus.DRAFT.value,
                ":expected_revision": expected_revision,
            }
        plan_put: dict[str, Any] = {
            "TableName": self.table.name,
            "Item": plan_item,
            "ConditionExpression": plan_condition,
            "ExpressionAttributeNames": plan_names,
        }
        if plan_values:
            plan_put["ExpressionAttributeValues"] = plan_values
        if batch_entries is not None:
            ledger_items = self._batch_ledger_transaction_items(
                user_id,
                plan,
                batch_entries,
                expected_revision=expected_revision,
            )
            state_delete = {
                "Delete": {
                    "TableName": self.table.name,
                    "Key": self._conversation_key(user_id),
                    "ConditionExpression": (
                        "#request_id = :request_id AND "
                        "#revision = :expected_state_revision"
                    ),
                    "ExpressionAttributeNames": {
                        "#request_id": "request_id",
                        "#revision": "revision",
                    },
                    "ExpressionAttributeValues": {
                        ":request_id": request_id,
                        ":expected_state_revision": expected_state_revision,
                    },
                }
            }
            try:
                self._transact_write_with_retry(
                    [
                        {"Put": plan_put},
                        *ledger_items,
                        state_delete,
                    ]
                )
            except ClientError as exc:
                if self._is_transaction_conditional_failure(exc):
                    return False
                raise
            return True
        try:
            self._transact_write_with_retry(
                [
                    {"Put": plan_put},
                    {
                        "Delete": {
                            "TableName": self.table.name,
                            "Key": self._conversation_key(user_id),
                            "ConditionExpression": (
                                "#request_id = :request_id AND "
                                "#revision = :expected_state_revision"
                            ),
                            "ExpressionAttributeNames": {
                                "#request_id": "request_id",
                                "#revision": "revision",
                            },
                            "ExpressionAttributeValues": {
                                ":request_id": request_id,
                                ":expected_state_revision": (
                                    expected_state_revision
                                ),
                            },
                        }
                    },
                ]
            )
        except ClientError as exc:
            if self._is_transaction_conditional_failure(exc):
                return False
            raise
        return True

    def save_repaired_draft_once(
        self,
        user_id: str,
        plan: WeeklyPlan,
        *,
        expected_revision: int | None,
        repair_id: str,
        batch_entries: list[BatchLedgerEntry] | None = None,
    ) -> RepairPublicationOutcome:
        """Atomically publish one untracked repair and its replay marker."""
        if expected_revision is None:
            plan_condition = "attribute_not_exists(#pk)"
            plan_names = {"#pk": "PK"}
            plan_values: dict[str, Any] = {}
        else:
            plan_condition = (
                "#status = :draft AND #revision = :expected_revision"
            )
            plan_names = {
                "#status": "status",
                "#revision": "revision",
            }
            plan_values = {
                ":draft": PlanStatus.DRAFT.value,
                ":expected_revision": expected_revision,
            }
        plan_put: dict[str, Any] = {
            "TableName": self.table.name,
            "Item": {
                "PK": f"USER#{user_id}",
                "SK": f"PLAN#{plan.week_start_date}",
                **plan.model_dump(by_alias=True, mode="json"),
            },
            "ConditionExpression": plan_condition,
            "ExpressionAttributeNames": plan_names,
        }
        if plan_values:
            plan_put["ExpressionAttributeValues"] = plan_values
        marker_put = {
            "TableName": self.table.name,
            "Item": self._repair_marker_key(user_id, repair_id),
            "ConditionExpression": "attribute_not_exists(PK)",
        }
        if batch_entries is not None:
            ledger_items = self._batch_ledger_transaction_items(
                user_id,
                plan,
                batch_entries,
                expected_revision=expected_revision,
            )
            try:
                self._transact_write_with_retry(
                    [plan_put, *ledger_items, {"Put": marker_put}]
                )
            except ClientError as exc:
                outcome = self._repair_publication_outcome(exc)
                if outcome is None:
                    raise
                return outcome
            return RepairPublicationOutcome.PUBLISHED
        try:
            self._transact_write_with_retry(
                [{"Put": plan_put}, {"Put": marker_put}]
            )
        except ClientError as exc:
            outcome = self._repair_publication_outcome(exc)
            if outcome is None:
                raise
            return outcome
        return RepairPublicationOutcome.PUBLISHED

    @staticmethod
    def _repair_publication_outcome(
        exc: ClientError,
    ) -> RepairPublicationOutcome | None:
        """Classify only exact plan/marker conditional cancellations."""
        error = exc.response.get("Error", {})
        code = error.get("Code") if isinstance(error, dict) else None
        if code != "TransactionCanceledException":
            return None
        reasons = exc.response.get("CancellationReasons")
        if not isinstance(reasons, list) or len(reasons) < 2:
            return None
        codes = [
            reason.get("Code") if isinstance(reason, dict) else None
            for reason in reasons
        ]
        if codes[-1] == "ConditionalCheckFailed" and all(
            code in {"None", "ConditionalCheckFailed"} for code in codes[:-1]
        ):
            return RepairPublicationOutcome.DUPLICATE
        if codes[0] == "ConditionalCheckFailed" and all(
            code in {"None", "ConditionalCheckFailed"} for code in codes[1:]
        ):
            return RepairPublicationOutcome.STALE
        return None

    def replace_draft_and_clear_revision_state(
        self,
        user_id: str,
        plan: WeeklyPlan,
        *,
        expected_plan_revision: int,
        request_id: str,
        expected_state_revision: int,
        batch_entries: list[BatchLedgerEntry] | None = None,
        replaced_request_id: str | None = None,
    ) -> bool:
        """Publish a revision and remove its request in one transaction."""
        plan_item = {
            "PK": f"USER#{user_id}",
            "SK": f"PLAN#{plan.week_start_date}",
            **plan.model_dump(by_alias=True, mode="json"),
        }
        ledger_items = (
            self._batch_ledger_transaction_items(
                user_id,
                plan,
                batch_entries,
                expected_revision=expected_plan_revision,
                replaced_request_id=replaced_request_id,
            )
            if batch_entries is not None
            else []
        )
        try:
            self._transact_write_with_retry(
                [
                    {
                        "Put": {
                            "TableName": self.table.name,
                            "Item": plan_item,
                            "ConditionExpression": (
                                "#status = :draft AND "
                                "#revision = :expected_plan_revision"
                            ),
                            "ExpressionAttributeNames": {
                                "#status": "status",
                                "#revision": "revision",
                            },
                            "ExpressionAttributeValues": {
                                ":draft": PlanStatus.DRAFT.value,
                                ":expected_plan_revision": (
                                    expected_plan_revision
                                ),
                            },
                        }
                    },
                    *ledger_items,
                    {
                        "Delete": {
                            "TableName": self.table.name,
                            "Key": self._conversation_key(user_id),
                            "ConditionExpression": (
                                "#request_id = :request_id AND "
                                "#state_revision = :expected_state_revision"
                            ),
                            "ExpressionAttributeNames": {
                                "#request_id": "request_id",
                                "#state_revision": "revision",
                            },
                            "ExpressionAttributeValues": {
                                ":request_id": request_id,
                                ":expected_state_revision": (
                                    expected_state_revision
                                ),
                            },
                        }
                    },
                ]
            )
        except ClientError as exc:
            if self._is_transaction_conditional_failure(exc):
                return False
            raise
        return True

    # Descriptive alias retained for callers that name the state explicitly.
    replace_draft_and_clear_conversation_state = (
        replace_draft_and_clear_revision_state
    )

    def confirm_plan(
        self, user_id: str, week_start: str, expected_revision: int
    ) -> bool:
        """Atomically confirm a draft and start grocery generation."""
        try:
            self.table.meta.client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self.table.name,
                            "Key": {
                                "PK": f"USER#{user_id}",
                                "SK": f"PLAN#{week_start}",
                            },
                            "UpdateExpression": (
                                "SET #status = :confirmed, "
                                "#grocery_status = :pending, "
                                "#grocery_list = :empty"
                            ),
                            "ConditionExpression": (
                                "#status = :draft AND #revision = :revision"
                            ),
                            "ExpressionAttributeNames": {
                                "#status": "status",
                                "#revision": "revision",
                                "#grocery_status": "grocery_status",
                                "#grocery_list": "grocery_list",
                            },
                            "ExpressionAttributeValues": {
                                ":confirmed": PlanStatus.CONFIRMED.value,
                                ":draft": PlanStatus.DRAFT.value,
                                ":pending": "pending",
                                ":empty": [],
                                ":revision": expected_revision,
                            },
                        }
                    },
                    {
                        "Update": {
                            "TableName": self.table.name,
                            "Key": {
                                "PK": f"USER#{user_id}",
                                "SK": "PLAN_STATE",
                            },
                            "UpdateExpression": (
                                "SET #active_epoch = "
                                "if_not_exists(#active_epoch, :zero) + :one"
                            ),
                            "ExpressionAttributeNames": {
                                "#active_epoch": "active_epoch",
                            },
                            "ExpressionAttributeValues": {
                                ":zero": 0,
                                ":one": 1,
                            },
                        }
                    },
                ]
            )
        except ClientError as exc:
            if self._is_transaction_conditional_failure(exc):
                return False
            raise
        return True

    def retry_grocery(
        self, user_id: str, week_start: str, expected_revision: int
    ) -> bool:
        """Atomically retry grocery generation for an errored plan."""
        try:
            self.table.update_item(
                Key={
                    "PK": f"USER#{user_id}",
                    "SK": f"PLAN#{week_start}",
                },
                UpdateExpression=(
                    "SET #grocery_status = :pending, #grocery_list = :empty"
                ),
                ConditionExpression=(
                    "#status = :confirmed AND #grocery_status = :error "
                    "AND #revision = :revision"
                ),
                ExpressionAttributeNames={
                    "#status": "status",
                    "#grocery_status": "grocery_status",
                    "#revision": "revision",
                    "#grocery_list": "grocery_list",
                },
                ExpressionAttributeValues={
                    ":confirmed": PlanStatus.CONFIRMED.value,
                    ":error": "error",
                    ":pending": "pending",
                    ":empty": [],
                    ":revision": expected_revision,
                },
            )
        except ClientError as exc:
            if self._is_conditional_failure(exc):
                return False
            raise
        return True

    def update_meal(
        self,
        user_id: str,
        week_start: str,
        day: int,
        meal_type: str,
        meal: Any,
        expected_revision: int,
        *,
        expected_status: PlanStatus,
    ) -> bool:
        """Replace one meal with revision and status checks atomically."""
        plan = self.get_plan(user_id, week_start)
        if not plan:
            return False
        day_index: int | None = None
        meal_index: int | None = None
        for candidate_day_index, plan_day in enumerate(plan.days):
            if plan_day.day != day:
                continue
            for candidate_meal_index, candidate_meal in enumerate(
                plan_day.meals
            ):
                if candidate_meal.meal_type.value == meal_type.lower():
                    day_index = candidate_day_index
                    meal_index = candidate_meal_index
                    break
        if day_index is None or meal_index is None:
            return False
        update_expression = (
            f"SET #days[{day_index}].#meals[{meal_index}] = :meal, "
            "#revision = #revision + :one"
        )
        names = {
            "#days": "days",
            "#meals": "meals",
            "#revision": "revision",
        }
        values: dict[str, Any] = {
            ":meal": meal.model_dump(mode="json"),
            ":one": 1,
            ":revision": expected_revision,
        }
        condition = "#revision = :revision AND #status = :status"
        names["#status"] = "status"
        values[":status"] = expected_status.value
        if expected_status is PlanStatus.CONFIRMED:
            update_expression += (
                ", #grocery_status = :pending, #grocery_list = :empty"
            )
            names.update(
                {
                    "#grocery_status": "grocery_status",
                    "#grocery_list": "grocery_list",
                }
            )
            values.update({":pending": "pending", ":empty": []})
        try:
            self.table.update_item(
                Key={
                    "PK": f"USER#{user_id}",
                    "SK": f"PLAN#{week_start}",
                },
                UpdateExpression=update_expression,
                ConditionExpression=condition,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
        except ClientError as exc:
            if self._is_conditional_failure(exc):
                return False
            raise
        return True

    def complete_grocery(
        self,
        user_id: str,
        week_start: str,
        revision: int,
        grocery_list: list[Any],
    ) -> bool:
        """Publish groceries only for the worker's pending revision."""
        return self._update_grocery_state(
            user_id,
            week_start,
            revision,
            status="ready",
            grocery_list=grocery_list,
        )

    def fail_grocery(
        self, user_id: str, week_start: str, revision: int
    ) -> bool:
        """Mark a pending grocery job as failed if it is still current."""
        return self._update_grocery_state(
            user_id,
            week_start,
            revision,
            status="error",
            grocery_list=[],
        )

    def _update_grocery_state(
        self,
        user_id: str,
        week_start: str,
        revision: int,
        *,
        status: str,
        grocery_list: list[Any],
    ) -> bool:
        try:
            self.table.update_item(
                Key={
                    "PK": f"USER#{user_id}",
                    "SK": f"PLAN#{week_start}",
                },
                UpdateExpression=(
                    "SET #grocery_status = :status, #grocery_list = :list"
                ),
                ConditionExpression=(
                    "#status = :confirmed AND #grocery_status = :pending "
                    "AND #revision = :revision"
                ),
                ExpressionAttributeNames={
                    "#status": "status",
                    "#grocery_status": "grocery_status",
                    "#grocery_list": "grocery_list",
                    "#revision": "revision",
                },
                ExpressionAttributeValues={
                    ":confirmed": PlanStatus.CONFIRMED.value,
                    ":pending": "pending",
                    ":status": status,
                    ":list": [
                        section.model_dump(mode="json")
                        for section in grocery_list
                    ],
                    ":revision": revision,
                },
            )
        except ClientError as exc:
            if self._is_conditional_failure(exc):
                return False
            raise
        return True

    def get_plan(
        self,
        user_id: str,
        week_start: str | date,
        *,
        consistent_read: bool = False,
    ) -> Optional[WeeklyPlan]:
        """Return one exact plan, optionally using a consistent read."""
        week_key = (
            week_start.isoformat()
            if isinstance(week_start, date)
            else week_start
        )
        get_kwargs: dict[str, Any] = {
            "Key": {"PK": f"USER#{user_id}", "SK": f"PLAN#{week_key}"}
        }
        if consistent_read:
            get_kwargs["ConsistentRead"] = True
        response = self.table.get_item(**get_kwargs)
        item = response.get("Item")
        return WeeklyPlan.model_validate(self._data(item)) if item else None

    def get_latest_plan(self, user_id: str) -> Optional[WeeklyPlan]:
        response = self.table.query(
            KeyConditionExpression=Key("PK").eq(f"USER#{user_id}")
            & Key("SK").begins_with("PLAN#"),
            ScanIndexForward=False,
            Limit=1,
        )
        items = response.get("Items", [])
        return (
            WeeklyPlan.model_validate(self._data(items[0])) if items else None
        )

    def get_planned_batch_link(
        self, user_id: str, target_date: date, meal_type: MealType
    ) -> PlannedBatchLink | None:
        """Return one unambiguous batch link for a planned meal slot."""
        response = self.table.query(
            KeyConditionExpression=Key("PK").eq(f"USER#{user_id}")
            & Key("SK").begins_with("PLAN#"),
            ScanIndexForward=False,
            Limit=_PLANNED_BATCH_PLAN_QUERY_LIMIT,
        )
        candidates: list[tuple[int, int, date, PlannedBatchLink]] = []
        for item in response.get("Items", []):
            try:
                plan = WeeklyPlan.model_validate(self._data(item))
            except ValidationError:
                logger.warning(
                    "Skipping malformed plan item reason_code=malformed"
                )
                continue
            if not plan.week_start <= target_date <= plan.week_end:
                continue
            day_number = (target_date - plan.week_start).days + 1
            day = next(
                (
                    plan_day
                    for plan_day in plan.days
                    if plan_day.day == day_number
                ),
                None,
            )
            if day is None:
                continue
            matches = [
                meal.batch_link
                for meal in day.meals
                if meal.meal_type is meal_type and meal.batch_link is not None
            ]
            if len(matches) != 1:
                continue
            status_rank = int(plan.status is PlanStatus.CONFIRMED)
            candidates.append(
                (status_rank, plan.revision, plan.week_start, matches[0])
            )

        if not candidates:
            return None
        return max(candidates, key=lambda candidate: candidate[:3])[-1]

    def get_active_plan(
        self, user_id: str, on_date: date | None = None
    ) -> Optional[WeeklyPlan]:
        """Return the confirmed plan covering a given date."""
        target = on_date or date.today()
        response = self.table.query(
            KeyConditionExpression=Key("PK").eq(f"USER#{user_id}")
            & Key("SK").begins_with("PLAN#"),
            ScanIndexForward=False,
        )
        for item in response.get("Items", []):
            plan = WeeklyPlan.model_validate(self._data(item))
            if (
                plan.status is PlanStatus.CONFIRMED
                and plan.week_start <= target <= plan.week_end
            ):
                return plan
        return None

    def get_active_plan_snapshot(
        self, user_id: str, on_date: date | None = None
    ) -> ActivePlanSnapshot | None:
        """Return the active plan and strongly consistent epoch snapshot."""
        state_response = self.table.get_item(
            Key={"PK": f"USER#{user_id}", "SK": "PLAN_STATE"},
            ConsistentRead=True,
        )
        state_item = state_response.get("Item")
        active_epoch: int | None = None
        if state_item is not None:
            raw_epoch = state_item.get("active_epoch")
            if isinstance(raw_epoch, bool) or not isinstance(
                raw_epoch, (int, Decimal)
            ):
                raise ValueError("PLAN_STATE active_epoch must be an integer")
            active_epoch = int(raw_epoch)

        target = on_date or date.today()
        response = self.table.query(
            KeyConditionExpression=Key("PK").eq(f"USER#{user_id}")
            & Key("SK").begins_with("PLAN#"),
            ScanIndexForward=False,
            ConsistentRead=True,
        )
        for item in response.get("Items", []):
            plan = WeeklyPlan.model_validate(self._data(item))
            if (
                plan.status is PlanStatus.CONFIRMED
                and plan.week_start <= target <= plan.week_end
            ):
                return ActivePlanSnapshot(plan=plan, active_epoch=active_epoch)
        return None

    def update_meal_outcome(
        self,
        user_id: str,
        week_start: str,
        day: int,
        meal_type: str,
        outcome: MealOutcome,
        *,
        expected_epoch: int | None = None,
    ) -> bool:
        """Update an outcome only if the active-plan epoch is unchanged."""
        plan = self.get_plan(user_id, week_start)
        if not plan or plan.status is not PlanStatus.CONFIRMED:
            return False
        day_index: int | None = None
        meal_index: int | None = None
        for candidate_day_index, plan_day in enumerate(plan.days):
            if plan_day.day != day:
                continue
            for candidate_meal_index, meal in enumerate(plan_day.meals):
                if meal.meal_type.value == meal_type.lower():
                    day_index = candidate_day_index
                    meal_index = candidate_meal_index
                    break
        if day_index is None or meal_index is None:
            return False
        try:
            epoch_condition: dict[str, Any]
            if expected_epoch is None:
                epoch_condition = {
                    "ConditionExpression": "attribute_not_exists(#pk)",
                    "ExpressionAttributeNames": {"#pk": "PK"},
                }
                epoch_values: dict[str, Any] = {}
            else:
                epoch_condition = {
                    "ConditionExpression": "#active_epoch = :expected_epoch",
                    "ExpressionAttributeNames": {
                        "#active_epoch": "active_epoch"
                    },
                }
                epoch_values = {":expected_epoch": expected_epoch}
                epoch_condition["ExpressionAttributeValues"] = epoch_values

            plan_names = {
                "#days": "days",
                "#meals": "meals",
                "#outcome": "outcome",
                "#status": "status",
            }
            plan_values: dict[str, Any] = {
                ":outcome": outcome.value,
                ":confirmed": PlanStatus.CONFIRMED.value,
            }
            transaction_update = {
                "TableName": self.table.name,
                "Key": {
                    "PK": f"USER#{user_id}",
                    "SK": f"PLAN#{week_start}",
                },
                "UpdateExpression": (
                    f"SET #days[{day_index}].#meals[{meal_index}].#outcome "
                    "= :outcome"
                ),
                "ConditionExpression": "#status = :confirmed",
                "ExpressionAttributeNames": plan_names,
                "ExpressionAttributeValues": plan_values,
            }
            transaction_check = {
                "ConditionCheck": {
                    "TableName": self.table.name,
                    "Key": {
                        "PK": f"USER#{user_id}",
                        "SK": "PLAN_STATE",
                    },
                    **epoch_condition,
                }
            }
            self.table.meta.client.transact_write_items(
                TransactItems=[
                    {"Update": transaction_update},
                    transaction_check,
                ]
            )
        except ClientError as exc:
            if self._is_transaction_conditional_failure(exc):
                return False
            raise
        return True
