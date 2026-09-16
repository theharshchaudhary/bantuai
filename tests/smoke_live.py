"""Live smoke test — needs a real Gemini key. Spends a handful of free-tier requests.

    .venv/Scripts/python.exe tests/smoke_live.py

Never prints a key. Covers what the fakes in test_phase1.py cannot: real wire
formats, the tool-call round trip, and vision.
"""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import config as cfg
from core.providers.base import Image, Message, ToolCall, ToolSpec
from core.providers.gemini import GeminiProvider
from core.providers.router import ProviderRouter

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "  ok   " if cond else "  FAIL "
    print(mark + name + (f"  -> {detail}" if detail else ""))


SEARCH_TOOL = ToolSpec(
    name="search_files",
    description="Search the user's filesystem for files matching a query.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Text to search for"},
            "limit": {"type": "integer", "description": "Maximum results to return"},
        },
        "required": ["query"],
    },
)

SYSTEM = "You are Bantu, a concise desktop assistant. Use tools when they help."

# --- key -------------------------------------------------------------------

print("\n[key]")
key = cfg.get_key("gemini")
if not key:
    print("  FAIL no gemini key found in keyring, env, or .env")
    sys.exit(1)
check("key found", True, f"{key[:4]}...{key[-3:]} ({len(key)} chars)")

g = GeminiProvider(key, cfg.Settings().gemini_models)

# --- models ----------------------------------------------------------------

print("\n[models]")
t0 = time.perf_counter()
models = g.available_models()
check("models endpoint reachable", len(models) > 0, f"{len(models)} models, {(time.perf_counter()-t0)*1000:.0f}ms")
flash = [m for m in models if "flash" in m][:6]
print("       flash models offered:", ", ".join(flash) if flash else "(none)")
resolved = g.resolve_model(cfg.Settings().gemini_models)
check("model resolved from the live list", bool(resolved), resolved)

# --- plain chat ------------------------------------------------------------

print("\n[plain chat]")
t0 = time.perf_counter()
r = g.chat([Message.user("Reply with exactly the word: ready")], system=SYSTEM, max_output_tokens=256)
ms = (time.perf_counter() - t0) * 1000
check("got a text reply", bool(r.text.strip()), f"{r.text.strip()[:60]!r}  {ms:.0f}ms")
check("usage reported", r.usage.input_tokens > 0, f"in={r.usage.input_tokens} out={r.usage.output_tokens}")

# --- tool call -------------------------------------------------------------

print("\n[tool calling]")
history = [Message.user("Find my invoice files, then tell me how many there are.")]
r = g.chat(history, tools=[SEARCH_TOOL], system=SYSTEM)
check("model asked for a tool", r.wants_tools, str([tc.name for tc in r.tool_calls]))
if r.wants_tools:
    tc = r.tool_calls[0]
    check("called the right tool", tc.name == "search_files", tc.name)
    check("arguments parsed into a dict", isinstance(tc.arguments, dict), repr(tc.arguments))
    check("required arg present", "query" in tc.arguments, repr(tc.arguments))

    # --- the round trip: feed the result back and get a real answer ---------
    print("\n[tool round trip]")
    history.append(Message.assistant(r.text or None, tool_calls=r.tool_calls))
    history.append(Message.tool_result(tc, "Found 7 files: invoice_jan.pdf, invoice_feb.pdf, "
                                           "invoice_mar.pdf, invoice_apr.pdf, invoice_may.pdf, "
                                           "invoice_jun.pdf, invoice_jul.pdf"))
    r2 = g.chat(history, tools=[SEARCH_TOOL], system=SYSTEM)
    check("final answer produced", bool(r2.text.strip()), r2.text.strip()[:90])
    check("answer used the tool result", "7" in r2.text or "seven" in r2.text.lower(),
          r2.text.strip()[:90])

# --- vision ----------------------------------------------------------------

print("\n[vision]")
try:
    from PIL import Image as PILImage, ImageDraw, ImageFont

    img = PILImage.new("RGB", (620, 130), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("segoeui.ttf", 26)
    except OSError:
        font = ImageFont.load_default()
    d.text((20, 26), "Disk C: 94% full", fill="black", font=font)
    d.text((20, 70), "Battery: 12%", fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    r3 = g.chat(
        [Message.user("What two numbers appear in this image? Answer briefly.",
                      [Image(buf.getvalue(), "image/png")])],
        system=SYSTEM,
        max_output_tokens=512,
    )
    txt = r3.text.strip()
    check("vision returned an answer", bool(txt), txt[:90])
    check("read both numbers correctly", "94" in txt and "12" in txt, txt[:90])
except ImportError:
    print("  skip  pillow not installed")

# --- router ----------------------------------------------------------------

print("\n[router end to end]")
router = ProviderRouter([g])
r4 = router.chat([Message.user("Reply with exactly: routed")], system=SYSTEM, max_output_tokens=256)
check("router returned a reply", bool(r4.text.strip()), f"via {r4.provider}/{r4.model}")
check("router status readable", router.status()[0]["calls"] >= 1, str(router.status()[0]))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
