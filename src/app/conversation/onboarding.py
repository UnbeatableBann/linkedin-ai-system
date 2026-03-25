"""
app/conversation/onboarding.py
───────────────────────────────
Multi-step onboarding flow. Runs entirely inside the bot conversation.

Steps (tracked in session.context.onboarding_step):
  1. ZERNIO_KEY   — User pastes their Zernio API key
  2. LLM_CHOICE   — User picks their LLM provider
  3. LLM_KEY      — User pastes their LLM API key
  4. LINKEDIN_OAUTH — Bot sends OAuth URL, waits for user to complete
  5. LINKEDIN_ORG   — Bot lists orgs, user picks one
  6. TIMEZONE       — User sets their timezone
  7. COMPLETE       — Session → IDLE, user is ready

Each step is idempotent: if the user sends an unexpected message mid-step,
we repeat the question for that step.
"""

import asyncio
from datetime import datetime, timezone
from uuid import UUID

from app.channels.base import NormalisedMessage
from app.core.encryption import EncryptionError, decrypt, encrypt
from app.core.logging import get_logger
from app.db.client import get_db
from app.db.models import LLMProvider, UserRow
from app.session.fsm import Event, transition
from app.session.models import OnboardingStep, SessionState
from app.session.store import Session, save_session
from app.zernio.client import ZernioClient, ZernioError, make_zernio_client

logger = get_logger(__name__)

# LLM choices presented to the user
LLM_CHOICES = {
    "1": (LLMProvider.ANTHROPIC, "claude-sonnet-4-5", "Anthropic (Claude)"),
    "2": (LLMProvider.OPENAI, "gpt-4o", "OpenAI (GPT-4o)"),
    "3": (LLMProvider.GROQ, "llama-3.3-70b-versatile", "Groq — LLaMA 3.3 (free tier available)"),
}


async def handle_onboarding(
    session: Session,
    user: UserRow,
    sender: object,
    msg: NormalisedMessage,
) -> None:
    """Route to the correct onboarding step handler."""
    if session.state != SessionState.ONBOARDING:
        # First time — kick off onboarding
        await start_onboarding(session, user, sender, msg)
        return

    step = session.context.onboarding_step

    match step:
        case OnboardingStep.ZERNIO_KEY:
            await _handle_zernio_key(session, user, sender, msg)
        case OnboardingStep.LLM_CHOICE:
            await _handle_llm_choice(session, user, sender, msg)
        case OnboardingStep.LLM_KEY:
            await _handle_llm_key(session, user, sender, msg)
        case OnboardingStep.LINKEDIN_OAUTH:
            await _handle_linkedin_oauth_wait(session, user, sender, msg)
        case OnboardingStep.LINKEDIN_ORG:
            await _handle_linkedin_org_select(session, user, sender, msg)
        case OnboardingStep.TIMEZONE:
            await _handle_timezone(session, user, sender, msg)
        case _:
            # Unexpected — restart
            await start_onboarding(session, user, sender, msg)


async def start_onboarding(
    session: Session,
    user: UserRow,
    sender: object,
    msg: NormalisedMessage,
) -> None:
    """Kick off the onboarding sequence from the beginning."""
    session.state = SessionState.ONBOARDING
    session.context.onboarding_step = OnboardingStep.ZERNIO_KEY

    await sender.send_text(
        msg.channel_user_id,
        "👋 *Welcome to LinkedIn AI Content System!*\n\n"
        "I'll help you write and schedule LinkedIn posts from right here in chat.\n\n"
        "Let's get you set up — it takes about 2 minutes.\n\n"
        "*Step 1 of 5:* Connect your Zernio account\n\n"
        "Zernio is the service that publishes to LinkedIn. You need a free account:\n"
        "1. Go to *zernio.com* and create a free account\n"
        "2. Go to *Settings → API Keys*\n"
        "3. Click *Create API Key* and copy the key\n\n"
        "Paste your Zernio API key here:",
    )


# ── Step handlers ──────────────────────────────────────────────────────────


