"""
app/conversation/settings.py
─────────────────────────────
Handles the /settings command — lets users view and update their preferences.
"""

from app.channels.base import NormalisedMessage
from app.db.client import get_db
from app.db.models import UserRow
from app.session.store import Session


async def handle_settings(
    session: Session,
    user: UserRow,
    sender: object,
    msg: NormalisedMessage,
) -> None:
    """Show current settings and available update commands."""
    prefs = user.style_prefs

    # Fetch schedule info
    db = get_db()
    sched = (
        db.table("user_schedules")
        .select("enabled, days_of_week, time_of_day")
        .eq("user_id", str(user.id))
        .maybe_single()
        .execute()
    )
    auto_enabled = sched.data["enabled"] if sched.data else False
    auto_status = "ON ✅" if auto_enabled else "OFF ❌"

    day_names = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
    days = sched.data.get("days_of_week", [0, 4]) if sched.data else [0, 4]
    days_str = " + ".join(day_names.get(d, str(d)) for d in days)

    text = (
        "*Your Settings*\n\n"
        f"*LLM:* {user.llm_provider or 'not set'} ({user.llm_model or 'no model'})\n"
        f"*Timezone:* {user.timezone}\n"
        f"*Auto-schedule:* {auto_status} — {days_str} at "
        f"{sched.data['time_of_day'] if sched.data else '09:00'}\n"
        f"*Default tone:* {prefs.get('tone', 'not set')}\n"
        f"*Default audience:* {prefs.get('audience', 'not set')}\n\n"
        "*To update:*\n"
        "• /timezone Asia/Kolkata\n"
        "• /schedule auto (toggle)\n"
        "• Reply *tone: storytelling* to set default tone\n"
        "• Reply *audience: founders* to set default audience\n"
        "• /reconnect — re-link LinkedIn"
    )

    await sender.send_text(msg.channel_user_id, text)

    # Handle inline setting updates ("tone: storytelling")
    body = msg.text.lower()
    if body.startswith("tone:"):
        new_tone = msg.text.split(":", 1)[1].strip()
        prefs["tone"] = new_tone
        db.table("users").update({"style_prefs": prefs}).eq("id", str(user.id)).execute()
        await sender.send_text(msg.channel_user_id, f"Default tone updated to: *{new_tone}*")

    elif body.startswith("audience:"):
        new_audience = msg.text.split(":", 1)[1].strip()
        prefs["audience"] = new_audience
        db.table("users").update({"style_prefs": prefs}).eq("id", str(user.id)).execute()
        await sender.send_text(msg.channel_user_id, f"Default audience updated to: *{new_audience}*")
