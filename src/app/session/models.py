"""
app/session/models.py
─────────────────────
Session state definitions and the context schema.

SessionState: every valid FSM state
SessionContext: typed wrapper around the session.context JSONB field
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class SessionState(StrEnum):
    IDLE = "idle"
    ONBOARDING = "onboarding"
    COLLECTING = "collecting"
    GENERATING = "generating"
    REVIEWING = "reviewing"
    REFINING = "refining"
    SCHEDULING = "scheduling"
    SCHEDULED = "scheduled"


class OnboardingStep(StrEnum):
    """Sub-steps within the ONBOARDING state."""

    ZERNIO_KEY = "zernio_key"
    LLM_CHOICE = "llm_choice"
    LLM_KEY = "llm_key"
    LINKEDIN_OAUTH = "linkedin_oauth"
    LINKEDIN_ORG = "linkedin_org"
    TIMEZONE = "timezone"
    COMPLETE = "complete"


class SessionContext(BaseModel):
    """
    Typed wrapper for the session.context JSONB field.
    All fields are optional — only populated as the conversation progresses.

    Stored as a plain dict in Supabase; parsed with SessionContext(**ctx)
    whenever we need typed access.
    """

    # ── Onboarding sub-state ────────────────────────────────────────────────
    onboarding_step: OnboardingStep | None = None
    onboarding_llm_provider: str | None = None
    onboarding_llm_model: str | None = None
    connect_token: str | None = None
    connect_token_issued_at: str | None = None  # ISO datetime string
    pending_orgs: list[dict[str, Any]] = Field(default_factory=list)

    # ── Content collection ──────────────────────────────────────────────────
    topic: str | None = None
    tone: str | None = None
    audience: str | None = None
    length_pref: str | None = None
    clarification_round: int = 0  # How many clarifying questions we've asked

    # ── Draft ───────────────────────────────────────────────────────────────
    draft_content: str | None = None
    draft_version: int = 0
    edit_history: list[str] = Field(default_factory=list)

    # ── Scheduling ──────────────────────────────────────────────────────────
    proposed_schedule_time: str | None = None  # ISO string, awaiting user confirmation

    # ── Message queued during async generation ──────────────────────────────
    pending_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize for storage in Supabase JSONB."""
        return self.model_dump(exclude_none=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionContext":
        return cls(**data)

    def clear_draft(self) -> None:
        """Reset all draft-related fields (on discard or new post)."""
        self.topic = None
        self.tone = None
        self.audience = None
        self.length_pref = None
        self.clarification_round = 0
        self.draft_content = None
        self.draft_version = 0
        self.edit_history = []
        self.proposed_schedule_time = None
        self.pending_message = None

    def clear_onboarding(self) -> None:
        """Reset onboarding-only transient state."""
        self.onboarding_step = None
        self.onboarding_llm_provider = None
        self.onboarding_llm_model = None
        self.connect_token = None
        self.connect_token_issued_at = None
        self.pending_orgs = []

    def push_edit_history(self, content: str) -> None:
        """Save current draft to history before overwriting with refinement."""
        if content and content not in self.edit_history:
            self.edit_history.append(content)
            # Keep only last 5 versions to avoid bloating the JSONB field
            self.edit_history = self.edit_history[-5:]
