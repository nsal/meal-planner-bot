"""Boundary tests for the retained bot workflows."""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from meal_planner.bot_handler import MEAL_INPUT_PROMPT, BotHandler
from meal_planner.models import (
    ConversationState,
    ConversationWorkflowKind,
    ConversationWorkflowStep,
    FamilyMember,
    MealLogDraft,
    MealType,
    ProfileDraft,
    ProfileEditCategory,
    ProfileEditOperation,
)
from meal_planner.router import RouteResult, RouteType, route_update
from meal_planner.telegram.api import TelegramAPIError
from tests.factories import make_meal, make_plan_chat_state, make_profile


@pytest.fixture
def handler() -> BotHandler:
    """Return a bot with isolated repository and Telegram collaborators."""
    repo = MagicMock()
    telegram = MagicMock()
    repo.get_profile.return_value = make_profile()
    return BotHandler(
        repo,
        telegram,
        access_policy=MagicMock(
            evaluate=MagicMock(return_value=MagicMock(allowed=True))
        ),
        processing_date=date(2026, 8, 28),
    )


def _command(name: str) -> RouteResult:
    """Build a command route for a private user."""
    return RouteResult(
        route_type=RouteType.COMMAND,
        chat_id=1,
        user_id="user",
        command=name,
    )


def _message_update(text: str, update_id: int = 1) -> dict[str, object]:
    """Build a private-chat message update for the public bot boundary."""
    return {
        "update_id": update_id,
        "message": {
            "date": int(datetime(2026, 8, 28, tzinfo=timezone.utc).timestamp()),
            "from": {"id": 1},
            "chat": {"id": 1, "type": "private"},
            "text": text,
        },
    }


def _profile_setup_state(
    step: ConversationWorkflowStep,
    *,
    revision: int = 0,
    last_update_id: str | None = None,
) -> ConversationState:
    """Build a valid deterministic profile-setup state for boundary tests."""
    now = datetime(2026, 8, 28, tzinfo=timezone.utc)
    return ConversationState(
        workflow_kind=ConversationWorkflowKind.PROFILE_SETUP,
        step=step,
        revision=revision,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
        last_update_id=last_update_id,
    )


def _profile_setup_harness(
    *,
    draft: ProfileDraft,
    state: ConversationState,
    profile: object = None,
) -> tuple[BotHandler, MagicMock, dict[str, object]]:
    """Create an in-memory collaborator harness for setup behavior."""
    repo = MagicMock()
    telegram = MagicMock()
    stored: dict[str, object] = {"draft": draft, "state": state}
    repo.get_profile.return_value = profile
    repo.get_profile_draft.side_effect = lambda _user_id: stored["draft"]
    repo.get_conversation_state.side_effect = lambda _user_id, **_kwargs: (
        stored["state"]
    )

    def save_draft(_user_id: str, value: ProfileDraft) -> None:
        stored["draft"] = value

    def transition(
        _user_id: str,
        value: ConversationState,
        **_kwargs: object,
    ) -> bool:
        stored["state"] = value
        return True

    def save_draft_and_transition(
        _user_id: str,
        value: ProfileDraft,
        next_state: ConversationState,
        _observed_state: ConversationState,
    ) -> bool:
        stored["draft"] = value
        stored["state"] = next_state
        return True

    def delete_state(_user_id: str, **_kwargs: object) -> bool:
        stored["state"] = None
        return True

    repo.save_profile_draft.side_effect = save_draft
    repo.save_profile_draft_and_transition_state.side_effect = (
        save_draft_and_transition
    )
    repo.save_conversation_state.side_effect = transition
    repo.transition_conversation_state.side_effect = transition
    repo.delete_conversation_state.side_effect = delete_state
    repo.save_profile.return_value = True
    repo.delete_profile_draft.return_value = None
    return (
        BotHandler(
            repo,
            telegram,
            access_policy=MagicMock(
                evaluate=MagicMock(return_value=MagicMock(allowed=True))
            ),
            processing_date=date(2026, 8, 28),
        ),
        repo,
        stored,
    )


def _setup_conversation(handler: BotHandler, text: str, update_id: int) -> None:
    """Submit one setup answer through the public update boundary."""
    result = handler.handle_update(_message_update(text, update_id))
    assert result == {"statusCode": 200, "body": "ok"}


def test_complete_profile_start_sends_one_welcome_without_setup_mutation(
    handler: BotHandler,
) -> None:
    """Complete users get one welcome and do not enter profile setup."""
    handler.handle_update(_message_update("/start"))

    handler.telegram_api.send_message.assert_called_once()
    assert "Welcome back" in handler.telegram_api.send_message.call_args.args[1]
    handler.repo.get_profile_draft.assert_not_called()
    handler.repo.get_conversation_state.assert_not_called()
    handler.repo.save_conversation_state.assert_not_called()
    handler.repo.save_profile_draft.assert_not_called()


