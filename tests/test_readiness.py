"""Regression tests for what the readiness stress test found.

`tests/stress_real.py` drives the real agent through everyday tasks; each
section here pins one of its findings so it cannot quietly come back. No API
key, no network, no screen.

    .venv/Scripts/python.exe tests/test_readiness.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from core.agent import SYSTEM_TEMPLATE, Agent
from core.memory import Memory
from core.providers.base import LLMProvider, LLMResponse, ProviderError, ToolCall
from core.providers.router import ProviderRouter
from core.tools.registry import Rejected, Tier, ToolRegistry

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"  -> {detail}" if detail and not cond else ""))


class Scripted(LLMProvider):
    """Replays responses in order and records what each request offered."""

    name = "scripted"

    def __init__(self, script, fail_on=()):
        self.script = list(script)
        self.fail_on = set(fail_on)
        self.requests = []  # (tool names or None, system prompt)

    def available_models(self):
        return ["s"]

    def resolve_model(self, preferences):
        return "s"

    def supports_vision(self):
        return False

    def chat(self, messages, tools=None, system=None, **kw):
        self.requests.append(([t.name for t in tools] if tools else None, system or ""))
        if len(self.requests) in self.fail_on:
            raise ProviderError("provider fell over")
        r = self.script.pop(0) if self.script else LLMResponse(text="done")
        r.provider, r.model = self.name, "s"
        return r


class S:
    username, assistant_name = "Harsh", "Bantu"
    max_tool_turns, max_output_tokens, temperature = 6, 512, 0.7


def make(script, confirm, fail_on=()):
    reg = ToolRegistry()
    ran = []

    @reg.register(tier=Tier.AUTO, category="test")
    def peek(what: str) -> str:
        """Look something up.

        Args:
            what: Subject.
        """
        ran.append("peek " + what)
        return f"result for {what}"

    @reg.register(tier=Tier.CONFIRM, category="test")
    def delete_file(path: str) -> str:
        """Delete a file.

        Args:
            path: Target.
        """
        ran.append("delete " + path)
        return "deleted"

    @reg.register(tier=Tier.CONFIRM, category="test")
    def run_powershell(command: str) -> str:
        """Run a command.

        Args:
            command: The command.
        """
        ran.append("powershell " + command)
        return "ran"

    prov = Scripted(script, fail_on)
    events, asked = [], []

    def ask(tool, args):
        asked.append(tool.name)
        return confirm(tool, args)

    agent = Agent(ProviderRouter([prov]), reg, Memory(Path(tempfile.mkdtemp()) / "a.db"), S(),
                  on_event=events.append, confirm=ask if confirm else None)
    return agent, prov, ran, events, asked


def call(i, name, **args):
    return ToolCall(str(i), name, args)


def orphans(memory):
    """Tool calls in history with no result: providers reject such a conversation."""
    answered = {m.tool_call_id for m in memory.history() if m.role == "tool"}
    return [c.id for m in memory.history() if m.role == "assistant" for c in m.tool_calls if c.id not in answered]


def no(_tool, _args):
    return False


# --- a "no" ends the request -------------------------------------------------
# Found live: a declined delete_file was followed by PowerShell Remove-Item, then
# File Explorer clicks; a declined shutdown by `shutdown /s /t 0`. The registry
# itself told the model to "try another way".

print("\n[a decline ends the request]")

agent, prov, ran, events, asked = make(
    [LLMResponse(text="", tool_calls=[call(1, "delete_file", path="notes.txt")]),
     LLMResponse(text="Okay, I didn't delete notes.txt.")],
    confirm=no,
)
res = agent.run("delete notes.txt")
check("the declined tool did not run", ran == [], str(ran))
check("the model was asked exactly twice: the step, then the closing reply",
      len(prov.requests) == 2, str(len(prov.requests)))
check("the closing reply was offered no tools at all", prov.requests[1][0] is None, str(prov.requests[1][0]))
check("the closing prompt says the user said no", "said no" in prov.requests[1][1])
check("the closing prompt forbids blaming a policy", "policy" in prov.requests[1][1])
# Found live: "in Harsh's language" produced Hindi for an English request.
check("the closing prompt ties language to the latest message, not the name",
      "same language as their" in prov.requests[1][1] and "Harsh's language" not in prov.requests[1][1])
check("the model's closing words are the reply", res.text == "Okay, I didn't delete notes.txt.", res.text)
check("a decline is a finished request, not a failure", not res.stopped_early)
check("one declined event", [e.kind for e in events].count("declined") == 1)
check("the reply is emitted as text", events[-1].kind == "text" and events[-1].text == res.text)
check("history has no unanswered tool calls", orphans(agent.memory) == [])
check("history ends with the closing reply",
      agent.memory.history()[-1].role == "assistant" and agent.memory.history()[-1].content == res.text)

registry_says = [m.content for m in agent.memory.history() if m.role == "tool"][0]
check("the tool result says the user said no", "said no" in registry_says, registry_says)
check("the tool result no longer invites another way", "try another way" not in registry_says, registry_says)

print("\n[a workaround attempted in the closing reply is ignored]")
agent, prov, ran, events, asked = make(
    [LLMResponse(text="", tool_calls=[call(1, "delete_file", path="notes.txt")]),
     LLMResponse(text="", tool_calls=[call(2, "run_powershell", command="Remove-Item notes.txt -Force")])],
    confirm=no,
)
res = agent.run("delete notes.txt")
check("the workaround did not run", ran == [], str(ran))
check("the user was asked only once", asked == ["delete_file"], str(asked))
check("a plain fallback reply is given", res.text == "Okay, I didn't run delete_file.", res.text)
check("the unanswerable call is not stored", orphans(agent.memory) == [], str(orphans(agent.memory)))

print("\n[later steps in the same response are skipped, earlier ones stand]")
agent, prov, ran, events, asked = make(
    [LLMResponse(text="", tool_calls=[call(1, "peek", what="size"),
                                      call(2, "delete_file", path="a.txt"),
                                      call(3, "delete_file", path="b.txt"),
                                      call(4, "peek", what="after")]),
     LLMResponse(text="I left both files alone.")],
    confirm=no,
)
res = agent.run("check size, then delete a and b")
check("the step before the no ran", ran == ["peek size"], str(ran))
check("the user was asked once, not once per file", asked == ["delete_file"], str(asked))
check("every call still has a result", orphans(agent.memory) == [], str(orphans(agent.memory)))
skipped = [m.content for m in agent.memory.history() if m.role == "tool" and m.tool_call_id in ("3", "4")]
check("skipped steps say why", len(skipped) == 2 and all("earlier step" in s for s in skipped), str(skipped))
check("tool count excludes skipped steps", res.tool_calls == 2, str(res.tool_calls))


def reject(_tool, _args):
    raise Rejected("user clicked Reject")


print("\n[every way of saying no ends it]")
agent, prov, ran, events, asked = make(
    [LLMResponse(text="", tool_calls=[call(1, "delete_file", path="x")]), LLMResponse(text="Not deleted.")],
    confirm=reject,
)
res = agent.run("delete x")
check("Rejected raised by the prompt ends the request", res.text == "Not deleted." and len(prov.requests) == 2)
check("...and emits a declined event", any(e.kind == "declined" for e in events))

agent, prov, ran, events, asked = make(
    [LLMResponse(text="", tool_calls=[call(1, "delete_file", path="x")]), LLMResponse(text="Couldn't ask you.")],
    confirm=None,
)
res = agent.run("delete x")
check("nothing able to ask also ends the request", ran == [] and len(prov.requests) == 2 and
      prov.requests[1][0] is None, str(prov.requests))

print("\n[the closing reply cannot fail the request]")
agent, prov, ran, events, asked = make(
    [LLMResponse(text="", tool_calls=[call(1, "delete_file", path="x")])],
    confirm=no, fail_on={2},
)
res = agent.run("delete x")
check("a provider failure falls back to a plain reply", res.text == "Okay, I didn't run delete_file.", res.text)
check("...which is still a finished request", not res.stopped_early)

print("\n[saying yes is unchanged]")
agent, prov, ran, events, asked = make(
    [LLMResponse(text="", tool_calls=[call(1, "delete_file", path="x")]),
     LLMResponse(text="", tool_calls=[call(2, "peek", what="bin")]),
     LLMResponse(text="Deleted and checked.")],
    confirm=lambda t, a: True,
)
res = agent.run("delete x and check")
check("an approved step runs and the task carries on", ran == ["delete x", "peek bin"], str(ran))
check("tools stay on offer after an approval", all(r[0] for r in prov.requests), str(prov.requests))

print("\n[a no does not carry into the next request]")
agent, prov, ran, events, asked = make(
    [LLMResponse(text="", tool_calls=[call(1, "delete_file", path="x")]), LLMResponse(text="Left it."),
     LLMResponse(text="", tool_calls=[call(2, "peek", what="time")]), LLMResponse(text="It's noon.")],
    confirm=no,
)
agent.run("delete x")
res = agent.run("what time is it")
check("the next request runs its tools", ran == ["peek time"], str(ran))
check("and ends normally", res.text == "It's noon." and prov.requests[2][0] is not None)

check("the system prompt also states the rule", "never look for another way" in SYSTEM_TEMPLATE)


print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
