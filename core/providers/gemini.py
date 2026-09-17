"""Gemini adapter — the primary brain.

Uses the stateless `models.generate_content` with full local history rather than
the stateful `interactions` API: server-side state cannot survive a failover to
Groq mid-conversation, and local history is what a future non-desktop client
would need anyway.

Free tier is multimodal, so vision costs nothing beyond the request itself.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
import warnings
from typing import Any

from .base import (
    AuthError,
    Image,
    LLMProvider,
    LLMResponse,
    Message,
    ModelUnavailable,
    ProviderError,
    RateLimited,
    ToolCall,
    ToolSpec,
    TransientError,
    Usage,
)

log = logging.getLogger("bantu.gemini")

#: A model that times out this many times in a session is treated as dead.
_TIMEOUT_STRIKES = 2

#: Google's documented stand-in for a function call with no signature of its own:
#: one Groq made before a failover, or one stored before signatures were kept.
#: Verified live 2026-09-17: a replayed call with no signature is rejected
#: ("Function call is missing a thought_signature"); with this it is accepted.
SIGNATURE_PLACEHOLDER = b"skip_thought_signature_validator"

#: The free quota is per model per day and resets at midnight Pacific; asking
#: an exhausted model again once an hour costs one refused request.
_DAILY_REST_S = 3600.0
_DEFAULT_REST_S = 60.0


def _rest_for(text: str) -> float:
    """How long a model that answered 429 should be left alone."""
    if "PerDay" in text:
        return _DAILY_REST_S
    delay = re.search(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s", text)
    return float(delay.group(1)) + 1 if delay else _DEFAULT_REST_S


def _rejects_thinking(exc: Exception) -> bool:
    """Did the model refuse the explicit thinking budget?

    Some say so. The lite models answer only "Request contains an invalid
    argument", verified live 2026-09-17, which is why the budget is retried
    without rather than trusted to be named in the error.
    """
    text = str(exc)
    return "thinking" in text.lower() or "Request contains an invalid argument" in text


def _classify(exc: Exception) -> ProviderError:
    """Map an SDK exception onto our taxonomy so the router can decide."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    text = str(exc)
    if code is None:
        for c in (404, 429, 401, 403, 500, 502, 503, 504):
            if str(c) in text:
                code = c
                break
    if code == 404 or "NOT_FOUND" in text or "no longer available" in text:
        return ModelUnavailable(f"gemini model unavailable: {text}")
    if code == 429 or "RESOURCE_EXHAUSTED" in text or "quota" in text.lower():
        return RateLimited(f"gemini quota/rate limit: {text}")
    if code in (401, 403) or "API key" in text or "PERMISSION_DENIED" in text:
        return AuthError(f"gemini auth: {text}")
    if code in (500, 502, 503, 504) or "UNAVAILABLE" in text or "DEADLINE_EXCEEDED" in text:
        return TransientError(f"gemini transient: {text}")
    return ProviderError(f"gemini: {text}")


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(
        self,
        api_key: str,
        model_preferences: list[str] | None = None,
        timeout_s: int = 30,
        thinking_budget: int = 0,
    ):
        from google import genai

        self._genai = genai
        self._client = genai.Client(api_key=api_key)
        self._models_cache: list[str] | None = None
        self._prefs = model_preferences or []
        self._timeout_ms = max(1, timeout_s) * 1000
        self._thinking_budget = thinking_budget
        #: Models that reject an explicit thinking budget.
        self._no_thinking: set[str] = set()
        #: Ids that 404'd or timed out repeatedly. Never retried this session.
        self._dead: set[str] = set()
        self._strikes: dict[str, int] = {}
        #: model id -> monotonic time it may be asked again, after a 429.
        self._resting: dict[str, float] = {}

    # --- models -------------------------------------------------------------

    def available_models(self) -> list[str]:
        if self._models_cache is None:
            try:
                out = []
                for m in self._client.models.list():
                    actions = getattr(m, "supported_actions", None) or []
                    if actions and "generateContent" not in actions:
                        continue
                    out.append(str(m.name).removeprefix("models/"))
                self._models_cache = out
            except Exception as e:
                raise _classify(e) from e
        return self._models_cache

    def _is_resting(self, model: str) -> bool:
        return self._resting.get(model, 0.0) > time.monotonic()

    def resolve_model(self, preferences: list[str], exclude: set[str] | None = None) -> str:
        resting = {m for m in self._resting if self._is_resting(m)}
        skip = self._dead | resting | (exclude or set())
        prefs = [p for p in (preferences or self._prefs) if p not in skip]
        try:
            have = set(self.available_models())
        except ProviderError:
            have = set()  # listing failed; trust the preference and let the call speak
        for p in prefs:
            if not have or p in have:
                return p
        # Nothing preferred survived. Fall back to any listed flash model that
        # has not already proven dead — exclude the non-text variants.
        flash = [
            m
            for m in have
            if "flash" in m
            and m not in skip
            and not any(x in m for x in ("image", "tts", "audio", "embed", "live", "omni"))
        ]
        if flash:
            return sorted(flash)[-1]
        raise ProviderError(
            f"no usable gemini model left (dead this session: {sorted(self._dead)})"
        )

    def _demote(self, model: str, reason: str) -> None:
        """Stop using a model id for the rest of the session."""
        self._dead.add(model)
        log.warning("gemini: dropping model %s (%s)", model, reason)

    def supports_vision(self) -> bool:
        return True

    def _all_resting(self, models: list[str]) -> RateLimited:
        wait = max(1.0, min(self._resting.get(m, 0.0) for m in models) - time.monotonic())
        return RateLimited(
            f"gemini: every model is at its free limit; the next frees up in {wait:.0f}s",
            retry_after=wait,
        )

    def _generate(self, model: str, contents: list[Any], cfg: dict[str, Any]) -> Any:
        """One request to one model, with the thinking budget if it accepts one."""
        types = self._genai.types
        conf = dict(cfg)
        thinking = self._thinking_budget >= 0 and model not in self._no_thinking
        if thinking:
            conf["thinking_config"] = types.ThinkingConfig(thinking_budget=self._thinking_budget)
        try:
            return self._send(model, contents, conf)
        except Exception as e:
            if not thinking or not _rejects_thinking(e):
                raise
        # Retry once without it, and remember only if that is what fixed it:
        # the lite models' refusal is too generic to trust on its own.
        conf.pop("thinking_config")
        resp = self._send(model, contents, conf)
        self._no_thinking.add(model)
        log.info("gemini: %s rejects an explicit thinking budget; sending none", model)
        return resp

    def _send(self, model: str, contents: list[Any], conf: dict[str, Any]) -> Any:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=".*automatic function calling.*")
            return self._client.models.generate_content(
                model=model,
                contents=contents,
                config=self._genai.types.GenerateContentConfig(**conf),
            )

    # --- conversion ---------------------------------------------------------

    def _parts_for(self, m: Message) -> list[Any]:
        types = self._genai.types
        parts: list[Any] = []
        if m.role == "tool":
            # Gemini wants the result keyed under the function's name.
            return [
                types.Part.from_function_response(
                    name=m.name or "tool", response={"result": m.content or ""}
                )
            ]
        if m.content:
            parts.append(types.Part.from_text(text=m.content))
        for img in m.images:
            parts.append(types.Part.from_bytes(data=img.data, mime_type=img.mime_type))
        for tc in m.tool_calls:
            # Gemini 3.x rejects replayed function calls that lost their
            # thought signature, so echo back whatever came with the call.
            parts.append(
                types.Part(
                    function_call=types.FunctionCall(name=tc.name, args=tc.arguments),
                    thought_signature=tc.meta.get("thought_signature") or SIGNATURE_PLACEHOLDER,
                )
            )
        return parts

    def _to_contents(self, messages: list[Message]) -> list[Any]:
        types = self._genai.types
        out = []
        for m in messages:
            parts = self._parts_for(m)
            if not parts:
                continue
            # Gemini has no "tool" role; results come back as a user turn.
            role = "model" if m.role == "assistant" else "user"
            out.append(types.Content(role=role, parts=parts))
        return out

    def _to_tools(self, tools: list[ToolSpec]) -> list[Any]:
        types = self._genai.types
        decls = [
            types.FunctionDeclaration(
                name=t.name,
                description=t.description,
                parameters_json_schema=t.parameters,
            )
            for t in tools
        ]
        return [types.Tool(function_declarations=decls)]

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
        types = self._genai.types
        contents = self._to_contents(messages)

        cfg: dict[str, Any] = {
            "temperature": temperature,
            # Thinking is billed against this, so never leave it tiny.
            "max_output_tokens": max(256, max_output_tokens),
            # Without an explicit timeout a model that never answers hangs the
            # whole app. Several advertised ids do exactly that.
            "http_options": types.HttpOptions(timeout=self._timeout_ms),
        }
        if system:
            cfg["system_instruction"] = system
        if tools:
            cfg["tools"] = self._to_tools(tools)
            # We drive the loop ourselves; the SDK must not call functions for us.
            cfg["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(
                disable=True
            )

        # Walk down the preference list: an id can be advertised and still be
        # retired or permanently timing out, so one dead model must not look
        # like a dead provider.
        attempts = max(1, len([p for p in self._prefs if p not in self._dead])) + 1
        last: ProviderError | None = None
        resp = None
        model = ""
        tried: set[str] = set()

        for _ in range(attempts):
            live = [p for p in self._prefs if p not in self._dead]
            if live and all(self._is_resting(p) for p in live):
                # Say when one frees up, rather than trying ids nobody verified.
                raise self._all_resting(live)
            try:
                model = self.resolve_model(self._prefs, exclude=tried)
            except ProviderError as e:
                raise last or e from (last or e)
            tried.add(model)
            try:
                resp = self._generate(model, contents, cfg)
                self._strikes.pop(model, None)
                break
            except Exception as e:
                err = _classify(e)
                last = err
                if isinstance(err, ModelUnavailable):
                    self._demote(model, "404 / retired")
                    continue
                if isinstance(err, RateLimited):
                    # Free-tier quota is per model, so an exhausted id says
                    # nothing about the next one: rotating turns 20/day into
                    # roughly 20 x (number of usable models). A model rests
                    # rather than being dropped, because quotas come back.
                    rest = _rest_for(str(e))
                    self._resting[model] = time.monotonic() + rest
                    log.warning("gemini: %s is at its free limit, resting %.0fs", model, rest)
                    continue
                if isinstance(err, TransientError):
                    # 503/504 happen: a model spikes or stalls. Move to the next
                    # preference straight away rather than failing the turn, and
                    # retire one that keeps doing it.
                    n = self._strikes.get(model, 0) + 1
                    self._strikes[model] = n
                    if n >= _TIMEOUT_STRIKES:
                        self._demote(model, f"{n} transient failures")
                    else:
                        log.info("gemini: %s unavailable, trying next model", model)
                    continue
                raise err from e

        if resp is None:
            live = [p for p in self._prefs if p not in self._dead]
            if live and all(self._is_resting(p) for p in live):
                raise self._all_resting(live)
            raise last or ProviderError("gemini: no model answered")

        # Read the raw parts rather than resp.function_calls: the thought
        # signature sits on the Part, and dropping it breaks the next turn.
        calls = []
        raw_parts = []
        try:
            cand = (resp.candidates or [None])[0]
            raw_parts = (cand.content.parts if cand and cand.content else []) or []
        except (AttributeError, IndexError):
            raw_parts = []

        for part in raw_parts:
            fc = getattr(part, "function_call", None)
            if not fc:
                continue
            args = fc.args
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            sig = getattr(part, "thought_signature", None)
            calls.append(
                ToolCall(
                    id=getattr(fc, "id", None) or f"call_{uuid.uuid4().hex[:8]}",
                    name=fc.name,
                    arguments=dict(args or {}),
                    meta={"thought_signature": sig} if sig else {},
                )
            )

        # .text raises rather than returning None when the turn is tool-calls only.
        try:
            text = resp.text or ""
        except Exception:
            text = ""

        if not text and not calls:
            finish = ""
            try:
                finish = str(resp.candidates[0].finish_reason or "")
            except (AttributeError, IndexError):
                pass
            if "MAX_TOKENS" in finish:
                raise ProviderError(
                    "gemini returned no text: the output budget was consumed before "
                    "any reply (finish_reason=MAX_TOKENS). Raise max_output_tokens or "
                    "lower thinking_budget."
                )

        um = getattr(resp, "usage_metadata", None)
        usage = Usage(
            input_tokens=getattr(um, "prompt_token_count", 0) or 0,
            output_tokens=getattr(um, "candidates_token_count", 0) or 0,
        )
        return LLMResponse(
            text=text, tool_calls=calls, provider=self.name, model=model, usage=usage
        )