def test_profile_setup_saves_all_steps_and_cleans_up(
    handler: BotHandler,
) -> None:
    """The public setup sequence saves its draft, profile, and cleanup."""
    handler.repo.get_profile.return_value = None
    draft: ProfileDraft | None = None
    state: ConversationState | None = None

    def get_draft(_user_id: str) -> ProfileDraft | None:
        return draft

    def get_state(_user_id: str, **_kwargs: object) -> ConversationState | None:
        return state

    def save_draft(_user_id: str, value: ProfileDraft) -> None:
        nonlocal draft
        draft = value

    def save_state(
        _user_id: str,
        value: ConversationState,
        **_kwargs: object,
    ) -> bool:
        nonlocal state
        state = value
        return True

    def save_draft_and_transition(
        _user_id: str,
        value: ProfileDraft,
        next_state: ConversationState,
        _observed_state: ConversationState,
    ) -> bool:
        save_draft(_user_id, value)
        return save_state(_user_id, next_state)

    def delete_state(_user_id: str, **_kwargs: object) -> bool:
        nonlocal state
        state = None
        return True

    handler.repo.get_profile_draft.side_effect = get_draft
    handler.repo.get_conversation_state.side_effect = get_state
    handler.repo.save_profile_draft.side_effect = save_draft
    handler.repo.save_profile_draft_and_transition_state.side_effect = (
        save_draft_and_transition
    )
    handler.repo.save_conversation_state.side_effect = save_state
    handler.repo.transition_conversation_state.side_effect = save_state
    handler.repo.delete_conversation_state.side_effect = delete_state

    def complete_profile_setup(
        _user_id: str,
        _profile: object,
        _observed_state: ConversationState,
        **_kwargs: object,
    ) -> bool:
        nonlocal state
        state = None
        return True

    handler.repo.complete_profile_setup.side_effect = complete_profile_setup

    _setup_conversation(handler, "/start", 1)
    _setup_conversation(handler, "Smith", 2)
    _setup_conversation(handler, "2", 3)
    _setup_conversation(handler, "Alex 2000 120 30\nSam 1800", 4)
    _setup_conversation(handler, "Peanuts\nVegetarian", 5)
    _setup_conversation(handler, "More vegetables", 6)

    saved_profile = handler.repo.complete_profile_setup.call_args.args[1]
    assert saved_profile.name == "Smith"
    assert saved_profile.people_count == 2
    assert saved_profile.family_members == [
        FamilyMember(
            name="Alex",
            calorie_target=2000,
            protein_target=120,
            fibre_target=30,
        ),
        FamilyMember(name="Sam", calorie_target=1800),
    ]
    assert saved_profile.dietary_constraints == ["Peanuts", "Vegetarian"]
    assert saved_profile.dietary_preferences == ["More vegetables"]
    handler.repo.complete_profile_setup.assert_called_once()
    handler.repo.delete_profile_draft.assert_not_called()
    handler.repo.delete_conversation_state.assert_not_called()
    assert state is None


@pytest.mark.parametrize(
    ("member_text", "expected_members"),
    [
        (
            "Alex 2000\nSam 1800 none 30",
            [
                FamilyMember(name="Alex", calorie_target=2000),
                FamilyMember(name="Sam", calorie_target=1800, fibre_target=30),
            ],
        ),
        (
            "Alex 2000 none none\nSam 1800 100",
            [
                FamilyMember(name="Alex", calorie_target=2000),
                FamilyMember(
                    name="Sam", calorie_target=1800, protein_target=100
                ),
            ],
        ),
    ],
)
def test_profile_setup_accepts_optional_targets_and_explicit_none(
    member_text: str, expected_members: list[FamilyMember]
) -> None:
    """Optional protein/fibre targets and ``none`` remain unset."""
    draft = ProfileDraft(name="Smith", people_count=2)
    state = _profile_setup_state(
        ConversationWorkflowStep.AWAITING_PROFILE_MEMBERS
    )
    handler, repo, stored = _profile_setup_harness(draft=draft, state=state)

    _setup_conversation(handler, member_text, 10)

    assert stored["draft"].family_members == expected_members  # type: ignore[union-attr]
    repo.save_profile_draft_and_transition_state.assert_called_once()
    assert (
        stored["state"].step
        is ConversationWorkflowStep.AWAITING_PROFILE_CONSTRAINTS
    )  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "member_text",
    [
        "Alex 2000\nalex 1800",
        "Alex 0\nSam 1800",
        "Alex 2000 0\nSam 1800",
        "Alex 2000 120 1001\nSam 1800",
        "Alex two-thousand\nSam 1800",
    ],
)
def test_profile_setup_rejects_duplicate_malformed_and_out_of_bounds_members(
    member_text: str,
) -> None:
    """Invalid member lines leave the draft and state untouched."""
    draft = ProfileDraft(name="Smith", people_count=2)
    state = _profile_setup_state(
        ConversationWorkflowStep.AWAITING_PROFILE_MEMBERS
    )
    handler, repo, stored = _profile_setup_harness(draft=draft, state=state)

    _setup_conversation(handler, member_text, 11)

    assert stored["draft"] == draft
    assert stored["state"] == state
    repo.save_profile_draft.assert_not_called()
    handler.telegram_api.send_profile_setup_prompt.assert_called_once()
    assert (
        "valid member lines"
        in (
            handler.telegram_api.send_profile_setup_prompt.call_args.kwargs[
                "text"
            ]
        )
    )


@pytest.mark.parametrize("count", ["0", "21", "two", "1.5"])
def test_profile_setup_rejects_invalid_household_counts(count: str) -> None:
    """Household size is an integer in the documented one-to-twenty range."""
    draft = ProfileDraft(name="Smith")
    state = _profile_setup_state(
        ConversationWorkflowStep.AWAITING_PROFILE_HOUSEHOLD_SIZE
    )
    handler, repo, stored = _profile_setup_harness(draft=draft, state=state)

    _setup_conversation(handler, count, 12)

    assert stored["draft"] == draft
    assert stored["state"] == state
    repo.save_profile_draft.assert_not_called()


@pytest.mark.parametrize("text", ["none", "No restrictions."])
def test_profile_setup_maps_explicit_no_constraints_to_empty_list(
    text: str,
) -> None:
    """A supported no-value response is not persisted as dietary text."""
    draft = ProfileDraft(
        name="Smith",
        people_count=1,
        family_members=[FamilyMember(name="Alex", calorie_target=2000)],
    )
    state = _profile_setup_state(
        ConversationWorkflowStep.AWAITING_PROFILE_CONSTRAINTS
    )
    handler, _repo, stored = _profile_setup_harness(draft=draft, state=state)

    _setup_conversation(handler, text, 13)

    assert stored["draft"].dietary_constraints == []  # type: ignore[union-attr]
    assert (
        stored["state"].step
        is ConversationWorkflowStep.AWAITING_PROFILE_PREFERENCES
    )  # type: ignore[union-attr]


