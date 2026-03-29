"""
app/content/style_memory.py
────────────────────────────
Learns and persists a user's writing style from their approved posts.

After a post is approved and scheduled, we extract style signals and
update the user's style_prefs in Supabase. These prefs are then injected
into future generation prompts so the LLM matches the user's voice.

Style signals extracted:
  - Average post length
  - Most-used tone
  - Hashtag usage patterns
  - Sentence structure signals (questions, lists, stories)
  - CTA patterns
"""

import re
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)


def extract_style_signals(content: str) -> dict[str, Any]:
    """
    Extract style signals from a single post.
    Returns a dict of signals that can be merged into style_prefs.
    """
    signals: dict[str, Any] = {}

    # Word count
    words = content.split()
    signals["word_count"] = len(words)

    # Hashtag count and examples
    hashtags = re.findall(r"#\w+", content)
    signals["hashtag_count"] = len(hashtags)
    signals["hashtags_sample"] = hashtags[:5]

    # Ends with question (CTA signal)
    signals["ends_with_question"] = "?" in content[-100:]

    # Uses numbered lists
    signals["uses_numbered_list"] = bool(re.search(r"^\d+\.", content, re.MULTILINE))

    # Uses bullet points
    signals["uses_bullets"] = bool(re.search(r"^[•\-\*]", content, re.MULTILINE))

    # Has a strong hook (short first line)
    first_line = content.split("\n")[0].strip()
    signals["hook_length"] = len(first_line.split())
    signals["short_hook"] = len(first_line.split()) <= 12

    # Emoji usage
    emoji_pattern = re.compile(
        "["
        "\U0001f600-\U0001f64f"
        "\U0001f300-\U0001f5ff"
        "\U0001f680-\U0001f6ff"
        "\U0001f1e0-\U0001f1ff"
        "\U00002702-\U000027b0"
        "]+",
        flags=re.UNICODE,
    )
    emojis = emoji_pattern.findall(content)
    signals["uses_emojis"] = len(emojis) > 0
    signals["emoji_count"] = len(emojis)

    # Paragraph count (rough proxy for structure)
    paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
    signals["paragraph_count"] = len(paragraphs)

    return signals


def merge_style_prefs(
    existing_prefs: dict[str, Any],
    new_signals: dict[str, Any],
    example_post: str | None = None,
) -> dict[str, Any]:
    """
    Merge new style signals into existing prefs using a rolling average.
    Keeps the last 3 example posts for few-shot prompting.
    """
    prefs = existing_prefs.copy()

    # Track how many posts we've learned from
    n = prefs.get("posts_learned", 0) + 1
    prefs["posts_learned"] = n

    # Rolling average for numeric signals
    for key in ("word_count", "hashtag_count", "emoji_count", "paragraph_count"):
        if key in new_signals:
            old_avg = prefs.get(f"avg_{key}", new_signals[key])
            prefs[f"avg_{key}"] = (old_avg * (n - 1) + new_signals[key]) / n

    # Boolean signals — majority vote (>50% of posts)
    for key in (
        "ends_with_question",
        "uses_numbered_list",
        "uses_bullets",
        "short_hook",
        "uses_emojis",
    ):
        if key in new_signals:
            old_rate = prefs.get(f"rate_{key}", 0.0)
            prefs[f"rate_{key}"] = (old_rate * (n - 1) + (1.0 if new_signals[key] else 0.0)) / n

    # Keep last 3 example posts for few-shot prompting
    if example_post:
        examples = prefs.get("example_posts", [])
        examples.append(example_post[:500])  # Truncate to 500 chars per example
        prefs["example_posts"] = examples[-3:]  # Keep only last 3

    # Derive a human-readable length preference from the rolling average
    avg_words = prefs.get("avg_word_count", 300)
    if avg_words < 150:
        prefs["preferred_length"] = "short (around 150 words)"
    elif avg_words < 400:
        prefs["preferred_length"] = "medium (around 300 words)"
    else:
        prefs["preferred_length"] = "long (around 500 words)"

    return prefs


async def update_style_from_post(user_id: str, content: str) -> None:
    """
    Called after a post is approved. Extracts style signals and
    updates the user's style_prefs in Supabase.
    """
    from app.db.client import get_db

    db = await get_db()

    # Load current prefs
    result = await db.table("users").select("style_prefs").eq("id", user_id).single().execute()
    current_prefs = result.data.get("style_prefs", {}) if result and result.data else {}

    # Extract and merge
    signals = extract_style_signals(content)
    updated_prefs = merge_style_prefs(current_prefs, signals, example_post=content)

    # Save back
    await db.table("users").update({"style_prefs": updated_prefs}).eq("id", user_id).execute()

    logger.info(
        "style_memory.updated",
        user_id=user_id,
        posts_learned=updated_prefs.get("posts_learned", 1),
        avg_word_count=round(updated_prefs.get("avg_word_count", 0)),
    )


def build_style_context(style_prefs: dict[str, Any]) -> str:
    """
    Convert stored style prefs into a natural-language description
    for injection into generation prompts.
    """
    if not style_prefs or style_prefs.get("posts_learned", 0) == 0:
        return ""

    parts = []
    n = style_prefs.get("posts_learned", 0)

    if n >= 2:
        parts.append(f"Based on this user's {n} previous posts:")

        # Length
        if "preferred_length" in style_prefs:
            parts.append(f"- Preferred length: {style_prefs['preferred_length']}")

        # Hashtags
        avg_hashtags = style_prefs.get("avg_hashtag_count", 0)
        if avg_hashtags > 0:
            parts.append(f"- Typically uses {round(avg_hashtags)} hashtags per post")

        # CTAs
        if style_prefs.get("rate_ends_with_question", 0) > 0.5:
            parts.append("- Usually ends posts with a question to drive engagement")

        # Structure
        if style_prefs.get("rate_uses_numbered_list", 0) > 0.5:
            parts.append("- Often uses numbered lists (1. 2. 3.)")
        elif style_prefs.get("rate_uses_bullets", 0) > 0.5:
            parts.append("- Often uses bullet points")

        # Hook style
        if style_prefs.get("rate_short_hook", 0) > 0.5:
            parts.append("- Prefers short, punchy opening hooks (under 12 words)")

        # Emoji usage
        if style_prefs.get("rate_uses_emojis", 0) > 0.5:
            parts.append("- Uses emojis moderately")
        else:
            parts.append("- Rarely uses emojis — keep it text-only")

    return "\n".join(parts)
