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



# --- a limit mid-task must not end the task -----------------------------------
# Found live re-testing the decline fix: gpt-oss-120b ran out of its daily tokens,
# the router benched all of Groq though gpt-oss-20b and qwen had their day unused,
# and Gemini then rejected its OWN previous tool call because Memory had dropped
# the thought_signature. Along the way the lite Gemini models refused every
# request carrying thinking_budget=0, with a 400 that never says "thinking".

import time as _time

from google.genai import types as gtypes

from core.providers.base import Message, ModelUnavailable, RateLimited, ToolNotLoaded, AuthError
from core.providers.gemini import SIGNATURE_PLACEHOLDER, GeminiProvider
from core.providers.groq import GroqProvider

print("\n[groq: a model at its limit hands over to the next]")


class GroqError(Exception):
    """Shaped like the SDK's APIStatusError: status_code, headers, message."""

    def __init__(self, status, message, retry_after=None):
        super().__init__(f"Error code: {status} - {message}")
        self.status_code = status
        self.response = type("R", (), {"headers": {"retry-after": str(retry_after)} if retry_after else {}})()


def groq_reply(text="hi"):
    msg = type("M", (), {"content": text, "tool_calls": []})()
    return type("C", (), {"choices": [type("Ch", (), {"message": msg})()], "usage": None})()


class FakeGroqClient:
    def __init__(self, behaviour):
        self.behaviour = behaviour  # model -> exception to raise, or None to answer
        self.asked = []
        outer = self

        class Completions:
            def create(self, model, **kw):
                outer.asked.append(model)
                err = outer.behaviour.get(model)
                if err:
                    raise err
                return groq_reply(f"from {model}")

        self.chat = type("Chat", (), {"completions": Completions()})()
        self.models = type("Models", (), {"list": lambda _s: type("L", (), {"data": []})()})()


MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"]
TPM = ("{'error': {'message': 'Rate limit reached for model `openai/gpt-oss-120b` in organization `org_x` "
       "service tier `on_demand` on tokens per minute (TPM): Limit 8000, Used 6597, Requested 3383. "
       "Please try again in 14.85s.', 'type': 'tokens', 'code': 'rate_limit_exceeded'}}")


def groq(behaviour):
    q = GroqProvider("dummy-key-not-used", MODELS)
    q._client = FakeGroqClient(behaviour)
    return q


check("the SDK's silent retries are off",
      GroqProvider("dummy-key-not-used", MODELS)._client.max_retries == 0)

q = groq({MODELS[0]: GroqError(429, TPM, retry_after=15)})
r = q.chat([Message.user("hi")])
check("the next model answers the same request", r.model == MODELS[1] and r.text == f"from {MODELS[1]}", r.model)
rest = q._resting.get(MODELS[0], 0.0) - _time.monotonic()
check("the limited model rests for Groq's retry-after", 13 < rest <= 15, f"{rest:.1f}")
q.chat([Message.user("again")])
check("a resting model is not asked again", q._client.asked == [MODELS[0], MODELS[1], MODELS[1]], str(q._client.asked))
q._resting[MODELS[0]] = _time.monotonic() - 1
q._client.behaviour = {}
check("it is used again once rested", q.chat([Message.user("later")]).model == MODELS[0])

q = groq({m: GroqError(429, TPM, retry_after=s) for m, s in zip(MODELS, (40, 15, 90))})
try:
    q.chat([Message.user("hi")])
    check("every model limited raises", False)
except RateLimited as e:
    check("every model limited raises RateLimited", True)
    check("...naming the soonest wait", e.retry_after is not None and 13 < e.retry_after <= 15, str(e.retry_after))
    check("...in words that do not trigger the router's 15-minute quota cooldown",
          "quota" not in str(e).lower() and "daily" not in str(e).lower(), str(e))
asked = len(q._client.asked)
try:
    q.chat([Message.user("hi")])
except RateLimited:
    pass
check("while all rest, no request is sent at all", len(q._client.asked) == asked)

router = ProviderRouter([groq({m: GroqError(429, TPM, retry_after=15) for m in MODELS}),
                         Scripted([LLMResponse(text="gemini here")])])
