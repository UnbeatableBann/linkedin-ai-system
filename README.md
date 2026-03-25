# LinkedIn AI Content System

AI-powered LinkedIn post generation and scheduling via Telegram and WhatsApp.

## What it does

Users chat with a Telegram or WhatsApp bot to:
- Generate LinkedIn posts from a topic or rough idea
- Refine drafts interactively (tone, length, hashtags, etc.)
- Schedule posts for specific times or auto Mon/Fri
- Publish immediately via Zernio → LinkedIn

Each user brings their own LLM API key (Anthropic, OpenAI, or Groq) and their own Zernio account.

---

## Prerequisites

| Requirement | Where to get it |
|---|---|
| Docker + Docker Compose | docker.com |
| Python 3.12+ (for scripts) | python.org |
| Supabase account | app.supabase.com |
| Telegram bot token | @BotFather on Telegram |
| WhatsApp Cloud API | Meta Developer Console |
| ngrok (local dev) | ngrok.com |

---

## Quick Start

### 1. Clone and install script deps

```bash
git clone <repo>
cd linkedin-ai-system
pip install cryptography httpx python-dotenv
```

### 2. Run setup wizard

```bash
python scripts/setup.py
```

This generates `.env` with all required credentials.

### 3. Run Supabase migrations

1. Open [app.supabase.com](https://app.supabase.com) → your project → SQL Editor
2. Copy the contents of `app/db/migrations/001_initial.sql`
3. Paste and run

### 4. Start ngrok (for local Telegram/WhatsApp webhooks)

```bash
ngrok http 8000
```

Copy the `https://` URL and update `OAUTH_CALLBACK_BASE_URL` in `.env`.

### 5. Start the system

```bash
docker-compose up --build
```

This starts:
- **api** — FastAPI on port 8000
- **worker** — Celery worker (LLM generation + publishing)
- **beat** — Celery Beat (scheduled jobs)
- **redis** — Redis (job queue)

### 6. Register Telegram webhook

```bash
python scripts/register_telegram_webhook.py
```

### 7. Register WhatsApp webhook

In Meta Developer Console → your app → WhatsApp → Configuration:
- **Webhook URL:** `https://your-ngrok-url.ngrok.io/webhooks/whatsapp`
- **Verify token:** value of `WHATSAPP_VERIFY_TOKEN` in your `.env`
- **Subscribe to:** `messages`

### 8. Test

Open Telegram, message your bot with `/start`. You should get the onboarding flow.

---

## Project Structure

```
app/
├── api/                    FastAPI routes (webhooks, oauth callback, health)
├── channels/               Telegram + WhatsApp adapters
├── content/                LLM generation, refinement, post rules
├── conversation/           FSM dispatcher, onboarding, collecting, reviewing, scheduling
├── core/                   Encryption, logging
├── db/                     Supabase client, models, migrations
├── gateway/                Message routing, deduplication
├── scheduler/              Celery app, tasks (generate, publish, remind)
├── session/                FSM, session store
└── zernio/                 Zernio API client
scripts/
├── setup.py                Interactive .env generator
└── register_telegram_webhook.py
tests/                      pytest test suite
```

---

## Running Tests

```bash
# Install dev deps
pip install -e ".[dev]"

# Run all tests
pytest

# Run with coverage
pytest --cov=app --cov-report=term-missing

# Run a specific test file
pytest tests/test_fsm.py -v
```

---

## Environment Variables

See `.env.example` for all required variables with descriptions.

Key variables:

| Variable | Description |
|---|---|
| `FERNET_SECRET_KEY` | AES encryption key for API keys at rest. **Back this up.** |
| `SUPABASE_SERVICE_KEY` | Full-access DB key. Never expose publicly. |
| `TELEGRAM_BOT_TOKEN` | From @BotFather |
| `OAUTH_CALLBACK_BASE_URL` | Your public HTTPS URL (ngrok for local dev) |

---

## Architecture

```
Telegram / WhatsApp
      ↓
Channel Gateway (HMAC verify → dedup → user resolve)
      ↓
Conversation Dispatcher (FSM state → handler)
      ↓
Celery Tasks (async LLM calls, publish jobs)
      ↓
Zernio API → LinkedIn
      ↓
Supabase (all state persisted — survives restarts)
```

---

## Commands Reference

| Command | Description |
|---|---|
| `/start` | Begin onboarding (new) or welcome back (existing) |
| `/new` | Start a new post draft |
| `/cancel` | Cancel current operation |
| `/schedule list` | View upcoming scheduled posts |
| `/schedule auto` | Toggle Mon/Fri auto-scheduling |
| `/settings` | View/update LLM, timezone, style preferences |
| `/timezone Asia/Kolkata` | Set your timezone |
| `/reconnect` | Re-link LinkedIn if token expired |
| `/help` | Show all commands |

---

## Deploying to Railway

1. Push to GitHub
2. Create Railway project → Deploy from GitHub
3. Add Redis service in Railway
4. Set all environment variables from `.env` in Railway dashboard
5. Add a second service for Celery worker:
   - Start command: `celery -A app.scheduler.celery_app worker --loglevel=info -Q generation,publishing,default`
6. Add a third service for Celery Beat:
   - Start command: `celery -A app.scheduler.celery_app beat --loglevel=info`
7. Update `OAUTH_CALLBACK_BASE_URL` to your Railway URL
8. Run `python scripts/register_telegram_webhook.py --url https://your-app.railway.app`
