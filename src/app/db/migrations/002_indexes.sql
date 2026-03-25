-- ─────────────────────────────────────────────────────────────────────────────
-- Migration 002: Performance indexes
-- Run after 001_initial.sql
--
-- Every index here targets a specific query pattern in the application code.
-- Explained inline so it's clear why each index exists.
-- ─────────────────────────────────────────────────────────────────────────────


-- ── users ─────────────────────────────────────────────────────────────────────

-- gateway/router.py: get_or_create_user — runs on EVERY inbound message
-- Query: WHERE channel = $1 AND channel_user_id = $2
-- Already covered by the UNIQUE constraint in 001, which creates an index.
-- Explicitly naming it for clarity:
-- idx_users_channel_user already exists from 001_initial.sql


-- ── sessions ──────────────────────────────────────────────────────────────────

-- session/store.py: get_or_create_session — runs on EVERY inbound message
-- Query: WHERE user_id = $1
-- The UNIQUE constraint on (user_id) creates this index automatically.
-- No additional index needed.

-- Celery Beat: expire_old_sessions cleanup — runs every hour
-- Query: WHERE expires_at < NOW() AND state != 'idle'
CREATE INDEX IF NOT EXISTS idx_sessions_expires_active
    ON sessions (expires_at)
    WHERE state != 'idle';


-- ── posts ─────────────────────────────────────────────────────────────────────

-- conversation/scheduling.py: _check_conflict — runs every time user schedules
-- Query: WHERE user_id = $1 AND status = 'scheduled'
--        AND scheduled_for BETWEEN $2 AND $3
CREATE INDEX IF NOT EXISTS idx_posts_conflict_check
    ON posts (user_id, scheduled_for)
    WHERE status = 'scheduled';

-- dispatcher.py: _handle_drafts — /drafts command
-- Query: WHERE user_id = $1 ORDER BY created_at DESC LIMIT 5
CREATE INDEX IF NOT EXISTS idx_posts_user_created
    ON posts (user_id, created_at DESC);

-- scheduler/tasks.py: watchdog_stale_jobs — checks every 5 minutes
-- Query: WHERE status = 'pending' AND run_at < NOW()
-- Already covered by idx_jobs_pending_run_at from 001, but let's confirm:
-- idx_jobs_pending_run_at already exists from 001_initial.sql


-- ── schedule_jobs ─────────────────────────────────────────────────────────────

-- dispatcher.py: _handle_delete — cancels all user's pending jobs
-- Query: WHERE user_id = $1 AND status = 'pending'
-- Already covered by idx_jobs_user from 001_initial.sql


-- ── webhook_log ───────────────────────────────────────────────────────────────

-- gateway/dedup.py: is_duplicate — runs on EVERY inbound message
-- Query: INSERT with unique check on idempotency_key
-- Already covered by the UNIQUE constraint which creates a btree index.

-- cleanup_webhook_log Beat task
-- Query: DELETE WHERE processed_at < NOW() - INTERVAL '30 days'
-- Already covered by idx_webhook_log_processed from 001_initial.sql


-- ── Partial index for active users ────────────────────────────────────────────

-- Used when listing active users for auto-schedule processing
-- Query: WHERE is_active = TRUE (in process_auto_schedules)
-- Already covered by idx_users_active from 001_initial.sql


-- ── Analyze tables after adding indexes ───────────────────────────────────────
-- Tells Postgres to update query planner statistics immediately
ANALYZE users;
ANALYZE sessions;
ANALYZE posts;
ANALYZE schedule_jobs;
ANALYZE webhook_log;


-- ─────────────────────────────────────────────────────────────────────────────
-- Summary of all indexes across migrations 001 + 002:
--
-- users:
--   idx_users_channel_user         (channel, channel_user_id) — UNIQUE
--   idx_users_active               (is_active) WHERE is_active = TRUE
--
-- sessions:
--   UNIQUE on (user_id)            automatic btree
--   idx_sessions_expires           (expires_at)
--   idx_sessions_state             (state)
--   idx_sessions_expires_active    (expires_at) WHERE state != 'idle'  [NEW]
--
-- posts:
--   idx_posts_user_status          (user_id, status)
--   idx_posts_scheduled            (scheduled_for) WHERE status = 'scheduled'
--   idx_posts_user_scheduled_for   (user_id, scheduled_for)
--   idx_posts_conflict_check       (user_id, scheduled_for) WHERE scheduled  [NEW]
--   idx_posts_user_created         (user_id, created_at DESC)                [NEW]
--
-- schedule_jobs:
--   idx_jobs_pending_run_at        (run_at) WHERE status = 'pending'
--   idx_jobs_user                  (user_id, status)
--   idx_jobs_post                  (post_id)
--
-- webhook_log:
--   UNIQUE on (idempotency_key)    automatic btree
--   idx_webhook_log_processed      (processed_at)
-- ─────────────────────────────────────────────────────────────────────────────
