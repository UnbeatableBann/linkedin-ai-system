"""
app/content/generator.py
─────────────────────────
Generates LinkedIn post drafts using the user's configured LLM.
Enforces LinkedIn content rules after generation.
"""

from typing import Any

from app.content.llm_client import call_llm
from app.content.post_rules import enforce_post_rules
from app.content.prompts import build_generation_system_prompt, build_generation_user_prompt
from app.core.logging import get_logger
from app.db.models import UserRow

logger = get_logger(__name__)


async def generate_post(
    user: UserRow,
    topic: str | None,
    tone: str | None,
    audience: str | None,
    length_pref: str | None,
    style_prefs: dict[str, Any],
) -> str:
    """
    Generate a LinkedIn post draft.

    1. Build system + user prompts
    2. Call the user's LLM
    3. Apply post rules (length limit, etc.)
    4. Return the clean post text
    """
    system_prompt = build_generation_system_prompt(tone, audience, length_pref, style_prefs)
    user_prompt = build_generation_user_prompt(topic, tone, audience, length_pref)

    logger.info(
        "generator.generating",
        user_id=str(user.id),
        provider=user.llm_provider,
        topic=(topic or "")[:60],
    )

    content = await call_llm(
        user=user,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_tokens=1200,
    )

    return enforce_post_rules(content)
