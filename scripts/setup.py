#!/usr/bin/env python3
"""
scripts/setup.py
────────────────
Interactive setup wizard that generates a .env file.

Run once before starting the system:
    python scripts/setup.py

What it does:
  1. Generates a Fernet encryption key
  2. Walks you through each required credential
  3. Writes a valid .env file
  4. Registers the Telegram webhook
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
ENV_FILE = ROOT / ".env"


def heading(text: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {text}")
    print(f"{'─' * 60}")


def ask(prompt: str, default: str = "", secret: bool = False) -> str:
    if default:
        display_prompt = f"{prompt} [{default}]: "
    else:
        display_prompt = f"{prompt}: "

    if secret:
        import getpass

        value = getpass.getpass(display_prompt)
    else:
        value = input(display_prompt).strip()

    return value or default


def generate_fernet_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def main() -> None:
    print("\n" + "═" * 60)
    print("  LinkedIn AI Content System — Setup Wizard")
    print("═" * 60)
    print("\nThis wizard will create your .env file.")
    print("You'll need: Supabase project, Telegram bot token,")
    print("WhatsApp Cloud API credentials, and a public URL (ngrok for local dev).\n")

    if ENV_FILE.exists():
        overwrite = ask("A .env file already exists. Overwrite? (y/N)", "N")
        if overwrite.lower() != "y":
            print("Keeping existing .env. Exiting.")
            sys.exit(0)

    config: dict[str, str] = {}

    # ── App ────────────────────────────────────────────────────────────────
    heading("App Environment")
    config["APP_ENV"] = ask("Environment", "development")
    config["LOG_LEVEL"] = ask("Log level", "INFO")

    # ── Encryption ─────────────────────────────────────────────────────────
    heading("Encryption Key")
    print("Generating a Fernet encryption key...")
    config["FERNET_SECRET_KEY"] = generate_fernet_key()
    print(f"  Generated: {config['FERNET_SECRET_KEY'][:20]}...")
    print("\n  ⚠️  SAVE THIS KEY SEPARATELY.")
    print("  Losing it makes all stored API keys unrecoverable.\n")

    # ── Supabase ────────────────────────────────────────────────────────────
    heading("Supabase (app.supabase.com → your project → Settings → API)")
    config["SUPABASE_URL"] = ask("Project URL (https://xxxx.supabase.co)")
    config["SUPABASE_SERVICE_KEY"] = ask("Service role key (starts with eyJ...)", secret=True)

    # ── Redis ───────────────────────────────────────────────────────────────
    heading("Redis (using Docker Compose default)")
    config["REDIS_URL"] = ask("Redis URL", "redis://redis:6379/0")
    config["CELERY_BROKER_URL"] = ask("Celery broker URL", "redis://redis:6379/0")
    config["CELERY_RESULT_BACKEND"] = ask("Celery result backend", "redis://redis:6379/1")

    # ── Telegram ────────────────────────────────────────────────────────────
    heading("Telegram Bot")
    print("Create a bot at https://t.me/BotFather → /newbot")
    config["TELEGRAM_BOT_TOKEN"] = ask("Bot token (123456:ABC...)", secret=True)

    print("\nGenerating a webhook secret...")
    import secrets

    config["TELEGRAM_WEBHOOK_SECRET"] = secrets.token_hex(32)
    print(f"  Generated: {config['TELEGRAM_WEBHOOK_SECRET'][:16]}...")

    # ── WhatsApp ────────────────────────────────────────────────────────────
    heading("WhatsApp Cloud API")
    print("Get these from Meta Developer Console → your app → WhatsApp → API Setup")
    config["WHATSAPP_APP_SECRET"] = ask("App secret", secret=True)
    config["WHATSAPP_ACCESS_TOKEN"] = ask("Permanent access token", secret=True)
    config["WHATSAPP_PHONE_NUMBER_ID"] = ask("Phone number ID")
    config["WHATSAPP_VERIFY_TOKEN"] = ask("Webhook verify token (any string you choose)", secrets.token_hex(16))

    # ── OAuth Callback ──────────────────────────────────────────────────────
    heading("OAuth Callback URL")
    print("For local dev, use ngrok: ngrok http 8000")
    print("Then use the https URL ngrok gives you.\n")
    config["OAUTH_CALLBACK_BASE_URL"] = ask("Your public HTTPS URL", "https://your-ngrok-url.ngrok.io")

    # ── Write .env ──────────────────────────────────────────────────────────
    heading("Writing .env file")
    _write_env(config)
    print(f"\n  ✅ Created: {ENV_FILE}")

    # ── Run DB migrations ───────────────────────────────────────────────────
    heading("Next Steps")
    print("1. Run your Supabase migrations:")
    print("   → Open Supabase Dashboard → SQL Editor")
    print(f"   → Copy and run: {ROOT}/src/app/db/migrations/001_initial.sql\n")
    print("2. Start the system:")
    print("   docker-compose up --build\n")
    print("3. Register Telegram webhook (after docker-compose is running):")
    print("   python scripts/register_telegram_webhook.py\n")
    print("4. Configure WhatsApp webhook in Meta Developer Console:")
    print(f"   URL: {config['OAUTH_CALLBACK_BASE_URL']}/webhooks/whatsapp")
    print(f"   Verify token: {config['WHATSAPP_VERIFY_TOKEN']}\n")
    print("5. Test: message your Telegram bot with /start\n")


def _write_env(config: dict[str, str]) -> None:
    lines = [
        "# LinkedIn AI Content System — Generated by scripts/setup.py",
        "# Do not commit this file to version control.\n",
    ]
    for key, value in config.items():
        lines.append(f"{key}={value}")

    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
