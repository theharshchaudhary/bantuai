"""Groq adapter — the failover brain.

Not the primary: the free tier caps at 8K tokens/minute, and an agent request
carrying the full tool schema set plus history can approach 4K on its own, so
two calls in a minute hit the wall.

Do not reach for Llama ids here. Groq moved the whole Llama family to Enterprise
tier; the free tier is gpt-oss, Compound, Qwen and Whisper.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from .base import (
    AuthError,
    LLMProvider,
    LLMResponse,
    Message,
    ProviderError,
    RateLimited,
    ToolCall,
    ToolSpec,
    TransientError,
    Usage,
)


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
    if code in (401, 403) or "invalid_api_key" in text:
        return AuthError(f"groq auth: {text}")
    if code in (500, 502, 503, 504):
        return TransientError(f"groq transient: {text}")
    return ProviderError(f"groq: {text}")


class GroqProvider(LLMProvider):
    name = "groq"

    def __init__(self, api_key: str, model_preferences: list[str] | None = None):
        from groq import Groq

        self._client = Groq(api_key=api_key)
        self._models_cache: list[str] | None = None
        self._model: str | None = None
        self._prefs = model_preferences or []

    # --- models -------------------------------------------------------------

    def available_models(self) -> list[str]:
        if self._models_cache is None:
            try:
                self._models_cache = [m.id for m in self._client.models.list().data]
            except Exception as e:
                raise _classify(e) from e
        return self._models_cache

    def resolve_model(self, preferences: list[str]) -> str:
        if self._model:
            return self._model
        prefs = preferences or self._prefs
        try:
            have = set(self.available_models())
        except ProviderError:
            have = set()
        for p in prefs:
            if not have or p in have:
                self._model = p
                return p
        # Prefer a tool-capable text model; exclude audio and guard models.
        cands = [
            m
            for m in have
            if not any(x in m for x in ("whisper", "tts", "guard", "prompt-guard"))
        ]
        if cands:
            self._model = sorted(cands)[0]
            return self._model
        raise ProviderError(f"no usable groq model; account offers {sorted(have)[:10]}")

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
        model = self.resolve_model(self._prefs)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": self._to_messages(messages, system),
            "temperature": temperature,
            "max_tokens": max_output_tokens,
        }
        if tools:
            kwargs["tools"] = self._to_tools(tools)
            kwargs["tool_choice"] = "auto"

        try:
            resp = self._client.chat.completions.create(**kwargs)
        except Exception as e:
            raise _classify(e) from e

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
