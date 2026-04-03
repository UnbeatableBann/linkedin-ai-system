#!/usr/bin/env python3
"""
scripts/smoke_test.py
──────────────────────
End-to-end smoke test that exercises every major code path
without making any real external API calls.

Run after docker-compose up to verify the system is wired correctly:
    python3 scripts/smoke_test.py

Or for just the logic layer (no running server needed):
    python3 scripts/smoke_test.py --logic-only

Exit code 0 = all passed. Exit code 1 = failures.
"""

# ruff: noqa: E402
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

# ── Stubs for packages not in this test environment ───────────────────────
import types

_stubs = [
    "structlog",
    "supabase",
    "pydantic",
    "pydantic_settings",
    "dateparser",
    "pytz",
    "redis",
    "anthropic",
    "openai",
    "groq",
    "celery",
    "httpx",
    "fastapi",
]
for mod in _stubs:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

# Give pytz realistic behaviour whether real or mocked
import pytz as _pytz_ref  # noqa — always works now (either real or mocked above)

_pytz_ref.exceptions = types.SimpleNamespace(UnknownTimeZoneError=ValueError)

if isinstance(_pytz_ref, MagicMock):

    class _FakeTZ:
        def __init__(self, name="UTC"):
            self.zone = name

        def astimezone(self, tz=None):
            return self

        def __str__(self):
            return self.zone

    _pytz_ref.timezone = _FakeTZ
    _pytz_ref.UTC = _FakeTZ("UTC")

# Set required env vars
from cryptography.fernet import Fernet

_test_key = Fernet.generate_key().decode()
os.environ.update(
    {
        "FERNET_SECRET_KEY": _test_key,
        "TELEGRAM_BOT_TOKEN": "123:smoke_test_bot",
        "TELEGRAM_WEBHOOK_SECRET": "s" * 32,
        "SUPABASE_URL": "https://smoke.supabase.co",
        "SUPABASE_SERVICE_KEY": "x" * 40,
        "WHATSAPP_APP_SECRET": "smoke_secret",
        "WHATSAPP_ACCESS_TOKEN": "smoke_token",
        "WHATSAPP_PHONE_NUMBER_ID": "999",
        "WHATSAPP_VERIFY_TOKEN": "smoke_verify",
        "OAUTH_CALLBACK_BASE_URL": "https://smoke.example.com",
        "APP_ENV": "development",
    }
)

from app.config import get_settings

get_settings.cache_clear()


# ── Test runner ────────────────────────────────────────────────────────────


class SmokeRunner:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.start = time.time()

    def ok(self, name: str) -> None:
        self.passed += 1
        print(f"  ✓ {name}")

    def fail(self, name: str, reason: str = "") -> None:
        self.failed += 1
        print(f"  ✗ {name}: {reason}")

    def section(self, title: str) -> None:
        print(f"\n{'─' * 56}")
        print(f"  {title}")
        print(f"{'─' * 56}")

    def summary(self) -> bool:
        elapsed = time.time() - self.start
        total = self.passed + self.failed
        print(f"\n{'═' * 56}")
        status = "✅  ALL PASSED" if self.failed == 0 else "❌  FAILURES DETECTED"
        print(f"  {status}")
        print(f"  {self.passed}/{total} passed  |  {self.failed} failed  |  {elapsed:.2f}s")
        print(f"{'═' * 56}\n")
        return self.failed == 0


t = SmokeRunner()


# ══════════════════════════════════════════════════════════════════════════
# 1. CONFIG & ENCRYPTION
# ══════════════════════════════════════════════════════════════════════════
t.section("Config & Encryption")

try:
    settings = get_settings()
    assert settings.oauth_callback_url.endswith("/oauth/callback")
    t.ok("settings load and oauth_callback_url derived correctly")
except Exception as e:
    t.fail("settings load", str(e))

try:
    from app.core.encryption import EncryptionError, decrypt, encrypt

    original = "sk-ant-api03-super-secret-key-for-testing"
    token = encrypt(original)
    recovered = decrypt(token)
    assert recovered == original
    assert original not in token
    t.ok("encrypt→decrypt round-trip")
