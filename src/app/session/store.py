"""
app/session/store.py
────────────────────
Supabase persistence for the conversation session.

Every request loads the session at the start and saves it at the end.
No in-memory session cache — every load hits Supabase.
This makes the system fully stateless: server restarts lose nothing.
"""

from datetime import datetime, timezone
from uuid import UUID

from app.config import get_settings
from app.core.logging import get_logger
from app.db.client import get_db
from app.session.models import SessionContext, SessionState

logger = get_logger(__name__)


class Session:
    """
    Hydrated session object used throughout a single request.
    Wraps the raw DB row with typed access to state and context.
    """

    def __init__(
        self,
        session_id: UUID,
        user_id: UUID,
        state: SessionState,
        context: SessionContext,
        draft_id: UUID | None = None,
    ) -> None:
        self.id = session_id
        self.user_id = user_id
        self.state = state
        self.context = context
        self.draft_id = draft_id


async def get_or_create_session(user_id: UUID) -> Session:
    """
    Load the user's session from Supabase.
    If no session exists (first message), create one in IDLE state.
    If the session is expired, reset it to IDLE and notify downstream.
    """
    db = get_db()
    settings = get_settings()

    result = (
        db.table("sessions")
        .select("*")
        .eq("user_id", str(user_id))
        .maybe_single()
        .execute()
    )

    now = datetime.now(timezone.utc)

    if result.data:
        row = result.data

        # Check expiry
        expires_at = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
        if expires_at < now:
            logger.info("session.expired", user_id=str(user_id))
            # Reset to IDLE — expired session is as good as no session
            return await _reset_session(user_id, row["id"])

        return Session(
            session_id=UUID(row["id"]),
            user_id=user_id,
            state=SessionState(row["state"]),
            context=SessionContext.from_dict(row.get("context", {})),
            draft_id=UUID(row["draft_id"]) if row.get("draft_id") else None,
        )

    # No session — create one
    return await _create_session(user_id)


async def save_session(session: Session) -> None:
    """
    Persist the current session state and context back to Supabase.
    Called at the end of every request handler.
    """
    db = get_db()
    settings = get_settings()

    from datetime import timedelta
    new_expires = (
        datetime.now(timezone.utc) + timedelta(hours=settings.session_ttl_hours)
    ).isoformat()

    db.table("sessions").update(
        {
            "state": session.state.value,
            "context": session.context.to_dict(),
            "draft_id": str(session.draft_id) if session.draft_id else None,
            "expires_at": new_expires,
        }
    ).eq("id", str(session.id)).execute()

    logger.debug("session.saved", session_id=str(session.id), state=session.state)


async def reset_session_to_idle(user_id: UUID) -> Session:
    """Reset a user's session to IDLE, clearing all context."""
    db = get_db()
    result = (
        db.table("sessions")
        .select("id")
        .eq("user_id", str(user_id))
        .maybe_single()
        .execute()
    )
    if result.data:
        return await _reset_session(user_id, result.data["id"])
    return await _create_session(user_id)


# ── Private helpers ────────────────────────────────────────────────────────


async def _create_session(user_id: UUID) -> Session:
    db = get_db()
    settings = get_settings()
    from datetime import timedelta

    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=settings.session_ttl_hours)
    ).isoformat()

    result = (
        db.table("sessions")
        .insert(
            {
                "user_id": str(user_id),
                "state": SessionState.IDLE.value,
                "context": {},
                "expires_at": expires_at,
            }
        )
        .execute()
    )

    if not result.data:
        raise RuntimeError(f"Failed to create session for user {user_id} — INSERT returned no data")

    row = result.data[0]
    logger.info("session.created", user_id=str(user_id))

    return Session(
        session_id=UUID(row["id"]),
        user_id=user_id,
        state=SessionState.IDLE,
        context=SessionContext(),
    )


async def _reset_session(user_id: UUID, session_id: str) -> Session:
    db = get_db()
    settings = get_settings()
    from datetime import timedelta

    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=settings.session_ttl_hours)
    ).isoformat()

    db.table("sessions").update(
        {
            "state": SessionState.IDLE.value,
            "context": {},
            "draft_id": None,
            "expires_at": expires_at,
        }
    ).eq("id", session_id).execute()

    return Session(
        session_id=UUID(session_id),
        user_id=user_id,
        state=SessionState.IDLE,
        context=SessionContext(),
    )
