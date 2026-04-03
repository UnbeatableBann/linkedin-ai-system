"""
app/core/encryption.py
──────────────────────
AES-128-CBC + HMAC encryption via Fernet for storing
API keys (Zernio, LLM providers) at rest in Supabase.

Keys are encrypted before INSERT and decrypted in memory
only at the moment of use. They are never logged.
"""

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


def _fernet() -> Fernet:
    try:
        settings = get_settings()
        key = settings.fernet_secret_key
        # When pydantic is mocked, key will be a MagicMock — fall back to env
        if not isinstance(key, str):
            raise TypeError("settings returned non-string")
        return Fernet(key.encode())
    except Exception:
        # Direct env var fallback (used in tests when pydantic is mocked)
        import os

        key = os.environ.get("FERNET_SECRET_KEY", "")
        if not key:
            raise RuntimeError("FERNET_SECRET_KEY not set")
        return Fernet(key.encode())


def encrypt(plaintext: str) -> str:
    """Encrypt a plaintext string. Returns a base64 token."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """
    Decrypt a Fernet token. Raises EncryptionError if the token
    is invalid or was encrypted with a different key.
    """
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise EncryptionError(
            "Failed to decrypt value. The FERNET_SECRET_KEY may have changed "
            "or the stored value is corrupted. Ask the user to re-enter their API key."
        ) from exc


class EncryptionError(Exception):
    """Raised when decryption fails due to key mismatch or corruption."""


# TODO:
# 1. Key rotation is not handled
# 2. No TTL / expiration use
# 3. It is not production-grade for key lifecycle management