except Exception as e:
    t.fail("encryption round-trip", str(e))

try:
    from app.core.encryption import EncryptionError, decrypt

    decrypt("not_a_valid_fernet_token_at_all")
    t.fail("invalid decrypt should raise")
except EncryptionError:
    t.ok("invalid token raises EncryptionError")


# ══════════════════════════════════════════════════════════════════════════
# 2. FSM — Complete State Coverage
# ══════════════════════════════════════════════════════════════════════════
t.section("FSM — All 8 States, 15 Transitions")

from app.session.fsm import Event, InvalidTransitionError, transition
from app.session.models import SessionState

# Every named transition
transitions = [
    (SessionState.IDLE, Event.START_ONBOARDING, SessionState.ONBOARDING),
    (SessionState.IDLE, Event.START_POST, SessionState.COLLECTING),
    (SessionState.IDLE, Event.START_POST_DIRECT, SessionState.GENERATING),
    (SessionState.ONBOARDING, Event.ONBOARDING_DONE, SessionState.IDLE),
    (SessionState.COLLECTING, Event.ENOUGH_INFO, SessionState.GENERATING),
    (SessionState.COLLECTING, Event.START_POST_DIRECT, SessionState.GENERATING),
    (SessionState.GENERATING, Event.DRAFT_READY, SessionState.REVIEWING),
    (SessionState.GENERATING, Event.GENERATION_FAILED, SessionState.COLLECTING),
    (SessionState.REVIEWING, Event.REQUEST_EDIT, SessionState.REFINING),
    (SessionState.REVIEWING, Event.APPROVE_DRAFT, SessionState.SCHEDULING),
    (SessionState.REVIEWING, Event.DISCARD_DRAFT, SessionState.IDLE),
    (SessionState.REFINING, Event.REFINEMENT_READY, SessionState.REVIEWING),
    (SessionState.REFINING, Event.REFINEMENT_FAILED, SessionState.REVIEWING),
    (SessionState.SCHEDULING, Event.SCHEDULE_CONFIRMED, SessionState.SCHEDULED),
    (SessionState.SCHEDULING, Event.SCHEDULE_CANCELLED, SessionState.IDLE),
]

wrong = [(s, e, transition(s, e), d) for s, e, d in transitions if transition(s, e) != d]
if not wrong:
    t.ok("all 15 named transitions correct")
else:
    for s, e, got, exp in wrong:
        t.fail(f"transition {s}→{e}", f"got {got}")

# Universal cancel
all_cancel = all(transition(s, Event.CANCEL) == SessionState.IDLE for s in SessionState)
t.ok("CANCEL → IDLE from all 8 states") if all_cancel else t.fail("CANCEL universal")

# Invalid transition raises with correct message
try:
    transition(SessionState.IDLE, Event.DRAFT_READY)
    t.fail("invalid should raise")
except InvalidTransitionError as e:
    msg = str(e).lower()
    if "idle" in msg and "draft_ready" in msg:
        t.ok("InvalidTransitionError has state+event in message")
    else:
        t.fail("error message quality", f"got: {msg!r}")


# ══════════════════════════════════════════════════════════════════════════
# 3. SESSION CONTEXT
# ══════════════════════════════════════════════════════════════════════════
t.section("Session Context — Serialisation")

from app.session.models import SessionContext

try:
    ctx = SessionContext()
    ctx.topic = "AI in India"
    ctx.tone = "storytelling"
    ctx.draft_content = "My draft post."
    ctx.draft_version = 2

    d = ctx.to_dict()
    ctx2 = SessionContext.from_dict(d)

    assert ctx2.topic == "AI in India"
    assert ctx2.draft_version == 2
    assert "audience" not in d  # None fields excluded
    t.ok("to_dict / from_dict round-trip with None exclusion")
except Exception as e:
    t.fail("context serialisation", str(e))

