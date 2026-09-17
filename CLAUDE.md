# BantuAI — working context

Read this first every session. It is the source of truth for what this project is and where it stands.
It lives in the repo, so it survives switching Claude Code accounts or machines.

**Keep it current.** At the end of any phase, update "Current status" and "Decision log" below in the
same commit as the code.

---

## What this is

A **local Windows desktop AI agent** — you type or speak, and it actually acts on the machine
(files, apps, browser, shell) rather than describing what you could do. Think capable assistant,
aimed at the JARVIS end of the spectrum.

Owner: Harsh. Repo: `theharshchaudhary/bantuai`, branch `main`.

## Hard constraints — do not break these without asking

1. **$0 forever.** No paid APIs, ever. Free tiers only. If a feature needs money, it does not ship.
2. **Nothing to download.** No Ollama, no local LLM weights, no separate installers (this is why
   Tesseract was rejected in favour of the Windows OCR API). The `.exe` must be one click.
   The line Harsh drew: **tens of megabytes are fine, gigabytes are not.** openWakeWord (~2MB)
   and a Selenium browser driver (~15MB) are acceptable; Ollama plus multi-GB model weights are
   not, and that is what this rule exists to prevent. No separate installer the user must run.
3. **`core/` stays portable.** No PyQt5, no Windows-only imports, no desktop dependencies inside
   `core/`. Tools are *registered into* it by the platform layer. This is what keeps a future Android
   client possible.
4. **Local product only.** No server, no accounts, no billing, no telemetry. Users bring their own keys.
5. **Permission tiers are enforced in code**, never in the prompt. See "Safety model".

## Current status

| Phase | What | State |
|---|---|---|
| 00 | Repo hygiene — `.gitignore`, `.env` untracked, `.env.example` | **done** (`251ad67`) |
| 01 | Foundation — config, keyring, provider interface, Gemini + Groq adapters, failover router | **done** |
| 02 | Agent core — tool registry, the loop, SQLite memory, vision, Windows OCR, CLI | **done** |
| 03 | File tools (15) — search, read, docs, write, organize, archives | **done** |
| 04 | System + app tools (17), guarded PowerShell | **done** |
| 05 | Web tools (8) — DuckDuckGo, page fetch, Selenium | **done** |
| 06 | Voice — Groq Whisper in, edge-tts out, barge-in | **done** |
| 07 | HUD — floating orb, chat panel, tray, global hotkey | **done** |
| 08 | GUI control — click, type, keys, scroll via OCR; vision fallback for icons | **done** |
| 09 | Onboarding — first-run setup, Settings window, live key checks | **done** |
| 10 | Packaging — PyInstaller `.exe` | **deferred** — Harsh, 2026-09-17: not ready enough to package. Do not start without asking. |
| 11 | README rewrite | — |

Post-v1: proactive trigger engine (v1.1), semantic memory + habits (v1.2), Home Assistant + phone (v2).

Leads from reading Shreshth Kaushik's "advanced Jarvis" gists (2026-09-17) — ideas only, the
gists state no license, so no code is copied:
- **Image generation via Pollinations.ai**, which his readme calls free with no key. Unverified —
  test it the way Gemini and Groq were tested before building on it.
- **A knowledge folder**: drop `.txt` files to teach Bantu. His uses FAISS plus torch and
  sentence-transformers; Bantu can index into the existing FTS5 with no download.
- Not adopted: his chat, intent and vision models are all Groq Llama, which is Enterprise-only on
  the free tier; his `brain_service.py` is a fixed intent classifier, the design Bantu retired;
  and rotating several Groq keys to multiply limits likely breaches Groq's terms.

## Architecture

```
main.py                 entry point
core/                   PORTABLE — no desktop imports allowed
  agent.py              the tool-calling loop (max 12 turns)
  memory.py             SQLite + FTS5 conversation and fact storage
  config.py             settings; keys via keyring
  providers/            base.py, gemini.py, groq.py, router.py
  tools/registry.py     @tool decorator -> JSON schema from type hints
platform_desktop/       Windows tools, registered INTO core
  files.py system.py web.py shell.py ocr.py gui.py
voice/                  stt.py, tts.py
ui/                     widgets.py (orb, panel, chips, confirm bar), app.py (worker thread, tray, hotkey),
                        onboarding.py (first-run setup), settings_dialog.py, setup_parts.py (shared)
```

