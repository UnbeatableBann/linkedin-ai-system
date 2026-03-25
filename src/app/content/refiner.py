"""
app/content/refiner.py
───────────────────────
Refines an existing LinkedIn post draft based on user edit instructions.

Unlike generation (which starts from scratch), refinement:
  - Always receives the current draft as context
  - Applies only what the user asked for
  - Preserves the author's voice
  - Tracks version history
"""

from typing import Any

from app.content.llm_client import call_llm
from app.content.post_rules import enforce_post_rules
from app.content.prompts import build_refinement_system_prompt, build_refinement_user_prompt
from app.core.logging import get_logger
from app.db.models import UserRow

logger = get_logger(__name__)


async def refine_post(
    user: UserRow,
    current_draft: str,
    edit_instruction: str,
    style_prefs: dict[str, Any],
) -> str:
    """
    Apply an edit instruction to an existing draft.

    Examples of edit_instruction:
      - "make it shorter"
      - "add 3 hashtags"
      - "make the opening hook punchier"
      - "change tone to more conversational"
      - "add a specific example about Zomato"

    Returns the refined post text.
    """
    system_prompt = build_refinement_system_prompt(style_prefs)
    user_prompt = build_refinement_user_prompt(current_draft, edit_instruction)

    logger.info(
        "refiner.refining",
        user_id=str(user.id),
        provider=user.llm_provider,
        instruction=edit_instruction[:80],
    )

    refined = await call_llm(
        user=user,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_tokens=1200,
    )

    return enforce_post_rules(refined)
