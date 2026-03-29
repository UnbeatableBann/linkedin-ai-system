"""
app/conversation/onboarding.py
------------------------------
Multi-step onboarding flow. Runs entirely inside the bot conversation.

Steps (tracked in session.context.onboarding_step):
  1. ZERNIO_KEY      - User pastes their Zernio API key
  2. LLM_CHOICE      - User picks their LLM provider
  3. LLM_KEY         - User pastes their LLM API key
  4. LINKEDIN_OAUTH  - Bot sends OAuth URL and waits for completion
  5. LINKEDIN_ORG    - Bot lists orgs and user picks one
  6. TIMEZONE        - User sets their timezone
  7. COMPLETE        - Session returns to IDLE

Each step is idempotent: if the user sends an unexpected message mid-step,
we repeat the question for that step.
"""

import asyncio
from datetime import UTC, datetime

from app.channels.base import NormalisedMessage
from app.core.encryption import EncryptionError, decrypt, encrypt
from app.core.logging import get_logger
from app.db.client import get_db
from app.db.models import LLMProvider, UserRow
from app.session.models import OnboardingStep, SessionState
from app.session.store import Session
from app.zernio.client import ZernioClient, ZernioError

logger = get_logger(__name__)

LLM_CHOICES = {
    "1": (LLMProvider.ANTHROPIC, "claude-sonnet-4-5", "Anthropic (Claude)"),
    "2": (LLMProvider.OPENAI, "gpt-4o", "OpenAI (GPT-4o)"),
    "3": (LLMProvider.GROQ, "llama-3.3-70b-versatile", "Groq - LLaMA 3.3 (free tier available)"),
    "4": (LLMProvider.GEMINI, "gemini-2.0-flash", "Google Gemini"),
}


async def handle_onboarding(
    session: Session,
    user: UserRow,
    sender: object,
    msg: NormalisedMessage,
) -> None:
    """Route to the correct onboarding step handler."""
    if session.state != SessionState.ONBOARDING:
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
            await start_onboarding(session, user, sender, msg)


async def start_onboarding(
    session: Session,
    user: UserRow,
    sender: object,
    msg: NormalisedMessage,
) -> None:
    """Kick off the onboarding sequence from the beginning."""
    session.state = SessionState.ONBOARDING
    session.context.clear_draft()
    session.context.clear_onboarding()
    session.context.onboarding_step = OnboardingStep.ZERNIO_KEY

    await sender.send_text(
        msg.channel_user_id,
        "*Welcome to LinkedIn AI Content System!*\n\n"
        "I'll help you write and schedule LinkedIn posts from right here in chat.\n\n"
        "Let's get you set up. It takes about 2 minutes.\n\n"
        "*Step 1 of 5:* Connect your Zernio account\n\n"
        "Zernio is the service that publishes to LinkedIn. You need a free account:\n"
        "1. Go to *zernio.com* and create a free account\n"
        "2. Go to *Settings -> API Keys*\n"
        "3. Click *Create API Key* and copy the key\n\n"
        "Paste your Zernio API key here:",
    )


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
            "Please try again, or go to zernio.com -> Settings -> API Keys to create one.",
        )
        return

    await sender.send_text(msg.channel_user_id, "Checking your key...")
    try:
        client = ZernioClient(api_key=key)
        accounts = await client.validate_key()

        linkedin_accounts = [a for a in accounts if a.platform == "linkedin"]
        if len(accounts) >= 2 and not linkedin_accounts:
            await sender.send_text(
                msg.channel_user_id,
                "Your Zernio account already has 2 connected accounts, which is the free tier limit.\n\n"
                "Please disconnect one at *zernio.com -> Accounts* to make room for LinkedIn, then try again.",
            )
            return

    except ZernioError as exc:
        await sender.send_text(
            msg.channel_user_id,
            f"Couldn't validate your Zernio key: {exc}\n\nPlease check the key and try again.",
        )
        return

    db = await get_db()
    await db.table("users").update({"zernio_api_key_enc": encrypt(key)}).eq("id", str(user.id)).execute()

    session.context.onboarding_step = OnboardingStep.LLM_CHOICE
    await _ask_llm_choice(sender, msg.channel_user_id)


