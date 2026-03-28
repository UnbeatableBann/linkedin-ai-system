from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.channels.base import NormalisedMessage
from app.db.models import Channel, LLMProvider, UserRow
from app.session.models import SessionContext, SessionState


def make_user() -> UserRow:
    return UserRow(
        id=uuid4(),
        channel=Channel.TELEGRAM,
        channel_user_id="123456",
        zernio_api_key_enc="enc_zernio",
        zernio_profile_id="prof_test",
        zernio_account_id="acc_test",
        llm_provider=LLMProvider.OPENAI,
        llm_api_key_enc="enc_llm",
        llm_model="gpt-4o",
        timezone="Asia/Kolkata",
        style_prefs={},
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )


def make_msg(text: str) -> NormalisedMessage:
    return NormalisedMessage(
        channel="telegram",
        channel_user_id="123456",
        message_id="msg_1",
        text=text,
    )


@pytest.mark.asyncio
async def test_handle_idle_rejects_unclear_topic():
    from app.conversation.collecting import handle_idle

    user = make_user()
    session = MagicMock()
    session.state = SessionState.IDLE
    session.context = SessionContext()

    sender = MagicMock()
    sender.send_text = AsyncMock()

    await handle_idle(session, user, sender, make_msg("sdf"))

    assert session.state == SessionState.IDLE
    assert session.context.topic is None
    sender.send_text.assert_awaited_once()
    assert "clearer topic or idea" in sender.send_text.await_args.args[1]


@pytest.mark.asyncio
async def test_handle_collecting_invalid_length_reasks_length():
    from app.conversation.collecting import handle_collecting

    user = make_user()
    session = MagicMock()
    session.state = SessionState.COLLECTING
    session.context = SessionContext(
        topic="how AI saves me time at work",
        tone="storytelling",
        audience="engineers / tech people",
        clarification_round=2,
    )

    sender = MagicMock()
    sender.send_text = AsyncMock()

    await handle_collecting(session, user, sender, make_msg("asdf"))

    assert session.state == SessionState.COLLECTING
    assert session.context.clarification_round == 2
    assert session.context.length_pref is None
    assert sender.send_text.await_count == 2
    assert "didn't catch the length" in sender.send_text.await_args_list[0].args[1]
    assert "How long should the post be?" in sender.send_text.await_args_list[1].args[1]


@pytest.mark.asyncio
async def test_handle_reviewing_schedule_shortcut_goes_to_scheduling():
    from app.conversation.reviewing import handle_reviewing

    user = make_user()
    session = MagicMock()
    session.state = SessionState.REVIEWING
    session.context = SessionContext(draft_content="Draft content")

    sender = MagicMock()
    sender.send_text = AsyncMock()

    with patch("app.conversation.scheduling.ask_for_schedule_time", new_callable=AsyncMock) as ask:
        await handle_reviewing(session, user, sender, make_msg("schedule"))

    assert session.state == SessionState.SCHEDULING
    ask.assert_awaited_once_with(sender, "123456")


@pytest.mark.asyncio
async def test_handle_reviewing_post_it_publishes_now():
    from app.conversation.reviewing import handle_reviewing

    user = make_user()
    session = MagicMock()
    session.id = uuid4()
    session.state = SessionState.REVIEWING
    session.context = SessionContext(draft_content="Draft content")

    sender = MagicMock()
    sender.send_text = AsyncMock()

    task = SimpleNamespace(delay=MagicMock())
    fake_tasks = SimpleNamespace(publish_now_task=task)

    with patch.dict("sys.modules", {"app.scheduler.tasks": fake_tasks}):
        await handle_reviewing(session, user, sender, make_msg("post it"))

    assert session.state == SessionState.IDLE
    task.delay.assert_called_once()


@pytest.mark.asyncio
async def test_handle_reviewing_unclear_text_does_not_refine():
    from app.conversation.reviewing import handle_reviewing

    user = make_user()
    session = MagicMock()
    session.state = SessionState.REVIEWING
    session.context = SessionContext(draft_content="Draft content")

    sender = MagicMock()
    sender.send_text = AsyncMock()

    await handle_reviewing(session, user, sender, make_msg("sdf"))

    assert session.state == SessionState.REVIEWING
    sender.send_text.assert_awaited_once()
    assert "didn't quite catch that" in sender.send_text.await_args.args[1]
