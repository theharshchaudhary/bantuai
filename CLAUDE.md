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

**Now: readiness work before packaging** (Harsh, 2026-09-17, chose all four): stress test first
(**done**, see "Stress test"), then trust (correct and robust), feel (faster, streamed replies,
past chats, new-chat button), and more abilities (image generation if Pollinations.ai is really
free, a knowledge folder).

**Roadmap after readiness** (Harsh, 2026-09-17, adapting his earlier "AI executive personal assistant"
plan to Bantu's constraints; he approved all four blocks). In order:

| Step | What | Notes |
|---|---|---|
| R1 | Readiness polish | **done** — faster speech instead of streaming, long chats fit Groq, past chats, new-chat button |
| R2 | Memory + tasks | **done** — structured records (commitments, decisions, action items, people, deadlines) in SQLite + FTS5; answers **cite date and source and say "no record" rather than guess**; tasks with overdue follow-up via the scheduler; knowledge folder; local audit log of approved/declined/blocked actions |
| R3 | Daily briefing + calendar | **done** (calendar tools still need a live model run: `stress_real.py --only=calendar_add,calendar_list`, quotas were spent); briefing **only when asked** and weather for a city set in Settings (Harsh, 2026-09-17); spoken morning briefing; local events plus Google Calendar's read-only secret iCal address (no OAuth); personality: **warm professional** by default, tone changeable in Settings, time-of-day greeting |
| R4 | Meeting notes | explicit start/stop with a visible indicator; chunked Groq Whisper transcript; summary, decisions, action items into memory; **transcript-only by default** (audio deleted), retention and delete controls; summaries state what was said with times, never judgments about people |
| R5 | Trust layer + wake word | Windows Hello (`UserConsentVerifier`) for chosen sensitive actions; Activity view in Settings; retention settings; wake-word spike on Windows' built-in offline recognizer, falling back to a ~2MB custom openWakeWord model |

Spikes to run before building on them: Groq Whisper long-audio limits, Windows Hello from a desktop
Python process, built-in keyword-spotting accuracy, the iCal feed. **Email: decide later** (Gmail via
IMAP app password is the likely $0 route; Outlook needs an app registration). Image generation via
Pollinations.ai is still unverified.

Adopted from that plan but already true in Bantu: risk tiers enforced in code, no financial actions.
Rejected, with reasons: a server, web dashboard and mobile app (no-server rule); voice-based speaker
verification and diarization (no $0 option without large downloads; Windows Hello verifies better);
vector search (embedding download, or sending all memory to Google); always-on ambient recording.

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
  records.py            commitments, decisions, action items, tasks, notes, people
  knowledge.py          the knowledge folder: index, poll, search tools
  activity.py           local log of approvals, refusals, blocks and failures
  weather.py            Open-Meteo forecasts (no key)
  briefing.py           "brief me": date, weather, calendar, reminders, due and overdue
  events.py             calendar: Bantu's own events + Google Calendar read-only (not calendar.py: stdlib name)
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

**70 tools**, but **only the core group is sent up front** — see "Tools on demand". `/tools` lists
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
- **notes** (4, portable, `core/tools/notes.py`): `note`, `find_notes`, `update_note` are AUTO;
  `delete_note` confirms. See "Structured memory".
- **knowledge** (2, portable, `core/knowledge.py`): `search_knowledge`, `list_knowledge`, both AUTO.
- **weather** (1, portable, `core/weather.py`): `get_weather`, AUTO. **core** also has `daily_briefing`.
- **calendar** (3, portable, `core/events.py`): `add_event`, `list_events`, `cancel_event`, all AUTO
  (Bantu's own local events, like reminders).

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

## Stress test — 2026-09-17 baseline

`tests/stress_real.py`, first run: **22/26 passed in 14.4 min.** Findings, worst first; fixes are
marked as they land, each pinned by `tests/test_readiness.py`.

1. **A decline made Bantu try another way to do the same thing.** The registry's own decline
   message said *"Do not retry it; try another way."* Declined `delete_file` -> it tried PowerShell
   `Remove-Item -Force`, then opened File Explorer and tried `press_keys` and `click_at`, hitting the
   12-step cap after 276s. Declined `power_action` -> it tried `shutdown /s /t 0`, then asked "Do you
   want me to shut down the computer now?" Every attempt was stopped by the confirm tier, so nothing
   happened, but this is a trust failure. A "no" must end the attempt, enforced in code.
   **Fixed.** A decline now ends the request in `Agent.run`: later calls in the same step are
   skipped (each still gets a result, or the next request is malformed), and the closing reply is
   requested with **no tools offered**, so there is nothing to try another way with. A call made
   anyway is ignored, and a provider failure falls back to "Okay, I didn't run X." Verified live
   that both Groq and Gemini accept a no-tools request after a real tool call, Gemini's signed
   call included. Live: declined delete (English and Hindi) and shutdown each asked once and
   stopped. Two wording traps found on the way: "in Harsh's language" made the model answer an
   English request in Hindi (it guessed from the name), so the prompt says "the same language as
   their latest message"; and Gemini told a Nepali user a "security policy" blocked it, so the
   closing prompt forbids blaming a policy.
2. **Sustained use stalls 10-30s per turn: Groq's 8K tokens/minute.** Turn latency tracked request
   size almost exactly: the bucket refills ~133 tok/s, so a 3,400-token request waits ~25s (measured
   24.9s, 24.9s, 21.9s) and a 2,000-token one ~15s (14.3s, 14.9s). First five tasks: median 0.7s per
   turn. After that: median 10.8s. The Groq SDK hides this by retrying silently.
   Measured from response headers: **each Groq model has its own bucket** (`gpt-oss-120b`,
   `gpt-oss-20b` and `qwen/qwen3.8-27b` each 8,000 tok/min and 1,000 req/day), so the other two sat
   idle during every stall. `max_tokens=2048` reserves only ~330 extra tokens against the minute, not
   the full 2,048. Loaded groups also inflate requests: after the declined delete loaded
   files+shell+gui+system, the next three tasks each sent 49 tools (~3,200 tokens) whether needed or not.
3. **Confidently wrong from not reading.** Asked the combined total of two invoice files, it listed the
   folder and answered "98 bytes" without opening either file.
4. **Turn-cap message is empty.** "I stopped after 12 tool steps without finishing. Here is where I got
   to: " followed by nothing, because the last response held only tool calls.
5. **Wasted steps.** A battery question in romanized Nepali called `read_screen` first (the screen group
   was still loaded) and cost an extra ~20s turn; it then replied in English. "Look at my screen and
   describe it" used OCR text, not vision: defensible given the vision budget, but the description
   was a guess from words.

   **3-5 fixed.** The system prompt now says to open and read a file, document or page before saying
   what it contains, never inferring from names, sizes or snippets; to prefer dedicated tools over
   `run_powershell`; and to reply in the language *and script* of the latest message (romanized
   Nepali gets romanized Nepali). The turn cap asks for a summary with no tools offered (what got
   done, what is left, ask to continue), sharing `_closing_reply` with the decline path; its
   fallback names the cap and the tools that ran. Tool groups now expire after **one** unused
   request (`keep_for_tasks=1`), so a group stays for a follow-up and then goes.
   Catalog lines matter most for the weaker fallback models, which see nothing else before loading:
   `gpt-oss-20b` said it had "no way to shut down the computer" without loading anything, and qwen
   used PowerShell for battery because the system line said only "system info". The system line
   now lists battery, CPU, memory, disk and uptime, and restart/shut down; the shell line says
   "only when no other group has a tool"; the load hint says to check the list before saying it
   cannot do something. Re-run twice on 20b/qwen: shutdown, battery, romanized Nepali all pass.

**Second full run, 2026-09-17 afternoon: 24/27 in 6.6 min** (baseline 22/26 in 14.4 min). Median turn
3.3s (was 10.8s). `gpt-oss-120b` had spent its day, so 59 of 67 turns ran on `gpt-oss-20b` and qwen.
Now correct: invoice total read from both files (12,000 NPR), the Cricket World Cup answered after
searching *and reading a page*, "look at my screen" used vision and described it accurately. The
three failures (read_docx grader missing a U+202F space, shutdown, romanized battery) were fixed
above. Sustained back-to-back use still waits: 22 turns over 8s, all visible countdowns.
6. **Once `gpt-oss-120b` runs out, no tool task can finish** (found re-testing fix 1). Two bugs:
   - The Groq adapter never tries its other models. Groq's published free limits are **per model**:
     30 req/min, 1,000 req/day, 8K tok/min and **200K tok/day** each for `gpt-oss-120b`,
     `gpt-oss-20b` and `qwen/qwen3.8-27b`. The daily *token* cap binds first: two stress runs
     (~150K input tokens each) exhausted `gpt-oss-120b`, and the router then benched all of Groq
     (a 497s cooldown) while the other two models had their full day unused. The per-minute 429
     reads "on tokens per minute (TPM) ... Please try again in 14.85s" with `retry-after: 15`.
   - **Memory drops `ToolCall.meta`**, so Gemini's `thought_signature` is lost the moment history is
     re-read, which the agent does every turn. Gemini then rejects its *own* previous call:
     `400 Function call is missing a thought_signature`. Any multi-step tool task on Gemini fails,
     and so does any failover after Groq made a call. `smoke_live.py` never caught it because it
     calls providers directly, not through `Memory`.
   - Also found on the way: **`thinking_budget=0` makes the lite Gemini models refuse every request**
     with a bare `400 Request contains an invalid argument` that never mentions thinking, so
     rotating onto them always failed.

   **Fixed** (all verified live 2026-09-17):
   - Groq: `max_retries=0` (no silent waits); a model at its limit rests for Groq's `retry-after`
     and the next model takes the same request; when all rest, `RateLimited` carries the soonest
     wait. Live: with `gpt-oss-120b` out for the day, `gpt-oss-20b` and qwen answered in <1s.
   - Memory stores `ToolCall.meta` (bytes as base64). Gemini replays a call with no signature using
     Google's placeholder `skip_thought_signature_validator` (verified: stripped -> 400, placeholder
     -> accepted; a signature from one Gemini model is also accepted by another).
   - Gemini retries a model once without the thinking budget when refused, remembering it per model
     only if that fixed it. A 429 now **rests** the model (1h for `PerDay`, else `retryDelay`)
     instead of dropping it for the session, and Gemini returns to its best model once rested.
   - Live: a 3-step tool task on Gemini alone through Memory, and Groq starting a task then dropping
     out with Gemini finishing it, both complete. Re-run of the stalled tasks: no turn over 5s.

   - **Short limits are waited out, visibly, not spent on Gemini.** With SDK retries off, a brief
     all-Groq rest had started sending plain text to Gemini, spending the vision budget. The router
     now waits for a provider that frees up within `max_wait_s` (20s), at most `MAX_WAITS` (2) times
     per request, then fails over. Vision requests never wait for a provider that cannot see, and
     a disabled provider is never waited for. The agent emits a `waiting` event with the seconds;
     the HUD counts down ("free limit reached · trying again in 12s") and the CLI prints a line.
     `router.interrupt()` ends a wait at once and refuses new ones, and the HUD calls it on quit,
     so a wait cannot outlast the 3s thread join. Windows timers wake a few ms early, which left the
     provider still resting and burned a second near-zero wait, hence `WAIT_SLACK_S`.
     Live, 11 heavy tasks back to back: 28 of 29 turns on Groq's three models, **one** on Gemini
     (after the wait budget ran out), waits mostly 1-6s, 2.0 min total (these tasks took over 6 min
     in the baseline).

   **Still open:** weaker fallback models (qwen) reach for `run_powershell` to list files, and a
   decline then ends the task, so the prompt should say to prefer dedicated tools.

Solid: capital, time, remember/recall across conversations, reminder set/list/cancel, create/edit a
file, read a .docx, dry-run organize, a 4-step read-and-summarize, example.com heading, battery and
volume match the real values, Hindi and Nepali replies in the right language, refusing Defender,
asking "what should I delete?" for a bare "Delete it". `.env` was blocked in code; no key leaked.
Cricket World Cup 2023 answered correctly (Australia), from knowledge, no search.

## Feel: speech and long conversations (R1, 2026-09-17)

**Reply streaming was dropped, by measurement.** Streaming from the API was the planned "feel" fix,
but Groq finishes a whole reply in 0.2-0.4s (60 chunks arrive together) and Gemini in ~1.5s (4
chunks). Streaming would not show words any sooner, and it complicates failover. The real delays
were elsewhere:

- **Speech froze the HUD.** `Speaker.speak` synthesized the entire reply with edge-tts *before* playing
  anything, on its caller's thread, which in the HUD is the UI thread. edge-tts measured 1.1-1.2s
  for a short line but up to **9.6s** for a 66-word reply. Now `speak` returns at once (15ms; 265ms
  on the first call while pygame's mixer starts). A background thread speaks sentence by sentence
  while another synthesizes ahead, so the first sentence starts in ~1.4-1.9s whatever the length
  (measured live, muted). `split_sentences` joins pieces under 40 chars and splits on the Devanagari
  danda too. `stop()` bumps a generation counter: every thread of the old utterance exits, and a
  sentence already synthesizing never plays. `is_speaking()` is true from `speak()` until the last
  sentence ends, so the orb stays in its speaking state through the gaps. The pygame calls sit behind
  `_mixer_play/_mixer_busy/_mixer_halt` so tests run the real threading with a fake player.
- **A long conversation could not fit in one Groq request.** Memory sent up to 8,000 tokens of history,
  and Groq refuses any single request over its 8,000 tokens/minute: `413 Request too large ... Limit
  8000, Requested 10091`, with a body that *also* says `rate_limit_exceeded` and a `retry-after: 16`.
  It was classified as a rate limit, so every model rested, the router waited twice, then Gemini took
  it - on every request of a long HUD conversation. Now: `RequestTooLarge` is its own error (checked
  before rate limits), Groq raises it without trying its other models (same cap) and rests nothing,
  and the router fails over at once without waiting. The agent sizes history per request so system
  prompt + tools + history stay under `REQUEST_TOKEN_TARGET` (6,500, estimated pessimistically), and
  Memory never trims the latest user message or anything after it. Live: a 22,580-token stored
  conversation went to Groq as 4,415- and 4,934-token requests and still recalled a codename given a
  few messages earlier.

**Past chats.** Opening Bantu within `RESUME_WITHIN_S` (6h) of the last message reopens that chat and
shows its transcript (`Memory.transcript`: user messages and replies with words, never tool
plumbing); after a longer gap it greets and starts fresh. The panel header has **New chat** (+) and
**Earlier chats** (clock): a menu of up to 15 chats, most recently *active* first
(`recent_conversations` now orders by last message and skips empty conversations), titled by their
first message with a relative time, the open one ticked. Switching changes the agent's context too,
since it is the same `Memory.conversation`. Both buttons are disabled while a request runs -
switching mid-request would file the reply under the wrong chat. Header icons are drawn in code
(`widgets.glyph`) so they match the panel; the old clip-art gear was replaced. The user's own
bubbles are indented with a teal tint so a reopened transcript reads as a conversation - via a
stylesheet margin, because alignment makes a word-wrapped label shrink to its narrowest line.

## Structured memory (R2, 2026-09-17)

`core/records.py` keeps **commitments, decisions, action items, tasks, notes and people** as rows
beside the conversations in `history.db`: kind, text, people, due date, priority, status (only
commitments, action items and tasks have one), the conversation it came from, and when it was noted.
Search is FTS5 over text and people plus filters (kind, person, status, noted since/until). Every
line the tools return says when it was noted, and a search that finds nothing returns an explicit
"No records ... tell the user nothing is on record rather than guessing". The system prompt says to
note promises, decisions, tasks and deadlines, and to answer about the past only from notes, past
conversations or remembered facts, saying when. `search_history` now also gives the date and who said
it, and leaves out tool output. The reminder scheduler follows up an overdue open item **once**
(`followed_up_at`); moving its due date re-arms it.

**Traps found live, each now handled — keep them handled:**
- **`remember` swallowed promises.** It is always loaded, so "I promised Ram..." went into facts with
  "this Friday" as literal text. Its description now sends promises, decisions, tasks, deadlines and
  people to the notes tools.
- **Hindi notes were translated to English**, so a Hindi recall ("सीता") found nothing. `note` now says
  to keep the user's own words and script; `find_notes` says to try once in the other script.
- **Devanagari in tool descriptions pulled an English reply into Hindi** on `gpt-oss-20b`. Tool
  descriptions carry no Devanagari (a test enforces it).
- **Weekday arithmetic is unreliable on weak models**: "this Friday" on Thursday 17 Sep became the 16th.
  `get_datetime` now lists the next seven dates; `note` warns when a due date is already past, and
  `update_note` can move it. Live after the fix: the 18th, both runs.
- **qwen leaked its native tool-call format as text** (`<tool_call><function=save_note>...`) when offered
  no tools. `agent.clean_reply` strips it from every reply; an all-markup reply falls back.
- **Hindi and Nepali search never worked, in facts and history too.** Two layers: Python's `isalnum()`
  rejects Devanagari vowel signs, so the query builder cut "रिपोर्ट भेजनी है" down to `"जन"`; and SQLite's
  default FTS5 tokenizer treats combining marks as separators, indexing "बैंकमा" as ब / कम, so कम ("less")
  matched "in the bank". Fixed with `unicodedata` categories L/N/M in `_fts_query` and
  `tokenize="unicode61 remove_diacritics 2 tokenchars '<Devanagari marks>'"` on every FTS table
  (`memory.FTS_TOKENIZE`; Latin accents still fold). An existing database has its FTS indexes dropped
  and rebuilt from their content tables on first open, losing nothing.

Live (`stress_real.py` memory tasks, twice, on gpt-oss-20b/qwen): noting "I promised Ram the vendor
report by this Friday" (due Fri 18 Sep), recalling it in a new conversation with its date, "no record"
for an office-move decision never made, a Hindi promise to सीता stored and recalled in Hindi, "mark it
done", and open commitments listing only the open one: **13 of 14**, the miss a grader not matching
"don’t" with a curly apostrophe (fixed).

## Knowledge folder (R2, 2026-09-17)

Drop `.txt`, `.md`, `.csv`, `.pdf`, `.docx` or `.xlsx` files into **Documents\Bantu Knowledge** (the real
Documents known folder, which OneDrive often moves; `settings.knowledge_dir` overrides it) and Bantu
answers from them, naming the file. **Not under `%APPDATA%\BantuAI`**: the path guard blocks every
tool from Bantu's own folder, so Bantu could not even copy a document in. Tray menu and Settings
(About tab) open the folder and show what is indexed.

`core/knowledge.py` chunks text (~1,000 chars at paragraphs, then sentences) into FTS5 with the
Devanagari-aware tokenizer. The folder is **polled** every 30s, not watched: `watchdog` was listed in
`Requirements.txt` but never installed, and an incremental poll (compare mtime and size, re-read what
changed) needs only the stdlib. `search_knowledge` also syncs first, so a file dropped a moment ago
counts. PDF/Word/Excel go through `platform_desktop.files.extract_document_text`, the same code as
`read_document`. An unreadable, empty or oversized (25MB) file is recorded with its reason and not
re-read until it changes; Word's `~$` lock files and hidden files are skipped. Text files decode as
UTF-8, UTF-16 only with a byte-order mark (without the check, Windows-1252 bytes "decoded" as UTF-16
nonsense), else Windows-1252.

**Search tries all words first.** If no passage has every word it falls back to any word, labelled
"No passage mentions all of ... may not answer the question": "parking policy" otherwise returned the
leave policy on "policy" alone. **The catalog line must claim "their documents"**: with a vaguer line,
"according to my documents" sent the model through `search_files` eight times first (238s); after, 3
turns and ~7s. Tests and `stress_real.py` point `knowledge_dir` at temp folders so nothing lands in the
real Documents. Live, twice: the leave policy answer (18 days, 5 carry over, file named), "documents do
not cover parking", and a Hindi question answered in Hindi from a Hindi document - 6 of 6.

## Personality (R3, 2026-09-17)

`settings.tone`: **warm** (default, Harsh's choice), **playful** or **professional**, chosen in Settings
(General) and applied at once, since the agent reads it on every request. Each is one sentence in
`agent.TONES` dropped into the system prompt; an unknown value falls back to warm. The HUD opens with a
time-of-day greeting by name ("Good afternoon, Harsh. Bantu is ready."; "Hello" from 22:00 to 05:00).
Live on gpt-oss-120b to "I finally finished that report I was dreading": warm - "Great job getting that
report done"; playful - "Congrats on slaying the dreaded report"; professional - "Report completed. Let
me know if you need to schedule a review".

## Weather and the briefing (R3, 2026-09-17)

**Briefing only when asked** ("brief me"; Harsh's choice): `daily_briefing` is a core tool, so it is
always available. It gathers the date, weather, today's calendar (R3c), reminders still to come today,
and open records split into overdue / due today / other, plus decisions noted since yesterday; the
model speaks it in the user's tone. Each section fails on its own - a weather outage still briefs.

**Weather** is Open-Meteo: free, no key, verified live (geocoding ~0.7s, forecast ~0.9s). The city is
typed once in Settings (General, with a Check button that looks it up in the background); nothing is
guessed from the IP address. **Latin letters only** - the geocoder returns nothing for "काठमाडौं", and the
error says so. Places are cached for the session and forecasts for 15 minutes; the city is read when
the tool runs, so a change in Settings applies at once. `get_weather` can take any other city.

**Reply language, again.** "Brief me." in English came back entirely in Hindi from
`gemini-3.5-flash-lite`, because one noted commitment in the briefing was in Hindi. The general rule
("reply in the language of the latest message, even when tool results are in another language") was
not enough on its own, so `agent.language_hint` adds a concrete line to every request naming the script
of the latest message. After: English briefing, the Hindi item quoted as written; romanized Nepali
still answered in romanized Nepali.

**Daily quota is real.** After a full day of stress runs every free quota was spent - all three Groq
models' 200K tokens and every Gemini model - and Bantu said "Every provider is out of quota right now".
Plan heavy live testing accordingly: a full `stress_real.py` pass costs ~130K tokens.

## Calendar (R3, 2026-09-17)

`core/events.py`: Bantu's own events (add, list, cancel) plus the user's **Google Calendar, read-only,
through its "Secret address in iCal format"** - no sign-in, no OAuth app, no server. That address grants
read access, so it is stored like a key (Credential Manager, Keys tab, tested before saving; clearing it
disconnects). The feed is polled every 15 minutes into a rolling window (yesterday to +60 days) that is
replaced wholesale; a failed update keeps the last good copy and shows why. Feed events cannot be
cancelled here. `icalendar` + `recurring-ical-events` expand recurrence (with `python-dateutil`,
`tzdata`, `x-wr-timezone`: ~6.5MB, within the download rule; `tzdata` is needed for time zones on
Windows). Verified live: Google's public "Holidays in Nepal" feed (88KB, 0.7s, 14 events); weekly
RRULE with an EXDATE, UTC to Nepal time, DURATION and all-day events expand correctly. A refused private
address reads "The calendar address was refused - it may have been reset in Google Calendar."

Today's events go into the briefing. **Alerts before an event are off by default** (Harsh chose no
unprompted briefing); "Your day" in Settings can set 5-30 minutes, and the reminder scheduler announces
each timed event once. The module is `events.py`, not `calendar.py`, which would shadow the stdlib
module of that name. Settings gained a **"Your day"** tab (weather city, event alerts): on General,
seven sections crushed every input into a clipped line.

**A PyQt trap found in tests, now handled:** `test_phase7.py` crashed natively (exit 127, no traceback,
all buffered output lost) in 5 of 6 runs once R3c added one attribute to `BantuApp` - but only with
stdout redirected to a file. Cause: each `hud, ran = make(...)` rebinding let Python destroy the previous
HUD's Qt objects mid-run in arbitrary order. Keeping every HUD alive until the end: 12 of 12 clean. The
real app is unaffected (one HUD per process): launched and quit 3 times, exit 0 each. **Tests that
build several HUDs must keep references to all of them.** Also: Settings tests now find tabs by name,
since adding a tab shifts indexes.

## The stack — all verified working on this machine

| Layer | Choice | Notes |
|---|---|---|
| Reasoning | Groq `openai/gpt-oss-120b` | **primary** — ~0.5s; 200K tok/day and 8K tok/min per model bind before 1,000 req/day |
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
- `tests/stress_real.py` — **26 realistic tasks through the real agent** and real tools, graded
  against the truth (files on disk, the real battery and volume, which tools ran, key leaks). Own
  memory and config go to a temp APPDATA and file tasks to a temp sandbox; it approves file changes
  only inside the sandbox and declines everything else. Costs ~70 Groq requests and ~150K input
  tokens, and takes ~15 min while the rate-limit stalls below exist. `--only=name1,name2` runs a
  subset. Declined GUI steps can still open apps (AUTO tier), so run it when windows popping up
  is acceptable.
- `tests/test_r2_memory.py` — 93 checks, no API key: records, filters and ordering, follow-ups once,
  notes tools and tiers, past-due warnings, honest-recall prompt, leaked-markup cleaning, and Hindi and
  Nepali search including rebuilding an old database's indexes.
- `tests/test_r2_knowledge.py` — 48 checks, no API key, temp folders only: chunking, text encodings,
  sync (add/edit/delete/ignore/unreadable/oversized), Word and Excel through the real readers, polling,
  all-words-first search, tools, default folder location, and the Settings section.
- `tests/test_r2_activity.py` — 25 checks, no API key: every outcome the registry logs and what it does
  not, argument truncation, pruning, a broken log not blocking actions, a decline through the agent, and
  the Settings Activity tab.
- `tests/test_r3_briefing.py` — 117 checks, no API key, weather and feeds faked: tones, greeting, reply
  language hint, weather (parsing, caching, unknown and Devanagari cities, outages), the briefing and
  its failing sections, the calendar store, Google feed parsing and sync, event alerts, calendar tools,
  and the Settings fields.
- `tests/test_readiness.py` — no API key: one section per stress-test finding that has been
  fixed, checked against the pre-fix behaviour (the decline tests fail 12 of 33 on the old code).
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

**Activity log** (`core/activity.py`, R2). `ToolRegistry.audit` is told about every decision the
registry makes: `approved` (confirm-tier, user said yes), `allowed` (covered by an earlier approve-all in
the same request), `declined`, `not asked` (nothing could ask, so it did not run), `blocked`, and `failed`
(any tier, including safety refusals such as `.env`). Successful auto-tier calls are **not** logged:
they change nothing, and logging them would bury what matters. It is written by the app, not the model,
so it cannot be talked out of recording something. Argument values are cut to 160 chars (no whole file
contents), entries are pruned after 90 days at start-up, and a failing log never blocks or changes an
action. Settings has an Activity tab (newest first, hover explains the outcome). Live through
`main.build()`: a declined delete and an approved write both appear, with the file left alone.
Found on the way: Settings tab labels were clipped because the stylesheet set `font-size` on
`QTabBar::tab` - Qt measured tabs with one font and drew them with another. The size is now set on the
tab bar's font instead.

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
