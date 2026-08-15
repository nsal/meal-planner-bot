"""DynamoDB repository for profiles, meal history, and weekly plans."""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from boto3.dynamodb.conditions import Key  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from pydantic import ValidationError

from meal_planner.models.schemas import (
    ConversationState,
    MealLogEntry,
    MealOutcome,
    PlanStatus,
    ProfileUpdateEntities,
    UserProfile,
    WeeklyPlan,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActivePlanSnapshot:
    """Active plan and the activity epoch observed with it."""

    plan: WeeklyPlan
    active_epoch: int | None


class DynamoRepository:
    """Repository handling CRUD operations on a single DynamoDB table."""

    def __init__(self, table: Any) -> None:
        self.table = table

    @staticmethod
    def _data(item: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value for key, value in item.items() if key not in {"PK", "SK"}
        }

    def get_profile(self, user_id: str) -> Optional[UserProfile]:
        response = self.table.get_item(
            Key={"PK": f"USER#{user_id}", "SK": "PROFILE"}
        )
        item = response.get("Item")
        return UserProfile.model_validate(self._data(item)) if item else None

    def save_profile(self, user_id: str, profile: UserProfile) -> None:
        self.table.put_item(
            Item={
                "PK": f"USER#{user_id}",
                "SK": "PROFILE",
                **profile.model_dump(mode="json"),
            }
        )

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
        self, user_id: str, *, now: datetime | None = None
    ) -> ConversationState | None:
        """Return non-expired conversation state, if present."""
        response = self.table.get_item(Key=self._conversation_key(user_id))
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
    ) -> bool:
        """Create or replace state only when its revision is current."""
        item = {
            **self._conversation_key(user_id),
            **state.model_dump(mode="json"),
        }
        if expected_revision is None:
            condition = "attribute_not_exists(#pk)"
            names = {"#pk": "PK"}
            values: dict[str, Any] = {}
        else:
            condition = "#revision = :expected_revision"
            names = {"#revision": "revision"}
            values = {":expected_revision": expected_revision}
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
    ) -> bool:
        """Atomically persist a state transition from one revision."""
        return self.save_conversation_state(
            user_id, state, expected_revision=expected_revision
        )

    def delete_conversation_state(
        self, user_id: str, *, expected_revision: int | None = None
    ) -> bool:
        """Delete state conditionally, preventing stale workflow cleanup."""
        kwargs: dict[str, Any] = {"Key": self._conversation_key(user_id)}
        if expected_revision is not None:
            kwargs.update(
                {
                    "ConditionExpression": "#revision = :revision",
                    "ExpressionAttributeNames": {"#revision": "revision"},
                    "ExpressionAttributeValues": {
                        ":revision": expected_revision
                    },
                }
            )
        try:
            self.table.delete_item(**kwargs)
        except ClientError as exc:
            if self._is_conditional_failure(exc):
                return False
            raise
        return True

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
        return self.save_conversation_state(
            user_id, state, expected_revision=expected_revision
        )

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
