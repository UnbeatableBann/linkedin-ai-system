"""
app/session/fsm.py
──────────────────
Finite State Machine for the conversation.

Every state transition goes through here — no state changes happen
directly in handler code. This makes the logic auditable and testable.

Rules:
  - transition() is pure (no side effects, no DB calls)
  - Guards are checked before transitioning
  - Invalid transitions raise InvalidTransitionError (not silently ignored)
  - /cancel is a universal escape hatch from any state back to IDLE
"""

from app.core.logging import get_logger
from app.session.models import SessionState

logger = get_logger(__name__)


class InvalidTransitionError(Exception):
    """Raised when a state transition is not permitted."""

    def __init__(self, from_state: SessionState, event: str) -> None:
        self.from_state = from_state
        self.event = event
        super().__init__(f"Invalid transition: {from_state} --[{event}]--> ?")


# ── Event names ────────────────────────────────────────────────────────────
# These are string constants used as event identifiers throughout the app.
# Keeping them here avoids magic strings scattered across handlers.


class Event:
    # Lifecycle
    CANCEL = "cancel"  # Universal: any → IDLE
    SESSION_EXPIRED = "session_expired"  # Any → IDLE (with notification)

    # Onboarding
    START_ONBOARDING = "start_onboarding"  # IDLE → ONBOARDING (new user)
    ONBOARDING_DONE = "onboarding_done"  # ONBOARDING → IDLE

    # Content creation
    START_POST = "start_post"  # IDLE → COLLECTING
    START_POST_DIRECT = "start_post_direct"  # IDLE → GENERATING (user sent full draft)
    ENOUGH_INFO = "enough_info"  # COLLECTING → GENERATING
    DRAFT_READY = "draft_ready"  # GENERATING → REVIEWING
    GENERATION_FAILED = "generation_failed"  # GENERATING → COLLECTING

    # Reviewing
    REQUEST_EDIT = "request_edit"  # REVIEWING → REFINING
    APPROVE_DRAFT = "approve_draft"  # REVIEWING → SCHEDULING
    DISCARD_DRAFT = "discard_draft"  # REVIEWING → IDLE
    REFINEMENT_READY = "refinement_ready"  # REFINING → REVIEWING
    REFINEMENT_FAILED = "refinement_failed"  # REFINING → REVIEWING (show error)

    # Scheduling
    SCHEDULE_CONFIRMED = "schedule_confirmed"  # SCHEDULING → SCHEDULED
    SCHEDULE_CANCELLED = "schedule_cancelled"  # SCHEDULING → IDLE


# ── Transition table ───────────────────────────────────────────────────────
# Maps (current_state, event) → next_state
# CANCEL and SESSION_EXPIRED are handled separately as universal transitions.

_TRANSITIONS: dict[tuple[SessionState, str], SessionState] = {
    # From IDLE
    (SessionState.IDLE, Event.START_ONBOARDING): SessionState.ONBOARDING,
    (SessionState.IDLE, Event.START_POST): SessionState.COLLECTING,
    (SessionState.IDLE, Event.START_POST_DIRECT): SessionState.GENERATING,
    # From ONBOARDING
    (SessionState.ONBOARDING, Event.ONBOARDING_DONE): SessionState.IDLE,
    # From COLLECTING
    (SessionState.COLLECTING, Event.ENOUGH_INFO): SessionState.GENERATING,
    (SessionState.COLLECTING, Event.START_POST_DIRECT): SessionState.GENERATING,
    # From GENERATING
    (SessionState.GENERATING, Event.DRAFT_READY): SessionState.REVIEWING,
    (SessionState.GENERATING, Event.GENERATION_FAILED): SessionState.COLLECTING,
    # From REVIEWING
    (SessionState.REVIEWING, Event.REQUEST_EDIT): SessionState.REFINING,
    (SessionState.REVIEWING, Event.APPROVE_DRAFT): SessionState.SCHEDULING,
    (SessionState.REVIEWING, Event.DISCARD_DRAFT): SessionState.IDLE,
    # From REFINING
    (SessionState.REFINING, Event.REFINEMENT_READY): SessionState.REVIEWING,
    (SessionState.REFINING, Event.REFINEMENT_FAILED): SessionState.REVIEWING,
    # From SCHEDULING
    (SessionState.SCHEDULING, Event.SCHEDULE_CONFIRMED): SessionState.SCHEDULED,
    (SessionState.SCHEDULING, Event.SCHEDULE_CANCELLED): SessionState.IDLE,
    # From SCHEDULED — user cancelled a queued post
    (SessionState.SCHEDULED, Event.CANCEL): SessionState.IDLE,
}

# States that are allowed to loop back (e.g. ask another clarifying question)
_LOOPBACK_STATES = {SessionState.ONBOARDING, SessionState.COLLECTING, SessionState.SCHEDULING}


def transition(current_state: SessionState, event: str) -> SessionState:
    """
    Compute the next state given the current state and an event.

    Universal transitions (work from ANY state):
      - CANCEL          → IDLE
      - SESSION_EXPIRED → IDLE

    All other transitions are validated against the table above.
    Raises InvalidTransitionError for illegal transitions.
    """
    # Universal escapes
    if event in (Event.CANCEL, Event.SESSION_EXPIRED):
        logger.info(
            "fsm.transition",
            from_state=current_state,
            input_event=event,
            to_state=SessionState.IDLE,
        )
        return SessionState.IDLE

    # Loopback: some states stay in the same state while gathering info
    if current_state in _LOOPBACK_STATES and event == "continue":
        return current_state

    # Look up in transition table
    key = (current_state, event)
    if key not in _TRANSITIONS:
        raise InvalidTransitionError(current_state, event)

    next_state = _TRANSITIONS[key]
    logger.info("fsm.transition", from_state=current_state, input_event=event, to_state=next_state)
    return next_state


def can_transition(current_state: SessionState, event: str) -> bool:
    """Check if a transition is valid without raising."""
    if event in (Event.CANCEL, Event.SESSION_EXPIRED):
        return True
    return (current_state, event) in _TRANSITIONS


def allowed_events(state: SessionState) -> list[str]:
    """Return all valid events from a given state (for debugging/help messages)."""
    universal = [Event.CANCEL]
    specific = [event for (s, event) in _TRANSITIONS if s == state]
    return universal + specific