One loop handles everything. **No intent routing** — the model reads the tool list and decides.
The old `Backend/Model.py` Cohere router is now retired.

Run it:
- `.venv/Scripts/python.exe main.py` — the floating orb (default). **On first launch it opens
  setup** (name → keys → voice) instead of failing for lack of keys. Click the orb, or press
  **Ctrl+Alt+Space** anywhere to speak. Settings is in the tray menu and on the panel's gear.
  Quit from the tray icon; closing the panel only hides it.
- `main.py --cli` — terminal REPL (`-v` for tool output; `/tools`, `/status`, `/facts`, `/new`).
- `main.py "a question"` — one-shot.

Only one instance runs at a time (a `QLockFile` in `%APPDATA%\BantuAI`).

**59 tools**, but **only the core group is sent up front** — see "Tools on demand". `/tools` lists
all of them with their tier:
- **core** (5, portable): `get_datetime`, `remember`, `recall`, `forget`, `search_history`
- **screen** (3): `read_screen`, `find_on_screen` (Windows OCR), `look_at_screen` (Gemini vision)
- **files** (15): `search_files`, `read_file`, `read_document`, `list_directory`, `file_info`,
  `disk_usage` are AUTO; `write_file`, `edit_file`, `move_file`, `copy_file`, `delete_file`,
  `create_folder`, `organize_folder`, `compress`, `extract` need confirmation.
- **core** now also has `set_reminder`, `list_reminders`, `cancel_reminder`.
- **system** (17): `open_app`, `list_running_apps`, `list_windows`, `focus_window`, `system_info`,
  `get_clipboard`, `set_clipboard`, `get_volume`, `set_volume`, `mute`, `media_control`, `notify`,
  `open_url`, `take_screenshot` are AUTO; `close_app`, `lock_screen`, `power_action` confirm.
- **shell** (1): `run_powershell` — always CONFIRM, with a deny-list backstop.
- **web** (8): `web_search`, `search_news`, `fetch_page`, `browser_open`, `browser_read` are AUTO;
  `download_file`, `browser_click`, `browser_type` confirm — a click can submit or purchase.
- **gui** (7): `click_text`, `click_at`, `type_text`, `press_keys` confirm; `scroll`,
  `wait_for_text`, `locate_on_screen` are AUTO. Drives **any** app, including ones with no API.

Reminders (`set_reminder`, `list_reminders`, `cancel_reminder`) live in `core/reminders.py`:
a daemon thread polls SQLite, so they survive a restart. `parse_when` accepts ISO 8601 or
"in N minutes" and **refuses vague phrasing** rather than guessing — a reminder that fires at the
wrong time is worse than one that is refused. The model is told to call `get_datetime` first and
compute the absolute time itself.
Tools default to `Tier.CONFIRM` when unspecified — a tool author who forgets to think about safety
gets the cautious behaviour, not the dangerous one.

## Tools on demand

Harsh chose this on 2026-09-17 over failing over to Gemini (which would spend the vision budget)
or waiting visibly (no faster).

**Why.** Sending all 59 tool schemas cost ~3,750 input tokens per request. Groq's free tier allows
8,000 tokens/minute, so only about two agent turns fit in a minute, and the Groq SDK then retries
*silently* (`max_retries=2`): turns stalled ~28s each while reporting success. The argument
schemas cost twice what the descriptions do (~3,090 vs ~1,450 tokens), so trimming descriptions
was the wrong lever.

**How.** `registry.enable_lazy_loading(base={"core"})` in `main.py`. Only core tools go up front,
plus a `load_tools` catalog: one line per group, with the group names as an enum so Groq rejects a
group that does not exist. Loaded groups stay loaded while used and **expire after 3 tasks unused**
— necessary because the HUD keeps one long conversation, and without expiry every group would
accumulate and requests would grow back to full size. Each group's catalog line is set with
`reg.describe_category()` in the module that owns its tools; **keep those lines accurate**, since
the model decides what to load from them alone. Lazy loading is opt-in on the registry, so tests
that build a registry directly still see every tool.

**Measured.**

