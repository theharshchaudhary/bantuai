"""Groq adapter — the primary brain.

Free-tier limits are per model: each of gpt-oss-120b, gpt-oss-20b and
qwen3.8-27b gets its own 8K tokens/minute and 200K tokens/day. A model at its
limit rests for as long as Groq says and the next one takes the request, so one
exhausted model does not bench the whole provider.

Do not reach for Llama ids here. Groq moved the whole Llama family to Enterprise
tier; the free tier is gpt-oss, Compound, Qwen and Whisper.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any

from .base import (
    AuthError,
    ModelUnavailable,
    LLMProvider,
    ToolNotLoaded,
    LLMResponse,
    Message,
    ProviderError,
    RateLimited,
    ToolCall,
    ToolSpec,
    TransientError,
    Usage,
)

log = logging.getLogger("bantu.groq")

#: Rest for a limited model when Groq does not say how long.
_DEFAULT_REST_S = 60.0


def _classify(exc: Exception) -> ProviderError:
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    text = str(exc)
    if code == 429 or "rate_limit" in text or "429" in text:
        retry = None
        resp = getattr(exc, "response", None)
        if resp is not None:
            try:
                retry = float(resp.headers.get("retry-after", "") or 0) or None
            except (ValueError, AttributeError):
                pass
        return RateLimited(f"groq rate limit: {text}", retry_after=retry)
    if code == 404 or "does not exist" in text or "model_not_found" in text:
        return ModelUnavailable(f"groq model unavailable: {text}")
    # Groq validates tool calls server-side and rejects a call to a tool that was
    # not in the request, naming it. Verified 2026-09-17.
    unloaded = re.search(r"attempted to call tool '([^']+)' which was not in request\.tools", text)
    if unloaded:
        return ToolNotLoaded(f"groq: {unloaded.group(1)} was not loaded", unloaded.group(1))
    if code in (401, 403) or "invalid_api_key" in text:
        return AuthError(f"groq auth: {text}")
    if code in (500, 502, 503, 504):
        return TransientError(f"groq transient: {text}")
    return ProviderError(f"groq: {text}")


class GroqProvider(LLMProvider):
    name = "groq"

    def __init__(
        self,
        api_key: str,
        model_preferences: list[str] | None = None,
        timeout_s: int = 30,
    ):
        from groq import Groq

        # No SDK retries. Its default of 2 honoured retry-after silently, so a
        # per-minute limit looked like a model taking 28s to answer.
        self._client = Groq(api_key=api_key, timeout=float(timeout_s), max_retries=0)
        self._models_cache: list[str] | None = None
        self._prefs = model_preferences or []
        #: model id -> monotonic time it may be asked again.
        self._resting: dict[str, float] = {}
        #: Ids that do not exist for this key. Never retried this session.
        self._dead: set[str] = set()

    # --- models -------------------------------------------------------------

    def available_models(self) -> list[str]:
        if self._models_cache is None:
            try:
                self._models_cache = [m.id for m in self._client.models.list().data]
            except Exception as e:
                raise _classify(e) from e
        return self._models_cache

    def resolve_model(self, preferences: list[str]) -> str:
        """The best model this key offers, ignoring any that are resting."""
        return self._candidates(preferences)[0]

    def _candidates(self, preferences: list[str] | None = None) -> list[str]:
        """Every usable model, best first."""
        prefs = [p for p in (preferences or self._prefs) if p not in self._dead]
        try:
            have = set(self.available_models())
        except ProviderError:
            have = set()  # listing failed; trust the preferences and let the call speak
        offered = [p for p in prefs if not have or p in have]
        if offered:
            return offered
        # Nothing preferred is offered: any text model, excluding audio and guard models.
        cands = sorted(
            m
            for m in have
            if m not in self._dead and not any(x in m for x in ("whisper", "tts", "guard", "prompt-guard"))
        )
        if cands:
            return cands
        raise ProviderError(f"no usable groq model; account offers {sorted(have)[:10]}")

    def _rest(self, model: str, seconds: float) -> None:
        self._resting[model] = time.monotonic() + seconds

    def _all_resting(self, models: list[str]) -> RateLimited:
        wait = max(1.0, min(self._resting.get(m, 0.0) for m in models) - time.monotonic())
        return RateLimited(
            f"groq: every model is at its free limit; the next frees up in {wait:.0f}s",
            retry_after=wait,
        )

    # --- conversion ---------------------------------------------------------

    def _to_messages(self, messages: list[Message], system: str | None) -> list[dict]:
        out: list[dict] = []
        if system:
            out.append({"role": "system", "content": system})
        for m in messages:
            if m.role == "tool":
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": m.tool_call_id or "",
                        "content": m.content or "",
                    }
                )
            elif m.role == "assistant":
                d: dict[str, Any] = {"role": "assistant", "content": m.content or ""}
                if m.tool_calls:
                    d["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for tc in m.tool_calls
                    ]
                out.append(d)
            else:
                # Groq's free text models are not multimodal; images are dropped
                # with a note so the model knows something was withheld.
                content = m.content or ""
                if m.images:
                    content += "\n[an image was attached but this provider cannot see images]"
                out.append({"role": "user", "content": content})
        return out

    @staticmethod
    def _to_tools(tools: list[ToolSpec]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
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
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "messages": self._to_messages(messages, system),
            "temperature": temperature,
            "max_tokens": max_output_tokens,
        }
        if tools:
            kwargs["tools"] = self._to_tools(tools)
            kwargs["tool_choice"] = "auto"

        models = self._candidates()
        now = time.monotonic()
        ready = [m for m in models if self._resting.get(m, 0.0) <= now]
        if not ready:
            raise self._all_resting(models)

        resp, model, last = None, "", None
        for model in ready:
            try:
                resp = self._client.chat.completions.create(model=model, **kwargs)
                break
            except Exception as e:
                err = _classify(e)
                if isinstance(err, RateLimited):
                    rest = err.retry_after or _DEFAULT_REST_S
                    self._rest(model, rest)
                    log.warning("groq: %s is at its free limit for %.0fs; trying the next model", model, rest)
                elif isinstance(err, ModelUnavailable):
                    self._dead.add(model)
                    log.warning("groq: dropping model %s (unavailable)", model)
                elif isinstance(err, TransientError):
                    log.info("groq: %s failed transiently; trying the next model", model)
                else:
                    raise err from e  # a problem with the request, which no other model would fix
                last = err

        if resp is None:
            if all(self._resting.get(m, 0.0) > time.monotonic() for m in models):
                raise self._all_resting(models)
            raise last or ProviderError("groq: no model answered")

        choice = resp.choices[0].message
        calls = []
        for tc in (getattr(choice, "tool_calls", None) or []):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(
                ToolCall(
                    id=tc.id or f"call_{uuid.uuid4().hex[:8]}",
                    name=tc.function.name,
                    arguments=args,
                )
            )

        u = getattr(resp, "usage", None)
        usage = Usage(
            input_tokens=getattr(u, "prompt_tokens", 0) or 0,
            output_tokens=getattr(u, "completion_tokens", 0) or 0,
        )
        return LLMResponse(
            text=choice.content or "",
            tool_calls=calls,
            provider=self.name,
            model=model,
            usage=usage,
        )
