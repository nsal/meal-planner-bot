"""Asynchronous Lambda workflows for plans and grocery finalization."""

import json
import logging
import os
import re
import signal
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from types import FrameType
from typing import Any, Optional

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from pydantic import ValidationError

from meal_planner.config import (
    DEFAULT_PLANNER_REPAIR_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_PLANNER_REPAIR_READ_TIMEOUT_SECONDS,
    get_planner_settings,
)
from meal_planner.db.dynamo import (
    DynamoRepository,
    RepairPublicationOutcome,
)
from meal_planner.llm.client import (
    LLMClient,
    LLMFailure,
    LLMPermanentError,
    LLMResponseFormatError,
    LLMTimeoutError,
    LLMTransientError,
)
from meal_planner.llm.parser import (
    PlanResponseFeedback,
    SafeValidationIssue,
    parse_grocery_response,
    parse_plan_response_with_metadata,
)
from meal_planner.llm.prompts import (
    build_grocery_prompt,
    build_plan_prompt,
    build_plan_revision_prompt,
)
from meal_planner.models.schemas import (
    ConversationState,
    ConversationWorkflowKind,
    ConversationWorkflowStep,
    GroceryStatus,
    MealOutcome,
    PlanGenerationContext,
    PlanRevisionContext,
    PlanStatus,
    PreferenceRequirement,
    WeeklyPlan,
)
from meal_planner.preferences import (
    PlanValidationResult,
    format_satisfaction_summary,
    format_unmet_preference_clauses,
    validate_generated_plan,
)
from meal_planner.telegram.api import TelegramAPI

logger = logging.getLogger(__name__)

GENERATE_PLAN = "generate_plan"
FINALIZE_GROCERY = "finalize_grocery"
REVISE_PLAN = "revise_plan"
_MODEL_LABEL_MAX_LENGTH = 64
_MODEL_LABEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:+-]*")


@dataclass(frozen=True, slots=True)
class _GenerationAttempt:
    """Result of the single provider call made by one Planner invocation."""

    plan: WeeklyPlan | None = None
    feedback: str | None = None
    failure_reason: str | None = None
    failure_category: str | None = None
    validation_metadata: tuple[tuple[str, str], ...] = ()


class PlannerDeadlineExceeded(BaseException):
    """Raised when a planner invocation exceeds its application deadline."""


@contextmanager
def planner_deadline(timeout_seconds: float) -> Iterator[None]:
    """Interrupt planner execution when its application deadline is reached."""
    if timeout_seconds <= 0:
        raise ValueError("Planner deadline must be positive")

    def raise_deadline(_signum: int, _frame: FrameType | None) -> None:
        raise PlannerDeadlineExceeded

    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, raise_deadline)
    signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous_handler)