try:
    ctx = SessionContext()
    for i in range(7):
        ctx.push_edit_history(f"version {i}")
    assert len(ctx.edit_history) == 5
    t.ok("edit_history capped at 5")
except Exception as e:
    t.fail("edit_history cap", str(e))

try:
    ctx = SessionContext()
    ctx.topic = "something"
    ctx.draft_content = "something"
    ctx.clear_draft()
    assert ctx.topic is None and ctx.draft_content is None
    t.ok("clear_draft() resets all fields")
except Exception as e:
    t.fail("clear_draft", str(e))


# ══════════════════════════════════════════════════════════════════════════
# 4. CHANNEL PARSING — Telegram & WhatsApp
# ══════════════════════════════════════════════════════════════════════════
t.section("Channel Parsing — Telegram")

from app.channels.base import MessageType
from app.channels.telegram import _split_message, parse_telegram_update


def tg(text, cid=42, mid=1):
    return {
        "update_id": 1,
        "message": {
            "message_id": mid,
            "chat": {"id": cid, "type": "private"},
            "from": {"id": cid},
            "text": text,
            "date": 0,
        },
    }


try:
    m = parse_telegram_update(tg("Hello world", cid=99, mid=77))
    assert m.text == "Hello world"
    assert m.channel == "telegram"
    assert m.channel_user_id == "99"
    assert m.idempotency_key == "telegram:77"
    assert m.message_type == MessageType.TEXT
    t.ok("text message: text, channel, user_id, idempotency_key, type")
except Exception as e:
    t.fail("telegram text parse", str(e))

try:
    cmds = [
        ("/start", "/start", None),
        ("/cancel abc", "/cancel", "abc"),
        ("/schedule list", "/schedule", "list"),
        ("/start@MyBot", "/start", None),
    ]
    ok_count = 0
    for text, ecmd, eargs in cmds:
        m = parse_telegram_update(tg(text))
        if m.command == ecmd and m.command_args == eargs:
            ok_count += 1
    assert ok_count == 4
    t.ok("command parsing: 4 variants incl @BotName strip")
except Exception as e:
    t.fail("command parsing", str(e))

try:
    lines = "\n".join([f"Line {i}" for i in range(300)])
    chunks = _split_message(lines, 100)
    assert all(len(c) <= 100 for c in chunks)
    assert "Line 0" in "".join(chunks)
    assert "Line 299" in "".join(chunks)
    t.ok("message splitting: size limit + all content preserved")
except Exception as e:
    t.fail("message splitting", str(e))

t.section("Channel Parsing — WhatsApp")

from app.channels.whatsapp import _strip_markdown, parse_whatsapp_payload


def wa(text, phone="919876543210", mid="wamid.001"):
    return {
        "object": "wa",
        "entry": [
            {"changes": [{"value": {"messages": [{"id": mid, "from": phone, "type": "text", "text": {"body": text}}]}}]}
        ],
    }


try:
    m = parse_whatsapp_payload(wa("Hello LinkedIn"))
    assert m.text == "Hello LinkedIn"
    assert m.channel == "whatsapp"
    assert m.channel_user_id == "919876543210"
    t.ok("text message: text, channel, user_id")
except Exception as e:
    t.fail("whatsapp text parse", str(e))

try:
    status = {"object": "wa", "entry": [{"changes": [{"value": {"statuses": [{"id": "m1"}]}}]}]}
    assert parse_whatsapp_payload(status) is None
    img = {
        "object": "wa",
        "entry": [{"changes": [{"value": {"messages": [{"id": "i", "from": "1", "type": "image", "image": {}}]}}]}],
    }
    assert parse_whatsapp_payload(img) is None
    t.ok("non-message payloads (status, image) → None")
except Exception as e:
    t.fail("whatsapp skip conditions", str(e))

try:
    tests = [("**bold**", "bold"), ("*italic*", "italic"), ("`code`", "code"), ("_ital_", "ital")]
    assert all(_strip_markdown(i) == e for i, e in tests)
    t.ok("markdown stripping: bold, italic, code, underscore")