| | before | after |
|---|---|---|
| sent up front | 59 tools, ~4,700 tok | 9 tools, ~780 tok |
| a GUI task (gui + screen loaded) | 59 tools | 19 tools, ~2,000 tok |
| "time + reminders" task | **58.5s** (1.1 / 28.8 / 28.5) | **2.9s** (1.9 / 0.5 / 0.4) |

Verified live: the real model calls `load_tools` with the right group itself (`system` for
battery, `web` for a search). Sustained back-to-back tasks can still exhaust the minute's tokens —
one web answer stalled 9.9s — so this is much rarer, not gone.

**Groq rejects a call to a tool not in the request** with a 400 naming it: `attempted to call tool
'search_files' which was not in request.tools`. The adapter raises `ToolNotLoaded`, the router
**re-raises it without failing over** (Gemini would reject the same request and only burn quota),
and the agent loads that tool's group and retries, at most twice. Earlier calls to unloaded tools
in the *history* are accepted — verified — so long conversations are safe.

**Observed, not yet addressed:** asked who won the most recent Cricket World Cup, the model answered
"England (2023)" from a search snippet without opening a page. Australia won in 2023. Answer
quality from snippets is unreliable; worth a system-prompt nudge to read a source for factual
claims.

## The stack — all verified working on this machine

| Layer | Choice | Notes |
|---|---|---|
| Reasoning | Groq `openai/gpt-oss-120b` | **primary** — ~1,000 req/day, ~0.5s |
| Overflow | Gemini `gemini-3.6-flash` | only 20 req/day/model, so second not first |
| Vision | Gemini Flash image input | **the only free provider that can see**; ~120 calls/day total |
| Screen text | **`winsdk` → `Windows.Media.Ocr`** | verified 13ms, zero install, **returns per-word boxes** |
| STT | Groq `whisper-large-v3-turbo` | free, 20 RPM / 2,000 RPD, separate quota pool |
| STT offline | `winsdk` → `SpeechRecognizer` | built into Windows |
| TTS | `edge-tts` | free, no API key, Microsoft neural voices |
| TTS offline | `winsdk` → `SpeechSynthesizer` | built into Windows, 3 voices |
| Memory search | SQLite **FTS5** | already inside Python, no embedding model |
| Summon | global hotkey via `keyboard` | **Ctrl+Alt+Space**, configurable as `hotkey` |
| Wake word | *not shipped* | see "Wake word" below |
| Search | DuckDuckGo | no key, no quota |

Dev environment: `.venv/` in the repo root (gitignored). Python 3.11.9.

Tests:
- `tests/test_phase1.py` — 45 checks, **no API key needed** (settings round-trip, both adapters'
  message/tool conversion, router failover).
- `tests/test_phase2.py` — 72 checks, no API key needed (schema generation, argument coercion,
  permission tiers, memory + FTS5, and the agent loop driven by a scripted provider).
- `tests/test_phase3.py` — 70 checks, no API key needed (path guards, every file tool, Recycle
  Bin semantics, zip-slip refusal, and that no hard delete is ever called).
- `tests/test_phase4.py` — 57 checks, no API key needed (real read-only system calls, gating of
  disruptive tools, critical-process refusal, and 21 PowerShell deny-list categories).
- `tests/test_phase5.py` — 37 checks; search and fetch hit the real network (both free).
- `tests/test_phase6.py` — 39 checks; real synthesis and transcription round trips. The Nepali
  assertion deliberately only requires Devanagari back, recording the real accuracy rather
  than an aspiration.
- `tests/test_phase7.py` — 47 checks, runs Qt **offscreen** against a scripted agent: real worker
  thread, the cross-thread confirmation handshake (reject / approve / approve-all), error state,
  and quitting while a prompt is open. No window appears.
- `tests/test_phase8.py` — 56 checks, no screen touched: vision box parsing, coordinate mapping
  (including negative multi-monitor origins), match ranking and ambiguity refusal, fuzzy OCR
  matches, and that a blocked command typed into a terminal is refused.
- `tests/test_phase8_live.py` — 23 checks that **really move the mouse and type** (~30s, hands off).
  Drives `tests/gui_target.py`, a separate app that records what happened to it. The only test
  that proves an OCR position becomes a click on the right pixel at real display scaling.
