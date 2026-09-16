"""Gemini adapter — the primary brain.

Uses the stateless `models.generate_content` with full local history rather than
the stateful `interactions` API: server-side state cannot survive a failover to
Groq mid-conversation, and local history is what a future non-desktop client
would need anyway.

Free tier is multimodal, so vision costs nothing beyond the request itself.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from .base import (
    AuthError,
    Image,
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
    """Map an SDK exception onto our taxonomy so the router can decide."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    text = str(exc)
    if code is None:
        for c in (429, 401, 403, 500, 502, 503, 504):
            if str(c) in text:
                code = c
                break
    if code == 429 or "RESOURCE_EXHAUSTED" in text or "quota" in text.lower():
        return RateLimited(f"gemini quota/rate limit: {text}")
    if code in (401, 403) or "API key" in text or "PERMISSION_DENIED" in text:
        return AuthError(f"gemini auth: {text}")
    if code in (500, 502, 503, 504) or "UNAVAILABLE" in text:
        return TransientError(f"gemini transient: {text}")
    return ProviderError(f"gemini: {text}")


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: str, model_preferences: list[str] | None = None):
        from google import genai

        self._genai = genai
        self._client = genai.Client(api_key=api_key)
        self._models_cache: list[str] | None = None
        self._model: str | None = None
        self._prefs = model_preferences or []

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

    def resolve_model(self, preferences: list[str]) -> str:
        if self._model:
            return self._model
        prefs = preferences or self._prefs
        try:
            have = set(self.available_models())
        except ProviderError:
            have = set()  # listing failed; trust the first preference and let the call fail loudly
        for p in prefs:
            if not have or p in have:
                self._model = p
                return p
        flash = [m for m in have if "flash" in m and "image" not in m and "tts" not in m]
        if flash:
            self._model = sorted(flash)[0]
            return self._model
        raise ProviderError(f"no usable gemini model; account offers {sorted(have)[:10]}")

    def supports_vision(self) -> bool:
        return True

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
            parts.append(types.Part.from_function_call(name=tc.name, args=tc.arguments))
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
        model = self.resolve_model(self._prefs)

        cfg: dict[str, Any] = {
            "temperature": temperature,
            "max_output_tokens": max_output_tokens,
        }
        if system:
            cfg["system_instruction"] = system
        if tools:
            cfg["tools"] = self._to_tools(tools)
            # We drive the loop ourselves; the SDK must not call functions for us.
            cfg["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(
                disable=True
            )

        try:
            resp = self._client.models.generate_content(
                model=model,
                contents=self._to_contents(messages),
                config=types.GenerateContentConfig(**cfg),
            )
        except Exception as e:
            raise _classify(e) from e

        calls = []
        for fc in (resp.function_calls or []):
            args = fc.args
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            calls.append(
                ToolCall(
                    id=getattr(fc, "id", None) or f"call_{uuid.uuid4().hex[:8]}",
                    name=fc.name,
                    arguments=dict(args or {}),
                )
            )

        # .text raises rather than returning None when the turn is tool-calls only.
        try:
            text = resp.text or ""
        except Exception:
            text = ""

        um = getattr(resp, "usage_metadata", None)
        usage = Usage(
            input_tokens=getattr(um, "prompt_token_count", 0) or 0,
            output_tokens=getattr(um, "candidates_token_count", 0) or 0,
        )
        return LLMResponse(
            text=text, tool_calls=calls, provider=self.name, model=model, usage=usage
        )
