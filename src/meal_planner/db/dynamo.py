"""DynamoDB repository for profiles, meal history, and weekly plans."""

import logging
from datetime import date, timedelta
from typing import Any, Optional

from boto3.dynamodb.conditions import Key  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from pydantic import ValidationError

from meal_planner.models.schemas import (
    MealLogEntry,
    MealOutcome,
    PlanStatus,
    ProfileUpdateEntities,
    UserProfile,
    WeeklyPlan,
)

logger = logging.getLogger(__name__)


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

    def log_meal(self, user_id: str, entry: MealLogEntry) -> None:
        """Store a meal without overwriting another meal of the same type."""
        created_key = entry.created_at.isoformat()
        self.table.put_item(
            Item={
                "PK": f"USER#{user_id}",
                "SK": (
                    f"MEAL#{entry.date_key}#{created_key}#"
                    f"{entry.meal_type.value}"
                ),
                **entry.model_dump(mode="json"),
            }
        )

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

    def save_generated_draft(self, user_id: str, plan: WeeklyPlan) -> bool:
        """Save a generated draft unless the week is already confirmed."""
        try:
            self.table.put_item(
                Item={
                    "PK": f"USER#{user_id}",
                    "SK": f"PLAN#{plan.week_start_date}",
                    **plan.model_dump(by_alias=True, mode="json"),
                },
                ConditionExpression=(
                    "attribute_not_exists(#status) OR #status = :draft"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":draft": PlanStatus.DRAFT.value,
                },
            )
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
            self.table.update_item(
                Key={
                    "PK": f"USER#{user_id}",
                    "SK": f"PLAN#{week_start}",
                },
                UpdateExpression=(
                    "SET #status = :confirmed, "
                    "#grocery_status = :pending, "
                    "#grocery_list = :empty"
                ),
                ConditionExpression=(
                    "#status = :draft AND #revision = :revision"
                ),
                ExpressionAttributeNames={
                    "#status": "status",
                    "#revision": "revision",
                    "#grocery_status": "grocery_status",
                    "#grocery_list": "grocery_list",
                },
                ExpressionAttributeValues={
                    ":confirmed": PlanStatus.CONFIRMED.value,
                    ":draft": PlanStatus.DRAFT.value,
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
        self, user_id: str, week_start: str | date
    ) -> Optional[WeeklyPlan]:
        week_key = (
            week_start.isoformat()
            if isinstance(week_start, date)
            else week_start
        )
        response = self.table.get_item(
            Key={"PK": f"USER#{user_id}", "SK": f"PLAN#{week_key}"}
        )
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

    def update_meal_outcome(
        self,
        user_id: str,
        week_start: str,
        day: int,
        meal_type: str,
        outcome: MealOutcome,
    ) -> bool:
        """Atomically update one nested outcome without rewriting the plan."""
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
            self.table.update_item(
                Key={
                    "PK": f"USER#{user_id}",
                    "SK": f"PLAN#{week_start}",
                },
                UpdateExpression=(
                    f"SET #days[{day_index}].#meals[{meal_index}].#outcome "
                    "= :outcome"
                ),
                ConditionExpression="#status = :confirmed",
                ExpressionAttributeNames={
                    "#days": "days",
                    "#meals": "meals",
                    "#outcome": "outcome",
                    "#status": "status",
                },
                ExpressionAttributeValues={
                    ":outcome": outcome.value,
                    ":confirmed": PlanStatus.CONFIRMED.value,
                },
            )
        except ClientError as exc:
            if (
                exc.response.get("Error", {}).get("Code")
                == "ConditionalCheckFailedException"
            ):
                return False
            raise
        return True