- `tests/test_lazy_tools.py` — 35 checks, no API key: what goes up front, loading, expiry,
  permission tiers still enforced, Groq's real rejection text, the router not failing over,
  agent recovery, and the size saving measured on the real 59 tools.
- `tests/test_phase9.py` — 79 checks, Qt offscreen with **fake services**: never touches the network,
  the real credential store, audio, or the real config (APPDATA points at a temp folder). Covers
  every setup page, key checks off the UI thread, keys found in .env, abandoning setup, hotkey
  rules, and Settings applying changes to the running app without a restart.
- `tests/test_fixes.py` — 34 checks guarding bugs that actually shipped: images surviving the
  agent loop, database migration, reminders, multi-word screen matching, region parsing.
  Add a `test_phaseN.py` per phase and keep them key-free.
- `tests/smoke_live.py` — 26 checks against the real API, needs Gemini + Groq keys, spends ~15
  free requests (mind the 20/day/model Gemini cap when re-running).
  Covers what fakes cannot: real wire formats, the tool-call round trip, vision, and the
  cross-provider handoff that proves locally-held history survives a failover mid-conversation.
  **Run this after touching any adapter.** Every trap listed below was found here and by nothing else.
Hardware: RTX 3050 Laptop 4GB, Ryzen 7 6800HS, 15.3GB RAM — enough for local *perception*,
not for local *reasoning*. That split is the whole reason $0 works.

## Verified facts — do not re-research these

- **Groq's Llama models are Enterprise-only.** `llama3-70b-8192` and `llama-3.3-70b-versatile` are not
  on the free tier. Free tier is gpt-oss, Compound, Qwen, Whisper. The legacy `Backend/*.py` files
  still reference the dead model.
- **`google-genai` 2.23.0** exposes both `client.models` (stateless) and `client.interactions`
  (server-side state). **Use `models.generate_content` with full local history** — server-side state
  cannot survive a failover to Groq mid-conversation.
- **Gemini free tier is 20 requests per day, PER MODEL.** Measured from a live 429 on 2026-09-16:
  `quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier, quotaValue: 20`. Not the 250-1,500
  that third-party sources claim. Two things follow, both verified:
  - The quota really is **per model** — `gemini-3-flash-preview` answered fine while
    `gemini-3.6-flash` was exhausted. Rotating across the ~6 working ids turns 20/day into ~120/day,
    which is why the adapter demotes a model on 429 and moves to the next instead of failing.
  - It really is **per day** — after waiting 65s an exhausted model still 429'd. The response's
    shrinking "retry in 25s -> 17s" countdown is misleading; ignore it for quota errors.
- **Groq carries the volume; Gemini is the eyes.** Groq allows ~1,000 requests/day per model at
  ~0.4-0.5s, versus Gemini's 20. So `provider_order` is `["groq", "gemini"]`. Gemini stays essential
  because it is the only free provider that can see, and the router sends `needs_vision` past
  providers that cannot. Practical ceiling: roughly 200 text tasks/day, and **~120 vision calls/day**
  — vision is the genuinely scarce resource, so use Windows OCR first and Gemini only when the
  question needs seeing rather than reading.
- **Optional tool arguments must be typed `[X, "null"]`.** Models routinely emit
  `"region": null` for an argument they mean to omit. Groq validates tool arguments
  server-side and rejects that against a plain `"string"`; Gemini tolerates it. Tested all three
  dialects: only the **type-list form is accepted by both** — `"nullable": true` fails on Groq
  exactly like a plain type does. `build_schema` does this automatically for any parameter with a
  default, and `_coerce` treats an explicit null as "omitted" while rejecting null for a
  *required* argument.
- **Force UTF-8 on stdout before printing anything.** The Windows console is cp1252 and cannot
  encode Devanagari, so every Hindi and Nepali reply raises `UnicodeEncodeError` — it even choked
  on a narrow no-break space in an English reply. `main.py` reconfigures both streams at import.
  Any new entry point must do the same.
- **Groq Compound cannot call your tools.** `groq/compound` and `compound-mini` return
  `400: tool calling is not supported` — their agentic tooling is built in, not yours. Tool-capable
  free ids: `openai/gpt-oss-120b`, `openai/gpt-oss-20b`, `qwen/qwen3.8-27b`. `allam-2-7b` cannot
  either.