def test_profile_setup_close_is_scoped_and_preserves_draft() -> None:
    """Close removes only setup state, leaving restartable draft data."""
    draft = ProfileDraft(name="Smith")
    state = _profile_setup_state(
        ConversationWorkflowStep.AWAITING_PROFILE_HOUSEHOLD_SIZE,
        revision=4,
    )
    handler, repo, stored = _profile_setup_harness(draft=draft, state=state)
    callback = {
        "callback_query": {
            "id": "close-query",
            "from": {"id": 1},
            "message": {"chat": {"id": 1, "type": "private"}},
            "data": "profile:close",
        }
    }

    handler.handle_update(callback)

    repo.delete_conversation_state.assert_called_once_with(
        "1", expected_revision=4, expected_step=state.step
    )
    assert stored["state"] is None
    assert stored["draft"] == draft
    handler.telegram_api.answer_callback_query.assert_called_once()


def test_profile_setup_duplicate_update_only_replays_prompt() -> None:
    """A retried Telegram update does not repeat draft or state writes."""
    draft = ProfileDraft(name="Smith")
    state = _profile_setup_state(
        ConversationWorkflowStep.AWAITING_PROFILE_HOUSEHOLD_SIZE,
        last_update_id="14",
    )
    handler, repo, _stored = _profile_setup_harness(draft=draft, state=state)

    _setup_conversation(handler, "2", 14)

    repo.save_profile_draft.assert_not_called()
    repo.transition_conversation_state.assert_not_called()
    handler.telegram_api.send_profile_setup_prompt.assert_called_once()


def test_profile_setup_stale_state_does_not_mutate_saved_draft() -> None:
    """A state/draft step mismatch is rejected without a write."""
    draft = ProfileDraft(name="Smith")
    state = _profile_setup_state(
        ConversationWorkflowStep.AWAITING_PROFILE_FAMILY_NAME
    )
    handler, repo, _stored = _profile_setup_harness(draft=draft, state=state)

    _setup_conversation(handler, "2", 15)

    repo.save_profile_draft.assert_not_called()
    repo.transition_conversation_state.assert_not_called()
    assert "stale" in handler.telegram_api.send_message.call_args.args[1]


def test_profile_setup_conflict_preserves_replacement_state_and_draft() -> None:
    """A state replacement makes the stale setup answer a no-op."""
    draft = ProfileDraft(name="Smith")
    state = _profile_setup_state(
        ConversationWorkflowStep.AWAITING_PROFILE_HOUSEHOLD_SIZE
    )
    replacement = state.model_copy(
        update={
            "revision": state.revision + 1,
            "step": ConversationWorkflowStep.AWAITING_PROFILE_MEMBERS,
            "last_update_id": "replacement-update",
        }
    )
    handler, repo, stored = _profile_setup_harness(draft=draft, state=state)

    def replace_before_commit(
        _user_id: str,
        _updated_draft: ProfileDraft,
        _next_state: ConversationState,
        _observed_state: ConversationState,
    ) -> bool:
        stored["state"] = replacement
        return False

    repo.save_profile_draft_and_transition_state.side_effect = (
        replace_before_commit
    )

    _setup_conversation(handler, "2", 18)

    assert stored["draft"] == draft
    assert stored["state"] == replacement
    repo.save_profile_draft.assert_not_called()
    repo.transition_conversation_state.assert_not_called()
    assert "changed" in handler.telegram_api.send_message.call_args.args[1]


def test_profile_setup_start_reconciles_state_to_saved_draft() -> None:
    """Restart moves setup to the first unanswered saved-draft step."""
    draft = ProfileDraft(name="Smith")
    state = _profile_setup_state(
        ConversationWorkflowStep.AWAITING_PROFILE_FAMILY_NAME,
        revision=2,
    )
    handler, repo, stored = _profile_setup_harness(draft=draft, state=state)

    handler.handle_update(_message_update("/start", 16))

    transitioned = repo.transition_conversation_state.call_args.args[1]
    assert (
        transitioned.step
        is ConversationWorkflowStep.AWAITING_PROFILE_HOUSEHOLD_SIZE
    )
    assert transitioned.revision == 3
    assert (
        stored["state"].step
        is ConversationWorkflowStep.AWAITING_PROFILE_HOUSEHOLD_SIZE
    )  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "completion_result", [False, RuntimeError("save failed")]
)
def test_profile_setup_completion_failure_keeps_setup_open(
    completion_result: bool | RuntimeError,
) -> None:
    """Profile conflicts and persistence failures do not clean up setup."""
    draft = ProfileDraft(
        name="Smith",
        people_count=1,
        family_members=[FamilyMember(name="Alex", calorie_target=2000)],
        dietary_constraints=[],
    )
    state = _profile_setup_state(
        ConversationWorkflowStep.AWAITING_PROFILE_PREFERENCES,
        revision=5,
    )
    handler, repo, stored = _profile_setup_harness(draft=draft, state=state)
    if isinstance(completion_result, RuntimeError):
        repo.complete_profile_setup.side_effect = completion_result
    else:
        repo.complete_profile_setup.return_value = completion_result

    _setup_conversation(handler, "none", 17)

    repo.delete_profile_draft.assert_not_called()
    repo.delete_conversation_state.assert_not_called()
    assert stored["state"] == state


