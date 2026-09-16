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
   Small model files may be *bundled into the exe at build time* (openWakeWord, ~2MB) — that is fine
   because the user never sees a download.
3. **`core/` stays portable.** No PyQt5, no Windows-only imports, no desktop dependencies inside
   `core/`. Tools are *registered into* it by the platform layer. This is what keeps a future Android
   client possible.
4. **Local product only.** No server, no accounts, no billing, no telemetry. Users bring their own keys.
5. **Permission tiers are enforced in code**, never in the prompt. See "Safety model".

## Current status

| Phase | What | State |
|---|---|---|
| 00 | Repo hygiene — `.gitignore`, `.env` untracked, `.env.example` | **done** (`251ad67`) |
| 01 | Foundation — config, keyring, provider interface, Gemini + Groq adapters, failover router | **next** |
| 02 | Agent core — tool registry, the loop, SQLite memory, vision, Windows OCR | — |
| 03 | File tools (14) | — |
| 04 | System + app tools (16), guarded PowerShell | — |
| 05 | Web tools (7) — DuckDuckGo, Selenium | — |
| 06 | Voice — Groq Whisper, edge-tts, wake word, barge-in | — |
| 07 | HUD overlay UI (PyQt5) — replaces the plain chat window | — |
| 08 | GUI control — click-by-name via OCR boxes, vision fallback | — |
| 09 | Onboarding — first-run wizard, settings | — |
| 10 | Packaging — PyInstaller `.exe` | — |
| 11 | README rewrite | — |

Post-v1: proactive trigger engine (v1.1), semantic memory + habits (v1.2), Home Assistant + phone (v2).

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
ui/                     PyQt5 HUD, settings, tray
```

One loop handles everything. **No intent routing** — the model reads the tool list and decides.
The old `Backend/Model.py` Cohere router is retired by Phase 02.

## The stack — all verified working on this machine

| Layer | Choice | Notes |
|---|---|---|
| Reasoning | Gemini Flash (free tier) | primary; multimodal, so vision is free |
| Failover | Groq `openai/gpt-oss-120b` | on 429; 8K TPM is why it is not primary |
| Vision | Gemini Flash image input | ~560–1,120 tokens per image, 1 request |
| Screen text | **`winsdk` → `Windows.Media.Ocr`** | verified 13ms, zero install, **returns per-word boxes** |
| STT | Groq `whisper-large-v3-turbo` | free, 20 RPM / 2,000 RPD, separate quota pool |
| STT offline | `winsdk` → `SpeechRecognizer` | built into Windows |
| TTS | `edge-tts` | free, no API key, Microsoft neural voices |
| TTS offline | `winsdk` → `SpeechSynthesizer` | built into Windows, 3 voices |
| Memory search | SQLite **FTS5** | already inside Python, no embedding model |
| Wake word | openWakeWord | ~2MB, bundled into the exe |
| Search | DuckDuckGo | no key, no quota |

Dev environment: `.venv/` in the repo root (gitignored). Python 3.11.9.
Hardware: RTX 3050 Laptop 4GB, Ryzen 7 6800HS, 15.3GB RAM — enough for local *perception*,
not for local *reasoning*. That split is the whole reason $0 works.

## Verified facts — do not re-research these

- **Groq's Llama models are Enterprise-only.** `llama3-70b-8192` and `llama-3.3-70b-versatile` are not
  on the free tier. Free tier is gpt-oss, Compound, Qwen, Whisper. The legacy `Backend/*.py` files
  still reference the dead model.
- **`google-genai` 2.23.0** exposes both `client.models` (stateless) and `client.interactions`
  (server-side state). **Use `models.generate_content` with full local history** — server-side state
  cannot survive a failover to Groq mid-conversation.
- **Gemini free-tier RPD is unconfirmed.** Google's docs defer to a per-account AI Studio page and
  third-party sources disagree (250 vs 1,500/day for Flash). Read real limits from the API at startup;
  do not hardcode a number.
- Windows OCR word boxes mean **"click Save" needs no model call** — a local lookup plus a mouse move.
  This makes most GUI control free.

## Language

Bantu must speak **English, Hindi and Nepali**. Voices are chosen per language and per gender.

> The legacy `Backend/Chatbot.py` system prompt says *"Reply in only English, even if the question is
> in Hindi."* **That rule is reversed.** Bantu replies in whatever language the user used.

Whisper handles Hindi well; Nepali accuracy is weaker — verify before relying on it.
Windows `SpeechRecognizer` likely has no Nepali at all, so offline STT is English/Hindi only.

## Safety model

Three tiers, enforced in the registry **before execution**:

- **auto** — read-only or trivially reversible; runs silently
- **confirm** — changes disk, process or website state; shows exact arguments first
- **blocked** — never runs

Blocklist: credential stores (`.env`, `id_rsa`, browser profiles), registry writes, System32 and
Program Files writes, disk formatting, Defender/firewall changes, and **Bantu's own config and keys**.
That last one matters most — without it, "set all tools to auto" is a valid tool call and the whole
model collapses.

Deletes go to the Recycle Bin, never a hard unlink.

## Conventions

- **Commit after each completed phase.** Not one big commit at the end.
- **No `Co-Authored-By` trailer.** Ever.
- **Never `git push`.** Harsh pushes himself.
- Secrets never enter the repo. Keys live in Windows Credential Manager via `keyring`.
- Runtime state goes to `%APPDATA%\BantuAI\`, never beside the executable.

## Open decisions

- **Voice selection** — 11 samples generated across English/Hindi/Nepali; awaiting Harsh's pick.
- **Android APK** — wanted eventually, but ~35 of 41 tools are meaningless on a phone and a remote
  client needs a reachable core, which conflicts with "no server". Unresolved; v2 conversation.
- **Code signing** — the `.exe` ships unsigned, so users see a SmartScreen warning. Certificate is a
  purchase decision.

## Legacy code

`Backend/Chatbot.py`, `Backend/Model.py`, `Backend/RealtimeSearchEngine.py` are the old
intent-router implementation. They call a decommissioned model and do not run. Delete each one when
its replacement lands — git history keeps them. `Backend/Automation.py`, `ImageGeneration.py`,
`SpeechToText.py`, `TextToSpeech.py`, `Frontend/GUI.py` and `Main.py` are empty stubs.

`Frontend/Graphics/` assets (including `Jarvis.gif`) are reused by the Phase 07 HUD.