### Gemini, measured live on 2026-09-16 with a real free-tier key

Four traps, all of which cost real debugging time. The adapter handles each — do not undo them.

1. **A listed model is not a callable model.** `models.list()` advertises 41 ids. `gemini-2.5-flash`,
   `gemini-2.5-pro` and `gemini-2.5-flash-lite` all return **404 "no longer available"**.
   `gemini-flash-latest`, `gemini-3.7-flash` and `gemini-3.8-flash` **hang until timeout (504)**.
   Every `pro`, `omni` and `nano-banana` id returns 429 — not on the free tier.
   Verified working: **`gemini-3.6-flash`** (~1.3-1.8s, the default), `gemini-3-flash-preview`,
   `gemini-3.5-flash`, `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`, `gemini-flash-lite-latest`.
2. **Set an HTTP timeout or the app hangs forever.** The SDK has no default. `HttpOptions(timeout=ms)`.
3. **Thinking is charged against `max_output_tokens`.** With `max_output_tokens=32` and default
   thinking, Gemini 3.x returns **empty text** with `finish_reason=MAX_TOKENS` — it spent the whole
   budget thinking. It also burned ~115 thinking tokens on a one-word reply. Hence
   `thinking_budget: int = 0` by default and a 256-token floor on output. Empty text plus MAX_TOKENS
   is raised as a clear error rather than returned as a silent empty reply.
4. **Gemini 3.x requires `thought_signature` when replaying tool calls.** Rebuilding a `function_call`
   part without the signature the model returned gets a **400 INVALID_ARGUMENT**. It lives on the
   `Part` (as `bytes`), not on the `FunctionCall`, so read `candidates[0].content.parts` rather than
   the `response.function_calls` convenience list. It rides in `ToolCall.meta`.

503 "high demand" happens in normal use, so a transient failure advances to the next preferred model
immediately instead of failing the turn.
- Windows OCR word boxes mean **"click Save" needs no model call** — a local lookup plus a mouse move.
  This makes most GUI control free.

### PyQt5 traps — each was a hard native crash, not an exception

Found by bisection in Phase 7. A native crash kills the process with no Python traceback (exit
127 from bash, nothing from `faulthandler`), so these are expensive to rediscover.

1. **`QPainterPath.addEllipse(QRect)` crashes.** Always pass `QRectF(rect)`. `QPainter.drawEllipse`
   accepts a `QRect` fine; only the path method does not.
2. **Never name a signal `event`.** It shadows the virtual `QObject.event()`, which Qt calls for
   every delivered event — `moveToThread` sends a ThreadChange event straight into it. The same
   applies to any QObject member (`thread`, `parent`, `timerEvent`, ...). `test_phase7.py` fails
   if a Bantu class shadows one.
3. **`setFocus()` on a widget that is not shown yet is silently ignored.** Defer it with
   `QTimer.singleShot(0, widget.setFocus)`. The confirm prompt's focus fell to the panel's close
   button until this was fixed; it now lands on **Reject**, so a stray Enter is the safe choice.

Stylesheets must target object names (`QFrame#shell`), never a bare `QFrame{}`: `QLabel` and
`QScrollArea` both subclass `QFrame`, so a class selector restyles every label inside.

### GUI control — measured on this machine, 2026-09-17

1. **Display is at 125% scaling, and an unaware process mixes two pixel spaces.** Screenshots come
   back physical (1920x1080) while window rectangles and the cursor come back scaled (1536x864).
   OCR would find "Save" at physical (1000, 600) and the click would land at physical (1250, 750).
   `main.py` calls `gui.ensure_dpi_awareness()` before anything touches the screen. pyautogui
   happens to fix this on import, which is exactly why it must not be relied on: correctness
   would depend on import order.
2. **Windows OCR misreads small UI text, and each scale misreads different words.** At 1x it read
   "Duplicate" as "Dupicate" and "Show later" as "Show Ster"; 2x fixed one and broke another; 3x
   fixed both but misread a list row. So `click_text` reads at **1x and 3x and merges**, then
   ranks exact > partial > fuzzy (0.8 similarity). Merging also matters for safety: if one pass
   misses the second "Delete" button, the other still sees it and the ambiguity refusal fires.
