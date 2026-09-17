"""Provider-neutral message, tool and response types.

Every adapter converts to and from these, so the agent loop never sees a
vendor's wire format. Conversation history is held here rather than
server-side — that is what lets the router fail over to a different provider
mid-conversation without losing context.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["user", "assistant", "tool"]


# --- errors ----------------------------------------------------------------


class ProviderError(Exception):
    """Base for anything a provider can fail with."""


class AuthError(ProviderError):
    """Key missing, invalid or revoked. Not worth retrying."""


class RateLimited(ProviderError):
    """Quota or rate limit hit. Try a different provider."""

    def __init__(self, msg: str, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


class TransientError(ProviderError):
    """Network blip, 5xx, timeout. Worth trying elsewhere."""


class ModelUnavailable(ProviderError):
    """This model id is gone or not callable on this account.

    Distinct from RateLimited: the provider itself is fine, so the adapter
    should drop to its next preferred model rather than failing over.
    Providers list models they will then 404 on, so this is routine.
    """


class RequestTooLarge(ProviderError):
    """This request is bigger than the provider accepts at all.

    Groq's free tier refuses any single request over its 8,000 tokens/minute
    with a 413 whose body also says "rate_limit_exceeded" and which carries a
    retry-after. Waiting never helps, so this is not a RateLimited: the other
    Groq models share the same cap, and the router moves on at once.
    """


class ToolNotLoaded(ProviderError):
    """The model called a tool that exists but was not sent with this request.

    Not a provider failure: every provider would reject the same request, so
    the router must not fail over. The agent loads the tool's group and retries.
    """

    def __init__(self, msg: str, tool_name: str):
        super().__init__(msg)
        self.tool_name = tool_name


class AllProvidersFailed(ProviderError):
    def __init__(self, failures: dict[str, Exception]):
        self.failures = failures
        detail = "; ".join(f"{k}: {type(v).__name__}: {v}" for k, v in failures.items())
        super().__init__(f"every provider failed - {detail}")


# --- data ------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """A tool offered to the model. `parameters` is a JSON Schema object."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    #: Opaque per-provider data that must be echoed back when this call is
    #: replayed in history. Gemini 3.x rejects a conversation whose function
    #: call parts lost their `thought_signature`; other providers ignore this.
    #: Excluded from equality so the dataclass stays hashable and comparable.
    meta: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class Image:
    """Raw image bytes for vision. Gemini accepts these directly."""

    data: bytes
    mime_type: str = "image/png"


@dataclass
class Message:
    role: Role
    content: str | None = None
    images: list[Image] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    # role="tool" only:
    tool_call_id: str | None = None
    name: str | None = None

    @classmethod
    def user(cls, text: str, images: list[Image] | None = None) -> "Message":
        return cls(role="user", content=text, images=images or [])

    @classmethod
    def assistant(cls, text: str | None = None, tool_calls: list[ToolCall] | None = None) -> "Message":
        return cls(role="assistant", content=text, tool_calls=tool_calls or [])

    @classmethod
    def tool_result(cls, call: ToolCall, result: str) -> "Message":
        return cls(role="tool", content=result, tool_call_id=call.id, name=call.name)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    usage: Usage = field(default_factory=Usage)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


# --- interface -------------------------------------------------------------


class LLMProvider(ABC):
    name: str = "base"

    @abstractmethod
    def available_models(self) -> list[str]:
        """Model ids this key can actually call, newest-relevant first."""

    @abstractmethod
    def resolve_model(self, preferences: list[str]) -> str:
        """First preference the account genuinely offers.

        Never hardcode an id at the call site: providers retire models and move
        them between tiers without notice.
        """

    @abstractmethod
    def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        system: str | None = None,
        *,
        temperature: float = 0.7,
        max_output_tokens: int = 2048,
    ) -> LLMResponse:
        """One turn. Raises a ProviderError subclass on failure."""

    def supports_vision(self) -> bool:
        return False