check("the router hands over when all of Groq rests", router.chat([Message.user("x")]).text == "gemini here")
cd = next(x for x in router.status() if x["provider"] == "groq")["cooldown_s"]
check("...and benches Groq for the real wait, not 15 minutes", 10 <= cd <= 16, str(cd))

q = groq({MODELS[0]: GroqError(404, "The model `openai/gpt-oss-120b` does not exist")})
check("a missing model is skipped", q.chat([Message.user("x")]).model == MODELS[1])
q._client.behaviour = {}
q.chat([Message.user("x")])
check("...and never asked again", q._client.asked.count(MODELS[0]) == 1, str(q._client.asked))

q = groq({MODELS[0]: GroqError(400, "attempted to call tool 'search_files' which was not in request.tools")})
try:
    q.chat([Message.user("x")])
    check("an unloaded tool is raised", False)
except ToolNotLoaded:
    check("an unloaded tool is raised to the agent, not retried on other models", q._client.asked == [MODELS[0]])

q = groq({MODELS[0]: GroqError(401, "invalid_api_key")})
try:
    q.chat([Message.user("x")])
    check("a bad key is raised", False)
except AuthError:
    check("a bad key is raised at once: every model shares the key", q._client.asked == [MODELS[0]])

q = groq({MODELS[0]: GroqError(503, "service unavailable")})
check("a transient failure moves to the next model", q.chat([Message.user("x")]).model == MODELS[1])
check("...without resting the model", MODELS[0] not in q._resting)


print("\n[gemini: signatures survive storage and failover]")

g = GeminiProvider("dummy-key-not-used", ["gemini-3.6-flash"])
signed = ToolCall("c1", "get_datetime", {}, meta={"thought_signature": b"real-sig"})
unsigned = ToolCall("c2", "get_datetime", {})
check("a real signature is replayed as-is",
      g._parts_for(Message.assistant(None, [signed]))[0].thought_signature == b"real-sig")
check("a call without one (Groq's, or an old row) gets Google's placeholder",
      g._parts_for(Message.assistant(None, [unsigned]))[0].thought_signature == b"skip_thought_signature_validator")

mem = Memory(Path(tempfile.mkdtemp()) / "m.db")
mem.append(Message.user("time?"))
mem.append(Message.assistant(None, [ToolCall("c1", "get_datetime", {}, meta={"thought_signature": b"\x00\xffsig", "n": 3})]))
back = mem.history()[1].tool_calls[0]
check("Memory keeps a bytes signature exactly", back.meta.get("thought_signature") == b"\x00\xffsig", str(back.meta))
check("Memory keeps other meta values", back.meta.get("n") == 3)
mem.db.execute("INSERT INTO messages (conversation, role, content, tool_calls, created_at) VALUES (?,?,?,?,?)",
               (mem.conversation, "assistant", None, '[{"id": "old", "name": "x", "arguments": {}}]', 0))
mem.db.commit()
check("a row stored before meta was kept still loads", mem.history()[-1].tool_calls[0].meta == {})


def gemini_reply(parts):
    return gtypes.GenerateContentResponse(candidates=[gtypes.Candidate(content=gtypes.Content(role="model", parts=parts))])


class FakeGeminiModels:
    def __init__(self, script=None, reject=None):
        self.script = list(script or [])
        self.reject = reject or (lambda model, config: None)
        self.requests = []  # (model, contents, config)

    def generate_content(self, model, contents, config):
        self.requests.append((model, contents, config))
        err = self.reject(model, config)
        if err:
            raise err
        return self.script.pop(0) if self.script else gemini_reply([gtypes.Part.from_text(text=f"from {model}")])

    def list(self):
        return []


def gemini(prefs, **fake):
    p = GeminiProvider("dummy-key-not-used", prefs)
    p._client = type("C", (), {"models": FakeGeminiModels(**fake)})()
    return p


g = gemini(["gemini-3.6-flash"], script=[
    gemini_reply([gtypes.Part(function_call=gtypes.FunctionCall(name="get_time", args={}), thought_signature=b"sig-123")]),
    gemini_reply([gtypes.Part.from_text(text="It's noon.")]),
])
reg = ToolRegistry()