async def _ask_llm_choice(sender: object, channel_user_id: str) -> None:
    await sender.send_text(
        channel_user_id,
        "Zernio connected.\n\n"
        "*Step 2 of 5:* Choose your AI writing assistant\n\n"
        "Reply with a number:\n\n"
        "1. *Claude* (Anthropic) - Best for nuanced, human-sounding writing\n"
        "2. *GPT-4o* (OpenAI) - Great all-rounder\n"
        "3. *LLaMA 3.3* (Groq) - Fast and has a free tier\n"
        "4. *Gemini* (Google) - Powerful and includes a free tier\n\n"
        "You can change this later with /settings.",
    )


async def _handle_llm_choice(
    session: Session,
    user: UserRow,
    sender: object,
    msg: NormalisedMessage,
) -> None:
    """Persist the selected LLM provider for the next onboarding step."""
    choice = msg.text.strip()
    if choice not in LLM_CHOICES:
        await sender.send_text(
            msg.channel_user_id,
            "Please reply with *1*, *2*, *3*, or *4* to choose your AI:",
        )
        await _ask_llm_choice(sender, msg.channel_user_id)
        return

    provider, model, label = LLM_CHOICES[choice]
    session.context.onboarding_llm_provider = provider.value
    session.context.onboarding_llm_model = model
    session.context.onboarding_step = OnboardingStep.LLM_KEY

    await sender.send_text(
        msg.channel_user_id,
        f"*{label}* selected!\n\n"
        "*Step 3 of 5:* Enter your API key\n\n"
        f"{'Get your key at: console.anthropic.com' if provider == LLMProvider.ANTHROPIC else ''}"
        f"{'Get your key at: platform.openai.com/api-keys' if provider == LLMProvider.OPENAI else ''}"
        f"{'Get your free key at: console.groq.com' if provider == LLMProvider.GROQ else ''}"
        f"{'Get your key at: aistudio.google.com/apikey' if provider == LLMProvider.GEMINI else ''}\n\n"
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
    provider_str = session.context.onboarding_llm_provider
    model = session.context.onboarding_llm_model

    if llm_key.lower() in ("back", "change", "go back", "different"):
        session.context.onboarding_llm_provider = None
        session.context.onboarding_llm_model = None
        session.context.onboarding_step = OnboardingStep.LLM_CHOICE
        await _ask_llm_choice(sender, msg.channel_user_id)
        return

    if not provider_str or not model:
        session.context.onboarding_llm_provider = None
        session.context.onboarding_llm_model = None
        session.context.onboarding_step = OnboardingStep.LLM_CHOICE
        await _ask_llm_choice(sender, msg.channel_user_id)
        return

    try:
        provider = LLMProvider(provider_str)
    except ValueError:
        session.context.onboarding_llm_provider = None
        session.context.onboarding_llm_model = None
        session.context.onboarding_step = OnboardingStep.LLM_CHOICE
        await _ask_llm_choice(sender, msg.channel_user_id)
        return

    if not _validate_llm_key_format(provider, llm_key):
        await sender.send_text(
            msg.channel_user_id,
            "That key doesn't look right for the provider you selected.\n\n"
            "Please double-check and paste it again, or send *back* to choose a different AI provider.",
        )
        return

    await sender.send_text(msg.channel_user_id, "Verifying your API key...")
    valid = await _verify_llm_key(provider, model, llm_key)
    if not valid:
        await sender.send_text(
            msg.channel_user_id,
            "That API key didn't work. Please check it and try again.\n\n"
            "Make sure you're copying the full key without extra spaces.\n\n"
            "Send *back* to choose a different AI provider.",
        )
        return

    db = await get_db()
    await (
        db.table("users")
        .update(
            {
                "llm_provider": provider.value,
                "llm_model": model,
                "llm_api_key_enc": encrypt(llm_key),
            }
        )
        .eq("id", str(user.id))
        .execute()
    )

    session.context.onboarding_llm_provider = None
    session.context.onboarding_llm_model = None
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
    session.context.connect_token = None
    session.context.pending_orgs = []
    session.context.connect_token_issued_at = None

    db = await get_db()
    fresh_user = await db.table("users").select("*").eq("id", str(user.id)).single().execute()
    user_data = fresh_user.data

    try:
        api_key = decrypt(user_data["zernio_api_key_enc"])
        client = ZernioClient(api_key=api_key)

        if not user_data.get("zernio_profile_id"):
            profile = await client.create_profile(
                name=f"User {str(user.id)[:8]}",
                description="LinkedIn AI Content System",
            )
            await db.table("users").update({"zernio_profile_id": profile.id}).eq("id", str(user.id)).execute()
            profile_id = profile.id
        else:
            profile_id = user_data["zernio_profile_id"]

        auth_url = await client.get_linkedin_oauth_url(
            profile_id=profile_id,
            redirect_url=settings.oauth_callback_url,
        )
        session.context.connect_token_issued_at = datetime.now(UTC).isoformat()

    except (ZernioError, EncryptionError) as exc:
        await sender.send_text(
            msg.channel_user_id,
            f"Couldn't start LinkedIn connection: {exc}\n\nPlease try /reconnect to try again.",
        )
        return

    prompt = (
        "*Step 4 of 5:* Connect your LinkedIn account\n\n"
        "Use the button below to authorise access to your LinkedIn.\n\n"
        "Open the link and approve access. I will continue automatically once LinkedIn sends the callback.\n\n"
        "If that callback is delayed, you can still send me any message to retry the check.\n\n"
        "_The link expires in 12 minutes._"
    )

    if msg.channel == "telegram" and hasattr(sender, "send_url_button"):
        await sender.send_url_button(
            msg.channel_user_id,
            prompt,
            "Connect LinkedIn",
            auth_url,
        )
        return

    await sender.send_text(
        msg.channel_user_id,
        f"{prompt}\n\n{auth_url}",
    )


async def _handle_linkedin_oauth_wait(
    session: Session,
    user: UserRow,
    sender: object,
    msg: NormalisedMessage,
) -> None:
    """User has returned after completing OAuth; poll for connect_token."""
    from app.config import get_settings

    issued_at_str = session.context.connect_token_issued_at
    if issued_at_str:
        issued_at = datetime.fromisoformat(issued_at_str)
        elapsed_minutes = (datetime.now(UTC) - issued_at).total_seconds() / 60
        settings = get_settings()
        if elapsed_minutes > settings.zernio_connect_token_ttl_minutes:
            await sender.send_text(
                msg.channel_user_id,
                "The OAuth link has expired (12 minutes).\n\nLet me generate a fresh one...",
            )
            await _start_linkedin_oauth(session, user, sender, msg)
            return

    await sender.send_text(msg.channel_user_id, "Checking your LinkedIn connection...")

    db = await get_db()
    user_data = (await db.table("users").select("*").eq("id", str(user.id)).single().execute()).data

    try:
        api_key = decrypt(user_data["zernio_api_key_enc"])
        client = ZernioClient(api_key=api_key)

        connect_token = None
        connected_account_id = None
        connected_username = None
        for attempt in range(3):
            status = await client.get_linkedin_connect_status()
            connect_token = status.get("connect_token")
            connected_account_id = status.get("accountId") or status.get("account_id")
            connected_username = status.get("username")
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
                "You do not need to send *done* anymore once the callback reaches me,"
                "but you can still send any message to retry this check.",
            )
            return

        if connected_account_id:
            await _complete_linkedin_account_connection(
                session,
                user,
                sender,
                msg.channel_user_id,
                connected_account_id,
                connected_username,
            )
            return

        await _continue_linkedin_onboarding(
            session,
            user,
            sender,
            msg.channel_user_id,
            connect_token,
        )

    except (ZernioError, EncryptionError) as exc:
        await sender.send_text(
            msg.channel_user_id,
            f"Error fetching LinkedIn accounts: {exc}\n\n"
            "Please try /reconnect to start the LinkedIn connection again.",
        )


