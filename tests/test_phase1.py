"""Phase 1 smoke tests — run with: .venv/Scripts/python.exe tests/test_phase1.py

Covers everything that does not need a live API key: settings round-trip,
voice lookup, both adapters' message/tool conversion, and router failover.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import config as cfg
from core.providers.base import (
    AllProvidersFailed,
    AuthError,
    Image,
    LLMProvider,
    LLMResponse,
    Message,
    RateLimited,
    ToolCall,
    ToolSpec,
    TransientError,
)
from core.providers.router import ProviderRouter

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"  {detail}" if detail and not cond else ""))


SAMPLE_TOOL = ToolSpec(
    name="search_files",
    description="Search files by name and content.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to look for"},
            "limit": {"type": "integer", "description": "Max results"},
        },
        "required": ["query"],
    },
)

CONVO = [
    Message.user("find my invoices"),
    Message.assistant(tool_calls=[ToolCall(id="c1", name="search_files", arguments={"query": "invoice"})]),
    Message.tool_result(ToolCall(id="c1", name="search_files", arguments={}), "7 files found"),
    Message.user("zip them"),
]


# --- settings ---------------------------------------------------------------

print("\n[settings]")
tmp = tempfile.mkdtemp()
os.environ["APPDATA"] = tmp
cfg.user_data_dir.__wrapped__ if hasattr(cfg.user_data_dir, "__wrapped__") else None

s = cfg.Settings()
check("defaults have a 12-turn cap", s.max_tool_turns == 12)
check("groq leads on volume, gemini kept for vision", s.provider_order == ["groq", "gemini"])
check("no llama in groq prefs", not any("llama" in m for m in s.groq_models),
      f"got {s.groq_models}")

check("english female is Ava", s.voice_for("en", "female") == "en-US-AvaMultilingualNeural")
check("english male is Ryan", s.voice_for("en", "male") == "en-GB-RyanNeural")
check("hindi female is Swara", s.voice_for("hi", "female") == "hi-IN-SwaraNeural")
check("hindi male is Madhur", s.voice_for("hi", "male") == "hi-IN-MadhurNeural")
check("nepali female is Hemkala", s.voice_for("ne", "female") == "ne-NP-HemkalaNeural")
check("nepali male is Sagar", s.voice_for("ne", "male") == "ne-NP-SagarNeural")
check("unknown language falls back to english", s.voice_for("zz", "male") == "en-GB-RyanNeural")

s.username = "Harsh"
s.max_tool_turns = 7
s.save()
check("config file written", s.path.exists())
again = cfg.Settings.load()
check("settings survive a round trip", again.max_tool_turns == 7 and again.username == "Harsh")
check("voices survive a round trip", again.voice_for("ne", "male") == "ne-NP-SagarNeural")

s.path.write_text("{ this is not json", encoding="utf-8")
check("corrupt config falls back to defaults", cfg.Settings.load().max_tool_turns == 12)


# --- gemini conversion ------------------------------------------------------

print("\n[gemini adapter]")
from core.providers.gemini import GeminiProvider, _classify as gclass

g = GeminiProvider("dummy-key-not-used", ["gemini-flash-latest"])
contents = g._to_contents(CONVO)
check("every message converted", len(contents) == 4, f"got {len(contents)}")
check("assistant maps to model role", contents[1].role == "model")
check("tool result comes back as a user turn", contents[2].role == "user")
check("tool call became a function_call part", contents[1].parts[0].function_call is not None)
check("tool result became a function_response part",
      contents[2].parts[0].function_response is not None)

gt = g._to_tools([SAMPLE_TOOL])
decl = gt[0].function_declarations[0]
check("tool name carried through", decl.name == "search_files")
check("raw json schema passed straight through",
      decl.parameters_json_schema == SAMPLE_TOOL.parameters)

img_parts = g._parts_for(Message.user("what is this?", [Image(b"\x89PNG_fake", "image/png")]))
check("image becomes a second part", len(img_parts) == 2)
check("gemini declares vision", g.supports_vision())

check("429 maps to RateLimited", isinstance(gclass(Exception("429 RESOURCE_EXHAUSTED")), RateLimited))
check("quota text maps to RateLimited", isinstance(gclass(Exception("quota exceeded")), RateLimited))
check("permission denied maps to AuthError", isinstance(gclass(Exception("PERMISSION_DENIED")), AuthError))
check("503 maps to TransientError", isinstance(gclass(Exception("503 UNAVAILABLE")), TransientError))


# --- groq conversion --------------------------------------------------------

print("\n[groq adapter]")
from core.providers.groq import GroqProvider, _classify as qclass

q = GroqProvider("dummy-key-not-used", ["openai/gpt-oss-120b"])
msgs = q._to_messages(CONVO, system="you are Bantu")
check("system prepended", msgs[0]["role"] == "system")
check("tool call serialised as json string",
      json.loads(msgs[2]["tool_calls"][0]["function"]["arguments"]) == {"query": "invoice"})
check("tool result keeps its call id", msgs[3]["tool_call_id"] == "c1")
check("image absence is disclosed, not silent",
      "cannot see images" in q._to_messages([Message.user("hi", [Image(b"x")])], None)[0]["content"])

qt = q._to_tools([SAMPLE_TOOL])
check("groq tool shape is openai-style", qt[0]["type"] == "function" and qt[0]["function"]["name"] == "search_files")
check("groq does not claim vision", not q.supports_vision())
check("groq 429 maps to RateLimited", isinstance(qclass(Exception("429 rate_limit")), RateLimited))
check("groq bad key maps to AuthError", isinstance(qclass(Exception("invalid_api_key")), AuthError))


# --- router -----------------------------------------------------------------

print("\n[router failover]")


class Fake(LLMProvider):
    def __init__(self, name, raises=None, vision=False):
        self.name, self._raises, self._vision = name, raises, vision
        self.calls = 0

    def available_models(self):
        return ["fake-1"]

    def resolve_model(self, preferences):
        return "fake-1"

    def supports_vision(self):
        return self._vision

    def chat(self, messages, tools=None, system=None, **kw):
        self.calls += 1
        if self._raises:
            raise self._raises
        return LLMResponse(text=f"hi from {self.name}", provider=self.name, model="fake-1")


a = Fake("a", RateLimited("429"))
b = Fake("b")
r = ProviderRouter([a, b])
check("falls over to the second provider", r.chat([Message.user("x")]).provider == "b")
check("first provider was actually tried", a.calls == 1)

# Same router again: the cooled-down provider must not be re-hit every turn.
check("cooled-down provider is skipped on the next turn",
      r.chat([Message.user("x")]).provider == "b" and a.calls == 1,
      f"a.calls={a.calls}")
check("cooldown is reported in status",
      next(x for x in r.status() if x["provider"] == "a")["cooldown_s"] > 0)

bad = Fake("bad", AuthError("no key"))
ok = Fake("ok")
r3 = ProviderRouter([bad, ok])
r3.chat([Message.user("x")])
r3.chat([Message.user("x")])
check("auth failure disables for the session, not just once", bad.calls == 1)

blind, seeing = Fake("blind", vision=False), Fake("seeing", vision=True)
r4 = ProviderRouter([blind, seeing])
check("vision request routes past a blind provider",
      r4.chat([Message.user("look")], needs_vision=True).provider == "seeing")
check("blind provider was not called for a vision request", blind.calls == 0)

r5 = ProviderRouter([Fake("x", RateLimited("429")), Fake("y", TransientError("boom"))])
try:
    r5.chat([Message.user("x")])
    check("all-fail raises", False)
except AllProvidersFailed as e:
    check("all-fail raises AllProvidersFailed", True)
    check("failure report names both providers", set(e.failures) == {"x", "y"})

st = ProviderRouter([Fake("s1"), Fake("s2")]).status()
check("status reports every provider", len(st) == 2 and st[0]["provider"] == "s1")

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print("  FAILED:", f)
    sys.exit(1)
