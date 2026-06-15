"""Multi-provider LLM fallback chain.

When one provider hits its rate limit (HTTP 429) or errors with 5xx, GitPilot
automatically switches to the next provider in ``PROVIDERS``. Combined free
daily limits across all providers are roughly ~17,800 requests/day.

Two hard safety rules are enforced here:
- ``_assert_no_secrets`` blocks any prompt containing secret-like patterns.
- Confidence returned to callers is always capped at ``MAX_CONFIDENCE``.
"""

from __future__ import annotations

import logging
import re

import httpx

from gitpilot.config.safety import MAX_CONFIDENCE, SENSITIVE_PATTERNS, cap_confidence

logger = logging.getLogger("gitpilot.llm")

# Default confidence reported for a successful provider call. Never 100.
DEFAULT_CONFIDENCE = 85

PROVIDERS: list[dict] = [
    {
        "name": "gemini-flash (openrouter)",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "google/gemini-flash-1.5",
        "key_env": "OPENROUTER_API_KEY",
        "format": "openai",
        "daily_limit": 1500,
    },
    {
        "name": "llama-3.1-70b (openrouter)",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "meta-llama/llama-3.1-70b-instruct:free",
        "key_env": "OPENROUTER_API_KEY",
        "format": "openai",
        "daily_limit": 200,
    },
    {
        "name": "mistral-7b (openrouter)",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "mistralai/mistral-7b-instruct:free",
        "key_env": "OPENROUTER_API_KEY",
        "format": "openai",
        "daily_limit": 200,
    },
    {
        "name": "gemini-flash (google direct)",
        "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent",
        "model": "gemini-1.5-flash",
        "key_env": "GOOGLE_AI_STUDIO_KEY",
        "format": "google",
        "daily_limit": 1500,
    },
    {
        "name": "llama-3.1-70b (groq)",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "model": "llama-3.1-70b-versatile",
        "key_env": "GROQ_API_KEY",
        "format": "openai",
        "daily_limit": 14400,
    },
]


class AllProvidersExhausted(Exception):
    """Raised when every configured provider failed or is rate limited."""


class RateLimitError(Exception):
    """Raised internally on HTTP 429 to trigger fallback to the next provider."""


class LLMResolver:
    def __init__(self, keys: dict, timeout: float = 30.0):
        self.keys = keys or {}
        self.timeout = timeout

    def call(self, prompt: str, system: str = "") -> tuple[str, str, int]:
        """Try each provider in order.

        Returns ``(response_text, provider_used, confidence)``. Falls back on
        429 or 5xx. NEVER sends prompts containing sensitive patterns.
        Confidence is always capped at ``MAX_CONFIDENCE``.
        """
        self._assert_no_secrets(prompt)
        if system:
            self._assert_no_secrets(system)

        last_error: Exception | None = None
        for provider in PROVIDERS:
            key = self.keys.get(provider["key_env"])
            if not key:
                continue
            try:
                text = self._call_provider(provider, prompt, system, key)
                logger.info("LLM provider used: %s", provider["name"])
                return text, provider["name"], cap_confidence(DEFAULT_CONFIDENCE)
            except RateLimitError:
                logger.warning("%s rate limited, trying next...", provider["name"])
                continue
            except Exception as exc:  # noqa: BLE001 — intentional broad fallback
                last_error = exc
                logger.warning("%s failed: %s, trying next...", provider["name"], exc)
                continue

        raise AllProvidersExhausted(
            "All LLM providers exhausted. Will post a human-review request."
            + (f" Last error: {last_error}" if last_error else "")
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _assert_no_secrets(self, text: str) -> None:
        """Block any prompt containing secret-like patterns."""
        for pattern in SENSITIVE_PATTERNS:
            if re.search(pattern, text or "", re.IGNORECASE):
                raise ValueError(
                    f"Blocked: prompt contains sensitive pattern '{pattern}'. "
                    "GitPilot never sends secrets to external providers."
                )

    def _call_provider(self, provider: dict, prompt: str, system: str, key: str) -> str:
        """Make the HTTP call, dispatching by provider response format."""
        if provider.get("format") == "google":
            return self._call_google(provider, prompt, system, key)
        return self._call_openai_compatible(provider, prompt, system, key)

    def _call_openai_compatible(self, provider, prompt, system, key) -> str:
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body = {"model": provider["model"], "messages": messages}

        response = httpx.post(provider["url"], headers=headers, json=body, timeout=self.timeout)
        self._raise_for_rate_limit(response)
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]

    def _call_google(self, provider, prompt, system, key) -> str:
        # Google AI Studio uses a different request/response shape and an API key
        # passed as a query parameter rather than a bearer token.
        headers = {"Content-Type": "application/json"}
        combined = f"{system}\n\n{prompt}" if system else prompt
        body = {"contents": [{"parts": [{"text": combined}]}]}
        url = f"{provider['url']}?key={key}"

        response = httpx.post(url, headers=headers, json=body, timeout=self.timeout)
        self._raise_for_rate_limit(response)
        response.raise_for_status()
        data = response.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]

    @staticmethod
    def _raise_for_rate_limit(response: httpx.Response) -> None:
        if response.status_code == 429:
            raise RateLimitError()
        if response.status_code >= 500:
            raise Exception(f"Server error: {response.status_code}")


__all__ = [
    "LLMResolver",
    "AllProvidersExhausted",
    "RateLimitError",
    "PROVIDERS",
    "MAX_CONFIDENCE",
]