async def _complete_linkedin_account_connection(
    session: Session,
    user: UserRow,
    sender: object,
    channel_user_id: str,
    account_id: str,
    username: str | None = None,
) -> None:
    """Complete LinkedIn onboarding when Zernio already gives us the connected account."""
    db = await get_db()
    await db.table("users").update({"zernio_account_id": account_id}).eq("id", str(user.id)).execute()

    session.context.connect_token = None
    session.context.pending_orgs = []
    session.context.connect_token_issued_at = None
    session.context.onboarding_step = OnboardingStep.TIMEZONE

    logger.info(
        "onboarding.linkedin_connected_direct",
        user_id=str(user.id),
        account_id=account_id,
        username=username,
    )

    await _ask_timezone(
        sender,
        channel_user_id,
        account_name=username or "LinkedIn account",
    )


async def _continue_linkedin_onboarding(
    session: Session,
    user: UserRow,
    sender: object,
    channel_user_id: str,
    connect_token: str,
) -> None:
    """Continue LinkedIn onboarding from a known connect token."""
    db = await get_db()
    user_data = (await db.table("users").select("*").eq("id", str(user.id)).single().execute()).data

    try:
        api_key = decrypt(user_data["zernio_api_key_enc"])
        client = ZernioClient(api_key=api_key)
        orgs = await client.list_linkedin_orgs(connect_token=connect_token)

        if not orgs:
            await sender.send_text(
                channel_user_id,
                "I connected to LinkedIn, but Zernio did not return any postable accounts.\n\n"
                "Please try /reconnect and make sure the LinkedIn account has posting access.",
            )
            return

        session.context.connect_token = connect_token
        session.context.pending_orgs = [{"id": o.id, "name": o.name, "type": o.type} for o in orgs]
        session.context.onboarding_step = OnboardingStep.LINKEDIN_ORG

        org_list = "\n".join(f"{i+1}. *{o.name}* ({o.type})" for i, o in enumerate(orgs))
        await sender.send_text(
            channel_user_id,
            "LinkedIn connected!\n\n"
            "*Step 5 of 5:* Which account would you like to post from?\n\n"
            f"{org_list}\n\n"
            "Reply with the number:",
        )

    except (ZernioError, EncryptionError) as exc:
        logger.warning(
            "onboarding.linkedin_org_fetch_failed",
            user_id=str(user.id),
            status_code=getattr(exc, "status_code", None),
            error=str(exc),
        )
        if isinstance(exc, ZernioError) and exc.status_code is not None and 400 <= exc.status_code < 500:
            await sender.send_text(
                channel_user_id,
                "I couldn't load your LinkedIn accounts from that authorisation.\n\n"
                "I'll generate a fresh connection link now.",
            )
            session.context.onboarding_step = OnboardingStep.LINKEDIN_OAUTH
            session.context.connect_token = None
            session.context.pending_orgs = []
            session.context.connect_token_issued_at = None
            retry_msg = NormalisedMessage(
                channel=user.channel.value,
                channel_user_id=channel_user_id,
                message_id="linkedin_oauth_retry",
                text="",
            )
            await _start_linkedin_oauth(session, user, sender, retry_msg)
            return

        raise


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
            f"Please reply with a number (1-{len(orgs)}):\n\n{org_list}",
        )
        return

    selected_org = orgs[idx]

    db = await get_db()
    user_data = (await db.table("users").select("zernio_api_key_enc").eq("id", str(user.id)).single().execute()).data

    try:
        api_key = decrypt(user_data["zernio_api_key_enc"])
        client = ZernioClient(api_key=api_key)
        account = await client.select_linkedin_org(
            connect_token=connect_token,
            org_id=selected_org["id"],
        )
        await db.table("users").update({"zernio_account_id": account.id}).eq("id", str(user.id)).execute()

    except (ZernioError, EncryptionError) as exc:
        await sender.send_text(
            msg.channel_user_id,
            f"Couldn't finalise LinkedIn selection: {exc}\n\nPlease try /reconnect to start over.",
        )
        return

    session.context.connect_token = None
    session.context.pending_orgs = []
    session.context.connect_token_issued_at = None
    session.context.onboarding_step = OnboardingStep.TIMEZONE

    await _ask_timezone(
        sender,
        msg.channel_user_id,
        account_name=selected_org["name"],
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

    db = await get_db()
    await db.table("users").update({"timezone": tz_input}).eq("id", str(user.id)).execute()

    await (
        db.table("user_schedules")
        .upsert(
            {
                "user_id": str(user.id),
                "days_of_week": [0, 4],
                "time_of_day": "09:00:00",
                "timezone": tz_input,
                "enabled": False,
            },
            on_conflict="user_id",
        )
        .execute()
    )

    session.state = SessionState.IDLE
    session.context.clear_draft()
    session.context.clear_onboarding()
    session.context.onboarding_step = OnboardingStep.COMPLETE

    await sender.send_text(
        msg.channel_user_id,
        "*You're all set!*\n\n"
        "Here's what I've configured:\n"
        "- LinkedIn account connected\n"
        f"- Timezone: {tz_input}\n"
        "- Auto-schedule: Mon/Fri 9am (currently *off* - use /schedule auto to enable)\n\n"
        "*Send me a topic or idea to write your first post!*\n\n"
        "_Tip: /help shows all commands_",
    )


async def restart_linkedin_oauth(
    session: Session,
    user: UserRow,
    sender: object,
    msg: NormalisedMessage,
) -> None:
    """Re-run just the LinkedIn OAuth step (for /reconnect command)."""
    session.state = SessionState.ONBOARDING
    session.context.connect_token = None
    session.context.connect_token_issued_at = None
    session.context.pending_orgs = []
    session.context.onboarding_step = OnboardingStep.LINKEDIN_OAUTH
    await _start_linkedin_oauth(session, user, sender, msg)


async def _ask_timezone(sender: object, channel_user_id: str, account_name: str) -> None:
    await sender.send_text(
        channel_user_id,
        f"*{account_name}* connected!\n\n"
        "Almost done!\n\n"
        "What timezone are you in? This is used for scheduling posts at the right time.\n\n"
        "Examples:\n"
        "- `Asia/Kolkata` (IST)\n"
        "- `America/New_York` (EST)\n"
        "- `Europe/London` (GMT)\n"
        "- `UTC`\n\n"
        "Reply with your timezone:",
    )


def _validate_llm_key_format(provider: LLMProvider, key: str) -> bool:
    """Basic format validation before making an API call."""
    if provider == LLMProvider.ANTHROPIC:
        return key.startswith("sk-ant-") and len(key) > 20
    if provider == LLMProvider.OPENAI:
        return key.startswith("sk-") and len(key) > 20
    if provider == LLMProvider.GROQ:
        return key.startswith("gsk_") and len(key) > 20
    if provider == LLMProvider.GEMINI:
        return len(key) > 20
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

        elif provider == LLMProvider.GEMINI:
            from google import genai

            client = genai.Client(api_key=key)
            await client.aio.models.generate_content(
                model=model,
                contents="Hi",
                config=genai.types.GenerationConfig(max_output_tokens=10, response_modalities=["TEXT"]),
            )

        return True

    except Exception as exc:
        logger.warning("onboarding.llm_key_invalid", provider=provider, error=str(exc))
        return False