async def _handle_zernio_key(
    session: Session,
    user: UserRow,
    sender: object,
    msg: NormalisedMessage,
) -> None:
    """Validate and store the Zernio API key."""
    key = msg.text.strip()

    if not key.startswith("sk_") or len(key) < 20:
        await sender.send_text(
            msg.channel_user_id,
            "That doesn't look like a valid Zernio API key.\n\n"
            "It should start with `sk_` and be about 67 characters long.\n\n"
            "Please try again, or go to zernio.com → Settings → API Keys to create one.",
        )
        return

    # Validate the key against Zernio API
    await sender.send_text(msg.channel_user_id, "Checking your key...")
    try:
        client = ZernioClient(api_key=key)
        accounts = await client.validate_key()

        # Check free-tier limit
        linkedin_accounts = [a for a in accounts if a.platform == "linkedin"]
        if len(accounts) >= 2 and not linkedin_accounts:
            await sender.send_text(
                msg.channel_user_id,
                "⚠️ Your Zernio account already has 2 connected accounts (the free tier limit).\n\n"
                "Please disconnect one at *zernio.com → Accounts* to make room for LinkedIn, "
                "then try again.",
            )
            return

    except ZernioError as exc:
        await sender.send_text(
            msg.channel_user_id,
            f"❌ Couldn't validate your Zernio key: {exc}\n\nPlease check the key and try again.",
        )
        return

    # Store encrypted key
    db = get_db()
    db.table("users").update({"zernio_api_key_enc": encrypt(key)}).eq("id", str(user.id)).execute()

    # Advance to next step
    session.context.onboarding_step = OnboardingStep.LLM_CHOICE
    await _ask_llm_choice(sender, msg.channel_user_id)


async def _ask_llm_choice(sender: object, channel_user_id: str) -> None:
    await sender.send_text(
        channel_user_id,
        "✅ Zernio connected!\n\n"
        "*Step 2 of 5:* Choose your AI writing assistant\n\n"
        "Reply with a number:\n\n"
        "1. *Claude* (Anthropic) — Best for nuanced, human-sounding writing\n"
        "2. *GPT-4o* (OpenAI) — Great all-rounder\n"
        "3. *LLaMA 3.3* (Groq) — Fast and has a free tier\n\n"
        "You can change this later with /settings.",
    )


async def _handle_llm_choice(
    session: Session,
    user: UserRow,
    sender: object,
    msg: NormalisedMessage,
) -> None:
    choice = msg.text.strip()
    if choice not in LLM_CHOICES:
        await sender.send_text(
            msg.channel_user_id,
            "Please reply with *1*, *2*, or *3* to choose your AI:",
        )
        await _ask_llm_choice(sender, msg.channel_user_id)
        return

    provider, model, label = LLM_CHOICES[choice]
    # Store choice temporarily in context (key stored in next step)
    session.context.topic = f"{provider}:{model}"  # Temp reuse of topic field

    session.context.onboarding_step = OnboardingStep.LLM_KEY
    await sender.send_text(
        msg.channel_user_id,
        f"*{label}* selected!\n\n"
        "*Step 3 of 5:* Enter your API key\n\n"
        f"{'Get your key at: console.anthropic.com' if provider == LLMProvider.ANTHROPIC else ''}"
        f"{'Get your key at: platform.openai.com/api-keys' if provider == LLMProvider.OPENAI else ''}"
        f"{'Get your free key at: console.groq.com' if provider == LLMProvider.GROQ else ''}\n\n"
        "Paste your API key here:",
    )


