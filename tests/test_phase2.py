"""Phase 2 tests — registry, memory, agent loop. No API key needed.

    .venv/Scripts/python.exe tests/test_phase2.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Literal, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.agent import Agent, Event
from core.memory import Memory, estimate_tokens
from core.providers.base import (
    AllProvidersFailed,
    LLMProvider,
    LLMResponse,
    Message,
    RateLimited,
    ToolCall,
)
from core.providers.router import ProviderRouter
from core.tools.registry import Tier, ToolError, ToolRegistry

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"  -> {detail}" if detail and not cond else ""))


# --- schema generation ------------------------------------------------------

print("\n[schema from type hints]")
reg = ToolRegistry()


@reg.register(tier=Tier.AUTO, category="test")
def sample(query: str, limit: int = 10, exact: bool = False,
           mode: Literal["fast", "deep"] = "fast", tags: Optional[list[str]] = None) -> str:
    """Search for things.

    Args:
        query: What to look for.
        limit: Maximum results.
        exact: Whether to match exactly.
        mode: How hard to search.
        tags: Optional tag filter.
    """
    return f"{query}|{limit}|{exact}|{mode}|{tags}"


t = reg.tools["sample"]
p = t.parameters["properties"]
check("description comes from the docstring summary", t.description == "Search for things.")
check("str maps to string", p["query"]["type"] == "string")
check("int maps to integer", p["limit"]["type"][0] == "integer", str(p["limit"]))
check("bool maps to boolean", p["exact"]["type"][0] == "boolean", str(p["exact"]))
check("Literal becomes an enum", p["mode"].get("enum") == ["fast", "deep"], str(p["mode"]))
check("Optional[list[str]] becomes an array", p["tags"]["type"][0] == "array", str(p["tags"]))
check("array item type preserved", p["tags"]["items"]["type"] == "string")
check("per-arg docs are attached", p["query"]["description"] == "What to look for.")
check("only args without defaults are required", t.parameters["required"] == ["query"],
      str(t.parameters.get("required")))

try:
    @reg.register(tier=Tier.AUTO)
    def undocumented(x: str) -> str:
        return x
    check("a tool with no docstring is refused", False, "it was accepted")
except ValueError:
    check("a tool with no docstring is refused", True)

try:
    reg.register(sample, name="sample")
    check("duplicate names are refused", False)
except ValueError:
    check("duplicate names are refused", True)


# --- execution & coercion ---------------------------------------------------

print("\n[execution]")
out = reg.execute(ToolCall("1", "sample", {"query": "hi", "limit": "25", "exact": "true"}))
check("string ints are coerced", "|25|" in out, out)
check("string bools are coerced", "|True|" in out, out)
check("invented arguments are dropped",
      "ok" in reg.execute(ToolCall("2", "sample", {"query": "ok", "nonsense": 1})), "")
check("missing required args give a readable error",
      reg.execute(ToolCall("3", "sample", {})).startswith("Error: bad arguments"),
      reg.execute(ToolCall("3", "sample", {})))
check("unknown tool names are explained",
      "no tool named" in reg.execute(ToolCall("4", "nope", {})))


@reg.register(tier=Tier.AUTO, category="test")
def explodes() -> str:
    """Always raises."""
    raise RuntimeError("boom")


check("a crashing tool returns an error instead of killing the agent",
      reg.execute(ToolCall("5", "explodes", {})).startswith("Error: explodes failed"))


@reg.register(tier=Tier.AUTO, category="test")
def refuses() -> str:
    """Always raises ToolError."""
    raise ToolError("cannot do that")


check("ToolError text reaches the model", reg.execute(ToolCall("6", "refuses", {})) == "Error: cannot do that")


# --- permission tiers -------------------------------------------------------

print("\n[permission tiers]")
ran = []


@reg.register(tier=Tier.CONFIRM, category="test")
def mutate(path: str) -> str:
    """Change something.

    Args:
        path: Target.
    """
    ran.append(path)
    return "changed"


@reg.register(tier=Tier.BLOCKED, category="test")
def forbidden() -> str:
    """Never allowed."""
    ran.append("forbidden")
    return "should never happen"


check("blocked tools refuse", reg.execute(ToolCall("7", "forbidden", {})).startswith("Error:"))
check("blocked tools never execute", "forbidden" not in ran)
check("blocked tools are not advertised to the model",
      "forbidden" not in [s.name for s in reg.specs()])
check("non-blocked tools are advertised", "mutate" in [s.name for s in reg.specs()])

check("confirm-tier refuses when nothing can ask",
      "needs confirmation" in reg.execute(ToolCall("8", "mutate", {"path": "x"})))
check("...and did not run", ran == [])

check("declining is reported back to the model",
      "said no" in reg.execute(ToolCall("9", "mutate", {"path": "x"}), confirm=lambda t, a: False))
check("...and still did not run", ran == [])

check("approving runs it",
      reg.execute(ToolCall("10", "mutate", {"path": "y"}), confirm=lambda t, a: True) == "changed")
check("...and it did run", ran == ["y"])

reg.allow_for_task("mutate")
check("allow-for-task skips the prompt",
      reg.execute(ToolCall("11", "mutate", {"path": "z"})) == "changed")
reg.new_task()
check("approvals do not leak into the next task",
      "needs confirmation" in reg.execute(ToolCall("12", "mutate", {"path": "w"})))

big = reg._stringify("x" * 9000)
check("huge results are truncated", len(big) < 7000 and "truncated" in big)


# --- memory -----------------------------------------------------------------

print("\n[memory]")
db = Path(tempfile.mkdtemp()) / "h.db"
mem = Memory(db)

mem.append(Message.user("hello there"))
mem.append(Message.assistant("hi"))
check("messages round trip", len(mem.history()) == 2)

tc = ToolCall("c1", "sample", {"query": "invoice"})
mem.append(Message.assistant(None, [tc]))
mem.append(Message.tool_result(tc, "7 files"))
h = mem.history()
check("tool calls survive storage", h[2].tool_calls[0].arguments == {"query": "invoice"})
check("tool results keep their call id", h[3].tool_call_id == "c1")

check("remember works", "Remembered" in mem.remember("Harsh prefers concise replies"))
check("duplicate facts are not stored twice", "Already knew" in mem.remember("Harsh prefers concise replies"))
mem.remember("Harsh is building BantuAI in Python")
check("recall finds a fact", any("concise" in f for f in mem.recall("concise replies")))
check("recall tolerates punctuation and quotes", mem.recall('what is "concise"?') != [])
check("recall of nothing is empty, not an error", mem.recall("zzzz") == [])
check("all_facts lists both", len(mem.all_facts()) == 2)
check("forget removes", "Forgotten" in mem.forget("Harsh prefers concise replies"))
check("forgetting twice is handled", "No such" in mem.forget("Harsh prefers concise replies"))

check("message search works", any("hello" in m for m in mem.search_messages("hello")))

conv = mem.conversation
mem.new_conversation()
check("a new conversation is isolated", mem.history() == [])
check("the old one is still listed", any(c["conversation"] == conv for c in mem.recent_conversations()))

mem.new_conversation()
for i in range(200):
    mem.append(Message.user("word " * 200))
trimmed = mem.history(max_tokens=2000)
check("history is trimmed to the token budget", estimate_tokens(trimmed) <= 2000,
      str(estimate_tokens(trimmed)))
check("trimming leaves something usable", len(trimmed) > 0)

mem.new_conversation()
mem.append(Message.user("q"))
mem.append(Message.assistant(None, [tc]))
mem.append(Message.tool_result(tc, "r"))
check("trimming never starts on an orphan tool result",
      mem.history(max_tokens=1)[0].role != "tool" if mem.history(max_tokens=1) else True)


# --- agent loop -------------------------------------------------------------

print("\n[agent loop]")


class Scripted(LLMProvider):
    """Replays a fixed list of responses so the loop can be tested offline."""

    name = "scripted"

    def __init__(self, script):
        self.script, self.seen = list(script), 0

    def available_models(self):
        return ["s"]

    def resolve_model(self, preferences):
        return "s"

    def supports_vision(self):
        return True

    def chat(self, messages, tools=None, system=None, **kw):
        self.seen += 1
        r = self.script.pop(0) if self.script else LLMResponse(text="done", provider=self.name)
        r.provider = self.name
        return r


class S:
    username, assistant_name = "Harsh", "Bantu"
    max_tool_turns, max_output_tokens, temperature = 5, 512, 0.7


def make_agent(script, confirm=None):
    m = Memory(Path(tempfile.mkdtemp()) / "a.db")
    r = ToolRegistry()
    calls = []

    @r.register(tier=Tier.AUTO, category="test")
    def peek(what: str) -> str:
        """Look something up.

        Args:
            what: Subject.
        """
        calls.append(what)
        return f"result for {what}"

    @r.register(tier=Tier.CONFIRM, category="test")
    def destroy(path: str) -> str:
        """Delete something.

        Args:
            path: Target.
        """
        calls.append("DESTROYED " + path)
        return "gone"

    events = []
    a = Agent(ProviderRouter([Scripted(script)]), r, m, S(),
              on_event=events.append, confirm=confirm)
    return a, calls, events


a, calls, events = make_agent([LLMResponse(text="Hello Harsh.")])
res = a.run("hi")
check("a plain reply comes straight back", res.text == "Hello Harsh.")
check("no tools were called", calls == [])
check("one turn was used", res.turns == 1)

a, calls, events = make_agent([
    LLMResponse(text="", tool_calls=[ToolCall("1", "peek", {"what": "disk"})]),
    LLMResponse(text="Your disk is fine."),
])
res = a.run("check my disk")
check("the loop runs a tool then answers", res.text == "Your disk is fine.")
check("the tool actually ran", calls == ["disk"])
check("tool call count is reported", res.tool_calls == 1)
check("tool_start and tool_end were emitted",
      [e.kind for e in events].count("tool_start") == 1 and
      [e.kind for e in events].count("tool_end") == 1)
check("the tool result is in history",
      any(m.role == "tool" and "result for disk" in (m.content or "") for m in a.memory.history()))

a, calls, events = make_agent([
    LLMResponse(text="", tool_calls=[ToolCall("1", "peek", {"what": "a"}),
                                     ToolCall("2", "peek", {"what": "b"})]),
    LLMResponse(text="both done"),
])
res = a.run("two things")
check("several tool calls in one turn all run", calls == ["a", "b"])
check("both were counted", res.tool_calls == 2)

a, calls, events = make_agent(
    [LLMResponse(text="", tool_calls=[ToolCall("1", "destroy", {"path": "/important"})]),
     LLMResponse(text="I did not delete it.")],
    confirm=lambda t, args: False,
)
res = a.run("delete everything")
check("a declined tool does not run", calls == [])
check("the model is told it was declined",
      any("said no" in (m.content or "") for m in a.memory.history() if m.role == "tool"))
check("a declined event is emitted", any(e.kind == "declined" for e in events))

a, calls, events = make_agent(
    [LLMResponse(text="", tool_calls=[ToolCall(str(i), "peek", {"what": str(i)})]) for i in range(20)]
)
res = a.run("loop forever")
check("the turn cap stops a runaway loop", res.turns == 5, str(res.turns))
check("hitting the cap is reported honestly", res.stopped_early and "stopped after" in res.text)
check("the cap actually limited the work", len(calls) == 5, str(len(calls)))


class Broke(LLMProvider):
    name = "broke"

    def available_models(self):
        return []

    def resolve_model(self, preferences):
        return "x"

    def supports_vision(self):
        return False

    def chat(self, *a, **kw):
        raise RateLimited("quota exhausted")


m = Memory(Path(tempfile.mkdtemp()) / "b.db")
a = Agent(ProviderRouter([Broke()]), ToolRegistry(), m, S())
res = a.run("hello")
check("total quota exhaustion is explained, not crashed", "out of quota" in res.text, res.text)
check("...and flagged as stopped early", res.stopped_early)


# --- nullable optionals (Groq rejects null against a plain type) -------------

print("\n[nullable optional args]")
np = reg.tools["sample"].parameters["properties"]
check("required args keep a plain type", np["query"]["type"] == "string", str(np["query"]))
check("optional args accept null", np["limit"]["type"] == ["integer", "null"], str(np["limit"]))
check("optional strings accept null", np["mode"]["type"][1] == "null", str(np["mode"]))
check("arrays stay arrays", np["tags"]["type"] == ["array", "null"], str(np["tags"]))
check("an explicit null falls back to the default",
      "|10|" in reg.execute(ToolCall("n1", "sample", {"query": "q", "limit": None})),
      reg.execute(ToolCall("n1", "sample", {"query": "q", "limit": None})))
check("a null required arg is still an error",
      reg.execute(ToolCall("n2", "sample", {"query": None})).startswith("Error"),
      reg.execute(ToolCall("n2", "sample", {"query": None})))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
