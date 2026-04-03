"""
app/session/store.py
────────────────────
Supabase persistence for the conversation session.

Every request loads the session at the start and saves it at the end.
No in-memory session cache — every load hits Supabase.
This makes the system fully stateless: server restarts lose nothing.
"""

from datetime import UTC, datetime
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
    row = await _upsert_session_row(user_id)

    now = datetime.now(UTC)

    # Check expiry after atomic fetch/create.
    expires_at = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
    if expires_at < now:
        logger.info("session.expired", user_id=str(user_id))
        return await _reset_session(user_id, row["id"])

    return Session(
        session_id=UUID(row["id"]),
        user_id=user_id,
        state=SessionState(row["state"]),
        context=SessionContext.from_dict(row.get("context", {})),
        draft_id=UUID(row["draft_id"]) if row.get("draft_id") else None,
    )


async def save_session(session: Session) -> None:
    """
    Persist the current session state and context back to Supabase.
    Called at the end of every request handler.
    """
    db = await get_db()
    settings = get_settings()

    from datetime import timedelta

    new_expires = (datetime.now(UTC) + timedelta(hours=settings.session_ttl_hours)).isoformat()

    await (
        db.table("sessions")
        .update(
            {
                "state": session.state.value,
                "context": session.context.to_dict(),
                "draft_id": str(session.draft_id) if session.draft_id else None,
                "expires_at": new_expires,
            }
        )
        .eq("id", str(session.id))
        .execute()
    )

    logger.debug("session.saved", session_id=str(session.id), state=session.state)


async def reset_session_to_idle(user_id: UUID) -> Session:
    """Reset a user's session to IDLE, clearing all context."""
    row = await _upsert_session_row(user_id)
    return await _reset_session(user_id, row["id"])


# ── Private helpers ────────────────────────────────────────────────────────


async def _upsert_session_row(user_id: UUID) -> dict:
    db = await get_db()
    settings = get_settings()
    from datetime import timedelta

    expires_at = (datetime.now(UTC) + timedelta(hours=settings.session_ttl_hours)).isoformat()

    result = await (
        db.table("sessions")
        .upsert(
            {
                "user_id": str(user_id),
                "state": SessionState.IDLE.value,
                "context": {},
                "expires_at": expires_at,
            },
            on_conflict="user_id",
            returning="representation",
            ignore_duplicates=True,
        )
        .execute()
    )

    if not result or not result.data:
        # If duplicate rows were ignored and representation is empty, fetch the row directly.
        fallback = await db.table("sessions").select("*").eq("user_id", str(user_id)).single().execute()
        row = _extract_row(fallback)
        if row is None:
            raise RuntimeError(f"Failed to fetch or create session for user {user_id}")
        return row

    row = _extract_row(result)
    if row is None:
        raise RuntimeError(f"Invalid session upsert response for user {user_id}")
    return row


async def _reset_session(user_id: UUID, session_id: str) -> Session:
    db = await get_db()
    settings = get_settings()
    from datetime import timedelta

    expires_at = (datetime.now(UTC) + timedelta(hours=settings.session_ttl_hours)).isoformat()

    await (
        db.table("sessions")
        .update(
            {
                "state": SessionState.IDLE.value,
                "context": {},
                "draft_id": None,
                "expires_at": expires_at,
            }
        )
        .eq("id", session_id)
        .execute()
    )

    return Session(
        session_id=UUID(session_id),
        user_id=user_id,
        state=SessionState.IDLE,
        context=SessionContext(),
    )


def _extract_row(result: object) -> dict | None:
    """Safely normalize Supabase response to a single row dict."""
    if not result or not getattr(result, "data", None):
        return None

    data = result.data
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        return data[0] if data else None
    return None
