#!/usr/bin/env python3
"""
scripts/register_telegram_webhook.py
──────────────────────────────────────
Registers your FastAPI server as the Telegram webhook receiver.

Run this AFTER docker-compose is up and your server is reachable.

Usage:
    python scripts/register_telegram_webhook.py

For local dev with ngrok:
    1. Run: ngrok http 8000
    2. Copy the https URL into OAUTH_CALLBACK_BASE_URL in .env
       (or pass it as --url)
    3. Run this script
"""

import argparse
import os
import sys
from pathlib import Path

import httpx


def load_env() -> dict[str, str]:
    env_file = Path(__file__).parent.parent / ".env"
    if not env_file.exists():
        print("❌ .env file not found. Run scripts/setup.py first.")
        sys.exit(1)

    env: dict[str, str] = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def main() -> None:
    parser = argparse.ArgumentParser(description="Register Telegram webhook")
    parser.add_argument("--url", help="Override the public server URL")
    parser.add_argument("--dry-run", action="store_true", help="Print the request without sending")
    args = parser.parse_args()

    env = load_env()

    token = env.get("TELEGRAM_BOT_TOKEN")
    secret = env.get("TELEGRAM_WEBHOOK_SECRET")
    base_url = args.url or env.get("OAUTH_CALLBACK_BASE_URL", "")

    if not token:
        print("❌ TELEGRAM_BOT_TOKEN not found in .env")
        sys.exit(1)

    if not base_url or "ngrok" in base_url and "your-ngrok" in base_url:
        print("❌ OAUTH_CALLBACK_BASE_URL is not set to a real URL in .env")
        print("   For local dev: run ngrok http 8000, then update the URL in .env")
        sys.exit(1)

    webhook_url = f"{base_url.rstrip('/')}/webhooks/telegram"
    api_url = f"https://api.telegram.org/bot{token}/setWebhook"

    payload = {
        "url": webhook_url,
        "secret_token": secret,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": True,
    }

    print(f"\nRegistering Telegram webhook:")
    print(f"  Webhook URL: {webhook_url}")
    print(f"  Secret token: {secret[:8]}...{secret[-4:] if secret else ''}")

    if args.dry_run:
        print("\n[DRY RUN] Would send:")
        import json
        print(json.dumps(payload, indent=2))
        return

    try:
        resp = httpx.post(api_url, json=payload, timeout=10)
        data = resp.json()
        if data.get("ok"):
            print(f"\n  ✅ Webhook registered successfully!")
            print(f"  Description: {data.get('description', '')}")
        else:
            print(f"\n  ❌ Registration failed: {data}")
            sys.exit(1)
    except Exception as exc:
        print(f"\n  ❌ Request failed: {exc}")
        sys.exit(1)

    # Verify the webhook is set correctly
    verify_url = f"https://api.telegram.org/bot{token}/getWebhookInfo"
    try:
        resp = httpx.get(verify_url, timeout=10)
        info = resp.json().get("result", {})
        print(f"\n  Webhook info:")
        print(f"    URL: {info.get('url', 'not set')}")
        print(f"    Pending updates: {info.get('pending_update_count', 0)}")
        if info.get("last_error_message"):
            print(f"    ⚠️  Last error: {info['last_error_message']}")
    except Exception:
        pass


if __name__ == "__main__":
    main()
