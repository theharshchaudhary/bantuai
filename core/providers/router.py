"""Quota-aware failover across providers.

Tries providers in order, stepping to the next on a rate limit or transient
failure and remembering a cooldown so an exhausted provider is skipped rather
than re-hit on every turn. Auth failures disable a provider for the session —
a bad key will not fix itself.

A provider that will be free again within `max_wait_s` is waited for instead of
skipped. Order is preference: Groq first because it has volume, Gemini second
because its small daily quota is also the only free vision. Skipping Groq over a
15-second per-minute limit would spend that vision budget on plain text.

Failing over mid-conversation only works because history is held locally.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .base import (
    AllProvidersFailed,
    AuthError,
    LLMProvider,
    LLMResponse,
    Message,
    ProviderError,
    RateLimited,
    ToolNotLoaded,
    ToolSpec,
    TransientError,
)

log = logging.getLogger("bantu.router")

#: How long to skip a provider after a 429 that carries no Retry-After.
DEFAULT_COOLDOWN = 60.0
#: A provider whose daily quota is gone should not be retried every minute.
LONG_COOLDOWN = 900.0
#: A preferred provider this close to free is waited for rather than skipped.
MAX_WAIT_S = 20.0
#: Waits per request, so a provider that keeps asking for more cannot stall forever.
MAX_WAITS = 2
#: Added to every wait. Windows timers wake a few ms early, which left the provider
#: still resting and cost a second, near-zero wait out of MAX_WAITS.
WAIT_SLACK_S = 0.05

#: Told (provider name, seconds) before the router sleeps.
WaitFn = Callable[[str, float], None]


class _Wait(Exception):
    def __init__(self, provider: str, seconds: float):
        self.provider, self.seconds = provider, seconds


@dataclass
class _State:
    provider: LLMProvider
    blocked_until: float = 0.0
    disabled: bool = False
    calls: int = 0
    failures: int = 0

    @property
    def ready(self) -> bool:
        return not self.disabled and time.monotonic() >= self.blocked_until


@dataclass
class ProviderRouter:
    providers: list[LLMProvider]
    max_wait_s: float = MAX_WAIT_S
    _states: list[_State] = field(init=False, default_factory=list)
    _interrupted: threading.Event = field(init=False, default_factory=threading.Event, repr=False)

    def __post_init__(self) -> None:
        if not self.providers:
            raise ValueError("router needs at least one provider")
        self._states = [_State(p) for p in self.providers]

    def replace(self, providers: list[LLMProvider]) -> None:
        """Swap in new providers in place, e.g. after keys change in Settings.

        In place, not a new router: tools such as look_at_screen captured this
        object when they were registered, and must see the new keys too.
        """
        if not providers:
            raise ValueError("router needs at least one provider")
        self.providers = list(providers)
        self._states = [_State(p) for p in self.providers]

    def interrupt(self) -> None:
        """End any wait now and refuse to start new ones: the app is quitting."""
        self._interrupted.set()

    # --- introspection ------------------------------------------------------

    @property
    def active(self) -> str | None:
        for s in self._states:
            if s.ready:
                return s.provider.name
        return None

    def status(self) -> list[dict]:
        now = time.monotonic()
        return [
            {
                "provider": s.provider.name,
                "ready": s.ready,
                "disabled": s.disabled,
                "cooldown_s": max(0, round(s.blocked_until - now)),
                "calls": s.calls,
                "failures": s.failures,
            }
            for s in self._states
        ]

    # --- call ---------------------------------------------------------------

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        system: str | None = None,
        *,
        temperature: float = 0.7,
        max_output_tokens: int = 2048,
        needs_vision: bool = False,
        on_wait: WaitFn | None = None,
    ) -> LLMResponse:
        waits = 0
        while True:
            try:
                return self._attempt(
                    messages, tools, system, temperature, max_output_tokens, needs_vision,
                    may_wait=waits < MAX_WAITS,
                )
            except _Wait as w:
                waits += 1
                log.info("waiting %.0fs for %s rather than failing over", w.seconds, w.provider)
                if on_wait:
                    on_wait(w.provider, w.seconds)
                if self._interrupted.wait(w.seconds + WAIT_SLACK_S):
                    raise ProviderError("stopped while waiting for a free limit") from None

    def _attempt(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None,
        system: str | None,
        temperature: float,
        max_output_tokens: int,
        needs_vision: bool,
        may_wait: bool,
    ) -> LLMResponse:
        failures: dict[str, Exception] = {}

        for s in self._states:
            if needs_vision and not s.provider.supports_vision():
                failures[s.provider.name] = ProviderError("cannot see images")
                continue
            if not s.ready:
                wait = s.blocked_until - time.monotonic()
                if may_wait and not s.disabled and 0 < wait <= self.max_wait_s:
                    raise _Wait(s.provider.name, wait)
                continue

            try:
                s.calls += 1
                resp = s.provider.chat(
                    messages,
                    tools,
                    system,
                    temperature=temperature,
                    max_output_tokens=max_output_tokens,
                )
                return resp

            except ToolNotLoaded:
                # A request-shape problem, not an outage: trying Gemini would
                # only spend the scarce vision budget on the same rejection.
                raise

            except RateLimited as e:
                s.failures += 1
                cd = e.retry_after or DEFAULT_COOLDOWN
                if "quota" in str(e).lower() or "daily" in str(e).lower():
                    cd = max(cd, LONG_COOLDOWN)
                s.blocked_until = time.monotonic() + cd
                failures[s.provider.name] = e
                log.warning("%s rate limited, cooling down %.0fs", s.provider.name, cd)
                if may_wait and cd <= self.max_wait_s:
                    raise _Wait(s.provider.name, cd) from None

            except AuthError as e:
                s.failures += 1
                s.disabled = True
                failures[s.provider.name] = e
                log.error("%s disabled for this session: %s", s.provider.name, e)

            except TransientError as e:
                s.failures += 1
                s.blocked_until = time.monotonic() + 5.0
                failures[s.provider.name] = e
                log.warning("%s transient failure: %s", s.provider.name, e)

            except ProviderError as e:
                s.failures += 1
                failures[s.provider.name] = e
                log.warning("%s failed: %s", s.provider.name, e)

        for s in self._states:
            if s.provider.name not in failures and not s.ready:
                failures[s.provider.name] = RateLimited(
                    f"cooling down {max(0, round(s.blocked_until - time.monotonic()))}s"
                )
        raise AllProvidersFailed(failures)


def build_providers(settings=None) -> list[LLMProvider]:
    """Providers for every stored key, in the configured order."""
    return build_router(settings).providers


def build_router(settings=None) -> ProviderRouter:
    """Assemble a router from stored settings and whichever keys are present."""
    from .. import config as cfg
    from .gemini import GeminiProvider
    from .groq import GroqProvider

    settings = settings or cfg.Settings.load()
    t = getattr(settings, "request_timeout_s", 30)
    builders = {
        "gemini": lambda k: GeminiProvider(
            k,
            settings.gemini_models,
            timeout_s=t,
            thinking_budget=getattr(settings, "thinking_budget", 0),
        ),
        "groq": lambda k: GroqProvider(k, settings.groq_models, timeout_s=t),
    }

    providers: list[LLMProvider] = []
    for name in settings.provider_order:
        key = cfg.get_key(name)
        if not key:
            log.info("no key for %s, skipping", name)
            continue
        try:
            providers.append(builders[name](key))
        except Exception as e:
            log.error("could not build %s: %s", name, e)

    if not providers:
        raise ProviderError(
            "No API keys configured. Add a Gemini key (free at aistudio.google.com) "
            "or a Groq key (free at console.groq.com), then restart."
        )
    return ProviderRouter(providers)
