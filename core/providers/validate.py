"""Checking an API key before trusting it.

Used by first-run setup and Settings so a bad key is caught while the user is
still looking at it, not as a cryptic failure an hour later. Listing models is
the check: it proves the key authenticates and costs no generation quota —
which matters for Gemini's 20 requests a day.

Portable: no UI and no platform imports.
"""

from __future__ import annotations

from dataclasses import dataclass

from .base import AuthError, ProviderError, RateLimited

#: Where a person gets a free key for each provider.
KEY_PAGES = {
    "groq": "https://console.groq.com/keys",
    "gemini": "https://aistudio.google.com/apikey",
}

PROVIDER_LABELS = {"groq": "Groq", "gemini": "Gemini"}


@dataclass
class KeyCheck:
    ok: bool
    message: str
    #: False when the provider could not be reached at all - the key may be fine.
    reachable: bool = True


def check_key(provider: str, key: str, timeout_s: int = 12) -> KeyCheck:
    """Try a key against the real service and explain the result in plain words."""
    label = PROVIDER_LABELS.get(provider, provider)
    key = (key or "").strip()
    if not key:
        return KeyCheck(False, "Paste a key first.")
    if any(ch.isspace() for ch in key):
        return KeyCheck(False, "That has spaces in it - the key was probably copied with extra text.")

    try:
        if provider == "groq":
            from .groq import GroqProvider

            count = len(GroqProvider(key, timeout_s=timeout_s).available_models())
        elif provider == "gemini":
            from google import genai
            from google.genai import types

            from .gemini import _classify

            client = genai.Client(
                api_key=key, http_options=types.HttpOptions(timeout=timeout_s * 1000)
            )
            try:
                count = sum(1 for _ in client.models.list())
            except Exception as e:  # classify with the adapter's own rules
                raise _classify(e) from e
        else:
            return KeyCheck(False, f"Unknown provider {provider!r}.")
    except AuthError:
        return KeyCheck(False, f"{label} rejected that key. Check it was copied completely.")
    except RateLimited:
        # A rate limit proves the key authenticated.
        return KeyCheck(True, f"Key works. {label} is busy right now, but that is temporary.")
    except ProviderError as e:
        text = str(e).lower()
        if any(w in text for w in ("connect", "timed out", "timeout", "network", "resolve", "unavailable")):
            return KeyCheck(False, f"Couldn't reach {label}. Check your internet connection.", reachable=False)
        return KeyCheck(False, f"{label} said: {str(e)[:160]}")
    except Exception as e:
        return KeyCheck(False, f"Couldn't check the key: {type(e).__name__}: {str(e)[:120]}", reachable=False)

    if count == 0:
        return KeyCheck(False, f"The key works but {label} offers it no models.")
    return KeyCheck(True, f"Key works - {count} models available.")
