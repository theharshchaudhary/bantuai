"""The agent loop.

One loop serves every request. There is no intent routing: the model reads the
tool list and decides. A new ability is a new function, not a new code path.

Portable — knows nothing about Windows, PyQt, or where tools come from.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .memory import Memory
from .providers.base import (
    AllProvidersFailed,
    Image,
    LLMResponse,
    Message,
    ProviderError,
    ToolCall,
    ToolNotLoaded,
)
from .providers.router import ProviderRouter
from .tools.registry import ConfirmFn, Tier, Tool, ToolRegistry

log = logging.getLogger("bantu.agent")

SYSTEM_TEMPLATE = """You are {assistant}, {user}'s desktop assistant on Windows.

You can act on this machine through the tools you have been given. Prefer doing
the thing over describing how to do it. Chain several tools when a request needs
it, and check your work — if a tool returns something unexpected, investigate
rather than assuming it worked.

Reply in whatever language {user} used. You speak English, Hindi and Nepali.

Be concise. No preamble, no restating the question, no offers of further help
unless they are genuinely useful. When a tool fails, say plainly what failed and
what you tried instead.

Never claim to have done something you did not do. If a tool was declined or
errored, say so.
{facts}"""


# --- events -----------------------------------------------------------------


@dataclass
class Event:
    """Something worth showing in the UI as the agent works."""

    kind: str  # thinking | tool_start | tool_end | text | error | declined
    text: str = ""
    tool: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    result: str = ""


EventFn = Callable[[Event], None]


@dataclass
class AgentResult:
    text: str
    turns: int
    tool_calls: int
    provider: str = ""
    model: str = ""
    stopped_early: bool = False


# --- agent ------------------------------------------------------------------


@dataclass
class Agent:
    router: ProviderRouter
    registry: ToolRegistry
    memory: Memory
    settings: Any
    on_event: EventFn | None = None
    confirm: ConfirmFn | None = None

    def _emit(self, kind: str, **kw: Any) -> None:
        if self.on_event:
            try:
                self.on_event(Event(kind=kind, **kw))
            except Exception:  # a broken UI callback must not kill the task
                log.exception("event handler raised")

    @property
    def _user(self) -> str:
        """The user's name, or a neutral stand-in when setup has not asked yet."""
        return (getattr(self.settings, "username", "") or "").strip() or "the user"

    def _system(self) -> str:
        facts = self.memory.all_facts(limit=40)
        block = ""
        if facts:
            joined = "\n".join(f"- {f}" for f in facts)
            block = f"\n\nThings you already know about {self._user}:\n{joined}"
        prompt = SYSTEM_TEMPLATE.format(
            assistant=self.settings.assistant_name,
            user=self._user,
            facts=block,
        )
        if getattr(self.registry, "lazy", False):
            prompt += (
                "\n\nYou start with only a few tools. When a task needs more - files, apps, "
                "the web, the screen - call load_tools first, loading every group you need at once."
            )
        return prompt

    def run(self, user_text: str, images: Iterable[Image] | None = None) -> AgentResult:
        """Handle one user request start to finish."""
        images = list(images or [])
        self.registry.new_task()  # per-task approvals never leak between requests
        self.memory.append(Message.user(user_text, images))

        system = self._system()
        max_turns = int(getattr(self.settings, "max_tool_turns", 12))
        budget = int(getattr(self.settings, "max_output_tokens", 2048))
        temp = float(getattr(self.settings, "temperature", 0.7))

        total_calls = 0
        last: LLMResponse | None = None
        recoveries = 0

        for turn in range(max_turns):
            # Recomputed every turn: load_tools changes what is available mid-task.
            specs = self.registry.specs()
            history = self.memory.history()
            # Memory stores no image bytes, so re-attach this run's images to the
            # user turn they belong to. Without this the model is handed a
            # question about a picture it was never shown.
            if images:
                for m in reversed(history):
                    if m.role == "user":
                        m.images = images
                        break
            needs_vision = any(m.images for m in history)
            self._emit("thinking")

            try:
                resp = self.router.chat(
                    history,
                    tools=specs,
                    system=system,
                    temperature=temp,
                    max_output_tokens=budget,
                    needs_vision=needs_vision,
                )
            except ToolNotLoaded as e:
                tool = self.registry.tools.get(e.tool_name)
                if tool is not None and recoveries < 2:
                    recoveries += 1
                    self.registry.load([tool.category])
                    log.info("model called unloaded %s; loaded %s and retrying", e.tool_name, tool.category)
                    continue
                self._emit("error", text=str(e))
                return AgentResult(f"Something went wrong: {e}", turn, total_calls, stopped_early=True)
            except AllProvidersFailed as e:
                msg = self._explain(e)
                self._emit("error", text=msg)
                return AgentResult(msg, turn, total_calls, stopped_early=True)
            except ProviderError as e:
                self._emit("error", text=str(e))
                return AgentResult(f"Something went wrong: {e}", turn, total_calls, stopped_early=True)

            last = resp
            self.memory.append(Message.assistant(resp.text or None, resp.tool_calls))

            if not resp.wants_tools:
                text = (resp.text or "").strip() or "(no reply)"
                self._emit("text", text=text)
                return AgentResult(text, turn + 1, total_calls, resp.provider, resp.model)

            for call in resp.tool_calls:
                total_calls += 1
                self._run_one(call)

        # Ran out of turns. Say so rather than pretending the task finished.
        note = (
            f"I stopped after {max_turns} tool steps without finishing. "
            f"Here is where I got to: {(last.text or '').strip() if last else '(nothing)'}"
        )
        self._emit("error", text=note)
        return AgentResult(
            note,
            max_turns,
            total_calls,
            last.provider if last else "",
            last.model if last else "",
            stopped_early=True,
        )

    def _run_one(self, call: ToolCall) -> None:
        tool = self.registry.tools.get(call.name)
        self._emit("tool_start", tool=call.name, arguments=call.arguments)
        result = self.registry.execute(call, confirm=self._confirm_for(tool))
        self._emit("tool_end", tool=call.name, arguments=call.arguments, result=result)
        self.memory.append(Message.tool_result(call, result))

    def _confirm_for(self, tool: Tool | None) -> ConfirmFn | None:
        if tool is None or tool.tier is not Tier.CONFIRM:
            return None

        def ask(t: Tool, args: dict[str, Any]) -> bool:
            if self.confirm is None:
                return False  # no way to ask means no permission, never assume yes
            allowed = self.confirm(t, args)
            if not allowed:
                self._emit("declined", tool=t.name, arguments=args)
            return allowed

        return ask

    @staticmethod
    def _explain(e: AllProvidersFailed) -> str:
        """Turn a provider pile-up into something a person can act on."""
        kinds = {type(v).__name__ for v in e.failures.values()}
        if kinds == {"RateLimited"}:
            return (
                "Every provider is out of quota right now. Gemini's free tier is only "
                "20 requests per day per model, so it runs out quickly; Groq resets sooner. "
                "Try again shortly, or add another key in Settings."
            )
        if "AuthError" in kinds:
            return "An API key was rejected. Check the keys in Settings."
        return f"No provider could answer: {e}"
