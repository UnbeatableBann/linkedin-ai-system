"""
app/content/post_rules.py
──────────────────────────
Enforces LinkedIn-specific content rules on generated/refined posts.

Applied after every LLM call before returning content to the user.
Rules are deterministic — no LLM needed here.
"""

import re

from app.core.logging import get_logger

logger = get_logger(__name__)

LINKEDIN_MAX_CHARS = 3000
SOFT_WARN_CHARS = 2800  # Warn user if close to limit

# Patterns that indicate the LLM leaked its own preamble
PREAMBLE_PATTERNS = [
    r"^here'?s?\s+(your|a|the)\s+(post|draft|linkedin)",
    r"^sure[,!]?\s+here",
    r"^of course[,!]?\s+here",
    r"^i'?ve?\s+(written|drafted|created)",
    r"^below\s+is\s+(your|a|the)",
    r"^---\s*\n",
]

# Patterns that indicate the LLM wrapped in quotes or code blocks
WRAPPER_PATTERNS = [
    r'^"(.*)"$',  # "content"
    r"^'(.*)'$",  # 'content'
    r"^```.*\n(.*)\n```$",  # ```\ncontent\n```
]


def enforce_post_rules(content: str) -> str:
    """
    Clean and enforce LinkedIn post rules.
    Applied after every LLM call.

    1. Strip LLM preamble / wrapper artifacts
    2. Enforce character limit
    3. Log warnings for near-limit content
    """
    if not content:
        return content

    content = content.strip()

    # ── Strip LLM preamble ─────────────────────────────────────────────────
    content = _strip_preamble(content)

    # ── Strip wrapper quotes or code fences ────────────────────────────────
    content = _strip_wrappers(content)

    # ── Normalise excessive blank lines ───────────────────────────────────
    content = re.sub(r"\n{3,}", "\n\n", content)

    # ── Enforce character limit ─────────────────────────────────────────────
    char_count = len(content)

    if char_count > LINKEDIN_MAX_CHARS:
        logger.warning(
            "post_rules.truncated",
            original_len=char_count,
            limit=LINKEDIN_MAX_CHARS,
        )
        # Truncate at last complete sentence before the limit
        content = _truncate_at_sentence(content, LINKEDIN_MAX_CHARS)

    elif char_count > SOFT_WARN_CHARS:
        logger.info(
            "post_rules.near_limit",
            char_count=char_count,
            limit=LINKEDIN_MAX_CHARS,
        )

    return content.strip()


def check_post_length(content: str) -> dict:
    """
    Return length info for display to the user.
    Used in reviewing.py to show char count alongside the draft.
    """
    char_count = len(content)
    word_count = len(content.split())
    return {
        "chars": char_count,
        "words": word_count,
        "over_limit": char_count > LINKEDIN_MAX_CHARS,
        "near_limit": char_count > SOFT_WARN_CHARS,
        "remaining": max(0, LINKEDIN_MAX_CHARS - char_count),
    }


# ── Private helpers ────────────────────────────────────────────────────────


def _strip_preamble(content: str) -> str:
    """Remove LLM preamble lines like 'Here's your post:' from the top."""
    lines = content.split("\n")
    start_idx = 0

    for i, line in enumerate(lines):
        stripped = line.strip().lower()
        if not stripped:
            continue
        # Check if this line matches a preamble pattern
        if any(re.match(p, stripped, re.IGNORECASE | re.DOTALL) for p in PREAMBLE_PATTERNS):
            start_idx = i + 1
            # Skip any blank lines after the preamble
            while start_idx < len(lines) and not lines[start_idx].strip():
                start_idx += 1
            break
        else:
            # First non-empty line is not a preamble — stop checking
            break

    return "\n".join(lines[start_idx:])


def _strip_wrappers(content: str) -> str:
    """Remove surrounding quotes or code fences if the whole post is wrapped."""
    for pattern in WRAPPER_PATTERNS:
        match = re.match(pattern, content, re.DOTALL)
        if match:
            return match.group(1).strip()
    return content


def _truncate_at_sentence(text: str, max_chars: int) -> str:
    """Truncate text at the last complete sentence before max_chars."""
    if len(text) <= max_chars:
        return text

    # Find the last sentence boundary before max_chars
    chunk = text[:max_chars]
    # Look for sentence endings: . ! ?
    last_end = max(
        chunk.rfind(". "),
        chunk.rfind("! "),
        chunk.rfind("? "),
        chunk.rfind(".\n"),
        chunk.rfind("!\n"),
        chunk.rfind("?\n"),
    )

    if last_end > max_chars * 0.7:  # Only truncate at sentence if it's not too short
        return chunk[: last_end + 1].strip()

    # Fallback: truncate at last space
    last_space = chunk.rfind(" ")
    if last_space > 0:
        return chunk[:last_space].strip() + "…"

    return chunk[:max_chars].strip() + "…"