3. **An exact match must stand alone.** "Save" inside a "Save As" title was an exact match; if
   OCR missed the real Save button, that title would have been clicked. A neighbour closer than
   word spacing now demotes a match to partial.
4. **Ambiguity is refused, never guessed.** Two "Duplicate" buttons produce an error listing both
   positions; the model must call again with `occurrence`.
5. **Bantu never operates itself.** Its own windows are excluded from OCR matches, a click that
   would land on one is refused, and in the HUD the panel hides before any capture via a
   `BlockingQueuedConnection`, so the worker waits until it is really gone.
6. **Typing into a terminal goes through the `run_powershell` deny-list**, otherwise
   `type_text` + Enter would be a way around it. Known gap: VS Code's integrated terminal runs
   inside `Code.exe` and cannot be told apart from the editor, so it is not covered.
7. **pyautogui's scroll units are 1/120 of a notch.** `scroll(5)` barely moves; multiply by 120.
8. **Vision boxes are `[ymin, xmin, ymax, xmax]` normalised to 0-1000** — y first. Verified live:
   Gemini located a red circle inside its radius.
9. **No synthetic Alt tap to take focus.** The common trick opens the menu bar in Notepad and VS
   Code, so typed text triggers menu items. `activate()` attaches input queues instead, then
   falls back to minimise-and-restore.

Verified end to end: Bantu's real agent, given "click the Launch probe button, then type Bantu was
here", chose `click_text` -> `click_text` -> `type_text` itself and the target app recorded
exactly that. Moving the mouse into a screen corner aborts any action (pyautogui fail-safe).

## First-run setup and Settings

**Setup runs when** `settings.onboarded` is false or no key is configured (`cfg.needs_onboarding`).
Keys found in `.env` or the credential store are **tested automatically** when setup opens; stored
does not mean working. Nothing is saved until the last page, and finishing moves keys into Windows
Credential Manager. Harsh's own machine still has keys only in `.env` and no `config.json`, so his
next launch shows setup, prefilled.

**Key checking** (`core/providers/validate.py`) lists models rather than generating anything, so it
costs no generation quota — which matters for Gemini's 20/day. It distinguishes a rejected key
from being offline. Verified live 2026-09-17: both real keys pass (Groq 13 models, Gemini 58);
well-formed fake keys are rejected by the real services; blank and pasted-with-spaces keys are
caught before any network call. **Do not validate by key prefix**: Harsh's working Gemini key
starts `AQ.`, not the long-standing `AIza`.

**Settings apply without a restart.** The dialog reports what changed; the app re-registers a new
hotkey, swaps providers into the *same* router object via `ProviderRouter.replace()` (tools such as
`look_at_screen` captured that object at registration), and recalibrates a newly chosen
microphone. A changed key must pass its test before Save is allowed; unchanged stored keys are
never re-checked just by opening Settings.

**Hotkey rules** (`validate_hotkey`): must include Ctrl, Alt or Windows. Shift alone is refused —
Shift+A is how people type a capital A, and a global hook on it would swallow typing everywhere.
Ubiquitous shortcuts (Ctrl+Space, Ctrl+C/V/X/Z/S, Alt+Tab, Alt+F4) are refused.

The default `username` was hard-coded to "Harsh" — every other user would have been called that.
It is now asked during setup, and the agent says "the user" when it is blank.

## Language and voice

Bantu must speak **English, Hindi and Nepali**. TTS is `edge-tts` — free, no API key, no download.

**Settled — Harsh chose these by ear from real samples. Do not substitute.**

| Language | Female | Male |
|---|---|---|
| English | `en-US-AvaMultilingualNeural` | `en-GB-RyanNeural` |
| Hindi | `hi-IN-SwaraNeural` | `hi-IN-MadhurNeural` |
| Nepali | `ne-NP-HemkalaNeural` | `ne-NP-SagarNeural` |

Per-language native voices, chosen over reusing one pair everywhere. Voice is selected by the language
Bantu is *replying in*, and the female/male pair are alternatives the user switches between in Settings
— they never speak together, so the mixed en-US / en-GB pairing is intentional and harmless.

Render all six at `rate="+4%"`; plain default rate sounds fractionally sluggish.