def test_profile_setup_completion_rejects_replaced_state_without_success(
    handler: BotHandler,
) -> None:
    """A stale final answer cannot save a profile or report success."""
    draft = ProfileDraft(
        name="Smith",
        people_count=1,
        family_members=[FamilyMember(name="Alex", calorie_target=2_000)],
        dietary_constraints=[],
    )
    state = _profile_setup_state(
        ConversationWorkflowStep.AWAITING_PROFILE_PREFERENCES,
        revision=5,
    )
    replacement = state.model_copy(
        update={
            "revision": state.revision + 1,
            "step": ConversationWorkflowStep.AWAITING_PROFILE_FAMILY_NAME,
            "last_update_id": "replacement-update",
        }
    )
    handler, repo, stored = _profile_setup_harness(
        draft=draft, state=state, profile=make_profile()
    )

    def reject_stale_completion(
        _user_id: str,
        _profile: object,
        _observed_state: ConversationState,
        **_kwargs: object,
    ) -> bool:
        stored["state"] = replacement
        return False

    repo.complete_profile_setup.side_effect = reject_stale_completion

    _setup_conversation(handler, "none", 19)

    assert stored["state"] == replacement
    assert stored["draft"] == draft
    repo.complete_profile_setup.assert_called_once()
    repo.save_profile.assert_not_called()
    assert not any(
        "Your profile has been saved" in call.args[1]
        for call in handler.telegram_api.send_message.call_args_list
    )
    assert "changed" in handler.telegram_api.send_message.call_args.args[1]


def test_unknown_legacy_command_is_not_dispatched(handler: BotHandler) -> None:
    """The dropped top-level cancellation command has no handler."""
    handler.handle_command(_command("cancel"))
    handler.telegram_api.send_message.assert_called_once()
    assert (
        "unknown command"
        in handler.telegram_api.send_message.call_args.args[1].lower()
    )


def test_plan_starts_a_temporary_session(handler: BotHandler) -> None:
    """Plan starts the awaiting-request workflow."""
    handler.repo.get_conversation_state.return_value = None
    handler.repo.save_conversation_state.return_value = True

    handler.handle_command(_command("plan"))

    state = handler.repo.save_conversation_state.call_args.args[1]
    assert state.step is ConversationWorkflowStep.AWAITING_PLAN_REQUEST
    assert state.session_id is not None


def test_plan_chat_request_dispatches_only_identifiers(
    handler: BotHandler,
) -> None:
    """A request is claimed and dispatched without profile or prompt data."""
    state = make_plan_chat_state()
    handler.repo.get_conversation_state.return_value = state
    handler.repo.transition_conversation_state.return_value = True
    handler._invoke_plan_chat = MagicMock(return_value=True)  # type: ignore[method-assign]

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="three dinners",
        )
    )

    handler._invoke_plan_chat.assert_called_once()  # type: ignore[attr-defined]
    args = handler._invoke_plan_chat.call_args.args  # type: ignore[attr-defined]
    assert args[0:2] == ("user", 1)
    assert len(args) == 5


def test_plan_chat_end_is_session_scoped(handler: BotHandler) -> None:
    """An end control cannot delete a replaced session."""
    state = make_plan_chat_state()
    handler.repo.get_conversation_state.return_value = state
    handler.repo.delete_conversation_state.return_value = True
    callback = RouteResult(
        route_type=RouteType.CALLBACK,
        chat_id=1,
        user_id="user",
        callback_query_id="query",
        callback_data=f"plan_chat:end:{state.session_id}",
    )

    handler.handle_callback(callback)

    handler.repo.delete_conversation_state.assert_called_once()
    handler.telegram_api.answer_callback_query.assert_called_once()


@pytest.mark.parametrize(
    "workflow_state",
    [
        _profile_setup_state(
            ConversationWorkflowStep.AWAITING_PROFILE_FAMILY_NAME
        ),
        make_plan_chat_state(),
        ConversationState(
            workflow_kind=ConversationWorkflowKind.PROFILE_EDIT,
            step=ConversationWorkflowStep.PROFILE_MENU,
            revision=2,
            created_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
            updated_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
            expires_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
        ),
        ConversationState(
            workflow_kind=ConversationWorkflowKind.MEAL_LOG,
            step=ConversationWorkflowStep.AWAITING_MEAL_INPUT,
            meal_draft=MealLogDraft(),
            request_id="meal-request",
            revision=3,
            created_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
            updated_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
            expires_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
        ),
    ],
)
def test_plan_replaces_every_prior_workflow_with_a_fresh_session(
    handler: BotHandler, workflow_state: ConversationState
) -> None:
    """Starting /plan replaces any retained workflow atomically."""
    handler.repo.get_conversation_state.return_value = workflow_state
    handler.repo.save_conversation_state.return_value = True

    handler.handle_command(_command("plan"))

    saved = handler.repo.save_conversation_state.call_args.args[1]
    assert saved.workflow_kind is ConversationWorkflowKind.PLAN_CHAT
    assert saved.step is ConversationWorkflowStep.AWAITING_PLAN_REQUEST
    assert saved.session_id is not None
    assert saved.session_id != workflow_state.session_id
    assert handler.telegram_api.send_plan_chat.call_args.args[2] == (
        saved.session_id
    )
    assert "replaced" in handler.telegram_api.send_plan_chat.call_args.args[1]


def test_plan_requires_a_complete_profile_before_creating_a_session(
    handler: BotHandler,
) -> None:
    """An incomplete profile cannot enter Plan Chat."""
    handler.repo.get_profile.return_value = None

    handler.handle_command(_command("plan"))

    handler.repo.save_conversation_state.assert_not_called()
    handler.telegram_api.send_plan_chat.assert_not_called()
    assert "complete your profile" in (
        handler.telegram_api.send_message.call_args.args[1].lower()
    )


def test_plan_initial_request_claim_refreshes_utc_context_and_keeps_control(
    handler: BotHandler,
) -> None:
    """The first request stores the update's UTC date and session control."""
    state = make_plan_chat_state()
    handler.repo.get_conversation_state.return_value = state
    handler.repo.transition_conversation_state.return_value = True
    handler._invoke_plan_chat = MagicMock(return_value=True)  # type: ignore[method-assign]
    route = RouteResult(
        route_type=RouteType.CONVERSATIONAL,
        chat_id=1,
        user_id="user",
        text="Plan quick dinners",
        raw_update=_message_update("Plan quick dinners", 41),
    )

    handler.handle_conversational(route)

    claimed = handler.repo.transition_conversation_state.call_args.args[1]
    assert claimed.initial_request == "Plan quick dinners"
    assert claimed.pending_message == "Plan quick dinners"
    assert claimed.latest_response is None
    assert claimed.context_date == date(2026, 8, 28)
    assert claimed.last_update_id == "41"
    sent = handler.telegram_api.send_plan_chat.call_args
    assert sent.args[2] == state.session_id


