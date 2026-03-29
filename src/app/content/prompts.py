"""
app/content/prompts.py
───────────────────────
All LLM prompt templates in one place.

Keeping prompts here (not scattered in generator.py/refiner.py)
makes them easy to tune, A/B test, and version.
"""

from typing import Any


def build_generation_system_prompt(
    tone: str | None,
    audience: str | None,
    length_pref: str | None,
    style_prefs: dict[str, Any],
) -> str:
    """
    System prompt for post generation.
    Incorporates the user's tone, audience, and any saved style preferences.
    """
    tone = tone or "professional"
    audience = audience or "professionals"
    length = length_pref or "medium (around 300 words)"

    # Pull additional style info from saved prefs
    hashtag_style = style_prefs.get("hashtag_style", "add 3-5 relevant hashtags at the end")
    cta_style = style_prefs.get("cta_style", "end with a question or call to action")
    example_posts = style_prefs.get("example_posts", [])

    # Build style memory context if we have learned from past posts
    from app.content.style_memory import build_style_context

    style_context = build_style_context(style_prefs)

    example_section = ""
    if example_posts:
        examples_text = "\n\n---\n\n".join(example_posts[:2])
        example_section = f"""
Here are examples of their past posts to match their voice:

{examples_text}

---

"""

    return f"""You are an expert LinkedIn content writer. Your job is to write high-quality LinkedIn posts.

## Post parameters
- Tone: {tone}
- Target audience: {audience}
- Length: {length}
- Hashtags: {hashtag_style}
- Call to action: {cta_style}

{style_context}
{example_section}## LinkedIn best practices you must follow
- Start with a strong hook — the first line must make people stop scrolling
- Use short paragraphs (1-3 sentences) and line breaks for readability
- Write in first person, naturally and authentically
- No corporate jargon or buzzwords (synergy, leverage, paradigm shift)
- Maximum 3000 characters
- The post should feel human-written, not AI-generated

## Output format
Return ONLY the post text, ready to copy-paste to LinkedIn.
No preamble like "Here's your post:", no markdown formatting, no quotes around the post.
Just the post text itself."""


def build_generation_user_prompt(
    topic: str | None,
    tone: str | None,
    audience: str | None,
    length_pref: str | None,
) -> str:
    """User prompt for new post generation."""
    if not topic:
        return "Write a general professional LinkedIn post."

    # If topic is long, it's likely a rough draft to polish
    if len(topic) > 150:
        return f"""Polish and improve this draft into a great LinkedIn post:

{topic}

Keep the core message but improve the structure, hook, and readability."""

    return f"""Write a LinkedIn post about: {topic}

Tone: {tone or 'professional'}
Audience: {audience or 'professionals'}
Length: {length_pref or 'medium, around 300 words'}"""


def build_refinement_system_prompt(style_prefs: dict[str, Any]) -> str:
    """System prompt for refining an existing draft."""
    return """You are an expert LinkedIn content editor.
Your job is to refine an existing LinkedIn post based on the user's feedback.

Rules:
- Apply ONLY the requested changes — don't rewrite everything unless asked
- Preserve the author's voice and key message
- Keep within 3000 characters
- Return ONLY the refined post text, no preamble

LinkedIn best practices:
- Strong first-line hook
- Short paragraphs with line breaks
- No jargon
- First person, authentic tone"""


def build_refinement_user_prompt(current_draft: str, edit_instruction: str) -> str:
    """User prompt for draft refinement."""
    return f"""Current draft:

{current_draft}

---

Edit instruction: {edit_instruction}

Return the refined post text only."""