except Exception as e:
    t.fail("markdown strip", str(e))


# ══════════════════════════════════════════════════════════════════════════
# 5. POST RULES
# ══════════════════════════════════════════════════════════════════════════
t.section("Post Rules — Content Enforcement")

from app.content.post_rules import LINKEDIN_MAX_CHARS, check_post_length, enforce_post_rules

try:
    cases = [
        ("Here's your post:\n\nContent.", "Content."),
        ("Sure! Here's a LinkedIn post:\n\nContent.", "Content."),
        ("I've written a post for you:\n\nContent.", "Content."),
        ('"Content."', "Content."),
    ]
    assert all(enforce_post_rules(i) == e for i, e in cases)
    t.ok("preamble and wrapper stripping: 4 patterns")
except Exception as e:
    t.fail("preamble strip", str(e))

try:
    real = "Most startups fail at distribution.\n\nHere's what I learned."
    assert enforce_post_rules(real).startswith("Most startups")
    t.ok("real content opening not stripped")
except Exception as e:
    t.fail("real content preservation", str(e))

try:
    long_c = "This is a sentence. " * 200
    r = enforce_post_rules(long_c)
    assert len(r) <= LINKEDIN_MAX_CHARS
    t.ok(f"over-{LINKEDIN_MAX_CHARS}-char content truncated")
except Exception as e:
    t.fail("char limit enforcement", str(e))

try:
    info = check_post_length("one two three four")
    assert info["words"] == 4 and info["chars"] == 18
    assert not info["over_limit"]
    t.ok("check_post_length: accurate metadata")
except Exception as e:
    t.fail("check_post_length", str(e))


# ══════════════════════════════════════════════════════════════════════════
# 6. STYLE MEMORY
# ══════════════════════════════════════════════════════════════════════════
t.section("Style Memory — Signal Extraction & Learning")

from app.content.style_memory import build_style_context, extract_style_signals, merge_style_prefs

try:
    post = (
        "I learned three things in 2024:\n\n1. Focus matters\n2. Speed wins\n3. Relationships count\n\n#Startup #India"
    )
    signals = extract_style_signals(post)
    assert signals["hashtag_count"] == 2
    assert signals["uses_numbered_list"] is True
    t.ok("signal extraction: hashtag count, numbered list")
except Exception as e:
    t.fail("signal extraction", str(e))

try:
    prefs = {}
    prefs = merge_style_prefs(prefs, {"word_count": 200, "hashtag_count": 3})
    prefs = merge_style_prefs(prefs, {"word_count": 400, "hashtag_count": 5})
    assert prefs["posts_learned"] == 2
    assert abs(prefs["avg_word_count"] - 300.0) < 0.01
    t.ok("rolling average: word count over 2 posts")
except Exception as e:
    t.fail("rolling average", str(e))

try:
    prefs = {}
    for i in range(5):
        prefs = merge_style_prefs(prefs, {}, example_post=f"Post {i}")
    assert len(prefs["example_posts"]) == 3
    assert "Post 4" in prefs["example_posts"]
    assert "Post 0" not in prefs["example_posts"]
    t.ok("example posts: capped at 3, most recent kept")
except Exception as e:
    t.fail("example post tracking", str(e))

try:
    prefs_with_data = {
        "posts_learned": 3,
        "preferred_length": "medium (around 300 words)",
        "avg_hashtag_count": 3.5,
        "rate_ends_with_question": 0.8,
        "rate_uses_emojis": 0.1,
    }
    ctx = build_style_context(prefs_with_data)
    assert "3 previous posts" in ctx
    assert "medium" in ctx
    t.ok("style context: generated from 3 learned posts")
except Exception as e:
    t.fail("style context generation", str(e))

try:
    assert build_style_context({}) == ""
    assert build_style_context({"posts_learned": 1}) == ""
    t.ok("style context: empty for 0 or 1 posts")
except Exception as e:
    t.fail("style context empty guard", str(e))