def test_plan_follow_up_retains_initial_and_latest_context(
    handler: BotHandler,
) -> None:
    """A follow-up replaces only pending input and refreshes its UTC date."""
    state = make_plan_chat_state(
        step=ConversationWorkflowStep.PLAN_CHAT_READY,
        latest_response="A useful draft",
        context_date=date(2026, 8, 27),
    )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.transition_conversation_state.return_value = True
    handler._invoke_plan_chat = MagicMock(return_value=True)  # type: ignore[method-assign]
    route = RouteResult(
        route_type=RouteType.CONVERSATIONAL,
        chat_id=1,
        user_id="user",
        text="Make the second dinner vegetarian",
        raw_update=_message_update("Make the second dinner vegetarian", 42),
    )

    handler.handle_conversational(route)

    claimed = handler.repo.transition_conversation_state.call_args.args[1]
    assert claimed.initial_request == state.initial_request
    assert claimed.latest_response == "A useful draft"
    assert claimed.pending_message == "Make the second dinner vegetarian"
    assert claimed.context_date == date(2026, 8, 28)


def test_plan_duplicate_update_replays_prompt_without_second_invocation(
    handler: BotHandler,
) -> None:
    """Telegram retries do not claim or invoke the same update twice."""
    state = make_plan_chat_state().model_copy(update={"last_update_id": "43"})
    handler.repo.get_conversation_state.return_value = state
    handler._invoke_plan_chat = MagicMock(return_value=True)  # type: ignore[method-assign]

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="Plan dinners",
            raw_update=_message_update("Plan dinners", 43),
        )
    )

    handler.repo.transition_conversation_state.assert_not_called()
    handler._invoke_plan_chat.assert_not_called()  # type: ignore[attr-defined]
    sent = handler.telegram_api.send_plan_chat.call_args
    assert "already received" in sent.args[1]
    assert sent.args[2] == state.session_id


def test_plan_concurrent_claim_loss_does_not_invoke_worker(
    handler: BotHandler,
) -> None:
    """A lost conditional transition leaves the competing session intact."""
    state = make_plan_chat_state()
    handler.repo.get_conversation_state.return_value = state
    handler.repo.transition_conversation_state.return_value = False
    handler._invoke_plan_chat = MagicMock(return_value=True)  # type: ignore[method-assign]

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="Plan dinners",
        )
    )

    handler._invoke_plan_chat.assert_not_called()  # type: ignore[attr-defined]
    assert "changed" in handler.telegram_api.send_plan_chat.call_args.args[1]


def test_plan_invocation_failure_restores_requestable_state(
    handler: BotHandler,
) -> None:
    """A dispatch failure restores the owned session for a retry."""
    state = make_plan_chat_state()
    handler.repo.get_conversation_state.return_value = state
    handler.repo.transition_conversation_state.side_effect = [True, True]
    handler._invoke_plan_chat = MagicMock(return_value=False)  # type: ignore[method-assign]

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="Plan dinners",
        )
    )

    transitions = handler.repo.transition_conversation_state.call_args_list
    restored = transitions[-1].args[1]
    assert restored.step is ConversationWorkflowStep.AWAITING_PLAN_REQUEST
    assert restored.request_id is None
    assert restored.initial_request is None
    assert restored.context_date is None
    assert (
        "start a draft"
        in (handler.telegram_api.send_plan_chat.call_args.args[1])
    )


@pytest.mark.parametrize("read_result", [None, "malformed-state"])
def test_plan_treats_expired_or_malformed_reads_as_no_active_session(
    handler: BotHandler, read_result: object
) -> None:
    """A non-state read cannot route arbitrary text into Plan Chat."""
    handler.repo.get_conversation_state.return_value = read_result

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="hello",
        )
    )

    handler.repo.transition_conversation_state.assert_not_called()
    assert "use /plan" in handler.telegram_api.send_message.call_args.args[1]


@pytest.mark.parametrize(
    "step",
    [
        ConversationWorkflowStep.AWAITING_PLAN_REQUEST,
        ConversationWorkflowStep.PLAN_CHAT_GENERATING,
        ConversationWorkflowStep.PLAN_CHAT_READY,
    ],
)
def test_plan_end_button_ends_each_live_session_and_acknowledges(
    handler: BotHandler, step: ConversationWorkflowStep
) -> None:
    """The scoped end control works for every active Plan Chat phase."""
    state = make_plan_chat_state(step=step)
    handler.repo.get_conversation_state.return_value = state
    handler.repo.delete_conversation_state.return_value = True
    route = RouteResult(
        route_type=RouteType.CALLBACK,
        chat_id=1,
        user_id="user",
        callback_query_id=f"query-{step.value}",
        callback_data=f"plan_chat:end:{state.session_id}",
    )

    handler.handle_callback(route)

    handler.repo.delete_conversation_state.assert_called_once_with(
        "user", expected_revision=state.revision, expected_step=step
    )
    handler.telegram_api.answer_callback_query.assert_called_once_with(
        f"query-{step.value}", "Planning ended"
    )


def test_plan_end_button_does_not_delete_replacement_or_suppress_response(
    handler: BotHandler,
) -> None:
    """A stale session button cannot affect a replacement session."""
    current = make_plan_chat_state()
    stale_session = make_plan_chat_state().session_id
    assert stale_session is not None
    handler.repo.get_conversation_state.return_value = current

    handler.handle_callback(
        RouteResult(
            route_type=RouteType.CALLBACK,
            chat_id=1,
            user_id="user",
            callback_query_id="stale-query",
            callback_data=f"plan_chat:end:{stale_session}",
        )
    )

    handler.repo.delete_conversation_state.assert_not_called()
    sent = handler.telegram_api.send_plan_chat.call_args
    assert "stale" in sent.args[1]
    assert sent.args[2] == stale_session
    handler.telegram_api.answer_callback_query.assert_called_once_with(
        "stale-query", "Stale planning button"
    )


