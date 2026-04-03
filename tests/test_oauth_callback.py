from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.db.models import Channel, UserRow
from app.session.models import OnboardingStep, SessionContext, SessionState


def make_user() -> UserRow:
    return UserRow(
        id=uuid4(),
        channel=Channel.TELEGRAM,
        channel_user_id="123456",
        zernio_api_key_enc="enc_key",
        zernio_profile_id="prof_test123",
        zernio_account_id=None,
        llm_provider=None,
        llm_api_key_enc=None,
        llm_model=None,
        timezone="Asia/Kolkata",
        style_prefs={},
        is_active=True,
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_oauth_callback_auto_continues_when_profile_id_present():
    from app.api.oauth_callback import oauth_callback

    with patch("app.api.oauth_callback._auto_continue_onboarding", new_callable=AsyncMock) as cont:
        response = await oauth_callback(
            connect_token="token123",
            profile_id="prof_test123",
            account_id="acc_test123",
            username="Shadab Jamadar",
            error=None,
        )

    assert response.status_code == 200
    cont.assert_awaited_once_with("prof_test123", "token123", "acc_test123", "Shadab Jamadar")


@pytest.mark.asyncio
async def test_auto_continue_onboarding_loads_user_and_saves_session():
    from app.api.oauth_callback import _auto_continue_onboarding

    user = make_user()
    session = MagicMock()
    session.state = SessionState.ONBOARDING
    session.context = SessionContext(onboarding_step=OnboardingStep.LINKEDIN_OAUTH)

    mock_db = MagicMock()
    (
        mock_db.table.return_value.select.return_value.eq.return_value.eq.return_value.maybe_single.return_value.execute
    ) = AsyncMock(return_value=MagicMock(data=user.model_dump(mode="json")))

    sender = MagicMock()

    with (
        patch("app.db.client.get_db", return_value=mock_db),
        patch("app.session.store.get_or_create_session", return_value=session),
        patch("app.session.store.save_session", new_callable=AsyncMock) as save_session,
        patch("app.api.oauth_callback._get_sender", return_value=sender),
        patch("app.conversation.onboarding._continue_linkedin_onboarding", new_callable=AsyncMock) as cont,
    ):
        await _auto_continue_onboarding("prof_test123", "token123")

    assert session.state == SessionState.ONBOARDING
    assert session.context.onboarding_step == OnboardingStep.LINKEDIN_OAUTH
    assert session.context.connect_token == "token123"
    cont.assert_awaited_once_with(session, user, sender, "123456", "token123")
    save_session.assert_awaited_once_with(session)


@pytest.mark.asyncio
async def test_auto_continue_onboarding_uses_account_id_when_present():
    from app.api.oauth_callback import _auto_continue_onboarding

    user = make_user()
    session = MagicMock()
    session.state = SessionState.ONBOARDING
    session.context = SessionContext(onboarding_step=OnboardingStep.LINKEDIN_OAUTH)

    mock_db = MagicMock()
    (
        mock_db.table.return_value.select.return_value.eq.return_value.eq.return_value.maybe_single.return_value.execute
    ) = AsyncMock(return_value=MagicMock(data=user.model_dump(mode="json")))

    sender = MagicMock()

    with (
        patch("app.db.client.get_db", return_value=mock_db),
        patch("app.session.store.get_or_create_session", return_value=session),
        patch("app.session.store.save_session", new_callable=AsyncMock) as save_session,
        patch("app.api.oauth_callback._get_sender", return_value=sender),
        patch(
            "app.conversation.onboarding._complete_linkedin_account_connection",
            new_callable=AsyncMock,
        ) as complete,
        patch("app.conversation.onboarding._continue_linkedin_onboarding", new_callable=AsyncMock) as cont,
    ):
        await _auto_continue_onboarding("prof_test123", "token123", "acc_test123", "Shadab Jamadar")

    complete.assert_awaited_once_with(
        session,
        user,
        sender,
        "123456",
        "acc_test123",
        "Shadab Jamadar",
    )
    cont.assert_not_called()
    save_session.assert_awaited_once_with(session)


@pytest.mark.asyncio
async def test_continue_linkedin_onboarding_advances_to_org_selection():
    from app.conversation.onboarding import _continue_linkedin_onboarding

    user = make_user()
    session = MagicMock()
    session.context = SessionContext(onboarding_step=OnboardingStep.LINKEDIN_OAUTH)
    sender = MagicMock()
    sender.send_text = AsyncMock()

    mock_db = MagicMock()
    mock_db.table.return_value.select.return_value.eq.return_value.single.return_value.execute = AsyncMock(
        return_value=MagicMock(data={"zernio_api_key_enc": "enc_key"})
    )

    orgs = [
        SimpleNamespace(id="org_1", name="Personal", type="personal"),
        SimpleNamespace(id="org_2", name="Acme Inc", type="organization"),
    ]
    client = MagicMock()
    client.list_linkedin_orgs = AsyncMock(return_value=orgs)

    with (
        patch("app.conversation.onboarding.get_db", return_value=mock_db),
        patch("app.conversation.onboarding.decrypt", return_value="sk_test"),
        patch("app.conversation.onboarding.ZernioClient", return_value=client),
    ):
        await _continue_linkedin_onboarding(session, user, sender, "123456", "token123")

    assert session.context.onboarding_step == OnboardingStep.LINKEDIN_ORG
    assert session.context.connect_token == "token123"
    assert session.context.pending_orgs == [
        {"id": "org_1", "name": "Personal", "type": "personal"},
        {"id": "org_2", "name": "Acme Inc", "type": "organization"},
    ]
    sender.send_text.assert_awaited_once()
    assert "Which account would you like to post from?" in sender.send_text.await_args.args[1]


@pytest.mark.asyncio
async def test_complete_linkedin_account_connection_sets_account_and_moves_to_timezone():
    from app.conversation.onboarding import _complete_linkedin_account_connection

    user = make_user()
    session = MagicMock()
    session.context = SessionContext(onboarding_step=OnboardingStep.LINKEDIN_OAUTH)
    sender = MagicMock()
    sender.send_text = AsyncMock()

    mock_db = MagicMock()
    mock_db.table.return_value.update.return_value.eq.return_value.execute = AsyncMock(return_value=MagicMock())

    with patch("app.conversation.onboarding.get_db", return_value=mock_db):
        await _complete_linkedin_account_connection(
            session,
            user,
            sender,
            "123456",
            "acc_test123",
            "Shadab Jamadar",
        )

    assert session.context.onboarding_step == OnboardingStep.TIMEZONE
    assert session.context.connect_token is None
    assert session.context.pending_orgs == []
    sender.send_text.assert_awaited_once()
    assert "What timezone are you in?" in sender.send_text.await_args.args[1]
