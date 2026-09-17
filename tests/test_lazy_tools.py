"""Tools on demand. No API key needed.

Every request used to carry all 59 tool schemas (~3,750 input tokens), which fit
only about two agent turns a minute into Groq's 8,000 tokens/minute free cap.
Now only core tools go up front, with a catalog the model uses to load more.

    .venv/Scripts/python.exe tests/test_lazy_tools.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from core.agent import Agent
from core.memory import Memory
from core.providers.base import LLMProvider, LLMResponse, ToolCall, ToolNotLoaded
from core.providers.groq import _classify as groq_classify
from core.providers.router import ProviderRouter
from core.tools.registry import LOAD_TOOLS, Tier, ToolRegistry

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"  -> {detail}" if detail and not cond else ""))


def build(lazy: bool = True) -> tuple[ToolRegistry, list[str]]:
    ran: list[str] = []
    reg = ToolRegistry()

    @reg.register(tier=Tier.AUTO, category="core")
    def get_datetime() -> str:
        """Current time."""
        ran.append("get_datetime")
        return "noon"

    @reg.register(tier=Tier.AUTO, category="files")
    def read_file(path: str) -> str:
        """Read a file.

        Args:
            path: File.
        """
        ran.append(f"read_file:{path}")
        return "contents"

    @reg.register(tier=Tier.CONFIRM, category="files")
    def delete_file(path: str) -> str:
        """Delete a file.

        Args:
            path: File.
        """
        ran.append(f"delete_file:{path}")
        return "deleted"

    @reg.register(tier=Tier.AUTO, category="gui")
    def click_text(text: str) -> str:
        """Click text.

        Args:
            text: Label.
        """
        ran.append(f"click_text:{text}")
        return "clicked"

    reg.describe_category("files", "find, read and delete files")
    reg.describe_category("gui", "click and type in any app")
    if lazy:
        reg.enable_lazy_loading(base={"core"}, keep_for_tasks=2)
    return reg, ran


names = lambda specs: sorted(s.name for s in specs)


def run(reg, tool, confirm=None, **kw):
    return reg.execute(ToolCall("t", tool, kw), confirm=confirm)


# --- off by default ---------------------------------------------------------

print("\n[off by default]")
reg, _ = build(lazy=False)
check("without lazy loading every tool is sent", names(reg.specs()) ==
      ["click_text", "delete_file", "get_datetime", "read_file"], names(reg.specs()))
check("...and there is no catalog tool", LOAD_TOOLS not in names(reg.specs()))


# --- what goes up front -----------------------------------------------------

print("\n[up front]")
reg, _ = build()
up_front = names(reg.specs())
check("only core tools plus the catalog are sent", up_front == ["get_datetime", LOAD_TOOLS], up_front)
catalog = next(s for s in reg.specs() if s.name == LOAD_TOOLS)
check("the catalog describes each group in one line",
      "- files: find, read and delete files" in catalog.description
      and "- gui: click and type in any app" in catalog.description, catalog.description)
check("the catalog does not offer the core group, which is already loaded",
      "- core" not in catalog.description)
check("categories are an enum, so Groq rejects a group that does not exist",
      catalog.parameters["properties"]["categories"]["items"]["enum"] == ["files", "gui"],
      catalog.parameters)


# --- loading ----------------------------------------------------------------

print("\n[loading]")
out = run(reg, LOAD_TOOLS, categories=["files"])
check("load_tools reports what it loaded and names the tools", "Loaded files" in out
      and "read_file" in out and "delete_file" in out, out)
check("loaded tools are sent from the next request on",
      {"read_file", "delete_file"} <= set(names(reg.specs())), names(reg.specs()))
check("...and the catalog now offers only what is still unloaded",
      next(s for s in reg.specs() if s.name == LOAD_TOOLS)
      .parameters["properties"]["categories"]["items"]["enum"] == ["gui"])
out = run(reg, LOAD_TOOLS, categories=["nonsense"])
check("an unknown group is explained, listing the real ones",
      "No such group: nonsense" in out and "files" in out, out)

run(reg, LOAD_TOOLS, categories=["gui"])
check("when everything is loaded the catalog disappears", LOAD_TOOLS not in names(reg.specs()))


# --- permission tiers still hold --------------------------------------------

print("\n[tiers still enforced]")
reg, ran = build()
out = run(reg, "delete_file", path="x")
check("an unloaded confirm-tier tool still refuses without confirmation",
      "needs confirmation" in out and ran == [], (out, ran))
out = run(reg, "read_file", path="notes.txt")
check("a provider that reaches an unloaded tool directly still gets it run", out == "contents", out)
check("...and that loads its group", "read_file" in names(reg.specs()))


# --- expiry -----------------------------------------------------------------

print("\n[expiry]")
reg, _ = build()
run(reg, LOAD_TOOLS, categories=["gui"])
reg.new_task()
check("a group stays loaded into the next task", "click_text" in names(reg.specs()))
reg.new_task()
check("...and the one after", "click_text" in names(reg.specs()))
reg.new_task()
check("it unloads after going unused for longer than keep_for_tasks",
      "click_text" not in names(reg.specs()), names(reg.specs()))

reg, _ = build()
run(reg, LOAD_TOOLS, categories=["gui"])
for _ in range(5):
    reg.new_task()
    run(reg, "click_text", text="Save")
check("using a group keeps it loaded indefinitely", "click_text" in names(reg.specs()))
check("the core group never expires", "get_datetime" in names(reg.specs()))


# --- size -------------------------------------------------------------------

print("\n[size, measured on Bantu's real tools]")
# The catalog has a fixed cost, so on a handful of toy tools it is larger than
# just sending them. The saving only exists at real scale - so measure that.
size = lambda specs: sum(len(s.name) + len(s.description) + len(json.dumps(s.parameters)) for s in specs) // 4
real = ToolRegistry()
from core.tools import builtin
from platform_desktop import files, gui, ocr, shell, system, vision, web

builtin.register(real, Memory(Path(tempfile.mkdtemp()) / "m.db"))
gui._tracker.start = lambda: None
for module in (files, system, shell, web, ocr, gui):
    module.register(real)
vision.register(real, None, None)
everything = size(real.specs())
real.enable_lazy_loading(base={"core"})
up_front = size(real.specs())
print(f"       all {len(real.tools)} tools ~{everything:,} tokens; up front ~{up_front:,} tokens")
check("at real scale the up-front request is under a quarter of sending everything",
      up_front < everything / 4, (up_front, everything))
real.load(["gui", "screen"])
check("even with a GUI task's groups loaded it stays under half",
      size(real.specs()) < everything / 2, (size(real.specs()), everything))


# --- Groq's rejection -------------------------------------------------------

print("\n[recognising Groq's rejection]")
real_text = ("Error code: 400 - {'error': {'message': \"Tool call validation failed: tool call validation "
             "failed: attempted to call tool 'search_files' which was not in request.tools\", "
             "'type': 'invalid_request_error', 'code': 'tool_use_failed'}}")
err = groq_classify(Exception(real_text))
check("Groq's real error text maps to ToolNotLoaded", isinstance(err, ToolNotLoaded), type(err).__name__)
check("...carrying the tool's name", getattr(err, "tool_name", None) == "search_files")
check("an unrelated 400 is not mistaken for it",
      not isinstance(groq_classify(Exception("Error code: 400 - bad request")), ToolNotLoaded))


# --- router -----------------------------------------------------------------

print("\n[router never fails over on it]")


class Rejects(LLMProvider):
    name = "rejects"

    def available_models(self):
        return ["r"]

    def resolve_model(self, p):
        return "r"

    def chat(self, *a, **k):
        raise ToolNotLoaded("not loaded", "read_file")


class Records(LLMProvider):
    name = "records"
    calls = 0

    def available_models(self):
        return ["x"]

    def resolve_model(self, p):
        return "x"

    def supports_vision(self):
        return True

    def chat(self, *a, **k):
        Records.calls += 1
        return LLMResponse(text="from the backup")


router = ProviderRouter([Rejects(), Records()])
try:
    router.chat([])
    check("ToolNotLoaded propagates out of the router", False, "it was swallowed")
except ToolNotLoaded:
    check("ToolNotLoaded propagates out of the router", True)
check("...without spending a request on the backup provider (Gemini's vision budget)",
      Records.calls == 0, Records.calls)
check("...and without cooling down the provider", router.status()[0]["cooldown_s"] == 0)


# --- agent ------------------------------------------------------------------

print("\n[agent]")


class Settings:
    username, assistant_name = "Harsh", "Bantu"
    max_tool_turns, max_output_tokens, temperature = 6, 512, 0.7


class Scripted(LLMProvider):
    name = "scripted"

    def __init__(self, script):
        self.script, self.tool_names = list(script), []

    def available_models(self):
        return ["s"]

    def resolve_model(self, p):
        return "s"

    def supports_vision(self):
        return True

    def chat(self, messages, tools=None, system=None, **kw):
        self.tool_names.append(sorted(t.name for t in tools or []))
        item = self.script.pop(0) if self.script else LLMResponse(text="done")
        if isinstance(item, Exception):
            raise item
        return item


def agent_with(script):
    reg, ran = build()
    prov = Scripted(script)
    return Agent(ProviderRouter([prov]), reg, Memory(Path(tempfile.mkdtemp()) / "h.db"), Settings()), prov, ran


agent, prov, ran = agent_with([
    LLMResponse(text="", tool_calls=[ToolCall("1", LOAD_TOOLS, {"categories": ["gui"]})]),
    LLMResponse(text="", tool_calls=[ToolCall("2", "click_text", {"text": "Save"})]),
    LLMResponse(text="Clicked Save."),
])
res = agent.run("click save")
check("the first request carries only core tools and the catalog",
      prov.tool_names[0] == ["get_datetime", LOAD_TOOLS], prov.tool_names[0])
check("after load_tools, the next request carries the group", "click_text" in prov.tool_names[1],
      prov.tool_names[1])
check("the task completes using the loaded tool", ran == ["click_text:Save"] and res.text == "Clicked Save.",
      (ran, res.text))
check("the system prompt tells the model to load tools first", "load_tools" in agent._system())

agent, prov, ran = agent_with([
    ToolNotLoaded("not loaded", "click_text"),
    LLMResponse(text="", tool_calls=[ToolCall("2", "click_text", {"text": "OK"})]),
    LLMResponse(text="Done."),
])
res = agent.run("click ok")
check("when the model skips load_tools, the agent loads the group and retries",
      ran == ["click_text:OK"] and res.text == "Done.", (ran, res.text))
check("...and the retry really carried the tool", "click_text" in prov.tool_names[1], prov.tool_names)

agent, prov, ran = agent_with([ToolNotLoaded("x", "click_text")] * 10)
res = agent.run("keep failing")
check("retrying is bounded, not an endless loop", res.stopped_early and len(prov.tool_names) <= 4,
      (res.stopped_early, len(prov.tool_names)))

agent, prov, ran = agent_with([ToolNotLoaded("x", "no_such_tool")])
res = agent.run("hallucinated tool")
check("a call to a tool that does not exist at all stops cleanly",
      res.stopped_early and len(prov.tool_names) == 1, (res.stopped_early, len(prov.tool_names)))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