def test_plan_end_button_for_missing_session_is_acknowledged(
    handler: BotHandler,
) -> None:
    """An already-ended session produces a bounded acknowledgement."""
    session_id = make_plan_chat_state().session_id
    assert session_id is not None
    handler.repo.get_conversation_state.return_value = None

    handler.handle_callback(
        RouteResult(
            route_type=RouteType.CALLBACK,
            chat_id=1,
            user_id="user",
            callback_query_id="ended-query",
            callback_data=f"plan_chat:end:{session_id}",
        )
    )

    handler.repo.delete_conversation_state.assert_not_called()
    sent = handler.telegram_api.send_plan_chat.call_args
    assert "already ended" in sent.args[1]
    assert sent.args[2] == session_id
    handler.telegram_api.answer_callback_query.assert_called_once_with(
        "ended-query", "Planning already ended"
    )


def test_plan_end_callback_handles_conflict_and_persistence_error(
    handler: BotHandler,
) -> None:
    """Conditional loss and persistence errors both acknowledge safely."""
    state = make_plan_chat_state(step=ConversationWorkflowStep.PLAN_CHAT_READY)
    route = RouteResult(
        route_type=RouteType.CALLBACK,
        chat_id=1,
        user_id="user",
        callback_query_id="query",
        callback_data=f"plan_chat:end:{state.session_id}",
    )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.delete_conversation_state.side_effect = [False, RuntimeError]

    handler.handle_callback(route)
    assert "changed" in handler.telegram_api.send_plan_chat.call_args.args[1]
    assert handler.telegram_api.answer_callback_query.call_args.args == (
        "query",
        "Planning changed",
    )

    handler.telegram_api.reset_mock()
    handler.handle_callback(route)
    assert (
        "couldn't end"
        in (handler.telegram_api.send_plan_chat.call_args.args[1])
    )
    assert handler.telegram_api.answer_callback_query.call_args.args == (
        "query",
        "Unable to end planning",
    )


def test_plan_end_success_delivery_failure_keeps_deleted_outcome(
    handler: BotHandler,
) -> None:
    """A deleted session stays ended when its success message cannot send."""
    state = make_plan_chat_state(step=ConversationWorkflowStep.PLAN_CHAT_READY)
    route = RouteResult(
        route_type=RouteType.CALLBACK,
        chat_id=1,
        user_id="user",
        callback_query_id="query",
        callback_data=f"plan_chat:end:{state.session_id}",
    )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.delete_conversation_state.return_value = True
    handler.telegram_api.send_message.side_effect = TelegramAPIError(
        "success delivery failed"
    )

    handler.handle_callback(route)

    handler.repo.delete_conversation_state.assert_called_once_with(
        "user",
        expected_revision=state.revision,
        expected_step=state.step,
    )
    handler.telegram_api.answer_callback_query.assert_called_once_with(
        "query", "Planning ended"
    )
    handler.telegram_api.send_plan_chat.assert_not_called()
    assert all(
        "couldn't end planning" not in call.args[1].lower()
        for call in handler.telegram_api.send_message.call_args_list
    )
    assert handler.telegram_api.send_message.call_args.args[1] == (
        "Planning ended. Use /plan whenever you want a new draft."
    )


def test_plan_end_persistence_failure_preserves_retry_path(
    handler: BotHandler,
) -> None:
    """A failed delete still reports failure with the session retry control."""
    state = make_plan_chat_state(step=ConversationWorkflowStep.PLAN_CHAT_READY)
    route = RouteResult(
        route_type=RouteType.CALLBACK,
        chat_id=1,
        user_id="user",
        callback_query_id="query",
        callback_data=f"plan_chat:end:{state.session_id}",
    )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.delete_conversation_state.side_effect = RuntimeError(
        "delete failed"
    )

    handler.handle_callback(route)

    handler.telegram_api.send_plan_chat.assert_called_once_with(
        1,
        "I couldn't end planning right now. Please try again.",
        state.session_id,
    )
    handler.telegram_api.answer_callback_query.assert_called_once_with(
        "query", "Unable to end planning"
    )


def test_profile_edit_categories_remain_deterministic(
    handler: BotHandler,
) -> None:
    """Retained profile controls keep explicit typed category operations."""
    assert ProfileEditCategory.DIETARY_CONSTRAINTS.value == (
        "dietary_constraints"
    )
    assert ProfileEditOperation.ADD.is_valid_for(
        ProfileEditCategory.DIETARY_PREFERENCES
    )


