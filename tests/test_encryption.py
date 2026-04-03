"""
tests/test_encryption.py
─────────────────────────
Tests for the Fernet encryption helpers.
Uses a real Fernet key so we test the actual crypto round-trip.
"""

import pytest
from cryptography.fernet import Fernet

# Generate a test key for all tests in this module
TEST_FERNET_KEY = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def set_test_env(monkeypatch):
    """Set all required env vars for Settings to initialise."""
    monkeypatch.setenv("FERNET_SECRET_KEY", TEST_FERNET_KEY)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:test_token")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "test-secret-1234567890123456789012")
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "x" * 40)
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "test_app_secret")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "test_access_token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "1234567890")
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "test_verify_token")
    monkeypatch.setenv("OAUTH_CALLBACK_BASE_URL", "https://test.example.com")

    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestEncryptDecrypt:
    def test_encrypt_returns_string(self):
        from app.core.encryption import encrypt

        result = encrypt("my_api_key_sk_abc123")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_decrypt_returns_original(self):
        from app.core.encryption import decrypt, encrypt

        original = "sk-ant-api03-test-key-12345"
        token = encrypt(original)
        recovered = decrypt(token)
        assert recovered == original

    def test_different_encryptions_of_same_value(self):
        """Fernet uses a random IV — same input should produce different tokens."""
        from app.core.encryption import encrypt

        key = "same_api_key"
        token1 = encrypt(key)
        token2 = encrypt(key)
        assert token1 != token2  # Different ciphertext due to random IV

    def test_both_decrypt_to_same_value(self):
        """Both tokens should decrypt to the same original value."""
        from app.core.encryption import decrypt, encrypt

        key = "same_api_key"
        token1 = encrypt(key)
        token2 = encrypt(key)
        assert decrypt(token1) == decrypt(token2) == key

    def test_encrypts_zernio_key(self):
        from app.core.encryption import decrypt, encrypt

        zernio_key = "sk_" + "a" * 64
        token = encrypt(zernio_key)
        assert decrypt(token) == zernio_key

    def test_encrypts_anthropic_key(self):
        from app.core.encryption import decrypt, encrypt

        api_key = "sk-ant-api03-" + "x" * 90
        token = encrypt(api_key)
        assert decrypt(token) == api_key

    def test_encrypts_unicode(self):
        from app.core.encryption import decrypt, encrypt

        # Keys shouldn't contain unicode, but test robustness
        value = "test_value_with_émojis_🔑"
        token = encrypt(value)
        assert decrypt(token) == value


class TestDecryptErrors:
    def test_invalid_token_raises(self):
        from app.core.encryption import EncryptionError, decrypt

        with pytest.raises(EncryptionError) as exc_info:
            decrypt("this_is_not_a_valid_fernet_token")
        assert "decrypt" in str(exc_info.value).lower()

    def test_tampered_token_raises(self):
        from app.core.encryption import EncryptionError, decrypt, encrypt

        token = encrypt("original")
        # Tamper with the token by flipping characters
        tampered = token[:-5] + "XXXXX"
        with pytest.raises(EncryptionError):
            decrypt(tampered)

    def test_error_message_is_user_friendly(self):
        """Error message should guide the user, not expose internals."""
        from app.core.encryption import EncryptionError, decrypt

        try:
            decrypt("invalid")
        except EncryptionError as exc:
            msg = str(exc)
            # Should NOT contain cryptographic terms
            assert "fernet" not in msg.lower()
            # Should suggest a user action
            assert "api key" in msg.lower() or "settings" in msg.lower()

    def test_empty_string_raises(self):
        from app.core.encryption import EncryptionError, decrypt

        with pytest.raises((EncryptionError, Exception)):
            decrypt("")
