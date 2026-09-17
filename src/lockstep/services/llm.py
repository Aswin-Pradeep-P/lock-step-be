"""Shared two-provider text completion: Anthropic primary, Groq fallback.

Every caller hands this a system prompt and user content and gets back plain
text or `None` — it never raises. Nothing here decides a status, an amount or a
date; callers own their own deterministic fallback, because a narrative
feature must never fail a reconciliation or a page load.
"""

from __future__ import annotations

from lockstep.config import Settings


async def _call_anthropic(
    settings: Settings, system: str, user_content: str, max_tokens: int
) -> str | None:
    if not settings.anthropic_api_key:
        return None
    try:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        response = await client.messages.create(
            model=settings.anthropic_model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_content}],
        )
        text = "".join(block.text for block in response.content if block.type == "text").strip()
        return text or None
    except Exception:
        return None


async def _call_groq(
    settings: Settings, system: str, user_content: str, max_tokens: int
) -> str | None:
    """Same prompt, same contract, an OpenAI-compatible chat-completions call — used
    only when there's no working Anthropic key."""
    if not settings.groq_api_key:
        return None
    try:
        import httpx

        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.groq_api_key}"},
                json={
                    "model": settings.groq_model,
                    "max_tokens": max_tokens,
                    # The default model is a reasoning model — without this, its own
                    # reasoning tokens can eat the entire max_tokens budget before any
                    # visible text comes out (confirmed: ~260 reasoning tokens at
                    # default effort vs ~25 at "low", for a task this short).
                    "reasoning_effort": "low",
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_content},
                    ],
                },
            )
            response.raise_for_status()
            text = response.json()["choices"][0]["message"]["content"].strip()
            return text or None
    except Exception:
        return None


async def complete(
    settings: Settings, system: str, user_content: str, max_tokens: int = 300
) -> str | None:
    """Try Anthropic, then Groq. `None` if neither is configured or both fail."""
    for provider in (_call_anthropic, _call_groq):
        text = await provider(settings, system, user_content, max_tokens)
        if text:
            return text
    return None
