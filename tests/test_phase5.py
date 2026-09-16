"""Phase 5 tests — web search, page reading, browser tools.

Search and fetch hit the real network (both are free and unlimited). Browser
tools are checked for gating and graceful failure rather than driven, since a
driver may not be present.

    .venv/Scripts/python.exe tests/test_phase5.py
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

from core.providers.base import ToolCall
from core.tools.registry import Tier, ToolRegistry
from platform_desktop import web

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"  -> {detail}" if detail and not cond else ""))


reg = ToolRegistry()
web.register(reg)


def run(tool, **kw):
    return reg.execute(ToolCall("t", tool, kw), confirm=lambda t, a: True)


def blocked(tool, **kw):
    return reg.execute(ToolCall("t", tool, kw))


# --- readable-text extraction (no network) ----------------------------------

print("\n[html extraction]")
HTML = """<html><head><title>Test Page</title></head><body>
<nav>Home About Contact</nav>
<script>var tracking = 1;</script>
<style>.x{color:red}</style>
<article><h1>Real Heading</h1><p>The actual content lives here.</p>
<p>A second paragraph.</p></article>
<footer>Copyright 2026</footer></body></html>"""

title, text = web._extract_text(HTML)
check("title extracted", title == "Test Page", title)
check("article content kept", "The actual content lives here." in text, text[:80])
check("second paragraph kept", "A second paragraph." in text)
check("scripts stripped", "tracking" not in text, text[:120])
check("styles stripped", "color:red" not in text)
check("navigation stripped", "Home About Contact" not in text, text[:120])
check("footer stripped", "Copyright 2026" not in text, text[:120])

_, plain = web._extract_text("<html><body><p>No article tag here.</p></body></html>")
check("falls back to body when there is no article tag", "No article tag here." in plain, plain[:60])

_, blank = web._extract_text("<html><body></body></html>")
check("an empty document yields empty text", blank.strip() == "", repr(blank[:40]))


# --- search (real network, free) --------------------------------------------

print("\n[web search]")
out = run("web_search", query="Kathmandu Nepal capital", count=3)
check("search returns results", "results for" in out and "http" in out, out[:100])
check("search includes snippets", len(out) > 200, str(len(out)))
check("an empty query is refused", run("web_search", query="  ").startswith("Error"))

out = run("web_search", query="asdkjhaskdjhaskjdhqwkejhaskdjh", count=3)
check("no results is a clear message, not an error",
      out.startswith("No results") or "results for" in out, out[:80])

out = run("search_news", query="technology", count=3)
check("news search works", "news results" in out or "No news" in out, out[:80])


# --- page fetch (real network) ----------------------------------------------

print("\n[fetch_page]")
out = run("fetch_page", url="https://example.com")
check("fetches a page", "Example Domain" in out, out[:100])
check("includes the url", "example.com" in out)

out = run("fetch_page", url="example.com")
check("a bare domain gets https:// added", "Example Domain" in out, out[:80])

out = run("fetch_page", url="https://example.com", max_chars=50)
check("max_chars truncates and says so", "truncated" in out, out[-80:])

out = run("fetch_page", url="https://this-domain-does-not-exist-bantu-test.invalid")
check("an unreachable host errors clearly", out.startswith("Error: could not fetch"), out[:90])

out = run("fetch_page", url="https://httpbin.org/status/404")
check("an http error is reported", out.startswith("Error"), out[:80])


# --- download ---------------------------------------------------------------

print("\n[download]")
dest = Path(tempfile.mkdtemp()) / "sample.html"
out = run("download_file", url="https://example.com", destination=str(dest))
check("download saves a file", dest.exists() and dest.stat().st_size > 100, out[:80])
check("download refuses to clobber",
      run("download_file", url="https://example.com", destination=str(dest)).startswith("Error"))

bad = Path(tempfile.mkdtemp()) / "nope.bin"
out = run("download_file", url="https://bantu-nonexistent-host.invalid/x", destination=str(bad))
check("a failed download errors", out.startswith("Error"), out[:70])
check("...and leaves no partial file behind", not bad.exists())


# --- gating -----------------------------------------------------------------

print("\n[gating]")
tiers = {n: t.tier for n, t in reg.tools.items()}
auto = {"web_search", "search_news", "fetch_page", "browser_open", "browser_read"}
confirm = {"download_file", "browser_click", "browser_type"}
check("reading the web is AUTO", all(tiers[n] is Tier.AUTO for n in auto),
      str([n for n in auto if tiers[n] is not Tier.AUTO]))
check("acting on a page needs confirmation", all(tiers[n] is Tier.CONFIRM for n in confirm),
      str([n for n in confirm if tiers[n] is not Tier.CONFIRM]))
check("clicking needs confirmation — it can submit or purchase",
      "needs confirmation" in blocked("browser_click", target="Buy now"))
check("typing needs confirmation", "needs confirmation" in blocked("browser_type", field="q", text="x"))
check("downloading needs confirmation",
      "needs confirmation" in blocked("download_file", url="https://example.com", destination="x"))

check("8 web tools registered", len(reg.tools) == 8, str(sorted(reg.tools)))
check("every tool documents itself for the model", all(t.description for t in reg.tools.values()))
check("optional args are nullable for Groq",
      reg.tools["web_search"].parameters["properties"]["count"]["type"] == ["integer", "null"],
      str(reg.tools["web_search"].parameters["properties"]["count"]))


# --- browser degradation ----------------------------------------------------
# A missing driver must produce an explanation, never a crash, and must not
# stop search and fetch from working.

print("\n[browser without a driver]")
out = run("browser_open", url="https://example.com")
started = not out.startswith("Error")
if started:
    check("a browser driver is present, so browser_open works", True, out[:60])
    out = run("browser_read")
    check("browser_read returns page text", "Example Domain" in out, out[:80])
    out = run("browser_click", target="definitely-not-on-this-page-xyz")
    check("clicking something absent errors clearly", out.startswith("Error: could not find"), out[:80])
    out = run("browser_type", field="no_such_field_xyz", text="x")
    check("typing into an absent field errors clearly", out.startswith("Error: no field"), out[:80])
    web.shutdown()
    check("the browser shuts down cleanly", True)
else:
    check("a missing driver explains itself rather than crashing",
          "could not start a browser" in out, out[:120])
    check("...and points at the tools that still work",
          "web_search" in out and "fetch_page" in out, out[:160])
    check("search still works without a driver", "results for" in run("web_search", query="test", count=2))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