async def _handle_llm_key(
    session: Session,
    user: UserRow,
    sender: object,
    msg: NormalisedMessage,
) -> None:
    """Validate and store the LLM API key."""
    llm_key = msg.text.strip()
    provider_model = session.context.topic or ""

    # Allow user to go back and pick a different provider
    if llm_key in ("back", "change", "go back", "different"):
        session.context.topic = None  # Clear stale provider choice
        session.context.onboarding_step = OnboardingStep.LLM_CHOICE
        await _ask_llm_choice(sender, msg.channel_user_id)
        return

    if ":" not in provider_model:
        # Stale / missing context — restart from LLM choice
        session.context.topic = None
        session.context.onboarding_step = OnboardingStep.LLM_CHOICE
        await _ask_llm_choice(sender, msg.channel_user_id)
        return

    provider_str, model = provider_model.split(":", 1)
    try:
        provider = LLMProvider(provider_str)
    except ValueError:
        # Corrupted context — restart
        session.context.topic = None
        session.context.onboarding_step = OnboardingStep.LLM_CHOICE
        await _ask_llm_choice(sender, msg.channel_user_id)
        return

    # Basic format checks
    is_valid = _validate_llm_key_format(provider, llm_key)
    if not is_valid:
        await sender.send_text(
            msg.channel_user_id,
            "That key doesn't look right for the provider you selected.\n\n"
            "Please double-check and paste it again, or send *back* to choose a different AI provider.",
        )
        return  # topic intentionally kept — user stays in LLM_KEY step with same provider

    # Verify the key works with a real API call
    await sender.send_text(msg.channel_user_id, "Verifying your API key...")
    valid = await _verify_llm_key(provider, model, llm_key)
    if not valid:
        await sender.send_text(
            msg.channel_user_id,
            "❌ That API key didn't work. Please check it and try again.\n\n"
            "Make sure you're copying the full key without extra spaces.\n\n"
            "Send *back* to choose a different AI provider.",
        )
        return  # topic intentionally kept — user retries same provider

    # Store in DB
    db = get_db()
    db.table("users").update(
        {
            "llm_provider": provider.value,
            "llm_model": model,
            "llm_api_key_enc": encrypt(llm_key),
        }
    ).eq("id", str(user.id)).execute()

    session.context.topic = None  # Clear temp storage — must happen after successful store

    # Advance to LinkedIn OAuth
    session.context.onboarding_step = OnboardingStep.LINKEDIN_OAUTH
    await _start_linkedin_oauth(session, user, sender, msg)


async def _start_linkedin_oauth(
    session: Session,
    user: UserRow,
    sender: object,
    msg: NormalisedMessage,
) -> None:
    """Create a Zernio profile and start LinkedIn OAuth."""
    from app.config import get_settings
    settings = get_settings()

    await sender.send_text(msg.channel_user_id, "Setting up your LinkedIn connection...")

    # Reload user to get fresh zernio key
    db = get_db()
    fresh_user = db.table("users").select("*").eq("id", str(user.id)).single().execute()
    user_data = fresh_user.data

    try:
        api_key = decrypt(user_data["zernio_api_key_enc"])
        client = ZernioClient(api_key=api_key)

        # Create Zernio profile if not already created
        if not user_data.get("zernio_profile_id"):
            profile = await client.create_profile(
                name=f"User {str(user.id)[:8]}",
                description="LinkedIn AI Content System",
            )
            db.table("users").update({"zernio_profile_id": profile.id}).eq("id", str(user.id)).execute()
            profile_id = profile.id
        else:
            profile_id = user_data["zernio_profile_id"]

        # Get LinkedIn OAuth URL (headless mode)
        auth_url = await client.get_linkedin_oauth_url(
            profile_id=profile_id,
            redirect_url=settings.oauth_callback_url,
        )

        # Store token issue time for expiry tracking
        session.context.connect_token_issued_at = datetime.now(timezone.utc).isoformat()

    except (ZernioError, EncryptionError) as exc:
        await sender.send_text(
            msg.channel_user_id,
            f"❌ Couldn't start LinkedIn connection: {exc}\n\nPlease try /reconnect to try again.",
        )
        return

    await sender.send_text(
        msg.channel_user_id,
        "*Step 4 of 5:* Connect your LinkedIn account\n\n"
        "Click the link below to authorise access to your LinkedIn:\n\n"
        f"{auth_url}\n\n"
        "👆 Open that link, approve access, then come back here and send me *any message* to continue.\n\n"
        "_The link expires in 12 minutes._",
    )