> The legacy `Backend/Chatbot.py` system prompt says *"Reply in only English, even if the question is
> in Hindi."* **That rule is reversed.** Bantu replies in whatever language the user used.

**Measured 2026-09-16, speaking each line with edge-tts and transcribing it back through
Groq Whisper:**

| Language | Speech out | Speech in |
|---|---|---|
| English | perfect | **perfect** |
| Hindi | perfect | **perfect** |
| Nepali | perfect | **poor** |

Nepali comes back with mangled word boundaries — `मेरो पुराना फाइलहरू मेटाऊ र` became
`मेरो पुराना फाइल हरु मे ताउर`, and `भोलि बिहान नौ बजे मलाई सम्झाऊ` became near-gibberish.
**Passing `language="ne"` does not help** — tested, identical output. So Bantu speaks Nepali well
and understands spoken Nepali badly. Prefer typing for Nepali, especially for anything with a
number in it such as a reminder time. There is no free alternative; Windows `SpeechRecognizer`
has no Nepali pack either. This is a genuine limitation, not a bug to chase.

## Wake word

**Not shipped, deliberately.** openWakeWord's pretrained models are `alexa`, `hey_mycroft`,
`hey_jarvis`, `hey_rhasspy`, `timer` and `weather` — **there is no "Bantu" model**, and
`hey_jarvis` is Marvel's trademark. A custom "Bantu" wake word means training a model with
openWakeWord's synthetic-data pipeline: its own project, post-v1.

The global hotkey delivers the same "always there" behaviour today, instantly and with nothing to
mishear. It is **Ctrl+Alt+Space, not Ctrl+Space** — Ctrl+Space is IntelliSense in VS Code and the
input-method switch on Windows, so it would fire constantly.

## Safety model

Three tiers, enforced in the registry **before execution**:

- **auto** — read-only or trivially reversible; runs silently
- **confirm** — changes disk, process or website state; shows exact arguments first
- **blocked** — never runs

Blocklist: credential stores (`.env`, `id_rsa`, browser profiles), registry writes, System32 and
Program Files writes, disk formatting, Defender/firewall changes, and **Bantu's own config and keys**.
Enforced in two places: `platform_desktop/paths.py` for filesystem tools, and the `DENY` list in
`platform_desktop/shell.py` for PowerShell. `close_app` additionally refuses a CRITICAL process list
(lsass, explorer, winlogon, ...) even when the user approves.
That last one matters most — without it, "set all tools to auto" is a valid tool call and the whole
model collapses.

Deletes go to the Recycle Bin, never a hard unlink — `platform_desktop/paths.py` enforces the
blocklist and `files.py` contains no `os.remove`/`rmtree`/`unlink` call at all, which
`test_phase3.py` asserts against the source. `organize_folder` defaults to a dry run, and `extract`
refuses zip-slip paths.

## Conventions

- **Commit after each completed phase.** Not one big commit at the end.
- **No `Co-Authored-By` trailer.** Ever.
- **Never `git push`.** Harsh pushes himself.
- Secrets never enter the repo. Keys live in Windows Credential Manager via `keyring`.
- Runtime state goes to `%APPDATA%\BantuAI\`, never beside the executable.

## Open decisions

- ~~Throughput on Groq's free tier~~ — **closed.** Tools load on demand; see "Tools on demand".
- ~~Voice selection~~ — **closed.** All six voices chosen; see "Language and voice".
- **Android APK** — wanted eventually, but ~35 of 41 tools are meaningless on a phone and a remote
  client needs a reachable core, which conflicts with "no server". Unresolved; v2 conversation.
- **Code signing** — the `.exe` ships unsigned, so users see a SmartScreen warning. Certificate is a
  purchase decision.

## Legacy code

All gone. The old `Backend/` intent-router modules were removed once their replacements landed;
git history keeps them. `Frontend/Files/*.data` (the old PyQt file-based IPC) and
`Data/ChatLog.json` are untracked.

`Frontend/Graphics/` assets (including `Jarvis.gif`) survive and are reused by the Phase 07 HUD.

**Resolved:** the ~15MB Selenium driver is within Harsh's accepted download size, so the browser
tools ship. They stay lazy anyway — nothing is fetched until one is used, and a missing driver
explains itself rather than crashing.
