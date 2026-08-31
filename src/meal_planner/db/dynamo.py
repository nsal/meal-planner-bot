"""DynamoDB repository for profiles, meal history, and meal logs."""

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from boto3.dynamodb.conditions import Key  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from pydantic import ValidationError

from meal_planner.models.schemas import (
    ConversationState,
    ConversationWorkflowKind,
    ConversationWorkflowStep,
    MealLogEntry,
    ProfileDraft,
    ProfileEditOperation,
    UserProfile,
)

logger = logging.getLogger(__name__)

_CONVERSATION_IDENTITY_FIELDS = (
    "workflow_kind",
    "step",
    "revision",
    "created_at",
    "updated_at",
    "expires_at",
    "request_id",
    "session_id",
)
_PROFILE_SETUP_IDENTITY_FIELDS = (
    "workflow_kind",
    "step",
    "revision",
    "created_at",
    "updated_at",
    "expires_at",
    "last_update_id",
)


class TransactionConflictKind(str, Enum):
    """Bounded classification for conditional transaction cancellations."""

    STALE_WORK = "stale_work"
    DUPLICATE_SUBMISSION = "duplicate_submission"
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

        return UserProfile.model_validate(self._data(item))

    @staticmethod
    def _canonical_profile(profile: UserProfile) -> UserProfile:
        """Validate and normalize a profile before saving it."""
        return UserProfile.model_validate(
            profile.model_dump(mode="json", warnings=False)
        )

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
        prepared_profile = self._canonical_profile(profile)
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
        prepared_profile = self._canonical_profile(profile)
        if (
            observed_state.profile_operation is not ProfileEditOperation.REMOVE
            or observed_state.profile_category is None
            or next_state.step
            is not ConversationWorkflowStep.AWAITING_PROFILE_INPUT
            or next_state.profile_category
            is not observed_state.profile_category
            or next_state.profile_operation is not ProfileEditOperation.REMOVE
            or next_state.revision != observed_state.revision + 1
            or prepared_profile.profile_revision != expected_profile_revision
        ):
            return None
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

    def get_profile_draft(self, user_id: str) -> Optional[ProfileDraft]:
        """Return accumulated onboarding fields, if a draft exists."""
        response = self.table.get_item(
            Key={"PK": f"USER#{user_id}", "SK": "PROFILE_DRAFT"}
        )
        item = response.get("Item")
        if not item:
            return None
        return ProfileDraft.model_validate(self._data(item))

    def save_profile_draft(self, user_id: str, draft: ProfileDraft) -> None:
        self.table.put_item(
            Item={
                "PK": f"USER#{user_id}",
                "SK": "PROFILE_DRAFT",
                **draft.model_dump(mode="json"),
            }
        )

    def save_profile_draft_and_transition_state(
        self,
        user_id: str,
        draft: ProfileDraft,
        next_state: ConversationState,
        observed_state: ConversationState,
    ) -> bool:
        """Atomically save setup progress owned by the observed state."""
        setup_steps = {
            ConversationWorkflowStep.AWAITING_PROFILE_FAMILY_NAME,
            ConversationWorkflowStep.AWAITING_PROFILE_HOUSEHOLD_SIZE,
            ConversationWorkflowStep.AWAITING_PROFILE_MEMBERS,
            ConversationWorkflowStep.AWAITING_PROFILE_CONSTRAINTS,
            ConversationWorkflowStep.AWAITING_PROFILE_PREFERENCES,
        }
        if (
            observed_state.workflow_kind
            is not ConversationWorkflowKind.PROFILE_SETUP
            or observed_state.step not in setup_steps
            or next_state.workflow_kind
            is not ConversationWorkflowKind.PROFILE_SETUP
            or next_state.step not in setup_steps
            or next_state.revision != observed_state.revision + 1
        ):
            return False

        draft_item = {
            "PK": f"USER#{user_id}",
            "SK": "PROFILE_DRAFT",
            **draft.model_dump(mode="json"),
        }
        state_item = {
            **self._conversation_key(user_id),
            **next_state.model_dump(mode="json"),
        }
        condition, names, values = self._profile_setup_state_condition(
            observed_state
        )
        transact_items = [
            {
                "Put": {
                    "TableName": self.table.name,
                    "Item": draft_item,
                }
            },
            {
                "Put": {
                    "TableName": self.table.name,
                    "Item": state_item,
                    "ConditionExpression": condition,
                    "ExpressionAttributeNames": names,
                    "ExpressionAttributeValues": values,
                }
            },
        ]
        client_request_token = str(uuid4())
        for transaction_attempt in range(2):
            try:
                self.table.meta.client.transact_write_items(
                    TransactItems=transact_items,
                    ClientRequestToken=client_request_token,
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
                }:
                    return False
                raise
            return True
        return False

    def complete_profile_setup(
        self,
        user_id: str,
        profile: UserProfile,
        observed_state: ConversationState,
        *,
        expected_profile_revision: int | None,
    ) -> bool:
        """Atomically save a profile and consume its setup state."""
        if (
            observed_state.workflow_kind
            is not ConversationWorkflowKind.PROFILE_SETUP
            or observed_state.step
            is not ConversationWorkflowStep.AWAITING_PROFILE_PREFERENCES
        ):
            return False

        prepared_profile = self._canonical_profile(profile)
        committed_profile = prepared_profile.model_copy(
            update={
                "profile_revision": (
                    0
                    if expected_profile_revision is None
                    else expected_profile_revision + 1
                )
            }
        )
        profile_item = {
            "PK": f"USER#{user_id}",
            "SK": "PROFILE",
            **committed_profile.model_dump(mode="json"),
        }
        if expected_profile_revision is None:
            profile_condition = "attribute_not_exists(#pk)"
            profile_names = {"#pk": "PK"}
            profile_values: dict[str, Any] = {}
        else:
            revision_condition = (
                "(attribute_not_exists(#profile_revision) OR "
                "#profile_revision = :expected_profile_revision)"
                if expected_profile_revision == 0
                else "attribute_exists(#profile_revision) AND "
                "#profile_revision = :expected_profile_revision"
            )
            profile_condition = (
                "attribute_exists(#pk) AND " + revision_condition
            )
            profile_names = {
                "#pk": "PK",
                "#profile_revision": "profile_revision",
            }
            profile_values = {
                ":expected_profile_revision": expected_profile_revision,
            }

        state_condition, state_names, state_values = (
            self._profile_setup_state_condition(observed_state)
        )
        profile_put: dict[str, Any] = {
            "TableName": self.table.name,
            "Item": profile_item,
            "ConditionExpression": profile_condition,
            "ExpressionAttributeNames": profile_names,
        }
        if profile_values:
            profile_put["ExpressionAttributeValues"] = profile_values
        transact_items: list[dict[str, Any]] = [
            {"Put": profile_put},
            {
                "Delete": {
                    "TableName": self.table.name,
                    "Key": {
                        "PK": f"USER#{user_id}",
                        "SK": "PROFILE_DRAFT",
                    },
                }
            },
            {
                "Delete": {
                    "TableName": self.table.name,
                    "Key": self._conversation_key(user_id),
                    "ConditionExpression": state_condition,
                    "ExpressionAttributeNames": state_names,
                    "ExpressionAttributeValues": state_values,
                }
            },
        ]
        client_request_token = str(uuid4())
        for transaction_attempt in range(2):
            try:
                self.table.meta.client.transact_write_items(
                    TransactItems=transact_items,
                    ClientRequestToken=client_request_token,
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
                }:
                    return False
                raise
            return True
        return False

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
        try:
            state = ConversationState.model_validate(self._data(item))
        except ValidationError:
            logger.warning(
                "Discarding incompatible conversation state "
                "reason_code=incompatible_shape"
            )
            self._delete_incompatible_conversation_state(user_id, item)
            return None
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

    def _delete_incompatible_conversation_state(
        self, user_id: str, item: dict[str, Any]
    ) -> None:
        """Delete an incompatible item only if its identity is unchanged."""
        names: dict[str, str] = {}
        values: dict[str, Any] = {}
        conditions: list[str] = []
        for field in _CONVERSATION_IDENTITY_FIELDS:
            name = f"#{field}"
            names[name] = field
            if field not in item:
                conditions.append(f"attribute_not_exists({name})")
                continue

            value = item[field]
            if isinstance(value, (str, int, float, bool, Decimal)):
                value_name = f":{field}"
                values[value_name] = value
                conditions.append(f"{name} = {value_name}")
            else:
                type_name = f":{field}_type"
                values[type_name] = self._dynamodb_type_name(value)
                conditions.append(f"attribute_type({name}, {type_name})")

        try:
            self.table.delete_item(
                Key=self._conversation_key(user_id),
                ConditionExpression=" AND ".join(conditions),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
        except ClientError as exc:
            if not self._is_conditional_failure(exc):
                raise

    @staticmethod
    def _dynamodb_type_name(value: Any) -> str:
        """Return the DynamoDB type token for a guarded identity field."""
        if value is None:
            return "NULL"
        if isinstance(value, str):
            return "S"
        if isinstance(value, (int, float, Decimal)) and not isinstance(
            value, bool
        ):
            return "N"
        if isinstance(value, bool):
            return "BOOL"
        if isinstance(value, (bytes, bytearray)):
            return "B"
        if isinstance(value, list):
            return "L"
        if isinstance(value, dict):
            return "M"
        if isinstance(value, set):
            if not value:
                return "SS"
            first = next(iter(value))
            if isinstance(first, str):
                return "SS"
            if isinstance(first, (int, float, Decimal)):
                return "NS"
            return "BS"
        return "S"

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

    @staticmethod
    def _profile_setup_state_condition(
        observed_state: ConversationState,
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        """Build the complete ownership condition for profile setup."""
        observed_data = observed_state.model_dump(mode="json")
        conditions: list[str] = []
        names: dict[str, str] = {}
        values: dict[str, Any] = {}
        for field in _PROFILE_SETUP_IDENTITY_FIELDS:
            name = f"#observed_{field}"
            value_name = f":observed_{field}"
            conditions.append(f"{name} = {value_name}")
            names[name] = field
            values[value_name] = observed_data[field]
        return " AND ".join(conditions), names, values

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

    def confirm_meal_and_transition(
        self,
        user_id: str,
        entry: MealLogEntry,
        state: ConversationState,
        *,
        expected_revision: int,
        submission_id: str,
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
        ]
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
                }:
                    return False
                raise
            return True
        return False

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
        }

    @staticmethod
    def _classify_transaction_conflict(
        exc: ClientError,
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
        if len(reason_codes) == 2:
            if reason_codes[0] == "None" and reason_codes[1] == (
                "ConditionalCheckFailed"
            ):
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