async def _handle_linkedin_oauth_wait(
    session: Session,
    user: UserRow,
    sender: object,
    msg: NormalisedMessage,
) -> None:
    """User has returned after completing OAuth — poll for connect_token."""
    from app.config import get_settings

    # Check if token window expired
    issued_at_str = session.context.connect_token_issued_at
    if issued_at_str:
        issued_at = datetime.fromisoformat(issued_at_str)
        elapsed_minutes = (datetime.now(timezone.utc) - issued_at).seconds / 60
        settings = get_settings()
        if elapsed_minutes > settings.zernio_connect_token_ttl_minutes:
            await sender.send_text(
                msg.channel_user_id,
                "⏱ The OAuth link has expired (12 minutes).\n\n"
                "Let me generate a fresh one...",
            )
            await _start_linkedin_oauth(session, user, sender, msg)
            return

    await sender.send_text(msg.channel_user_id, "Checking your LinkedIn connection...")

    db = get_db()
    user_data = db.table("users").select("*").eq("id", str(user.id)).single().execute().data

    try:
        api_key = decrypt(user_data["zernio_api_key_enc"])
        client = ZernioClient(api_key=api_key)

        # Poll up to 3 times with short delays
        connect_token = None
        for attempt in range(3):
            status = await client.get_linkedin_connect_status()
            connect_token = status.get("connect_token")
            if connect_token:
                break
            if attempt < 2:
                await asyncio.sleep(2)

        if not connect_token:
            await sender.send_text(
                msg.channel_user_id,
                "I couldn't detect that you completed the LinkedIn authorisation.\n\n"
                "Please make sure you:\n"
                "1. Clicked the link\n"
                "2. Logged in to LinkedIn\n"
                "3. Clicked *Allow* on the permissions screen\n\n"
                "Then send me any message here to continue.",
            )
            return

        # Fetch organisations
        orgs = await client.list_linkedin_orgs(connect_token=connect_token)
        session.context.connect_token = connect_token
        session.context.pending_orgs = [{"id": o.id, "name": o.name, "type": o.type} for o in orgs]
        session.context.onboarding_step = OnboardingStep.LINKEDIN_ORG

        # Present org choices
        org_list = "\n".join(
            f"{i+1}. *{o.name}* ({o.type})" for i, o in enumerate(orgs)
        )
        await sender.send_text(
            msg.channel_user_id,
            f"✅ LinkedIn connected!\n\n"
            f"*Step 5 of 5:* Which account would you like to post from?\n\n"
            f"{org_list}\n\n"
            "Reply with the number:",
        )

    except (ZernioError, EncryptionError) as exc:
        await sender.send_text(
            msg.channel_user_id,
            f"❌ Error fetching LinkedIn accounts: {exc}\n\n"
            "Please try /reconnect to start the LinkedIn connection again.",
        )


async def _handle_linkedin_org_select(
    session: Session,
    user: UserRow,
    sender: object,
    msg: NormalisedMessage,
) -> None:
    """User has picked which LinkedIn account to post from."""
    orgs = session.context.pending_orgs
    connect_token = session.context.connect_token

    if not orgs or not connect_token:
        await sender.send_text(
            msg.channel_user_id,
            "Something went wrong with the org selection. Let's try again...",
        )
        session.context.onboarding_step = OnboardingStep.LINKEDIN_OAUTH
        await _start_linkedin_oauth(session, user, sender, msg)
        return

    choice = msg.text.strip()
    try:
        idx = int(choice) - 1
        if idx < 0 or idx >= len(orgs):
            raise ValueError("out of range")
    except ValueError:
        org_list = "\n".join(f"{i+1}. {o['name']}" for i, o in enumerate(orgs))
        await sender.send_text(
            msg.channel_user_id,
            f"Please reply with a number (1–{len(orgs)}):\n\n{org_list}",
        )
        return

    selected_org = orgs[idx]

    db = get_db()
    user_data = db.table("users").select("zernio_api_key_enc").eq("id", str(user.id)).single().execute().data

    try:
        api_key = decrypt(user_data["zernio_api_key_enc"])
        client = ZernioClient(api_key=api_key)
        account = await client.select_linkedin_org(
            connect_token=connect_token,
            org_id=selected_org["id"],
        )

        # Store LinkedIn account ID
        db.table("users").update({"zernio_account_id": account.id}).eq("id", str(user.id)).execute()

    except (ZernioError, EncryptionError) as exc:
        await sender.send_text(
            msg.channel_user_id,
            f"❌ Couldn't finalise LinkedIn selection: {exc}\n\n"
            "Please try /reconnect to start over.",
        )
        return

    # Clear OAuth temp data
    session.context.connect_token = None
    session.context.pending_orgs = []
    session.context.connect_token_issued_at = None
    session.context.onboarding_step = OnboardingStep.TIMEZONE

    await sender.send_text(
        msg.channel_user_id,
        f"✅ *{selected_org['name']}* selected!\n\n"
        "Almost done!\n\n"
        "What timezone are you in? This is used for scheduling posts at the right time.\n\n"
        "Examples:\n"
        "• `Asia/Kolkata` (IST)\n"
        "• `America/New_York` (EST)\n"
        "• `Europe/London` (GMT)\n"
        "• `UTC`\n\n"
        "Reply with your timezone:",
    )


