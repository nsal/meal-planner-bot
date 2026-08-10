"""DynamoDB repository implementation for meal planner bot."""

from typing import Any, Optional

from boto3.dynamodb.conditions import Key  # type: ignore[import-untyped]

from meal_planner.models.schemas import MealLogEntry, UserProfile, WeeklyPlan


class DynamoRepository:
    """Repository handling CRUD operations on single-table DynamoDB."""

    def __init__(self, table: Any) -> None:
        """Initialize repository with a boto3 DynamoDB Table resource."""
        self.table = table

    def get_profile(self, user_id: str) -> Optional[UserProfile]:
        """Fetch user profile by user_id."""
        response = self.table.get_item(
            Key={"PK": f"USER#{user_id}", "SK": "PROFILE"}
        )
        item = response.get("Item")
        if not item:
            return None
        data = {k: v for k, v in item.items() if k not in ("PK", "SK")}
        return UserProfile.model_validate(data)

    def save_profile(self, user_id: str, profile: UserProfile) -> None:
        """Upsert user profile entity."""
        data = profile.model_dump()
        item = {"PK": f"USER#{user_id}", "SK": "PROFILE", **data}
        self.table.put_item(Item=item)

    def log_meal(self, user_id: str, entry: MealLogEntry) -> None:
        """Log a meal entry for a user."""
        data = entry.model_dump()
        item = {
            "PK": f"USER#{user_id}",
            "SK": f"MEAL#{entry.date}#{entry.meal_type}",
            **data,
        }
        self.table.put_item(Item=item)

    def get_meal_history(
        self, user_id: str, days: int = 14
    ) -> list[MealLogEntry]:
        """Query meal history for a user, returned sorted by date."""
        response = self.table.query(
            KeyConditionExpression=Key("PK").eq(f"USER#{user_id}")
            & Key("SK").begins_with("MEAL#"),
            ScanIndexForward=False,
        )
        items = response.get("Items", [])
        entries: list[MealLogEntry] = []
        for item in items:
            data = {k: v for k, v in item.items() if k not in ("PK", "SK")}
            entries.append(MealLogEntry.model_validate(data))
        entries.sort(key=lambda x: x.date, reverse=True)
        return entries[:days]

    def save_plan(self, user_id: str, plan: WeeklyPlan) -> None:
        """Save weekly plan entity."""
        data = plan.model_dump(by_alias=True)
        item = {
            "PK": f"USER#{user_id}",
            "SK": f"PLAN#{plan.week_start_date}",
            **data,
        }
        self.table.put_item(Item=item)

    def get_current_plan(self, user_id: str) -> Optional[WeeklyPlan]:
        """Fetch the most recent weekly plan for a user."""
        response = self.table.query(
            KeyConditionExpression=Key("PK").eq(f"USER#{user_id}")
            & Key("SK").begins_with("PLAN#"),
            ScanIndexForward=False,
            Limit=1,
        )
        items = response.get("Items", [])
        if not items:
            return None
        item = items[0]
        data = {k: v for k, v in item.items() if k not in ("PK", "SK")}
        return WeeklyPlan.model_validate(data)

    def update_meal_status(
        self,
        user_id: str,
        week_start: str,
        day: int,
        meal_type: str,
        was_cooked: bool,
    ) -> bool:
        """Update was_cooked status for a specific meal in a weekly plan."""
        response = self.table.get_item(
            Key={"PK": f"USER#{user_id}", "SK": f"PLAN#{week_start}"}
        )
        item = response.get("Item")
        if not item:
            return False

        data = {k: v for k, v in item.items() if k not in ("PK", "SK")}
        plan = WeeklyPlan.model_validate(data)

        updated = False
        for plan_day in plan.days:
            if plan_day.day == day:
                for meal in plan_day.meals:
                    if meal.meal_type.lower() == meal_type.lower():
                        meal.was_cooked = was_cooked
                        updated = True

        if updated:
            self.save_plan(user_id, plan)

        return updated
