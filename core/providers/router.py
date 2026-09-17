"""Quota-aware failover across providers.

Tries providers in order, stepping to the next on a rate limit or transient
failure and remembering a cooldown so an exhausted provider is skipped rather
than re-hit on every turn. Auth failures disable a provider for the session —
a bad key will not fix itself.

Failing over mid-conversation only works because history is held locally.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

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
    _states: list[_State] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        if not self.providers:
            raise ValueError("router needs at least one provider")
        self._states = [_State(p) for p in self.providers]

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
    ) -> LLMResponse:
        failures: dict[str, Exception] = {}

        for s in self._states:
            if not s.ready:
                continue
            if needs_vision and not s.provider.supports_vision():
                failures[s.provider.name] = ProviderError("cannot see images")
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