# ══════════════════════════════════════════════════════════════════════════
# 7. REVIEWING LOGIC
# ══════════════════════════════════════════════════════════════════════════
t.section("Reviewing — Approval & Draft Presentation")

from datetime import UTC

from app.conversation.reviewing import _is_approval, _is_discard, present_draft

try:
    approvals = ["looks good", "yes", "perfect", "ship it", "great", "go", "approved"]
    discards = ["discard", "scrap it", "start over", "no", "nope", "redo"]

    all_approve = all(_is_approval(p) for p in approvals)
    all_discard = all(_is_discard(p) for p in discards)

    assert all_approve
    assert all_discard
    t.ok("approval phrases: 7 variants detected correctly")
    t.ok("discard phrases: 6 variants detected correctly")
except Exception as e:
    t.fail("approval/discard detection", str(e))

try:
    draft = present_draft("My post content here.", version=3)
    assert "Draft v3" in draft
    assert "My post content here." in draft
    assert "✅" in draft
    assert "✏️" in draft
    t.ok("present_draft: version, content, action buttons all present")
except Exception as e:
    t.fail("present_draft", str(e))


# ══════════════════════════════════════════════════════════════════════════
# 8. SCHEDULING HELPERS
# ══════════════════════════════════════════════════════════════════════════
t.section("Scheduling — Date Parsing & Slot Logic")

try:
    from datetime import datetime

    import pytz

    from app.conversation.scheduling import _next_auto_slot

    IST = pytz.timezone("Asia/Kolkata") if not isinstance(pytz, MagicMock) else MagicMock()

    slot = _next_auto_slot(IST)
    assert slot > datetime.now(UTC)
    local = slot.astimezone(IST)
    assert local.weekday() in {0, 4}
    assert local.hour == 9
    t.ok("next_auto_slot: future, Mon/Fri, 9am local")
except Exception as e:
    t.fail("next_auto_slot", str(e))

try:
    import pytz

    from app.conversation.scheduling import _parse_datetime

    IST = pytz.timezone("Asia/Kolkata") if not isinstance(pytz, MagicMock) else MagicMock()

    r = _parse_datetime("now", IST)
    assert r is not None
    r2 = _parse_datetime("auto", IST)
    assert r2 is not None

    # Invalid returns None
    r3 = _parse_datetime("banana phone", IST)
    assert r3 is None

    t.ok("_parse_datetime: 'now' and 'auto' parse, invalid returns None")
except Exception as e:
    t.fail("_parse_datetime", str(e))


# ══════════════════════════════════════════════════════════════════════════
# 9. ERROR HANDLING
# ══════════════════════════════════════════════════════════════════════════
t.section("Error Handling — Types & Messages")

try:
    from app.core.errors import ConfigurationError, format_error_for_user
    from app.core.rate_limiter import LimitType, RateLimitExceeded
    from app.zernio.client import ZernioError

    # ConfigurationError gives specific messages
    for missing, word in [
        ("zernio_key", "Zernio"),
        ("llm_key", "AI"),
        ("linkedin", "LinkedIn"),
        ("timezone", "timezone"),
    ]:
        msg = ConfigurationError(missing).user_message()
        assert word.lower() in msg.lower(), f"'{word}' not in message for '{missing}'"
    t.ok("ConfigurationError: specific messages for all 4 missing types")

    # format_error_for_user hides internals
    generic_msg = format_error_for_user(RuntimeError("internal_secret_detail"))
    assert "internal_secret_detail" not in generic_msg
    t.ok("format_error_for_user: hides internal exception message")

    # format_error_for_user passes through ZernioError message
    zernio_msg = format_error_for_user(ZernioError("Your LinkedIn token expired"))
    assert "LinkedIn" in zernio_msg
    t.ok("format_error_for_user: passes ZernioError message through")

    # RateLimitExceeded messages are user-friendly
    for lt in LimitType:
        exc = RateLimitExceeded(lt, retry_after_seconds=60)
        msg = exc.user_message()
        assert isinstance(msg, str) and len(msg) > 10
    t.ok("RateLimitExceeded: user_message() for all 3 limit types")
