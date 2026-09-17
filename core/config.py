"""Settings and secret storage.

Non-secret preferences live in a JSON file under the user's data directory.
API keys live in the OS credential store via `keyring`, never on disk in plaintext.

Portable by design: no Windows-only imports. Platform differences are handled by
branching on `sys.platform`, not by importing platform libraries.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

APP_NAME = "BantuAI"
SERVICE = "BantuAI"

#: Keys we know how to store. Maps our name -> the env var used as a dev fallback.
KEY_NAMES = {
    "gemini": "GEMINI_API_KEY",
    "groq": "GroqAPIKey",
}

#: Settled by ear from real samples — see CLAUDE.md. Do not substitute.
DEFAULT_VOICES: dict[str, dict[str, str]] = {
    "en": {"female": "en-US-AvaMultilingualNeural", "male": "en-GB-RyanNeural"},
    "hi": {"female": "hi-IN-SwaraNeural", "male": "hi-IN-MadhurNeural"},
    "ne": {"female": "ne-NP-HemkalaNeural", "male": "ne-NP-SagarNeural"},
}


def user_data_dir() -> Path:
    """Per-user writable directory. Never beside the executable."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / APP_NAME


@dataclass
class Settings:
    # --- models -------------------------------------------------------------
    # Preference order, verified callable on a free-tier key on 2026-09-16.
    # Being *listed* is not enough — the models endpoint advertises ids that then
    # 404 (gemini-2.5-*) or hang with a 504 (gemini-flash-latest, 3.7, 3.8).
    # The adapter probes and demotes dead ids at runtime, so this is a starting
    # preference, not a guarantee.
    gemini_models: list[str] = field(
        default_factory=lambda: [
            "gemini-3.6-flash",  # newest full flash that answers reliably (~1.8s)
            "gemini-3-flash-preview",
            "gemini-3.5-flash",
            "gemini-3.5-flash-lite",
            "gemini-flash-lite-latest",
        ]
    )
    # Verified tool-capable on a free key on 2026-09-16. NOTE: groq/compound and
    # compound-mini return "tool calling is not supported" - their agentic
    # tooling is built in, not yours - so they are useless to this agent.
    groq_models: list[str] = field(
        default_factory=lambda: [
            "openai/gpt-oss-120b",
            "openai/gpt-oss-20b",
            "qwen/qwen3.8-27b",
        ]
    )
    # Groq leads on volume: 1,000 requests/day and ~0.4s, versus Gemini's
    # measured 20/day/model. Gemini is still required for anything visual -
    # it is the only free provider that can see - and the router routes
    # needs_vision past providers that cannot.
    provider_order: list[str] = field(default_factory=lambda: ["groq", "gemini"])

    # --- agent --------------------------------------------------------------
    max_tool_turns: int = 12
    temperature: float = 0.7
    max_output_tokens: int = 2048
    #: Per-request HTTP timeout. Some advertised models never answer; without
    #: this the app hangs rather than moving on.
    request_timeout_s: int = 30
    #: Gemini 3.x thinking budget, in tokens. 0 disables it.
    #: Thinking is charged against max_output_tokens, so a small budget plus
    #: thinking returns EMPTY text with finish_reason=MAX_TOKENS. It also cost
    #: ~115 tokens on a one-word reply in testing, which matters on a free tier.
    #: Raise this for genuinely hard multi-step reasoning.
    thinking_budget: int = 0

    # --- voice --------------------------------------------------------------
    voice_enabled: bool = True
    voice_gender: str = "female"
    voice_rate: str = "+4%"
    #: Index from voice.list_devices(). None uses the system default, which on
    #: this machine is a webcam microphone rather than the built-in array.
    mic_device: int | None = None
    #: Hindi and Nepali share the Devanagari script, so a reply in it is
    #: ambiguous. Word markers decide when they can; this breaks the tie.
    devanagari_default: str = "hi"
    #: Global push-to-talk combination. Not Ctrl+Space: that is IntelliSense in
    #: VS Code and the input-method switch on Windows, so it would fire constantly.
    hotkey: str = "ctrl+alt+space"
    voices: dict[str, dict[str, str]] = field(
        default_factory=lambda: json.loads(json.dumps(DEFAULT_VOICES))
    )

    # --- identity -----------------------------------------------------------
    #: Asked during first-run setup. Was hard-coded to the developer's name,
    #: which every other user would have been called.
    username: str = ""
    assistant_name: str = "Bantu"

    # --- setup --------------------------------------------------------------
    #: False until first-run setup completes. Keys found in .env do not count:
    #: setup moves them into the credential store.
    onboarded: bool = False

    # ------------------------------------------------------------------------
    @property
    def display_name(self) -> str:
        return self.username.strip() or "the user"

    def voice_for(self, lang: str, gender: str | None = None) -> str:
        """Voice id for a reply language. Falls back to English for unknown languages."""
        table = self.voices.get(lang) or self.voices["en"]
        g = gender or self.voice_gender
        return table.get(g) or next(iter(table.values()))

    # --- persistence --------------------------------------------------------
    @property
    def path(self) -> Path:
        return user_data_dir() / "config.json"

    @classmethod
    def load(cls) -> "Settings":
        p = user_data_dir() / "config.json"
        if not p.exists():
            return cls()
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt config must not stop the app booting.
            return cls()
        known = {f for f in cls().__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self) -> None:
        p = self.path
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(p)  # atomic, so a crash mid-write cannot corrupt the config


# --- secrets ---------------------------------------------------------------


def get_key(name: str) -> str | None:
    """Read an API key: credential store first, then env/.env as a dev fallback."""
    try:
        import keyring

        v = keyring.get_password(SERVICE, name)
        if v:
            return v
    except Exception:
        pass  # no backend available (headless CI, etc.) — fall through to env

    env = os.environ.get(KEY_NAMES.get(name, name).upper()) or os.environ.get(
        KEY_NAMES.get(name, name)
    )
    if env:
        return env

    try:
        from dotenv import dotenv_values

        vals = dotenv_values(".env")
        for candidate in (KEY_NAMES.get(name, name), name):
            if vals.get(candidate):
                return vals[candidate]
    except Exception:
        pass
    return None


def set_key(name: str, value: str) -> None:
    import keyring

    keyring.set_password(SERVICE, name, value)


def delete_key(name: str) -> None:
    try:
        import keyring

        keyring.delete_password(SERVICE, name)
    except Exception:
        pass


def key_source(name: str) -> str | None:
    """Where a key currently comes from: 'keyring', 'environment', '.env', or None."""
    try:
        import keyring

        if keyring.get_password(SERVICE, name):
            return "keyring"
    except Exception:
        pass
    env_name = KEY_NAMES.get(name, name)
    if os.environ.get(env_name) or os.environ.get(env_name.upper()):
        return "environment"
    try:
        from dotenv import dotenv_values

        vals = dotenv_values(".env")
        if vals.get(env_name) or vals.get(name):
            return ".env"
    except Exception:
        pass
    return None


def needs_onboarding(settings: "Settings") -> bool:
    """First-run setup is needed until it has been completed with a working key."""
    return not settings.onboarded or not configured_providers()


def configured_providers() -> list[str]:
    """Which providers currently have a usable key."""
    return [n for n in KEY_NAMES if get_key(n)]