@pytest.mark.parametrize(
    ("days_ago", "expected_valid"),
    [(7, True), (8, False)],
)
def test_submitted_meal_date_window_is_inclusive_on_active_route(
    handler: BotHandler,
    days_ago: int,
    expected_valid: bool,
) -> None:
    """The active route accepts exactly the documented eight dates."""
    reference_date = date(2026, 8, 30)
    reference_timestamp = int(
        datetime(2026, 8, 30, tzinfo=timezone.utc).timestamp()
    )
    submitted_date = reference_date - timedelta(days=days_ago)
    submitted_text = f"{submitted_date.isoformat()}, lunch, Soup"
    command_route = route_update(
        {
            "message": {
                "date": reference_timestamp,
                "from": {"id": 1},
                "chat": {"id": 1, "type": "private"},
                "text": "/submit_meals",
            }
        }
    )
    handler.repo.get_conversation_state.return_value = None
    handler.repo.save_conversation_state.return_value = True

    handler.handle_command(command_route)

    created_state = handler.repo.save_conversation_state.call_args.args[1]
    assert created_state.step is ConversationWorkflowStep.AWAITING_MEAL_INPUT
    startup_prompt = handler.telegram_api.send_message.call_args_list[-1]
    assert (
        "from UTC today through the previous seven dates, inclusive "
        "(eight calendar dates)" in startup_prompt.args[1]
    )

    handler.repo.get_conversation_state.return_value = created_state
    conversation_route = route_update(
        {
            "update_id": 100 + days_ago,
            "message": {
                "date": reference_timestamp,
                "from": {"id": 1},
                "chat": {"id": 1, "type": "private"},
                "text": submitted_text,
            },
        }
    )

    handler.handle_conversational(conversation_route)

    if expected_valid:
        transition = handler.repo.transition_conversation_state.call_args.args[
            1
        ]
        assert (
            transition.step
            is ConversationWorkflowStep.AWAITING_MEAL_CONFIRMATION
        )
        assert transition.meal_draft.date == submitted_date
        handler.telegram_api.send_meal_review.assert_called_once_with(
            1, submitted_text, created_state.request_id
        )
    else:
        handler.repo.transition_conversation_state.assert_not_called()
        handler.telegram_api.send_meal_review.assert_not_called()
        assert handler.telegram_api.send_message.call_args.args[1] == (
            "date must be from UTC today through the previous seven dates, "
            f"inclusive (eight calendar dates)\n\n{MEAL_INPUT_PROMPT}"
        )


def _meal_state(
    step: ConversationWorkflowStep,
    *,
    request_id: str,
    revision: int = 0,
) -> ConversationState:
    """Build a state for public submitted-meal callback tests."""
    now = datetime(2026, 8, 28, tzinfo=timezone.utc)
    draft = MealLogDraft(
        date=date(2026, 8, 28),
        meal_type=MealType.LUNCH,
        description="Soup",
    )
    return ConversationState(
        workflow_kind=ConversationWorkflowKind.MEAL_LOG,
        step=step,
        meal_draft=draft,
        request_id=request_id,
        revision=revision,
        created_at=now,
        updated_at=now,
        expires_at=int((now + timedelta(hours=24)).timestamp()),
    )


def _meal_callback_update(action: str, request_id: str) -> dict[str, object]:
    """Build a callback update for the active meal route."""
    return {
        "callback_query": {
            "id": "callback-1",
            "from": {"id": 1},
            "message": {"chat": {"id": 1, "type": "private"}},
            "data": f"meal:{action}:{request_id}",
        }
    }


def test_submit_meals_startup_renders_recent_today_and_yesterday(
    handler: BotHandler,
) -> None:
    """The active startup route shows both calendar history buckets."""
    handler.repo.get_conversation_state.return_value = None
    handler.repo.save_conversation_state.return_value = True
    handler.repo.get_meal_history.return_value = [
        make_meal(date(2026, 8, 28), MealType.DINNER, "Pasta"),
        make_meal(date(2026, 8, 27), MealType.BREAKFAST, "Toast"),
    ]

    handler.handle_update(_message_update("/submit_meals"))

    startup = handler.telegram_api.send_message.call_args_list[0].args[1]
    assert "Today\n- Dinner: Pasta" in startup
    assert "Yesterday\n- Breakfast: Toast" in startup
    assert handler.telegram_api.send_message.call_args_list[-1].args[1] == (
        MEAL_INPUT_PROMPT
    )


def test_meal_confirm_retry_duplicate_and_done_are_idempotent(
    handler: BotHandler,
) -> None:
    """Confirm delivery can retry, and duplicate/done controls stay scoped."""
    request_id = "00000000-0000-0000-0000-000000000001"
    state = _meal_state(
        ConversationWorkflowStep.AWAITING_MEAL_CONFIRMATION,
        request_id=request_id,
    )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.confirm_meal_and_transition.return_value = True
    handler.telegram_api.send_meal_saved.side_effect = TelegramAPIError(
        "delivery"
    )

    handler.handle_update(_meal_callback_update("confirm", request_id))

    handler.repo.confirm_meal_and_transition.assert_called_once()
    assert handler.telegram_api.answer_callback_query.call_args.args == (
        "callback-1",
        "Meal saved",
    )

    saved_state = _meal_state(
        ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION,
        request_id=request_id,
        revision=1,
    )
    handler.repo.get_conversation_state.return_value = saved_state
    handler.telegram_api.send_meal_saved.side_effect = None
    handler.handle_update(_meal_callback_update("confirm", request_id))
    handler.handle_update(_meal_callback_update("done", request_id))

    handler.repo.delete_conversation_state.assert_called_once_with(
        "1",
        expected_revision=1,
        expected_request_id=request_id,
        expected_step=ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION,
    )


@pytest.mark.parametrize("action", ["cancel", "add", "done"])
def test_meal_callbacks_handle_persistence_loss_and_acknowledge(
    handler: BotHandler, action: str
) -> None:
    """All active meal controls report conditional and persistence failures."""
    request_id = "00000000-0000-0000-0000-000000000002"
    step = (
        ConversationWorkflowStep.AWAITING_MEAL_CONFIRMATION
        if action == "cancel"
        else ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION
    )
    handler.repo.get_conversation_state.return_value = _meal_state(
        step, request_id=request_id
    )
    if action == "cancel" or action == "done":
        handler.repo.delete_conversation_state.return_value = False
    else:
        handler.repo.transition_conversation_state.return_value = False

    handler.handle_update(_meal_callback_update(action, request_id))

    assert handler.telegram_api.answer_callback_query.call_args.args[0] == (
        "callback-1"
    )
    assert "stale" in handler.telegram_api.send_message.call_args.args[1]


