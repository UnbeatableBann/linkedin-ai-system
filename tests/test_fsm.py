"""
tests/test_fsm.py
──────────────────
Tests for the conversation state machine.

Covers:
  - Every valid transition
  - Every invalid transition (should raise)
  - Universal transitions (cancel, session_expired)
  - Guard conditions
  - allowed_events() helper
"""

import pytest

from app.session.fsm import Event, InvalidTransitionError, allowed_events, can_transition, transition
from app.session.models import SessionState


# ── Valid transitions ──────────────────────────────────────────────────────


class TestValidTransitions:
    def test_idle_to_onboarding(self):
        assert transition(SessionState.IDLE, Event.START_ONBOARDING) == SessionState.ONBOARDING

    def test_idle_to_collecting(self):
        assert transition(SessionState.IDLE, Event.START_POST) == SessionState.COLLECTING

    def test_idle_to_generating_direct(self):
        assert transition(SessionState.IDLE, Event.START_POST_DIRECT) == SessionState.GENERATING

    def test_onboarding_to_idle(self):
        assert transition(SessionState.ONBOARDING, Event.ONBOARDING_DONE) == SessionState.IDLE

    def test_collecting_to_generating(self):
        assert transition(SessionState.COLLECTING, Event.ENOUGH_INFO) == SessionState.GENERATING

    def test_collecting_direct_to_generating(self):
        assert transition(SessionState.COLLECTING, Event.START_POST_DIRECT) == SessionState.GENERATING

    def test_generating_to_reviewing(self):
        assert transition(SessionState.GENERATING, Event.DRAFT_READY) == SessionState.REVIEWING

    def test_generating_failed_to_collecting(self):
        assert transition(SessionState.GENERATING, Event.GENERATION_FAILED) == SessionState.COLLECTING

    def test_reviewing_to_refining(self):
        assert transition(SessionState.REVIEWING, Event.REQUEST_EDIT) == SessionState.REFINING

    def test_reviewing_to_scheduling(self):
        assert transition(SessionState.REVIEWING, Event.APPROVE_DRAFT) == SessionState.SCHEDULING

    def test_reviewing_to_idle_on_discard(self):
        assert transition(SessionState.REVIEWING, Event.DISCARD_DRAFT) == SessionState.IDLE

    def test_refining_to_reviewing_on_success(self):
        assert transition(SessionState.REFINING, Event.REFINEMENT_READY) == SessionState.REVIEWING

    def test_refining_to_reviewing_on_failure(self):
        assert transition(SessionState.REFINING, Event.REFINEMENT_FAILED) == SessionState.REVIEWING

    def test_scheduling_to_scheduled(self):
        assert transition(SessionState.SCHEDULING, Event.SCHEDULE_CONFIRMED) == SessionState.SCHEDULED

    def test_scheduling_cancelled_to_idle(self):
        assert transition(SessionState.SCHEDULING, Event.SCHEDULE_CANCELLED) == SessionState.IDLE


# ── Universal transitions ──────────────────────────────────────────────────


class TestUniversalTransitions:
    @pytest.mark.parametrize("state", list(SessionState))
    def test_cancel_from_any_state(self, state: SessionState):
        """Cancel must work from every single state."""
        result = transition(state, Event.CANCEL)
        assert result == SessionState.IDLE

    @pytest.mark.parametrize("state", list(SessionState))
    def test_session_expired_from_any_state(self, state: SessionState):
        """Session expiry must reset to IDLE from any state."""
        result = transition(state, Event.SESSION_EXPIRED)
        assert result == SessionState.IDLE


# ── Invalid transitions ────────────────────────────────────────────────────


class TestInvalidTransitions:
    def test_idle_cannot_get_draft_ready(self):
        with pytest.raises(InvalidTransitionError) as exc_info:
            transition(SessionState.IDLE, Event.DRAFT_READY)
        assert exc_info.value.from_state == SessionState.IDLE
        assert exc_info.value.event == Event.DRAFT_READY

    def test_generating_cannot_approve(self):
        with pytest.raises(InvalidTransitionError):
            transition(SessionState.GENERATING, Event.APPROVE_DRAFT)

    def test_scheduled_cannot_request_edit(self):
        with pytest.raises(InvalidTransitionError):
            transition(SessionState.SCHEDULED, Event.REQUEST_EDIT)

    def test_collecting_cannot_approve(self):
        with pytest.raises(InvalidTransitionError):
            transition(SessionState.COLLECTING, Event.APPROVE_DRAFT)

    def test_reviewing_cannot_be_done(self):
        with pytest.raises(InvalidTransitionError):
            transition(SessionState.REVIEWING, Event.ONBOARDING_DONE)

    def test_idle_cannot_get_refinement_ready(self):
        with pytest.raises(InvalidTransitionError):
            transition(SessionState.IDLE, Event.REFINEMENT_READY)

    def test_completely_unknown_event(self):
        with pytest.raises(InvalidTransitionError):
            transition(SessionState.IDLE, "banana")


# ── can_transition ─────────────────────────────────────────────────────────


class TestCanTransition:
    def test_valid_returns_true(self):
        assert can_transition(SessionState.IDLE, Event.START_POST) is True

    def test_invalid_returns_false(self):
        assert can_transition(SessionState.IDLE, Event.DRAFT_READY) is False

    def test_cancel_always_true(self):
        for state in SessionState:
            assert can_transition(state, Event.CANCEL) is True

    def test_unknown_event_returns_false(self):
        assert can_transition(SessionState.IDLE, "not_real") is False


# ── allowed_events ─────────────────────────────────────────────────────────


class TestAllowedEvents:
    def test_idle_includes_cancel(self):
        events = allowed_events(SessionState.IDLE)
        assert Event.CANCEL in events

    def test_idle_includes_start_post(self):
        events = allowed_events(SessionState.IDLE)
        assert Event.START_POST in events

    def test_reviewing_includes_approve_and_edit(self):
        events = allowed_events(SessionState.REVIEWING)
        assert Event.APPROVE_DRAFT in events
        assert Event.REQUEST_EDIT in events
        assert Event.DISCARD_DRAFT in events

    def test_all_states_have_cancel(self):
        for state in SessionState:
            assert Event.CANCEL in allowed_events(state)


# ── Error message quality ──────────────────────────────────────────────────


class TestErrorMessages:
    def test_error_includes_from_state(self):
        try:
            transition(SessionState.IDLE, Event.DRAFT_READY)
        except InvalidTransitionError as exc:
            assert "idle" in str(exc).lower()

    def test_error_includes_event(self):
        try:
            transition(SessionState.IDLE, Event.DRAFT_READY)
        except InvalidTransitionError as exc:
            assert "draft_ready" in str(exc).lower()
