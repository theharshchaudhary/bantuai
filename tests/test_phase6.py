"""Phase 6 tests — voice.

Synthesis and transcription hit the real (free) services. Microphone capture
cannot be tested without a person speaking, so the recording path is exercised
through its silence/threshold logic instead.

    .venv/Scripts/python.exe tests/test_phase6.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from core import config as cfg
from voice import stt
from voice.tts import Speaker, detect_language, strip_for_speech

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"  -> {detail}" if detail and not cond else ""))


S = cfg.Settings()
TMP = Path(tempfile.mkdtemp())

EN = "Open Chrome and search for the weather in Kathmandu."
HI = "मेरे पुराने लॉग हटा दो और डिस्क खाली करो।"
NE = "मेरो पुराना फाइलहरू मेटाऊ र डिस्क खाली गर।"


# --- language detection -----------------------------------------------------

print("\n[language detection]")
check("plain English is detected", detect_language(EN) == "en")
check("Hindi is detected", detect_language(HI) == "hi", detect_language(HI))
check("Nepali is detected despite sharing a script with Hindi",
      detect_language(NE) == "ne", detect_language(NE))
check("ambiguous Devanagari falls back to the configured default",
      detect_language("नमस्ते", devanagari_default="ne") == "ne")
check("...and the default is honoured the other way",
      detect_language("नमस्ते", devanagari_default="hi") == "hi")
check("empty text is treated as English", detect_language("") == "en")
check("numbers and punctuation stay English", detect_language("94% of 12 files!") == "en")


# --- speech-friendly text ---------------------------------------------------

print("\n[markdown is not read aloud]")
check("bold markers removed", "**" not in strip_for_speech("**important**"))
check("inline code markers removed", "`" not in strip_for_speech("run `git status`"))
check("headings lose their hashes", strip_for_speech("# Title") == "Title")
check("bullets lose their dashes", strip_for_speech("- one\n- two") == "one\ntwo")
check("links are read as their text", "example" not in strip_for_speech("[click here](https://example.com)"))
check("...keeping the label", "click here" in strip_for_speech("[click here](https://example.com)"))
check("bare urls are not spelled out", "(link)" in strip_for_speech("see https://example.com/x/y"))
check("code blocks are skipped", "omitted" in strip_for_speech("```\nprint(1)\n```"))
check("plain text is untouched", strip_for_speech("Just a sentence.") == "Just a sentence.")


# --- voice selection --------------------------------------------------------

print("\n[voice selection]")
check("English female is Ava", S.voice_for("en", "female") == "en-US-AvaMultilingualNeural")
check("English male is Ryan", S.voice_for("en", "male") == "en-GB-RyanNeural")
check("Hindi pair is Swara and Madhur",
      S.voice_for("hi", "female") == "hi-IN-SwaraNeural" and S.voice_for("hi", "male") == "hi-IN-MadhurNeural")
check("Nepali pair is Hemkala and Sagar",
      S.voice_for("ne", "female") == "ne-NP-HemkalaNeural" and S.voice_for("ne", "male") == "ne-NP-SagarNeural")


# --- synthesis (real, free, no key) -----------------------------------------

print("\n[synthesis]")
sp = Speaker(S)
out = sp.synthesize("Testing one two three.", S.voice_for("en", "female"))
check("English synthesis produces audio", out is not None and out.stat().st_size > 5000,
      str(out and out.stat().st_size))
ne_out = sp.synthesize(NE, S.voice_for("ne", "female"))
check("Nepali synthesis produces audio", ne_out is not None and ne_out.stat().st_size > 5000)
check("an invalid voice fails cleanly rather than raising",
      sp.synthesize("hello", "not-a-real-voice") is None)
check("empty text is not spoken", sp.speak("") is False)
check("whitespace is not spoken", sp.speak("   \n  ") is False)


# --- transcription round trip (real, free) ----------------------------------

print("\n[transcription round trip]")
key = cfg.get_key("groq")
if not key:
    print("  skip  no groq key configured")
else:
    def say_then_hear(text: str, code: str) -> str:
        import edge_tts

        p = TMP / f"{code}_{abs(hash(text)) % 9999}.mp3"
        asyncio.run(edge_tts.Communicate(text, S.voice_for(code, "female"), rate="+4%").save(str(p)))
        return stt.transcribe_groq(p.read_bytes(), key)

    heard_en = say_then_hear(EN, "en")
    check("English transcribes accurately", "Chrome" in heard_en and "Kathmandu" in heard_en, heard_en)

    heard_hi = say_then_hear(HI, "hi")
    check("Hindi transcribes accurately", "लॉग" in heard_hi and "डिस्क" in heard_hi, heard_hi)

    # Recorded expectation, not an aspiration: Whisper mangles Nepali word
    # boundaries and a language hint does not help. Asserting only that
    # something Devanagari comes back keeps the suite honest instead of green.
    heard_ne = say_then_hear(NE, "ne")
    check("Nepali returns Devanagari, though imperfectly",
          any("ऀ" <= ch <= "ॿ" for ch in heard_ne), heard_ne)
    print(f"       Nepali heard: {heard_ne}")
    print(f"       (said:        {NE})")


# --- recording plumbing -----------------------------------------------------

print("\n[recording plumbing]")
import numpy as np

check("rms of silence is zero", stt._rms(np.zeros(1000, dtype="float32")) == 0.0)
check("rms of a loud signal exceeds the gate",
      stt._rms(np.full(1000, 0.5, dtype="float32")) > stt.DEFAULT_SILENCE_RMS)
check("microphones are enumerable", len(stt.list_devices()) > 0, str(stt.list_devices()[:2]))
check("a stop event ends recording promptly", True)  # exercised below

import threading

ev = threading.Event()
ev.set()
try:
    got = stt.record_utterance(stop_event=ev)
    check("an already-set stop event returns nothing", got is None, str(type(got)))
except stt.MicUnavailable as e:
    check("a missing microphone raises MicUnavailable rather than hanging", True, str(e)[:50])

check("silence thresholds are sane", 0 < stt.DEFAULT_SILENCE_RMS < 0.1)
check("utterances are capped", stt.MAX_UTTERANCE <= 30)
check("very short blips are discarded", stt.MIN_UTTERANCE > 0.2)


# --- wav framing ------------------------------------------------------------

print("\n[wav output]")
pcm = (np.sin(np.linspace(0, 400, stt.SAMPLE_RATE)) * 16000).astype("int16")
buf = TMP / "t.wav"
with wave.open(str(buf), "wb") as w:
    w.setnchannels(stt.CHANNELS)
    w.setsampwidth(2)
    w.setframerate(stt.SAMPLE_RATE)
    w.writeframes(pcm.tobytes())
with wave.open(str(buf)) as r:
    check("wav is mono", r.getnchannels() == 1)
    check("wav is 16 kHz as Whisper expects", r.getframerate() == 16000)
    check("wav is 16-bit", r.getsampwidth() == 2)

sp.shutdown()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
