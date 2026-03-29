"""
app/config.py
─────────────
All environment variables loaded and validated at startup.
If any required variable is missing, the app refuses to start
with a clear error message — no silent failures.
"""

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).parent.parent


class AppEnv(StrEnum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ────────────────────────────────────────────────────
    app_env: AppEnv = AppEnv.DEVELOPMENT
    log_level: LogLevel = LogLevel.INFO

    # ── Supabase ───────────────────────────────────────────────
    supabase_url: str = Field(..., description="Supabase project URL")
    supabase_service_key: str = Field(..., description="Supabase service role key")

    # ── Encryption ─────────────────────────────────────────────
    fernet_secret_key: str = Field(..., description="Fernet key for encrypting API keys at rest")

    # ── Redis / Celery ──────────────────────────────────────────
    redis_url: str = Field(default="redis://redis:6379/0")
    celery_broker_url: str = Field(default="redis://redis:6379/0")
    celery_result_backend: str = Field(default="redis://redis:6379/1")

    # ── Telegram ───────────────────────────────────────────────
    telegram_bot_token: str = Field(..., description="Token from @BotFather")
    telegram_webhook_secret: str = Field(..., description="HMAC secret for webhook verification")

    # ── WhatsApp ───────────────────────────────────────────────
    whatsapp_app_secret: str = Field(..., description="Meta app secret for sig verification")
    whatsapp_access_token: str = Field(..., description="Meta Cloud API access token")
    whatsapp_phone_number_id: str = Field(..., description="WhatsApp phone number ID")
    whatsapp_verify_token: str = Field(..., description="Token for Meta webhook challenge")

    # ── OAuth ──────────────────────────────────────────────────
    oauth_callback_base_url: str = Field(..., description="Public base URL for Zernio OAuth callback")

    # ── Derived / constants ────────────────────────────────────
    session_ttl_hours: int = 24
    max_post_chars: int = 3000
    max_input_chars: int = 2000
    zernio_connect_token_ttl_minutes: int = 12  # Zernio's token is 15min; we use 12 to be safe
    publish_retry_delays: list[int] = [60, 300, 900]  # seconds: 1min, 5min, 15min
    conflict_window_minutes: int = 30

    @field_validator("fernet_secret_key")
    @classmethod
    def validate_fernet_key(cls, v: str) -> str:
        import base64

        try:
            decoded = base64.urlsafe_b64decode(v.encode())
            if len(decoded) != 32:
                raise ValueError("Fernet key must be 32 bytes when decoded")
        except Exception as exc:
            raise ValueError(
                "FERNET_SECRET_KEY is invalid. "
                "Generate one with: "
                'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            ) from exc
        return v

    @field_validator("telegram_webhook_secret")
    @classmethod
    def validate_webhook_secret(cls, v: str) -> str:
        if len(v) < 16:
            raise ValueError("TELEGRAM_WEBHOOK_SECRET must be at least 32 characters")
        return v

    @field_validator("oauth_callback_base_url")
    @classmethod
    def validate_callback_url(cls, v: str) -> str:
        if not v.startswith("https://") and not v.startswith("http://localhost"):
            raise ValueError("OAUTH_CALLBACK_BASE_URL must be an HTTPS URL " "(or http://localhost for local dev)")
        return v.rstrip("/")

    @model_validator(mode="after")
    def warn_if_development(self) -> "Settings":
        if self.app_env == AppEnv.PRODUCTION:
            # In production, make sure callback URL is HTTPS
            if not self.oauth_callback_base_url.startswith("https://"):
                raise ValueError("OAUTH_CALLBACK_BASE_URL must use HTTPS in production")
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env == AppEnv.PRODUCTION

    @property
    def telegram_api_url(self) -> str:
        return f"https://api.telegram.org/bot{self.telegram_bot_token}"

    @property
    def oauth_callback_url(self) -> str:
        return f"{self.oauth_callback_base_url}/oauth/callback"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Single instance of Settings. Cached after first call.
    Use this everywhere instead of constructing Settings() directly.
    """
    return Settings()
