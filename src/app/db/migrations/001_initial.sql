-- ─────────────────────────────────────────────────────────────────────────────
-- Migration 001: Initial schema
-- Run this in Supabase SQL editor (Dashboard → SQL Editor → New query)
-- ─────────────────────────────────────────────────────────────────────────────

-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "pgcrypto";


-- ── users ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    channel             TEXT NOT NULL CHECK (channel IN ('telegram', 'whatsapp')),
    channel_user_id     TEXT NOT NULL,
    zernio_api_key_enc  TEXT,                         -- Fernet-encrypted
    zernio_profile_id   TEXT,
    zernio_account_id   TEXT,
    llm_provider        TEXT CHECK (llm_provider IN ('anthropic', 'openai', 'groq')),
    llm_api_key_enc     TEXT,                         -- Fernet-encrypted
    llm_model           TEXT,
    timezone            TEXT NOT NULL DEFAULT 'UTC',
    style_prefs         JSONB NOT NULL DEFAULT '{}',
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT users_channel_user_unique UNIQUE (channel, channel_user_id)
);

CREATE INDEX IF NOT EXISTS idx_users_channel_user
    ON users (channel, channel_user_id);

CREATE INDEX IF NOT EXISTS idx_users_active
    ON users (is_active) WHERE is_active = TRUE;


-- ── sessions ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sessions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    state       TEXT NOT NULL DEFAULT 'idle',
    context     JSONB NOT NULL DEFAULT '{}',
    draft_id    UUID,                                -- FK set after posts table exists
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '24 hours',

    CONSTRAINT sessions_user_unique UNIQUE (user_id)  -- one active session per user
);

CREATE INDEX IF NOT EXISTS idx_sessions_expires
    ON sessions (expires_at);

CREATE INDEX IF NOT EXISTS idx_sessions_state
    ON sessions (state);


-- ── posts ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS posts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft','approved','scheduled','published','failed','cancelled')),
    zernio_post_id  TEXT,
    scheduled_for   TIMESTAMPTZ,
    published_at    TIMESTAMPTZ,
    version         INTEGER NOT NULL DEFAULT 1,
    edit_history    JSONB NOT NULL DEFAULT '[]',     -- array of previous content strings
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_posts_user_status
    ON posts (user_id, status);

CREATE INDEX IF NOT EXISTS idx_posts_scheduled
    ON posts (scheduled_for) WHERE status = 'scheduled';

CREATE INDEX IF NOT EXISTS idx_posts_user_scheduled_for
    ON posts (user_id, scheduled_for) WHERE scheduled_for IS NOT NULL;

-- Now add the FK from sessions to posts
ALTER TABLE sessions
    ADD CONSTRAINT fk_sessions_draft
    FOREIGN KEY (draft_id) REFERENCES posts(id) ON DELETE SET NULL;


-- ── schedule_jobs ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS schedule_jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    post_id         UUID NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    job_type        TEXT NOT NULL CHECK (job_type IN ('publish', 'reminder', 'expire_session')),
    run_at          TIMESTAMPTZ NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','running','done','failed','cancelled')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    celery_task_id  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_jobs_pending_run_at
    ON schedule_jobs (run_at) WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_jobs_user
    ON schedule_jobs (user_id, status);

CREATE INDEX IF NOT EXISTS idx_jobs_post
    ON schedule_jobs (post_id);


-- ── webhook_log ───────────────────────────────────────────────────────────────
-- Keeps a record of every processed webhook for deduplication.
-- Safe to truncate rows older than 30 days.
CREATE TABLE IF NOT EXISTS webhook_log (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    idempotency_key     TEXT NOT NULL UNIQUE,         -- channel:message_id
    channel             TEXT NOT NULL,
    user_channel_id     TEXT NOT NULL,
    payload             JSONB NOT NULL DEFAULT '{}',
    processed_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_webhook_log_processed
    ON webhook_log (processed_at);


-- ── user_schedules ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS user_schedules (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    days_of_week    INTEGER[] NOT NULL DEFAULT '{0,4}',  -- 0=Mon, 4=Fri
    time_of_day     TIME NOT NULL DEFAULT '09:00:00',
    timezone        TEXT NOT NULL DEFAULT 'UTC',
    enabled         BOOLEAN NOT NULL DEFAULT FALSE,
    auto_generate   BOOLEAN NOT NULL DEFAULT FALSE,

    CONSTRAINT user_schedules_user_unique UNIQUE (user_id)
);


-- ── updated_at trigger ────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER sessions_updated_at
    BEFORE UPDATE ON sessions
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();


-- ─────────────────────────────────────────────────────────────────────────────
-- Row Level Security
-- The backend uses the service role key (bypasses RLS).
-- RLS protects against any accidental anon key exposure.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE users          ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions       ENABLE ROW LEVEL SECURITY;
ALTER TABLE posts          ENABLE ROW LEVEL SECURITY;
ALTER TABLE schedule_jobs  ENABLE ROW LEVEL SECURITY;
ALTER TABLE webhook_log    ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_schedules ENABLE ROW LEVEL SECURITY;

-- Service role bypasses all RLS (Supabase default behaviour).
-- These policies are a safety net for anon/authenticated roles only.
-- No anon access to anything.
CREATE POLICY "No anon access to users"
    ON users FOR ALL TO anon USING (FALSE);

CREATE POLICY "No anon access to sessions"
    ON sessions FOR ALL TO anon USING (FALSE);

CREATE POLICY "No anon access to posts"
    ON posts FOR ALL TO anon USING (FALSE);

CREATE POLICY "No anon access to schedule_jobs"
    ON schedule_jobs FOR ALL TO anon USING (FALSE);

CREATE POLICY "No anon access to webhook_log"
    ON webhook_log FOR ALL TO anon USING (FALSE);

CREATE POLICY "No anon access to user_schedules"
    ON user_schedules FOR ALL TO anon USING (FALSE);


-- ─────────────────────────────────────────────────────────────────────────────
-- Cleanup job: auto-expire old webhook_log rows (keep 30 days)
-- Supabase doesn't support pg_cron on free tier; this is a manual cleanup query.
-- Run periodically from a Celery Beat task.
-- ─────────────────────────────────────────────────────────────────────────────
-- DELETE FROM webhook_log WHERE processed_at < NOW() - INTERVAL '30 days';
