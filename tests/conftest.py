"""
tests/conftest.py
──────────────────
Shared pytest fixtures available to all tests.
"""

from datetime import UTC
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet

TEST_FERNET_KEY = Fernet.generate_key().decode()


@pytest.fixture(scope="session")
def fernet_key() -> str:
    return TEST_FERNET_KEY


@pytest.fixture()
def mock_settings(monkeypatch):
    """
    Set all required environment variables so Settings can initialise
    without a real .env file.
    """
    monkeypatch.setenv("FERNET_SECRET_KEY", TEST_FERNET_KEY)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999:test_bot_token")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "test-webhook-secret-123456789012345")
    monkeypatch.setenv("SUPABASE_URL", "https://test-project.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "eyJhbGciOiJIUzI1NiJ9." + "x" * 40)
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "test_whatsapp_secret")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "test_access_token_12345")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "9876543210")
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "test_verify_token_abc")
    monkeypatch.setenv("OAUTH_CALLBACK_BASE_URL", "https://test.example.com")
    monkeypatch.setenv("APP_ENV", "development")

    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def mock_db():
    """
    Mock Supabase client. Returns a MagicMock that mimics
    the chained query builder pattern: db.table().select().eq().execute()
    """
    db = MagicMock()

    # Default: return empty results
    execute_mock = MagicMock(return_value=MagicMock(data=[]))

    # Build a chainable mock
    query = MagicMock()
    query.execute = execute_mock
    query.select = MagicMock(return_value=query)
    query.insert = MagicMock(return_value=query)
    query.update = MagicMock(return_value=query)
    query.delete = MagicMock(return_value=query)
    query.upsert = MagicMock(return_value=query)
    query.eq = MagicMock(return_value=query)
    query.neq = MagicMock(return_value=query)
    query.gte = MagicMock(return_value=query)
    query.lte = MagicMock(return_value=query)
    query.lt = MagicMock(return_value=query)
    query.gt = MagicMock(return_value=query)
    query.in_ = MagicMock(return_value=query)
    query.like = MagicMock(return_value=query)
    query.order = MagicMock(return_value=query)
    query.limit = MagicMock(return_value=query)
    query.single = MagicMock(return_value=query)
    query.maybe_single = MagicMock(return_value=query)

    db.table = MagicMock(return_value=query)

    return db


@pytest.fixture()
def sample_user_row():
    """A fully configured UserRow for testing."""
    from datetime import datetime
    from uuid import uuid4

    from cryptography.fernet import Fernet

    from app.db.models import Channel, LLMProvider, UserRow

    fernet = Fernet(TEST_FERNET_KEY.encode())
    encrypted_llm_key = fernet.encrypt(b"sk-ant-test-key").decode()
    encrypted_zernio_key = fernet.encrypt(b"sk_test_zernio_key").decode()

    return UserRow(
        id=uuid4(),
        channel=Channel.TELEGRAM,
        channel_user_id="123456789",
        zernio_api_key_enc=encrypted_zernio_key,
        zernio_profile_id="prof_test123",
        zernio_account_id="acc_test456",
        llm_provider=LLMProvider.ANTHROPIC,
        llm_api_key_enc=encrypted_llm_key,
        llm_model="claude-haiku-4-5-20251001",
        timezone="Asia/Kolkata",
        style_prefs={"tone": "thought leadership", "audience": "founders"},
        is_active=True,
        created_at=datetime.now(UTC),
    )
