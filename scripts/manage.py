#!/usr/bin/env python3
"""
scripts/manage.py
──────────────────
Management CLI for the LinkedIn AI Content System.

Commands:
  users list              — List all registered users
  users show <id>         — Show a user's details
  users delete <id>       — Soft-delete a user and cancel their jobs
  posts list [--user=id]  — List scheduled/recent posts
  posts cancel <id>       — Cancel a post by ID
  jobs list               — List pending Celery jobs
  jobs retry <id>         — Retry a failed job
  db cleanup              — Delete old webhook_log rows
  db stats                — Show table row counts
  bot webhook-info        — Show current Telegram webhook status
  bot set-webhook         — Re-register Telegram webhook

Usage:
    python scripts/manage.py users list
    python scripts/manage.py posts list --status=scheduled
    python scripts/manage.py db stats
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Add src layout to import path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))


def load_env() -> None:
    """Load .env file into os.environ."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        print("❌ .env not found. Run scripts/setup.py first.")
        sys.exit(1)
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def run_async(coro):
    """Run an async coroutine from this synchronous CLI."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def get_db():
    from app.db.client import get_db as _get_db

    return await _get_db()


# ── Commands ───────────────────────────────────────────────────────────────


def cmd_users_list(args) -> None:
    db = run_async(get_db())
    result = run_async(
        db.table("users")
        .select("id, channel, channel_user_id, llm_provider, timezone, is_active, created_at")
        .order("created_at", desc=True)
        .limit(50)
        .execute()
    )

    if not result.data:
        print("No users found.")
        return

    print(f"\n{'ID':<12} {'Channel':<12} {'Channel User':<20} {'LLM':<12} {'TZ':<20} {'Active'}")
    print("─" * 85)
    for u in result.data:
        uid = u["id"][:8]
        active = "✓" if u["is_active"] else "✗"
        print(
            f"{uid:<12} {u['channel']:<12} {u['channel_user_id']:<20} "
            f"{u.get('llm_provider','—'):<12} {u['timezone']:<20} {active}"
        )

    print(f"\nTotal: {len(result.data)} users")


def cmd_users_show(args) -> None:
    db = run_async(get_db())
    result = run_async(
        db.table("users").select("*").like("id", f"{args.id}%").maybe_single().execute()
    )
    if not result.data:
        print(f"User not found: {args.id}")
        return

    u = result.data
    print(f"\nUser: {u['id']}")
    print(f"  Channel:          {u['channel']} / {u['channel_user_id']}")
    print(f"  LLM:              {u.get('llm_provider', '—')} ({u.get('llm_model', '—')})")
    print(f"  Timezone:         {u['timezone']}")
    print(f"  Zernio profile:   {u.get('zernio_profile_id', '—')}")
    print(f"  Zernio account:   {u.get('zernio_account_id', '—')}")
    print(f"  Active:           {u['is_active']}")
    print(f"  Created:          {u['created_at']}")

    prefs = u.get("style_prefs", {})
    if prefs:
        print(f"\n  Style prefs:")
        for k, v in prefs.items():
            if not isinstance(v, (list, dict)):
                print(f"    {k}: {v}")


def cmd_users_delete(args) -> None:
    db = run_async(get_db())
    result = run_async(
        db.table("users")
        .select("id, channel_user_id")
        .like("id", f"{args.id}%")
        .maybe_single()
        .execute()
    )
    if not result.data:
        print(f"User not found: {args.id}")
        return

    user = result.data
    confirm = input(f"Delete user {user['id']} ({user['channel_user_id']})? [y/N] ")
    if confirm.lower() != "y":
        print("Cancelled.")
        return

    # Soft delete
    run_async(db.table("users").update({"is_active": False}).eq("id", user["id"]).execute())
    # Cancel pending jobs
    run_async(
        db.table("schedule_jobs")
        .update({"status": "cancelled"})
        .eq("user_id", user["id"])
        .eq("status", "pending")
        .execute()
    )
    print(f"✓ User {user['id']} deactivated and pending jobs cancelled.")


def cmd_posts_list(args) -> None:
    db = run_async(get_db())
    query = db.table("posts").select("id, user_id, status, scheduled_for, created_at, content")

    status_filter = getattr(args, "status", None)
    if status_filter:
        query = query.eq("status", status_filter)

    user_filter = getattr(args, "user", None)
    if user_filter:
        query = query.like("user_id", f"{user_filter}%")

    result = run_async(query.order("created_at", desc=True).limit(20).execute())

    if not result.data:
        print("No posts found.")
        return

    print(f"\n{'ID':<12} {'User':<12} {'Status':<12} {'Scheduled':<22} {'Preview'}")
    print("─" * 90)
    for p in result.data:
        pid = p["id"][:8]
        uid = p["user_id"][:8]
        sf = p.get("scheduled_for", "—")[:16] if p.get("scheduled_for") else "—"
        preview = p["content"][:35].replace("\n", " ")
        print(f"{pid:<12} {uid:<12} {p['status']:<12} {sf:<22} {preview}...")


def cmd_posts_cancel(args) -> None:
    db = run_async(get_db())
    result = run_async(
        db.table("posts")
        .select("id, status, user_id, zernio_post_id")
        .like("id", f"{args.id}%")
        .maybe_single()
        .execute()
    )
    if not result.data:
        print(f"Post not found: {args.id}")
        return

    post = result.data
    if post["status"] == "published":
        print("Post is already published — cannot cancel.")
        return

    run_async(db.table("posts").update({"status": "cancelled"}).eq("id", post["id"]).execute())
    run_async(
        db.table("schedule_jobs")
        .update({"status": "cancelled"})
        .eq("post_id", post["id"])
        .execute()
    )
    print(f"✓ Post {post['id']} cancelled.")


def cmd_jobs_list(args) -> None:
    db = run_async(get_db())
    result = run_async(
        (
            db.table("schedule_jobs")
            .select("id, user_id, post_id, job_type, run_at, status, attempts, last_error")
            .in_("status", ["pending", "failed"])
            .order("run_at", desc=False)
            .limit(20)
            .execute()
        )
    )

    if not result.data:
        print("No pending or failed jobs.")
        return

    print(f"\n{'ID':<12} {'User':<12} {'Type':<12} {'Run At':<22} {'Status':<10} {'Attempts'}")
    print("─" * 80)
    for j in result.data:
        jid = j["id"][:8]
        uid = j["user_id"][:8]
        run_at = j.get("run_at", "—")[:16]
        print(
            f"{jid:<12} {uid:<12} {j['job_type']:<12} {run_at:<22} {j['status']:<10} {j['attempts']}"
        )
        if j.get("last_error"):
            print(f"  └─ Error: {j['last_error'][:80]}")


def cmd_db_cleanup(args) -> None:
    from datetime import datetime, timedelta, timezone

    db = run_async(get_db())
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    result = run_async(db.table("webhook_log").delete().lt("processed_at", cutoff).execute())
    print(f"✓ Deleted webhook_log rows older than 30 days. Cutoff: {cutoff[:10]}")


def cmd_db_stats(args) -> None:
    db = run_async(get_db())
    tables = ["users", "sessions", "posts", "schedule_jobs", "webhook_log", "user_schedules"]
    print("\nTable row counts:")
    print("─" * 30)
    for table in tables:
        try:
            result = run_async(db.table(table).select("id", count="exact").execute())
            count = result.count if hasattr(result, "count") else len(result.data)
            print(f"  {table:<20} {count:>8}")
        except Exception as e:
            print(f"  {table:<20} error: {e}")


def cmd_bot_webhook_info(args) -> None:
    import httpx

    from app.config import get_settings

    settings = get_settings()

    resp = httpx.get(
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/getWebhookInfo",
        timeout=10,
    )
    info = resp.json().get("result", {})
    print(f"\nWebhook URL:      {info.get('url', 'not set')}")
    print(f"Pending updates:  {info.get('pending_update_count', 0)}")
    print(f"Max connections:  {info.get('max_connections', '—')}")
    if info.get("last_error_message"):
        print(f"Last error:       {info['last_error_message']}")
        print(f"Last error date:  {info.get('last_error_date', '—')}")


def cmd_bot_set_webhook(args) -> None:
    """Shortcut to run the webhook registration script."""
    os.execv(
        sys.executable,
        [sys.executable, str(ROOT / "scripts" / "register_telegram_webhook.py")],
    )


# ── Argument parser ────────────────────────────────────────────────────────


def main() -> None:
    load_env()

    parser = argparse.ArgumentParser(
        description="LinkedIn AI Content System — management CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="group", required=True)

    # users
    users_p = sub.add_parser("users")
    users_sub = users_p.add_subparsers(dest="action", required=True)
    users_sub.add_parser("list")
    show_p = users_sub.add_parser("show")
    show_p.add_argument("id", help="User ID prefix")
    del_p = users_sub.add_parser("delete")
    del_p.add_argument("id", help="User ID prefix")

    # posts
    posts_p = sub.add_parser("posts")
    posts_sub = posts_p.add_subparsers(dest="action", required=True)
    list_p = posts_sub.add_parser("list")
    list_p.add_argument("--status", help="Filter by status (draft/scheduled/published/failed)")
    list_p.add_argument("--user", help="Filter by user ID prefix")
    cancel_p = posts_sub.add_parser("cancel")
    cancel_p.add_argument("id", help="Post ID prefix")

    # jobs
    jobs_p = sub.add_parser("jobs")
    jobs_sub = jobs_p.add_subparsers(dest="action", required=True)
    jobs_sub.add_parser("list")

    # db
    db_p = sub.add_parser("db")
    db_sub = db_p.add_subparsers(dest="action", required=True)
    db_sub.add_parser("cleanup")
    db_sub.add_parser("stats")

    # bot
    bot_p = sub.add_parser("bot")
    bot_sub = bot_p.add_subparsers(dest="action", required=True)
    bot_sub.add_parser("webhook-info")
    bot_sub.add_parser("set-webhook")

    args = parser.parse_args()

    dispatch = {
        ("users", "list"): cmd_users_list,
        ("users", "show"): cmd_users_show,
        ("users", "delete"): cmd_users_delete,
        ("posts", "list"): cmd_posts_list,
        ("posts", "cancel"): cmd_posts_cancel,
        ("jobs", "list"): cmd_jobs_list,
        ("db", "cleanup"): cmd_db_cleanup,
        ("db", "stats"): cmd_db_stats,
        ("bot", "webhook-info"): cmd_bot_webhook_info,
        ("bot", "set-webhook"): cmd_bot_set_webhook,
    }

    handler = dispatch.get((args.group, args.action))
    if handler:
        handler(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