@pytest.mark.parametrize("action", ["cancel", "add", "done"])
def test_meal_callbacks_contain_dynamodb_failures(
    handler: BotHandler, action: str
) -> None:
    """A DynamoDB exception is user-visible and does not lose the callback."""
    request_id = "00000000-0000-0000-0000-000000000005"
    step = (
        ConversationWorkflowStep.AWAITING_MEAL_CONFIRMATION
        if action == "cancel"
        else ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION
    )
    handler.repo.get_conversation_state.return_value = _meal_state(
        step, request_id=request_id
    )
    method = (
        handler.repo.delete_conversation_state
        if action != "add"
        else handler.repo.transition_conversation_state
    )
    method.side_effect = RuntimeError("dynamodb unavailable")

    handler.handle_update(_meal_callback_update(action, request_id))

    assert handler.telegram_api.answer_callback_query.call_args.args[0] == (
        "callback-1"
    )
    assert "couldn't" in handler.telegram_api.send_message.call_args.args[1]


def test_stale_meal_callback_does_not_mutate_replacement(
    handler: BotHandler,
) -> None:
    """A callback from an old submission cannot affect a new submission."""
    old_id = "00000000-0000-0000-0000-000000000003"
    new_state = _meal_state(
        ConversationWorkflowStep.AWAITING_MEAL_CONFIRMATION,
        request_id="00000000-0000-0000-0000-000000000004",
    )
    handler.repo.get_conversation_state.return_value = new_state

    handler.handle_update(_meal_callback_update("confirm", old_id))

    handler.repo.confirm_meal_and_transition.assert_not_called()
    assert "stale" in handler.telegram_api.send_message.call_args.args[1]
    handler.telegram_api.answer_callback_query.assert_called_once()


@pytest.mark.parametrize("action", ["back", "done", "close"])
def test_profile_navigation_controls_are_state_scoped(
    handler: BotHandler, action: str
) -> None:
    """Profile navigation cannot operate when its workflow is absent."""
    handler.repo.get_conversation_state.return_value = None

    handler.handle_update(
        {
            "callback_query": {
                "id": f"profile-{action}",
                "from": {"id": 1},
                "message": {"chat": {"id": 1, "type": "private"}},
                "data": f"profile:{action}",
            }
        }
    )

    handler.repo.delete_conversation_state.assert_not_called()
    assert (
        "no longer active"
        in handler.telegram_api.send_message.call_args.args[1]
    )
    handler.telegram_api.answer_callback_query.assert_called_once()


def test_stale_numbered_profile_removal_does_not_mutate_current_profile(
    handler: BotHandler,
) -> None:
    """A removal button with an old profile revision is harmless."""
    state = ConversationState(
        workflow_kind=ConversationWorkflowKind.PROFILE_EDIT,
        step=ConversationWorkflowStep.AWAITING_PROFILE_INPUT,
        profile_category=ProfileEditCategory.FAMILY,
        profile_operation=ProfileEditOperation.REMOVE,
        revision=2,
        created_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
        updated_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
        expires_at=1_800_000_000,
    )
    handler.repo.get_conversation_state.return_value = state
    current_profile = make_profile()
    current_profile = current_profile.model_copy(update={"profile_revision": 3})
    handler.repo.get_profile.return_value = current_profile

    handler.handle_update(
        {
            "callback_query": {
                "id": "profile-remove",
                "from": {"id": 1},
                "message": {"chat": {"id": 1, "type": "private"}},
                "data": "profile:remove:family:1:2",
            }
        }
    )

    handler.repo.remove_profile_item_and_transition_state.assert_not_called()
    assert "stale" in handler.telegram_api.send_message.call_args.args[1]


@pytest.mark.parametrize("action", ["back", "done", "close"])
def test_profile_navigation_success_only_changes_owned_state(
    handler: BotHandler, action: str
) -> None:
    """Active profile controls transition or delete only their own state."""
    state = ConversationState(
        workflow_kind=ConversationWorkflowKind.PROFILE_EDIT,
        step=(
            ConversationWorkflowStep.AWAITING_PROFILE_INPUT
            if action == "back"
            else ConversationWorkflowStep.PROFILE_MENU
        ),
        profile_category=(
            ProfileEditCategory.FAMILY if action == "back" else None
        ),
        profile_operation=(
            ProfileEditOperation.ADD if action == "back" else None
        ),
        revision=4,
        created_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
        updated_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
        expires_at=1_800_000_000,
    )
    handler.repo.get_conversation_state.return_value = state
    if action == "back":
        handler.repo.transition_conversation_state.return_value = True
    else:
        handler.repo.delete_conversation_state.return_value = True

    handler.handle_update(
        {
            "callback_query": {
                "id": f"profile-success-{action}",
                "from": {"id": 1},
                "message": {"chat": {"id": 1, "type": "private"}},
                "data": f"profile:{action}",
            }
        }
    )

    if action == "back":
        handler.repo.transition_conversation_state.assert_called_once_with(
            "1",
            handler.repo.transition_conversation_state.call_args.args[1],
            expected_revision=4,
        )
        handler.repo.delete_conversation_state.assert_not_called()
    else:
        handler.repo.delete_conversation_state.assert_called_once_with(
            "1", expected_revision=4
        )
        handler.repo.transition_conversation_state.assert_not_called()
    handler.telegram_api.answer_callback_query.assert_called_once()


@pytest.mark.parametrize("command", ["cancel", "today", "checkin", "grocery"])
def test_removed_commands_remain_unavailable(
    handler: BotHandler, command: str
) -> None:
    """Retired top-level commands are not reintroduced by routing."""
    handler.handle_update(_message_update(f"/{command}"))

    assert handler.telegram_api.send_message.call_args.args[1] == (
        f"Unknown command: /{command}. Type /help for options."
    )


def test_retired_callback_payload_is_unavailable(handler: BotHandler) -> None:
    """Retired callback namespaces receive no active workflow mutation."""
    handler.handle_update(
        {
            "callback_query": {
                "id": "retired",
                "from": {"id": 1},
                "message": {"chat": {"id": 1, "type": "private"}},
                "data": "checkin:confirm:today",
            }
        }
    )

    handler.repo.get_conversation_state.assert_not_called()
    assert (
        "invalid or outdated"
        in (handler.telegram_api.send_message.call_args.args[1])
    )
    handler.telegram_api.answer_callback_query.assert_called_once()