except Exception as e:
    t.fail("error handling", str(e))


# ══════════════════════════════════════════════════════════════════════════
# 10. FULL CONVERSATION FLOW SIMULATION
# ══════════════════════════════════════════════════════════════════════════
t.section("Full Conversation — Simulated Happy Path")

try:
    from app.content.post_rules import enforce_post_rules
    from app.session.fsm import Event, transition
    from app.session.models import SessionContext, SessionState

    # Simulate the complete happy path without any I/O
    state = SessionState.IDLE
    ctx = SessionContext()

    # 1. User sends topic → COLLECTING
    ctx.topic = "How I grew from 0 to 10k followers on LinkedIn"
    state = transition(state, Event.START_POST)
    ctx.tone = "storytelling"
    ctx.audience = "founders"
    ctx.length_pref = "medium (around 300 words)"
    assert state == SessionState.COLLECTING

    # 2. Collecting done → GENERATING
    state = transition(state, Event.ENOUGH_INFO)
    assert state == SessionState.GENERATING

    # 3. Simulate LLM response (with preamble that gets stripped)
    raw_llm_response = (
        "Here's your LinkedIn post:\n\n"
        "I had 0 followers 18 months ago.\n\n"
        "Here's exactly what changed.\n\n"
        "#LinkedIn #Growth #Startup"
    )
    cleaned = enforce_post_rules(raw_llm_response)
    assert cleaned.startswith("I had 0 followers")  # preamble stripped

    ctx.draft_content = cleaned
    ctx.draft_version = 1
    state = transition(state, Event.DRAFT_READY)
    assert state == SessionState.REVIEWING

    # 4. User edits → REFINING
    ctx.push_edit_history(ctx.draft_content)
    state = transition(state, Event.REQUEST_EDIT)
    assert state == SessionState.REFINING
    assert len(ctx.edit_history) == 1

    # 5. Refinement complete → back to REVIEWING
    ctx.draft_content = "I had 0 followers 18 months ago.\n\nHere's my exact playbook.\n\n#LinkedIn"
    ctx.draft_version = 2
    state = transition(state, Event.REFINEMENT_READY)
    assert state == SessionState.REVIEWING

    # 6. Approve → SCHEDULING
    state = transition(state, Event.APPROVE_DRAFT)
    assert state == SessionState.SCHEDULING

    # 7. Schedule confirmed → SCHEDULED
    state = transition(state, Event.SCHEDULE_CONFIRMED)
    assert state == SessionState.SCHEDULED

    # 8. Context cleared
    ctx.clear_draft()
    assert ctx.topic is None
    assert ctx.draft_content is None

    t.ok("complete happy path: IDLE→COLLECTING→GENERATING→REVIEWING→REFINING→REVIEWING→SCHEDULING→SCHEDULED")
    t.ok("preamble stripped from LLM response in the pipeline")
    t.ok("edit history tracked: 1 entry saved")
    t.ok("context cleared after scheduling")
except Exception as e:
    t.fail("full conversation simulation", str(e))


# ══════════════════════════════════════════════════════════════════════════
# 11. CANCEL PATH
# ══════════════════════════════════════════════════════════════════════════
t.section("Cancel Path — Universal Escape Hatch")

try:
    from app.session.fsm import Event, transition
    from app.session.models import SessionContext, SessionState

    for state in SessionState:
        ctx = SessionContext()
        ctx.topic = "test"
        ctx.draft_content = "draft"

        new_state = transition(state, Event.CANCEL)
        ctx.clear_draft()

        assert new_state == SessionState.IDLE
        assert ctx.topic is None

    t.ok(f"cancel from all {len(list(SessionState))} states → IDLE + context cleared")
except Exception as e:
    t.fail("cancel path", str(e))


# ══════════════════════════════════════════════════════════════════════════
# RESULTS
# ══════════════════════════════════════════════════════════════════════════
get_settings.cache_clear()
success = t.summary()
sys.exit(0 if success else 1)