@reg.register(tier=Tier.AUTO, category="test")
def get_time() -> str:
    """The time."""
    return "12:00"


agent = Agent(ProviderRouter([g]), reg, Memory(Path(tempfile.mkdtemp()) / "g.db"), S())
res = agent.run("what time is it?")
second = g._client.models.requests[1][1]
replayed = [p.thought_signature for c in second for p in (c.parts or []) if p.function_call]
check("a two-step task on Gemini finishes", res.text == "It's noon.", res.text)
check("its own signature reached the second request through Memory", replayed == [b"sig-123"], str(replayed))

print("\n[gemini: a model that refuses the thinking budget]")
INVALID = Exception("400 INVALID_ARGUMENT. {'error': {'code': 400, 'message': 'Request contains an invalid argument.', 'status': 'INVALID_ARGUMENT'}}")


def lite_refuses_thinking(model, config):
    return INVALID if "lite" in model and config.thinking_config is not None else None


g = gemini(["gemini-3.5-flash-lite"], reject=lite_refuses_thinking)
try:
    answer = g.chat([Message.user("x")]).text
except ProviderError as e:
    answer = f"raised: {e}"
check("it is retried without the budget and answers", answer == "from gemini-3.5-flash-lite", answer)
check("...and remembered for that model", "gemini-3.5-flash-lite" in g._no_thinking)
n = len(g._client.models.requests)
g.chat([Message.user("again")])
check("...so the next request is sent once, without it",
      len(g._client.models.requests) == n + 1 and g._client.models.requests[-1][2].thinking_config is None)

g = gemini(["gemini-3.6-flash", "gemini-3.5-flash-lite"], reject=lite_refuses_thinking)
g.chat([Message.user("x")])
check("a model that accepts it keeps its budget", g._client.models.requests[0][2].thinking_config is not None)

g = gemini(["gemini-3.5-flash-lite"], reject=lambda m, c: INVALID)
try:
    g.chat([Message.user("x")])
    check("a 400 that is not about thinking is raised", False)
except ProviderError:
    check("a 400 that is not about thinking is still raised", True)
check("...and the model is not wrongly marked", "gemini-3.5-flash-lite" not in g._no_thinking)

print("\n[gemini: a limited model rests instead of being dropped for the session]")
PER_MINUTE = Exception("429 RESOURCE_EXHAUSTED. {'error': {'details': [{'violations': [{'quotaId': "
                       "'GenerateRequestsPerMinutePerProjectPerModel-FreeTier'}]}, {'retryDelay': '25s'}]}}")
PER_DAY = Exception("429 RESOURCE_EXHAUSTED. {'error': {'details': [{'violations': [{'quotaId': "
                    "'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}]}, {'retryDelay': '17s'}]}}")
g = gemini(["gemini-3.6-flash", "gemini-3.5-flash"],
           reject=lambda m, c: PER_MINUTE if m == "gemini-3.6-flash" else None)
check("the next model answers", g.chat([Message.user("x")]).text == "from gemini-3.5-flash")
rest = g._resting.get("gemini-3.6-flash", 0.0) - _time.monotonic()
check("a per-minute limit rests for Google's retryDelay", 24 < rest <= 26, f"{rest:.1f}")
check("...and the model is not dead", "gemini-3.6-flash" not in g._dead)
g._resting["gemini-3.6-flash"] = _time.monotonic() - 1
g._client.models.reject = lambda m, c: None
check("it is used again once rested", g.chat([Message.user("x")]).text == "from gemini-3.6-flash")

g = gemini(["gemini-3.6-flash", "gemini-3.5-flash"], reject=lambda m, c: PER_DAY)
try:
    g.chat([Message.user("x")])
    check("all models out raises", False)
except RateLimited as e:
    check("a daily limit rests for an hour, not the misleading retryDelay",
          all(3500 < g._resting[m] - _time.monotonic() <= 3600 for m in g._resting), str(g._resting))
    check("all models out raises RateLimited with a wait", e.retry_after and e.retry_after > 3000, str(e.retry_after))
n = len(g._client.models.requests)
try:
    g.chat([Message.user("x")])
except RateLimited:
    pass
check("while all rest, no request is sent and no unverified model is tried", len(g._client.models.requests) == n)


print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
