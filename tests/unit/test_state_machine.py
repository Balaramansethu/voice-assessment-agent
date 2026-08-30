"""Pure state-machine rules — no DB, no external services."""
from app.domain.states import (
    Action,
    Intent,
    InterviewStatus,
    IllegalTransition,
    assert_transition,
    can_transition,
    resolve_action,
)
import pytest


def test_not_started_can_start():
    assert can_transition(InterviewStatus.NOT_STARTED, InterviewStatus.IN_PROGRESS)


def test_interrupted_can_resume():
    assert can_transition(InterviewStatus.INTERRUPTED, InterviewStatus.IN_PROGRESS)


def test_completed_is_terminal():
    assert not can_transition(InterviewStatus.COMPLETED, InterviewStatus.IN_PROGRESS)
    with pytest.raises(IllegalTransition):
        assert_transition(InterviewStatus.COMPLETED, InterviewStatus.IN_PROGRESS)


def test_expired_cannot_start():
    assert not can_transition(InterviewStatus.EXPIRED, InterviewStatus.IN_PROGRESS)


def test_resolve_action_start_on_not_started():
    assert resolve_action(InterviewStatus.NOT_STARTED, Intent.START_INTERVIEW) == Action.START


def test_resolve_action_resume_on_interrupted():
    assert resolve_action(InterviewStatus.INTERRUPTED, Intent.CONTINUE_INTERVIEW) == Action.RESUME


def test_resolve_action_reject_on_completed():
    assert resolve_action(InterviewStatus.COMPLETED, Intent.CONTINUE_INTERVIEW) == Action.REJECT


def test_talk_to_human_always_escalates():
    for status in InterviewStatus:
        assert resolve_action(status, Intent.TALK_TO_HUMAN) == Action.ESCALATE


def test_unknown_intent_escalates_not_guesses():
    assert resolve_action(InterviewStatus.NOT_STARTED, Intent.UNKNOWN) == Action.ESCALATE
