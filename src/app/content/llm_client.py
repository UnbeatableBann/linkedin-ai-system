"""
app/content/llm_client.py
──────────────────────────
Unified async LLM client that dispatches to the right provider SDK
based on the user's configuration.

All providers return a plain string (the generated text).
Provider-specific errors are caught and re-raised as LLMError.
"""

from app.core.logging import get_logger
from app.db.models import LLMProvider, UserRow

logger = get_logger(__name__)


class LLMError(Exception):
    """Raised when an LLM call fails. Always has a user-friendly message."""

    def __init__(self, message: str, retryable: bool = True) -> None:
        self.retryable = retryable
        super().__init__(message)


async def call_llm(
    user: UserRow,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 1024,
) -> str:
    """
    Make an LLM completion call using the user's configured provider.

    Dispatches to the correct provider SDK, handles errors uniformly,
    and returns the generated text as a plain string.

    Raises LLMError on failure.
    """
    from app.core.encryption import EncryptionError, decrypt

    if not user.llm_provider or not user.llm_api_key_enc:
        raise LLMError(
            "No LLM configured. Please run /settings to set up your AI provider.",
            retryable=False,
        )

    try:
        api_key = decrypt(user.llm_api_key_enc)
    except EncryptionError:
        raise LLMError(
            "Your API key couldn't be decrypted. Please re-enter it via /settings.",
            retryable=False,
        )

    provider = user.llm_provider
    model = user.llm_model or _default_model(provider)

    logger.info("llm.call", provider=provider, model=model)

    try:
        if provider == LLMProvider.ANTHROPIC:
            return await _call_anthropic(api_key, model, system_prompt, user_prompt, max_tokens)
        elif provider == LLMProvider.OPENAI:
            return await _call_openai(api_key, model, system_prompt, user_prompt, max_tokens)
        elif provider == LLMProvider.GROQ:
            return await _call_groq(api_key, model, system_prompt, user_prompt, max_tokens)
        elif provider == LLMProvider.GEMINI:
            return await _call_gemini(api_key, model, system_prompt, user_prompt, max_tokens)
        else:
            raise LLMError(f"Unknown LLM provider: {provider}", retryable=False)

    except LLMError:
        raise
    except Exception as exc:
        logger.error("llm.unexpected_error", provider=provider, error=str(exc))
        raise LLMError(f"Unexpected error calling {provider}: {str(exc)[:200]}")


async def _call_anthropic(api_key: str, model: str, system: str, user: str, max_tokens: int) -> str:
    import anthropic

    try:
        client = anthropic.AsyncAnthropic(api_key=api_key)
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return response.content[0].text.strip()

    except anthropic.AuthenticationError:
        raise LLMError(
            "Your Anthropic API key is invalid. Update it via /settings.",
            retryable=False,
        )
    except anthropic.RateLimitError:
        raise LLMError("Anthropic rate limit hit. Retrying shortly...", retryable=True)
    except anthropic.APIError as exc:
        raise LLMError(f"Anthropic API error: {str(exc)[:200]}", retryable=True)


async def _call_openai(api_key: str, model: str, system: str, user: str, max_tokens: int) -> str:
    import openai

    try:
        client = openai.AsyncOpenAI(api_key=api_key)
        response = await client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return (response.choices[0].message.content or "").strip()

    except openai.AuthenticationError:
        raise LLMError(
            "Your OpenAI API key is invalid. Update it via /settings.",
            retryable=False,
        )
    except openai.RateLimitError:
        raise LLMError("OpenAI rate limit hit. Retrying shortly...", retryable=True)
    except openai.APIError as exc:
        raise LLMError(f"OpenAI API error: {str(exc)[:200]}", retryable=True)


async def _call_groq(api_key: str, model: str, system: str, user: str, max_tokens: int) -> str:
    import groq

    try:
        client = groq.AsyncGroq(api_key=api_key)
        response = await client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return (response.choices[0].message.content or "").strip()

    except groq.AuthenticationError:
        raise LLMError(
            "Your Groq API key is invalid. Update it via /settings.",
            retryable=False,
        )
    except groq.RateLimitError:
        raise LLMError("Groq rate limit hit. Retrying shortly...", retryable=True)
    except Exception as exc:
        raise LLMError(f"Groq error: {str(exc)[:200]}", retryable=True)


async def _call_gemini(api_key: str, model: str, system: str, user: str, max_tokens: int) -> str:
    from google import errors, genai

    try:
        client = genai.Client(api_key=api_key)
        response = await client.aio.models.generate_content(
            model=model,
            contents=[genai.Content(role="user", parts=[genai.Part(text=system + "\n\n" + user)])],
            config=genai.types.GenerationConfig(max_output_tokens=max_tokens),
        )
        return (response.text or "").strip()

    except errors.APIError as exc:
        if "API key" in str(exc) or "authorization" in str(exc).lower():
            raise LLMError(
                "Your Gemini API key is invalid. Update it via /settings.",
                retryable=False,
            )
        raise LLMError(f"Gemini API error: {str(exc)[:200]}", retryable=True)

    except Exception as exc:
        raise LLMError(f"Gemini error: {str(exc)[:200]}", retryable=True)


def _default_model(provider: LLMProvider) -> str:
    defaults = {
        LLMProvider.ANTHROPIC: "claude-haiku-4-5-20251001",
        LLMProvider.OPENAI: "gpt-4o-mini",
        LLMProvider.GROQ: "llama-3.3-70b-versatile",
        LLMProvider.GEMINI: "gemini-2.0-flash",
    }
    return defaults.get(provider, "")