async def _handle_timezone(
    session: Session,
    user: UserRow,
    sender: object,
    msg: NormalisedMessage,
) -> None:
    """Validate and store the user's timezone, then complete onboarding."""
    import pytz

    tz_input = msg.text.strip()

    try:
        pytz.timezone(tz_input)
    except pytz.exceptions.UnknownTimeZoneError:
        await sender.send_text(
            msg.channel_user_id,
            f"Unknown timezone: `{tz_input}`\n\n"
            "Try values like `Asia/Kolkata`, `America/New_York`, `Europe/London`, or `UTC`.",
        )
        return

    db = get_db()
    db.table("users").update({"timezone": tz_input}).eq("id", str(user.id)).execute()

    # Also create their default auto-schedule (Mon/Fri 9am, disabled by default)
    db.table("user_schedules").upsert(
        {
            "user_id": str(user.id),
            "days_of_week": [0, 4],
            "time_of_day": "09:00:00",
            "timezone": tz_input,
            "enabled": False,
        },
        on_conflict="user_id",
    ).execute()

    # Complete onboarding → IDLE
    session.state = SessionState.IDLE
    session.context.onboarding_step = OnboardingStep.COMPLETE
    session.context.clear_draft()

    await sender.send_text(
        msg.channel_user_id,
        f"🎉 *You're all set!*\n\n"
        f"Here's what I've configured:\n"
        f"• LinkedIn account connected\n"
        f"• Timezone: {tz_input}\n"
        f"• Auto-schedule: Mon/Fri 9am (currently *off* — use /schedule auto to enable)\n\n"
        f"*Send me a topic or idea to write your first post!*\n\n"
        f"_Tip: /help shows all commands_",
    )


async def restart_linkedin_oauth(
    session: Session,
    user: UserRow,
    sender: object,
    msg: NormalisedMessage,
) -> None:
    """Re-run just the LinkedIn OAuth step (for /reconnect command)."""
    session.state = SessionState.ONBOARDING
    session.context.onboarding_step = OnboardingStep.LINKEDIN_OAUTH
    await _start_linkedin_oauth(session, user, sender, msg)


# ── Helpers ────────────────────────────────────────────────────────────────


def _validate_llm_key_format(provider: LLMProvider, key: str) -> bool:
    """Basic format validation before making an API call."""
    if provider == LLMProvider.ANTHROPIC:
        return key.startswith("sk-ant-") and len(key) > 20
    if provider == LLMProvider.OPENAI:
        return key.startswith("sk-") and len(key) > 20
    if provider == LLMProvider.GROQ:
        return key.startswith("gsk_") and len(key) > 20
    return len(key) > 10


async def _verify_llm_key(provider: LLMProvider, model: str, key: str) -> bool:
    """Make a minimal API call to verify the key is valid."""
    try:
        if provider == LLMProvider.ANTHROPIC:
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=key)
            await client.messages.create(
                model=model,
                max_tokens=10,
                messages=[{"role": "user", "content": "Hi"}],
            )

        elif provider == LLMProvider.OPENAI:
            import openai
            client = openai.AsyncOpenAI(api_key=key)
            await client.chat.completions.create(
                model=model,
                max_tokens=10,
                messages=[{"role": "user", "content": "Hi"}],
            )

        elif provider == LLMProvider.GROQ:
            import groq
            client = groq.AsyncGroq(api_key=key)
            await client.chat.completions.create(
                model=model,
                max_tokens=10,
                messages=[{"role": "user", "content": "Hi"}],
            )

        return True

    except Exception as exc:
        logger.warning("onboarding.llm_key_invalid", provider=provider, error=str(exc))
        return False