class PlannerHandler:
    """Manage asynchronous plan generation and grocery finalization."""

    def __init__(
        self,
        repo: DynamoRepository,
        telegram_api: TelegramAPI,
        llm_client: Optional[LLMClient] = None,
        max_attempts: int = 1,
        *,
        grocery_llm_client: Optional[LLMClient] = None,
        lambda_client: Any | None = None,
        repair_connect_timeout_seconds: float = (
            DEFAULT_PLANNER_REPAIR_CONNECT_TIMEOUT_SECONDS
        ),
        repair_read_timeout_seconds: float = (
            DEFAULT_PLANNER_REPAIR_READ_TIMEOUT_SECONDS
        ),
    ) -> None:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self.repo = repo
        self.telegram_api = telegram_api
        self.llm_client = llm_client
        self.grocery_llm_client = grocery_llm_client
        self.lambda_client = lambda_client
        self.repair_connect_timeout_seconds = repair_connect_timeout_seconds
        self.repair_read_timeout_seconds = repair_read_timeout_seconds
        self.max_attempts = max_attempts

    def generate_plan(
        self,
        user_id: str,
        chat_id: int | str,
        *,
        week_start: date | None = None,
        preference: str | None = None,
        requirements: list[PreferenceRequirement] | None = None,
        attempt: int = 1,
        repair_feedback: str | None = None,
        request_id: str | None = None,
        state_revision: int | None = None,
        repair_id: str | None = None,
    ) -> None:
        """Generate and persist a draft plan without a grocery list."""
        started_at = time.monotonic()
        model = self._model_name(self.llm_client)
        try:
            generation_context = PlanGenerationContext(
                preference=preference,
                requirements=requirements or [],
                attempt=attempt,
                repair_feedback=repair_feedback,
                request_id=request_id,
                state_revision=state_revision,
                repair_id=repair_id,
            )
            if request_id is not None and state_revision is not None:
                current_state = self.repo.get_conversation_state(
                    user_id, consistent_read=True
                )
                if not self._request_matches(
                    current_state, request_id, state_revision
                ):
                    logger.info("Discarded stale planner request")
                    return
            profile = self.repo.get_profile(user_id)
            if not profile or not profile.is_complete:
                self.telegram_api.send_message(
                    chat_id,
                    "Complete your profile before generating a meal plan.",
                )
                return
            target_week = week_start or date.today()
            current_plan = self.repo.get_plan(
                user_id, target_week, consistent_read=True
            )
            if current_plan and current_plan.status is PlanStatus.CONFIRMED:
                if request_id and state_revision is not None:
                    self.repo.clear_conversation_state_if_matches(
                        user_id,
                        request_id=request_id,
                        expected_revision=state_revision,
                    )
                self.telegram_api.send_message(
                    chat_id,
                    "That week's plan is already confirmed, so I kept it "
                    "unchanged.",
                )
                return
            client = self.llm_client or LLMClient()
            prompt = build_plan_prompt(
                profile=profile,
                meal_history=self.repo.get_meal_history(user_id, days=14),
                previous_plan=self.repo.get_latest_plan(user_id),
                week_start=target_week.isoformat(),
                preference=generation_context.preference,
                requirements=generation_context.requirements,
                repair_feedback=generation_context.repair_feedback,
            )
            generation = self._generate_once(
                client,
                prompt,
                target_week,
                attempt=generation_context.attempt,
            )
            if generation.plan is None:
                if generation.feedback and generation_context.attempt == 1:
                    repair_status = self._schedule_repair(
                        user_id,
                        chat_id,
                        target_week,
                        generation_context,
                        generation.feedback,
                    )
                    if repair_status is True or repair_status is None:
                        return
                self._finish_failed_generation(
                    user_id,
                    chat_id,
                    request_id=request_id,
                    state_revision=state_revision,
                    attempt=generation_context.attempt,
                    started_at=started_at,
                    reason=generation.failure_reason,
                    failure_category=generation.failure_category,
                    validation_metadata=generation.validation_metadata,
                    requirements=generation_context.requirements,
                )
                return
            plan = generation.plan
            if request_id and state_revision is not None:
                current_state = self.repo.get_conversation_state(
                    user_id, consistent_read=True
                )
                if not self._request_matches(
                    current_state, request_id, state_revision
                ):
                    logger.info("Discarded stale planner request")
                    return
            validation = validate_generated_plan(
                plan, generation_context.requirements
            )
            if not validation.is_valid:
                if generation_context.attempt == 1:
                    repair_status = self._schedule_repair(
                        user_id,
                        chat_id,
                        target_week,
                        generation_context,
                        self._validation_feedback(validation),
                    )
                    if repair_status is True or repair_status is None:
                        return
                self._finish_failed_generation(
                    user_id,
                    chat_id,
                    request_id=request_id,
                    state_revision=state_revision,
                    attempt=generation_context.attempt,
                    started_at=started_at,
                    reason=None,
                    failure_category=self._validation_category(validation),
                    validation=validation,
                    requirements=generation_context.requirements,
                )
                return
            plan.status = PlanStatus.DRAFT
            plan.revision = (
                0 if current_plan is None else current_plan.revision + 1
            )
            plan.grocery_status = GroceryStatus.NOT_REQUESTED
            plan.grocery_list = []
            plan.planning_instructions = [preference] if preference else []
            for plan_day in plan.days:
                for meal in plan_day.meals:
                    meal.outcome = MealOutcome.UNREPORTED
            expected_revision = (
                None if current_plan is None else current_plan.revision
            )
            tracked_request = (
                request_id is not None and state_revision is not None
            )
            repair_outcome: RepairPublicationOutcome | None = None
            if tracked_request:
                assert request_id is not None
                assert state_revision is not None
                published = (
                    self.repo.save_generated_draft_and_clear_conversation_state(
                        user_id,
                        plan,
                        expected_revision=expected_revision,
                        request_id=request_id,
                        expected_state_revision=state_revision,
                    )
                )
            else:
                if generation_context.repair_id is None:
                    published = self.repo.save_generated_draft(
                        user_id, plan, expected_revision=expected_revision
                    )
                else:
                    repair_outcome = self.repo.save_repaired_draft_once(
                        user_id,
                        plan,
                        expected_revision=expected_revision,
                        repair_id=generation_context.repair_id,
                    )
                    published = (
                        repair_outcome is RepairPublicationOutcome.PUBLISHED
                    )
            if not published:
                if repair_outcome is RepairPublicationOutcome.DUPLICATE:
                    logger.info("Ignored duplicate planner repair")
                    return
                logger.info("Discarded stale generated plan")
                if not tracked_request:
                    self.telegram_api.send_message(
                        chat_id,
                        "That week's plan changed while I was generating it, "
                        "so I discarded the stale result.",
                    )
                return
        except Exception:
            self._log_safe_failure(
                attempt=attempt,
                started_at=started_at,
                model=model,
                category="internal",
            )
            self._notify_failure(
                chat_id,
                "Sorry, an error occurred while generating your plan.",
                attempt=attempt,
            )
            return
        delivery_started_at = time.monotonic()
        try:
            self.telegram_api.send_plan(chat_id, plan)
            if validation.requirements:
                self.telegram_api.send_message(
                    chat_id,
                    format_satisfaction_summary(
                        validation, generation_context.requirements
                    ),
                )
            self.telegram_api.send_message(
                chat_id,
                "Review this draft, request edits, then tell me to confirm it.",
            )
        except Exception:
            self._log_safe_failure(
                attempt=attempt,
                started_at=delivery_started_at,
                model=model,
                category="delivery",
            )

    def _generate_once(
        self,
        client: LLMClient,
        prompt: str,
        target_week: date,
        *,
        attempt: int = 1,
    ) -> _GenerationAttempt:
        """Make exactly one provider call for this Planner invocation."""
        started_at = time.monotonic()
        try:
            raw = self._strict_json_call(
                client, prompt, "Generate weekly meal plan"
            )
        except LLMFailure as exc:
            self._log_llm_failure(
                client,
                attempt=attempt,
                started_at=started_at,
                failure=exc,
            )
            if isinstance(exc, LLMTimeoutError):
                reason = "timed out"
            elif isinstance(exc, LLMTransientError):
                reason = "temporarily unavailable"
            elif isinstance(exc, LLMPermanentError):
                reason = "rejected the request"
            elif isinstance(exc, LLMResponseFormatError):
                reason = "returned an invalid response format"
            else:
                reason = self._invalid_plan_message()
            return _GenerationAttempt(
                failure_reason=reason,
                failure_category=self._llm_failure_category(exc),
            )

        plan, feedback = parse_plan_response_with_metadata(raw)
        if plan and plan.week_start == target_week:
            return _GenerationAttempt(plan=plan)
        if plan is not None:
            feedback = PlanResponseFeedback(
                category="structural",
                issues=(SafeValidationIssue("wrong_week", "week_start_date"),),
            )
        if feedback is None:
            feedback = PlanResponseFeedback(
                category="structural",
                issues=(SafeValidationIssue("schema_validation", "$"),),
            )
        metadata = tuple(
            (issue.code, issue.location) for issue in feedback.issues
        )
        return _GenerationAttempt(
            feedback=feedback.render(),
            failure_category=feedback.category,
            validation_metadata=metadata,
        )

    def _generate_with_bounded_repair(
        self,
        client: LLMClient,
        prompt: str,
        target_week: date,
        chat_id: int | str,
        failure_mode: str = "initial",
    ) -> WeeklyPlan | None:
        """Keep revisions to one provider call per Planner invocation."""
        generation = self._generate_once(client, prompt, target_week)
        if generation.plan is not None:
            return generation.plan
        reason = generation.failure_reason or self._invalid_plan_message()
        self._notify_failure(
            chat_id,
            self._generation_failure_message(failure_mode, reason),
            attempt=1,
        )
        return None

    @staticmethod
    def _validation_feedback(validation: PlanValidationResult) -> str:
        """Return bounded, coded feedback suitable for repair transport."""
        feedback = "; ".join(
            "code={} location={}".format(
                issue.code,
                PlannerHandler._validation_location(issue),
            )
            for issue in validation.issues
        )
        return (feedback or "code=validation_required location=$")[:800]

    @staticmethod
    def _validation_location(issue: Any) -> str:
        """Return a safe schema location for a domain validation issue."""
        if issue.day is not None and issue.meal_type is not None:
            return f"days[{issue.day - 1}].meals.{issue.meal_type.value}"
        if issue.requirement_id is not None:
            return "requirements"
        return "plan"

    @staticmethod
    def _validation_category(validation: PlanValidationResult) -> str:
        """Classify validation failures for stable terminal messaging."""
        if any(
            issue.code.startswith("requirement_") for issue in validation.issues
        ):
            return "compliance"
        if any(
            issue.code
            in {"impossible_requirement_count", "duplicate_requirement_id"}
            for issue in validation.issues
        ):
            return "compliance"
        return "completeness"

    @staticmethod
    def _llm_failure_category(failure: LLMFailure) -> str:
        """Return the bounded category used in logs and user outcomes."""
        if isinstance(failure, LLMTimeoutError):
            return "timeout"
        if isinstance(failure, LLMTransientError):
            return "transient"
        if isinstance(failure, LLMPermanentError):
            return "permanent"
        if isinstance(failure, LLMResponseFormatError):
            return "response_format"
        return "failure"

    @staticmethod
    def _model_name(client: LLMClient | None) -> str:
        """Return a bounded model label without exposing client content."""
        model = getattr(client, "model", "unknown")
        if (
            isinstance(model, str)
            and 0 < len(model) <= _MODEL_LABEL_MAX_LENGTH
            and _MODEL_LABEL_PATTERN.fullmatch(model) is not None
        ):
            return model
        return "unknown"

    @staticmethod
    def _log_safe_failure(
        *,
        attempt: int,
        started_at: float,
        model: str,
        category: str,
        validation_metadata: tuple[tuple[str, str], ...] = (),
    ) -> None:
        """Emit only bounded operational failure metadata."""
        elapsed_ms = max((time.monotonic() - started_at) * 1000.0, 0.0)
        metadata = ";".join(
            f"code={code} location={location}"
            for code, location in validation_metadata[:6]
        )[:400]
        logger.warning(
            "Planner LLM attempt failed attempt=%d elapsed_ms=%.1f model=%s "
            "category=%s validation=%s",
            attempt,
            elapsed_ms,
            model,
            category,
            metadata or "none",
            extra={
                "attempt": attempt,
                "elapsed_ms": elapsed_ms,
                "model": model,
                "category": category,
                "validation": metadata,
            },
        )

    def _schedule_repair(
        self,
        user_id: str,
        chat_id: int | str,
        target_week: date,
        context: PlanGenerationContext,
        feedback: str,
    ) -> bool | None:
        """Queue one fresh attempt, or no-op when the request is stale."""
        if context.attempt != 1:
            return False
        if context.request_id is not None:
            ownership_started_at = time.monotonic()
            try:
                state = self.repo.get_conversation_state(
                    user_id, consistent_read=True
                )
            except Exception:
                PlannerHandler._log_safe_failure(
                    attempt=context.attempt,
                    started_at=ownership_started_at,
                    model=PlannerHandler._model_name(self.llm_client),
                    category="repair_ownership",
                )
                return False
            state_revision = context.state_revision
            if state_revision is None or not self._request_matches(
                state, context.request_id, state_revision
            ):
                logger.info("Suppressed stale planner repair request")
                return None
        elif context.repair_id is None:
            PlannerHandler._log_safe_failure(
                attempt=context.attempt,
                started_at=time.monotonic(),
                model=PlannerHandler._model_name(self.llm_client),
                category="repair_dispatch",
            )
            return False

        dispatch_started_at = time.monotonic()
        function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
        if not function_name:
            PlannerHandler._log_safe_failure(
                attempt=context.attempt,
                started_at=dispatch_started_at,
                model=PlannerHandler._model_name(self.llm_client),
                category="repair_dispatch",
            )
            return False
        payload = {
            "action": GENERATE_PLAN,
            "user_id": user_id,
            "chat_id": chat_id,
            "week_start": target_week.isoformat(),
            "preference": context.preference,
            "requirements": [
                requirement.model_dump(mode="json")
                for requirement in context.requirements
            ],
            "attempt": 2,
            "repair_feedback": feedback[:800],
            "request_id": context.request_id,
            "state_revision": context.state_revision,
            "repair_id": (
                None if context.request_id is not None else context.repair_id
            ),
        }
        try:
            client = self.lambda_client
            if client is None:
                client = boto3.client(
                    "lambda",
                    config=Config(
                        connect_timeout=self.repair_connect_timeout_seconds,
                        read_timeout=self.repair_read_timeout_seconds,
                        retries={
                            "mode": "standard",
                            "total_max_attempts": 1,
                        },
                    ),
                )
            response = client.invoke(
                FunctionName=function_name,
                InvocationType="Event",
                Payload=json.dumps(payload),
            )
        except Exception:
            PlannerHandler._log_safe_failure(
                attempt=context.attempt,
                started_at=dispatch_started_at,
                model=PlannerHandler._model_name(self.llm_client),
                category="repair_dispatch",
            )
            return False
        if response.get("StatusCode") != 202:
            PlannerHandler._log_safe_failure(
                attempt=context.attempt,
                started_at=dispatch_started_at,
                model=PlannerHandler._model_name(self.llm_client),
                category="repair_dispatch",
            )
            return False
        return True

    def _finish_failed_generation(
        self,
        user_id: str,
        chat_id: int | str,
        *,
        request_id: str | None,
        state_revision: int | None,
        attempt: int,
        started_at: float,
        reason: str | None,
        failure_category: str | None = None,
        validation: PlanValidationResult | None = None,
        validation_metadata: tuple[tuple[str, str], ...] = (),
        requirements: list[PreferenceRequirement] | None = None,
    ) -> None:
        """Transition a terminal generation failure to manual retry."""
        recovered = self._retain_retry_state(
            user_id,
            request_id=request_id,
            state_revision=state_revision,
            attempt=attempt,
        )
        if recovered is False:
            return
        failure_reason = reason or self._invalid_plan_message(attempt)
        category = failure_category or (
            self._validation_category(validation)
            if validation is not None
            else "structural"
        )
        if validation is not None:
            validation_metadata = tuple(
                (issue.code, self._validation_location(issue))
                for issue in validation.issues
            )
        if category not in {
            "timeout",
            "transient",
            "permanent",
            "response_format",
        }:
            self._log_safe_failure(
                attempt=attempt,
                started_at=started_at,
                model=self._model_name(self.llm_client),
                category=category,
                validation_metadata=validation_metadata,
            )
        self._notify_failure(
            chat_id,
            self._generation_failure_message(
                "initial",
                failure_reason,
                category=category,
                validation=validation,
                requirements=requirements or [],
            ),
            attempt=attempt,
        )

    @staticmethod
    def _log_llm_failure(
        client: LLMClient,
        *,
        attempt: int,
        started_at: float,
        failure: LLMFailure,
    ) -> None:
        """Log safe operational context for one failed Planner request."""
        PlannerHandler._log_safe_failure(
            attempt=attempt,
            started_at=started_at,
            model=PlannerHandler._model_name(client),
            category=PlannerHandler._llm_failure_category(failure),
        )

    @staticmethod
    def _generation_failure_message(
        mode: str,
        reason: str,
        *,
        category: str = "failure",
        validation: PlanValidationResult | None = None,
        requirements: list[PreferenceRequirement] | None = None,
    ) -> str:
        """Return a user-facing failure message for one planner workflow."""
        if mode == "revision":
            if reason == "timed out":
                return (
                    "Draft revision timed out. Your original draft is "
                    "unchanged; reply retry or use /cancel."
                )
            if reason == "temporarily unavailable":
                return (
                    "Draft revision is temporarily unavailable. Your original "
                    "draft is unchanged; reply retry or use /cancel."
                )
            if reason == "rejected the request":
                return (
                    "The revision service rejected the request. Your original "
                    "draft is unchanged; reply retry or use /cancel."
                )
            return (
                f"{reason} Your original draft is unchanged; reply retry or "
                "use /cancel."
            )
        if reason == "timed out":
            return (
                "Plan generation timed out. No draft was saved. Your "
                "preference is retained; use /plan to retry."
            )
        if reason == "temporarily unavailable":
            return (
                "The meal-planning service is temporarily unavailable. Your "
                "preference is retained. No draft was saved; use /plan to "
                "retry."
            )
        if reason == "rejected the request":
            return (
                "The meal-planning service rejected the request. Your "
                "preference is retained. No draft was saved; use /plan to "
                "retry."
            )
        if reason == "returned an invalid response format":
            return (
                "The meal-planning service returned an invalid response "
                "format. No draft was saved. Your preference is retained; "
                "use /plan to retry."
            )
        if category == "compliance":
            unmet_ids = {
                issue.requirement_id
                for issue in (validation.issues if validation else ())
                if issue.requirement_id is not None
            }
            clauses = [
                requirement.source_text
                for requirement in requirements or []
                if requirement.id in unmet_ids
            ]
            return format_unmet_preference_clauses(clauses)
        if category == "completeness":
            return (
                "The AI returned an invalid meal plan because it was "
                "incomplete. No draft was saved. Your preference is "
                "retained; use /plan to retry."
            )
        if category == "structural":
            return (
                "The AI returned an invalid meal plan structure. No draft "
                "was saved. Your preference is retained; use /plan to "
                "retry."
            )
        return (
            f"{reason} No draft was saved. Your preference is retained; "
            "use /plan to retry."
        )

    def _invalid_plan_message(self, attempt: int = 1) -> str:
        """Return the terminal message for invalid planner output."""
        if attempt == 1:
            return "The AI returned an invalid meal plan."
        if attempt == 2:
            return "The AI returned an invalid meal plan twice."
        return f"The AI returned an invalid meal plan {attempt} times."

    @staticmethod
    def _strict_json_call(
        client: LLMClient, prompt: str, user_message: str
    ) -> dict[str, Any]:
        strict_method = getattr(client, "chat_json_strict_sync", None)
        if callable(strict_method):
            raw = strict_method(prompt, user_message)
            if isinstance(raw, dict):
                return raw
        raw = client.chat_json_sync(prompt, user_message)
        if not isinstance(raw, dict):
            raise LLMResponseFormatError("LLM response was not a JSON object")
        return raw

    @staticmethod
    def _request_matches(
        state: ConversationState | None,
        request_id: str,
        revision: int,
    ) -> bool:
        return bool(
            state
            and state.request_id == request_id
            and state.revision == revision
            and state.step is ConversationWorkflowStep.GENERATING
        )

    def _retain_retry_state(
        self,
        user_id: str,
        *,
        request_id: str | None,
        state_revision: int | None,
        attempt: int = 1,
    ) -> bool | None:
        if not request_id or state_revision is None:
            return None
        recovery_read_started_at = time.monotonic()
        try:
            state = self.repo.get_conversation_state(
                user_id, consistent_read=True
            )
        except Exception:
            self._log_safe_failure(
                attempt=attempt,
                started_at=recovery_read_started_at,
                model=self._model_name(self.llm_client),
                category="state_recovery",
            )
            return None
        if not self._request_matches(state, request_id, state_revision):
            return False
        assert state is not None
        retry_state = state.model_copy(
            update={
                "step": ConversationWorkflowStep.RETRY_READY,
                "revision": state.revision + 1,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        recovery_write_started_at = time.monotonic()
        try:
            transitioned = self.repo.mark_conversation_retry_ready(
                user_id, retry_state, expected_revision=state_revision
            )
        except Exception:
            self._log_safe_failure(
                attempt=attempt,
                started_at=recovery_write_started_at,
                model=self._model_name(self.llm_client),
                category="state_recovery",
            )
            return None
        if not transitioned:
            logger.info("Planner request changed before retry recovery")
            return False
        return True

    def finalize_grocery(
        self, user_id: str, chat_id: int | str, week_start: str
    ) -> None:
        """Generate groceries for one exact confirmed week."""
        plan = self.repo.get_plan(user_id, week_start, consistent_read=True)
        if not plan or plan.week_start_date != week_start:
            self._notify_failure(
                chat_id, "That meal-plan week no longer exists.", attempt=1
            )
            return
        if plan.status is not PlanStatus.CONFIRMED:
            self._notify_failure(
                chat_id, "Confirm the plan before groceries.", attempt=1
            )
            return
        if plan.grocery_status is not GroceryStatus.PENDING:
            logger.info("Ignoring stale grocery event")
            return
        revision = plan.revision
        started_at = time.monotonic()
        try:
            profile = self.repo.get_profile(user_id)
            if not profile:
                raise ValueError("profile missing")
            client = self.grocery_llm_client or self.llm_client or LLMClient()
            sections = parse_grocery_response(
                client.chat_json_sync(
                    build_grocery_prompt(plan, profile.people_count),
                    "Generate grocery list",
                )
            )
            if not sections:
                raise ValueError("grocery response contained no valid sections")
            if not self.repo.complete_grocery(
                user_id, week_start, revision, sections
            ):
                logger.info("Discarded stale grocery result")
                return
        except Exception:
            self._log_safe_failure(
                attempt=1,
                started_at=started_at,
                model=self._model_name(self.grocery_llm_client),
                category="grocery",
            )
            if self.repo.fail_grocery(user_id, week_start, revision):
                self._notify_failure(
                    chat_id,
                    "I couldn't generate groceries for that plan. Please "
                    "retry.",
                    attempt=1,
                )
            else:
                logger.info("Suppressed stale grocery failure")
            return
        delivery_started_at = time.monotonic()
        try:
            self.telegram_api.send_message(
                chat_id, "Your grocery list is ready. Use /grocery to view it."
            )
        except Exception:
            self._log_safe_failure(
                attempt=1,
                started_at=delivery_started_at,
                model=self._model_name(self.grocery_llm_client),
                category="delivery",
            )

    def revise_plan(
        self,
        user_id: str,
        chat_id: int | str,
        context: PlanRevisionContext,
    ) -> None:
        """Generate and atomically publish a complete draft replacement."""
        started_at = time.monotonic()
        try:
            profile = self.repo.get_profile(user_id)
            plan = self.repo.get_plan(
                user_id, context.week_start, consistent_read=True
            )
            state = self.repo.get_conversation_state(
                user_id, consistent_read=True
            )
            if not self._revision_state_matches(state, context):
                logger.info("Discarded stale plan revision")
                return
            if not profile:
                self._resolve_revision_conflict(
                    user_id,
                    chat_id,
                    context,
                    "I couldn't revise the draft because your profile is "
                    "missing. Use /plan to create a new draft.",
                )
                return
            if not plan:
                self._resolve_revision_conflict(
                    user_id,
                    chat_id,
                    context,
                    "That draft is no longer available. Use /plan to create a "
                    "new draft.",
                )
                return
            if not self._revision_request_matches(state, context, plan):
                self._resolve_revision_conflict(
                    user_id,
                    chat_id,
                    context,
                    "The draft changed while I was revising it, so I "
                    "discarded the stale result.",
                )
                return
            if (
                plan.status is not PlanStatus.DRAFT
                or plan.week_end < date.today()
            ):
                self._resolve_revision_conflict(
                    user_id,
                    chat_id,
                    context,
                    "That draft is no longer eligible for revision. Use /plan "
                    "to create a new draft.",
                )
                return
            client = self.llm_client or LLMClient()
            revised = self._generate_with_bounded_repair(
                client,
                build_plan_revision_prompt(
                    profile,
                    plan,
                    context.amendment,
                    week_start=context.week_start.isoformat(),
                ),
                context.week_start,
                chat_id,
                failure_mode="revision",
            )
            if revised is None:
                self._retain_retry_state(
                    user_id,
                    request_id=context.request_id,
                    state_revision=context.state_revision,
                    attempt=1,
                )
                return
            revised.status = PlanStatus.DRAFT
            revised.revision = context.expected_plan_revision + 1
            revised.week_start = context.week_start
            revised.grocery_status = GroceryStatus.NOT_REQUESTED
            revised.grocery_list = []
            revised.planning_instructions = [
                *plan.planning_instructions,
                context.amendment,
            ]
            for plan_day in revised.days:
                for meal in plan_day.meals:
                    meal.outcome = MealOutcome.UNREPORTED
            published = self.repo.replace_draft_and_clear_revision_state(
                user_id,
                revised,
                expected_plan_revision=context.expected_plan_revision,
                request_id=context.request_id,
                expected_state_revision=context.state_revision,
            )
            if not published:
                self._resolve_revision_conflict(
                    user_id,
                    chat_id,
                    context,
                    "The draft changed while I was revising it, so I "
                    "discarded the stale result.",
                )
                return
        except Exception:
            self._log_safe_failure(
                attempt=1,
                started_at=started_at,
                model=self._model_name(self.llm_client),
                category="revision",
            )
            recovered = self._retain_retry_state(
                user_id,
                request_id=context.request_id,
                state_revision=context.state_revision,
                attempt=1,
            )
            if recovered is False:
                logger.info("Suppressed failure after ownership loss")
                return
            self._notify_failure(
                chat_id,
                "I couldn't revise the draft. Your original draft is "
                "unchanged; reply retry or use /cancel.",
                attempt=1,
            )
            return
        delivery_started_at = time.monotonic()
        try:
            self.telegram_api.send_plan(chat_id, revised)
            self.telegram_api.send_message(
                chat_id,
                "Review this revised draft, request more edits, or tell me to "
                "confirm it.",
            )
        except Exception:
            self._log_safe_failure(
                attempt=1,
                started_at=delivery_started_at,
                model=self._model_name(self.llm_client),
                category="delivery",
            )

    @staticmethod
    def _revision_request_matches(
        state: ConversationState | None,
        context: PlanRevisionContext,
        plan: WeeklyPlan,
    ) -> bool:
        """Check the durable state and exact plan snapshot for an event."""
        return bool(
            PlannerHandler._revision_state_matches(state, context)
            and plan.week_start == context.week_start
            and plan.revision == context.expected_plan_revision
        )

    def _resolve_revision_conflict(
        self,
        user_id: str,
        chat_id: int | str,
        context: PlanRevisionContext,
        message: str,
    ) -> bool:
        """Clear and report a stale revision only while it still owns state."""
        state = self.repo.get_conversation_state(user_id, consistent_read=True)
        if not self._revision_state_matches(state, context):
            logger.info("Suppressed stale revision conflict")
            return False
        cleared = self.repo.clear_conversation_state_if_matches(
            user_id,
            request_id=context.request_id,
            expected_revision=context.state_revision,
        )
        if not cleared:
            logger.info(
                "Suppressed stale revision conflict after ownership loss"
            )
            return False
        self._notify_failure(chat_id, message, attempt=1)
        return True

    @staticmethod
    def _revision_state_matches(
        state: ConversationState | None,
        context: PlanRevisionContext,
    ) -> bool:
        """Check only the durable request snapshot for a revision event."""
        return bool(
            state
            and state.workflow_kind is ConversationWorkflowKind.PLAN_REVISION
            and state.step is ConversationWorkflowStep.GENERATING
            and state.request_id == context.request_id
            and state.revision == context.state_revision
            and state.target_week == context.week_start
            and state.expected_plan_revision == context.expected_plan_revision
        )

    def _notify_failure(
        self, chat_id: int | str, message: str, *, attempt: int = 1
    ) -> None:
        delivery_started_at = time.monotonic()
        try:
            self.telegram_api.send_message(chat_id, message)
        except Exception:
            self._log_safe_failure(
                attempt=attempt,
                started_at=delivery_started_at,
                model=self._model_name(self.llm_client),
                category="notification",
            )

    def handle_event(self, event: dict[str, Any]) -> bool:
        """Dispatch a validated asynchronous planner event."""
        user_id = str(event.get("user_id", ""))
        chat_id = event.get("chat_id")
        action = event.get("action", GENERATE_PLAN)
        raw_user_id = event.get("user_id")
        if (
            not isinstance(raw_user_id, str)
            or not raw_user_id.strip()
            or chat_id is None
            or isinstance(chat_id, bool)
            or not isinstance(chat_id, (int, str))
            or not isinstance(action, str)
        ):
            return False
        user_id = raw_user_id.strip()
        if action == GENERATE_PLAN:
            requested_week = event.get("week_start")
            try:
                week = (
                    date.fromisoformat(requested_week)
                    if requested_week
                    else None
                )
                context = PlanGenerationContext.model_validate(
                    {
                        "preference": event.get("preference"),
                        "requirements": event.get("requirements", []),
                        "attempt": event.get("attempt", 1),
                        "repair_feedback": event.get("repair_feedback"),
                        "request_id": event.get("request_id"),
                        "state_revision": event.get("state_revision"),
                        "repair_id": event.get("repair_id"),
                    }
                )
            except TypeError, ValueError, ValidationError:
                return False
            self.generate_plan(
                user_id,
                chat_id,
                week_start=week,
                preference=context.preference,
                requirements=context.requirements,
                attempt=context.attempt,
                repair_feedback=context.repair_feedback,
                request_id=context.request_id,
                state_revision=context.state_revision,
                repair_id=context.repair_id,
            )
            return True
        if action == FINALIZE_GROCERY:
            week_start = event.get("week_start")
            if not isinstance(week_start, str):
                return False
            try:
                date.fromisoformat(week_start)
            except ValueError:
                return False
            self.finalize_grocery(user_id, chat_id, week_start)
            return True
        if action == REVISE_PLAN:
            try:
                revision_context = PlanRevisionContext.model_validate(event)
            except TypeError, ValueError, ValidationError:
                return False
            self.revise_plan(user_id, chat_id, revision_context)
            return True
        return False


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point for asynchronous planner events."""
    settings = get_planner_settings()
    dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
    repo = DynamoRepository(dynamodb.Table(settings.dynamodb_table_name))
    telegram_api = TelegramAPI(
        settings.telegram_bot_token,
        request_timeout=settings.planner_telegram_request_timeout_seconds,
    )
    plan_llm_client = LLMClient(
        model=settings.planner_llm_model,
        api_key=settings.llm_api_key,
        reasoning_effort=settings.planner_llm_reasoning_effort,
        max_retries=settings.planner_llm_max_retries,
        initial_backoff=settings.planner_llm_initial_backoff_seconds,
        request_timeout=settings.planner_llm_request_timeout_seconds,
    )
    grocery_llm_client = LLMClient(
        model=settings.planner_llm_model,
        api_key=settings.llm_api_key,
        reasoning_effort=settings.planner_llm_reasoning_effort,
        max_retries=settings.planner_grocery_llm_max_retries,
        initial_backoff=settings.planner_llm_initial_backoff_seconds,
        request_timeout=settings.planner_grocery_llm_request_timeout_seconds,
    )
    planner = PlannerHandler(
        repo,
        telegram_api,
        plan_llm_client,
        grocery_llm_client=grocery_llm_client,
        max_attempts=settings.planner_llm_max_retries,
        repair_connect_timeout_seconds=(
            settings.planner_repair_connect_timeout_seconds
        ),
        repair_read_timeout_seconds=settings.planner_repair_read_timeout_seconds,
    )
    try:
        with planner_deadline(settings.planner_function_timeout_seconds):
            if not planner.handle_event(event):
                logger.error("Invalid planner event")
                return {"statusCode": 400, "body": "invalid event"}
    except PlannerDeadlineExceeded:
        logger.error(
            "Planner application deadline of %s seconds exceeded",
            settings.planner_function_timeout_seconds,
        )
        return {"statusCode": 504, "body": "planner deadline exceeded"}
    return {"statusCode": 200, "body": "ok"}
