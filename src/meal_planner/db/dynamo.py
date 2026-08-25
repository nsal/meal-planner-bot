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
    ConversationState,
    ConversationWorkflowKind,
    ConversationWorkflowStep,
    MealLogEntry,
    MealOutcome,
    PlanStatus,
    ProfileEditCategory,
    ProfileUpdateEntities,
    UserProfile,
    WeeklyPlan,
    canonicalize_profile_rule_ids,
)

logger = logging.getLogger(__name__)


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
        return (
            canonicalize_profile_rule_ids(
                UserProfile.model_validate(self._data(item))
            )
            if item
            else None
        )

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
        try:
            self.table.meta.client.transact_write_items(
                TransactItems=transact_items
            )
        except ClientError as exc:
            if self._is_transaction_conditional_failure(exc):
                return False
            raise
        return True

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
        try:
            self.table.meta.client.transact_write_items(
                TransactItems=[
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
                                ":meal_log": (
                                    ConversationWorkflowKind.MEAL_LOG.value
                                ),
                                ":awaiting_confirmation": (
                                    ConversationWorkflowStep.AWAITING_MEAL_CONFIRMATION.value
                                ),
                                ":expected_revision": expected_revision,
                                ":request_id": submission_id,
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
                except ValidationError as exc:
                    logger.warning(
                        "Skipping malformed meal history item: %s", exc
                    )
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            query_kwargs["ExclusiveStartKey"] = last_key
        entries.sort(key=lambda entry: entry.created_at, reverse=True)
        return entries

    def save_plan(self, user_id: str, plan: WeeklyPlan) -> None:
        self.table.put_item(
            Item={
                "PK": f"USER#{user_id}",
                "SK": f"PLAN#{plan.week_start_date}",
                **plan.model_dump(by_alias=True, mode="json"),
            }
        )

    @staticmethod
    def _is_conditional_failure(exc: ClientError) -> bool:
        error = exc.response.get("Error", {})
        code = error.get("Code") if isinstance(error, dict) else None
        return code == "ConditionalCheckFailedException"

    @staticmethod
    def _is_transaction_conditional_failure(exc: ClientError) -> bool:
        """Return whether a transaction failed only on an expected condition."""
        error = exc.response.get("Error", {})
        code = error.get("Code") if isinstance(error, dict) else None
        if code != "TransactionCanceledException":
            return False
        reasons = exc.response.get("CancellationReasons")
        if not isinstance(reasons, list):
            return False
        reason_codes = {
            reason.get("Code") for reason in reasons if isinstance(reason, dict)
        }
        return "ConditionalCheckFailed" in reason_codes and reason_codes <= {
            "ConditionalCheckFailed",
            "None",
        }

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
        try:
            self.table.meta.client.transact_write_items(
                TransactItems=[
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
        try:
            self.table.meta.client.transact_write_items(
                TransactItems=[{"Put": plan_put}, {"Put": marker_put}]
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
        if not isinstance(reasons, list) or len(reasons) != 2:
            return None
        first = reasons[0] if isinstance(reasons[0], dict) else {}
        second = reasons[1] if isinstance(reasons[1], dict) else {}
        first_code = first.get("Code")
        second_code = second.get("Code")
        if second_code == "ConditionalCheckFailed" and first_code in {
            "None",
            "ConditionalCheckFailed",
        }:
            return RepairPublicationOutcome.DUPLICATE
        if first_code == "ConditionalCheckFailed" and second_code == "None":
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
    ) -> bool:
        """Publish a revision and remove its request in one transaction."""
        plan_item = {
            "PK": f"USER#{user_id}",
            "SK": f"PLAN#{plan.week_start_date}",
            **plan.model_dump(by_alias=True, mode="json"),
        }
        try:
            self.table.meta.client.transact_write_items(
                TransactItems=[
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
