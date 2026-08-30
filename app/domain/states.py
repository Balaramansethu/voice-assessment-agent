"""Domain enums and the interview/call state machines.

These are the single authority on which transitions are legal. The LLM proposes
an intent; this module decides whether it is allowed. Nothing here touches the DB
or any provider — it is pure, and therefore fully unit-testable.
"""
from __future__ import annotations

from enum import Enum


class InterviewStatus(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    INTERRUPTED = "INTERRUPTED"
    COMPLETED = "COMPLETED"
    RESCHEDULED = "RESCHEDULED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class CallDirection(str, Enum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class CallStatus(str, Enum):
    INITIATED = "INITIATED"
    RINGING = "RINGING"
    ANSWERED = "ANSWERED"
    NO_ANSWER = "NO_ANSWER"
    VOICEMAIL = "VOICEMAIL"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    DISCONNECTED = "DISCONNECTED"
    FAILED = "FAILED"
    ABANDONED = "ABANDONED"


class Intent(str, Enum):
    """What the LLM classifies the candidate's request into."""
    CONTINUE_INTERVIEW = "CONTINUE_INTERVIEW"
    START_INTERVIEW = "START_INTERVIEW"
    RESCHEDULE = "RESCHEDULE"
    TALK_TO_HUMAN = "TALK_TO_HUMAN"
    GENERAL_HELP = "GENERAL_HELP"
    UNKNOWN = "UNKNOWN"


class Action(str, Enum):
    """Deterministic actions the orchestrator can execute."""
    START = "START"
    RESUME = "RESUME"
    RESCHEDULE = "RESCHEDULE"
    ESCALATE = "ESCALATE"
    REJECT = "REJECT"


# Allowed interview transitions. Anything not listed here is illegal by default.
_ALLOWED_TRANSITIONS: dict[InterviewStatus, set[InterviewStatus]] = {
    InterviewStatus.NOT_STARTED: {
        InterviewStatus.IN_PROGRESS,
        InterviewStatus.RESCHEDULED,
        InterviewStatus.CANCELLED,
        InterviewStatus.EXPIRED,
    },
    InterviewStatus.IN_PROGRESS: {
        InterviewStatus.COMPLETED,
        InterviewStatus.INTERRUPTED,
        InterviewStatus.RESCHEDULED,
        InterviewStatus.CANCELLED,
    },
    InterviewStatus.INTERRUPTED: {
        InterviewStatus.IN_PROGRESS,
        InterviewStatus.RESCHEDULED,
        InterviewStatus.CANCELLED,
        InterviewStatus.EXPIRED,
    },
    InterviewStatus.RESCHEDULED: {
        InterviewStatus.IN_PROGRESS,
        InterviewStatus.NOT_STARTED,
        InterviewStatus.CANCELLED,
        InterviewStatus.EXPIRED,
    },
    # Terminal states — no outgoing transitions.
    InterviewStatus.COMPLETED: set(),
    InterviewStatus.CANCELLED: set(),
    InterviewStatus.EXPIRED: set(),
}


class IllegalTransition(Exception):
    def __init__(self, current: InterviewStatus, target: InterviewStatus):
        self.current = current
        self.target = target
        super().__init__(f"Illegal interview transition: {current.value} -> {target.value}")


def can_transition(current: InterviewStatus, target: InterviewStatus) -> bool:
    return target in _ALLOWED_TRANSITIONS.get(current, set())


def assert_transition(current: InterviewStatus, target: InterviewStatus) -> None:
    if not can_transition(current, target):
        raise IllegalTransition(current, target)


def resolve_action(status: InterviewStatus, intent: Intent) -> Action:
    """Map (current interview status, candidate intent) -> the action the
    orchestrator should attempt. The orchestrator still validates the resulting
    transition under a row lock before persisting anything.
    """
    if intent == Intent.TALK_TO_HUMAN:
        return Action.ESCALATE

    if intent in (Intent.START_INTERVIEW, Intent.CONTINUE_INTERVIEW):
        if status == InterviewStatus.NOT_STARTED:
            return Action.START
        if status in (InterviewStatus.INTERRUPTED, InterviewStatus.IN_PROGRESS):
            # IN_PROGRESS is continuable — the candidate reconnected (or a prior call
            # dropped without being marked INTERRUPTED). Resume at the current question.
            return Action.RESUME
        if status == InterviewStatus.RESCHEDULED:
            return Action.START
        # COMPLETED / EXPIRED / CANCELLED
        return Action.REJECT

    if intent == Intent.RESCHEDULE:
        if status in (InterviewStatus.NOT_STARTED, InterviewStatus.INTERRUPTED,
                      InterviewStatus.RESCHEDULED):
            return Action.RESCHEDULE
        return Action.REJECT

    return Action.ESCALATE  # GENERAL_HELP / UNKNOWN -> hand off rather than guess
