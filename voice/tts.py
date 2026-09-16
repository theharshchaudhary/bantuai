"""Speech out, via Microsoft's neural voices through edge-tts.

Free, no API key, nothing to download. Playback goes through pygame's mixer so
it can be stopped mid-sentence — barge-in is what separates a conversation from
a monologue.

Voices were chosen by ear and are recorded in CLAUDE.md. Do not substitute them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path

# pygame prints a banner on import; a CLI must stay clean. This has to be set
# before pygame is first imported anywhere in the process.
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

log = logging.getLogger("bantu.tts")

#: Devanagari covers both Hindi and Nepali, so script alone cannot tell them
#: apart. These are ordinary function words that rarely appear in the other.
_NEPALI_MARKERS = ("छ", "छन्", "छु", "हो", "होइन", "गर्न", "गर्नु", "भएको", "तपाईं",
                   "हरू", "लाई", "पनि", "अघि", "सकियो", "हटाऊ")
_HINDI_MARKERS = ("है", "हैं", "हूँ", "हूं", "करना", "करें", "आप", "नहीं", "गया",
                  "रहा", "लिए", "और", "कृपया", "दूँ")

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def detect_language(text: str, devanagari_default: str = "hi") -> str:
    """Guess which of English, Hindi or Nepali a reply is in.

    Returns an 'en' / 'hi' / 'ne' code. Hindi and Nepali share a script, so this
    counts distinctive function words; when neither side wins it falls back to
    the configured default rather than pretending to be sure.
    """
    if not text or not _DEVANAGARI.search(text):
        return "en"
    ne = sum(text.count(w) for w in _NEPALI_MARKERS)
    hi = sum(text.count(w) for w in _HINDI_MARKERS)
    if ne > hi:
        return "ne"
    if hi > ne:
        return "hi"
    return devanagari_default


def strip_for_speech(text: str) -> str:
    """Remove markup that should be read as nothing rather than as symbols."""
    t = re.sub(r"```.*?```", " (code omitted) ", text, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"\*\*([^*]*)\*\*", r"\1", t)
    t = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"\1", t)
    t = re.sub(r"^#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"^\s*[-*+]\s+", "", t, flags=re.M)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)          # links -> their text
    t = re.sub(r"https?://\S+", " (link) ", t)
    return re.sub(r"\s{2,}", " ", t).strip()


class Speaker:
    """Speaks text aloud, and can be interrupted."""

    def __init__(self, settings):
        self.settings = settings
        self._lock = threading.Lock()
        self._playing = False
        self._mixer_ready = False
        self._tmp = Path(tempfile.gettempdir()) / "bantu_tts"
        self._tmp.mkdir(exist_ok=True)
        self._seq = 0

    # --- mixer --------------------------------------------------------------

    def _ensure_mixer(self) -> bool:
        if self._mixer_ready:
            return True
        try:
            import pygame

            pygame.mixer.init()
            self._mixer_ready = True
        except Exception as e:
            log.warning("audio output unavailable: %s", e)
        return self._mixer_ready

    # --- synthesis ----------------------------------------------------------

    def synthesize(self, text: str, voice: str, rate: str = "+4%") -> Path | None:
        """Render text to an mp3 file. Returns None if synthesis fails."""
        self._seq += 1
        out = self._tmp / f"say_{self._seq}.mp3"

        async def go():
            import edge_tts

            await edge_tts.Communicate(text, voice, rate=rate).save(str(out))

        try:
            asyncio.run(go())
        except Exception as e:
            log.warning("speech synthesis failed (%s): %s", voice, e)
            return None
        return out if out.exists() and out.stat().st_size > 0 else None

    # --- speaking -----------------------------------------------------------

    def speak(self, text: str, lang: str | None = None, blocking: bool = False) -> bool:
        """Say something. Returns False if nothing could be spoken."""
        spoken = strip_for_speech(text)
        if not spoken:
            return False
        if not getattr(self.settings, "voice_enabled", True):
            return False

        code = lang or detect_language(spoken)
        voice = self.settings.voice_for(code)
        rate = getattr(self.settings, "voice_rate", "+4%")

        path = self.synthesize(spoken, voice, rate)
        if path is None:
            return False
        if not self._ensure_mixer():
            return False

        self.stop()  # never overlap two replies
        try:
            import pygame

            with self._lock:
                pygame.mixer.music.load(str(path))
                pygame.mixer.music.play()
                self._playing = True
        except Exception as e:
            log.warning("playback failed: %s", e)
            return False

        if blocking:
            self.wait()
        return True

    def is_speaking(self) -> bool:
        if not self._mixer_ready:
            return False
        try:
            import pygame

            return bool(pygame.mixer.music.get_busy())
        except Exception:
            return False

    def wait(self, timeout: float = 120.0) -> None:
        deadline = time.time() + timeout
        while self.is_speaking() and time.time() < deadline:
            time.sleep(0.05)
        self._playing = False

    def stop(self) -> None:
        """Cut playback immediately. This is barge-in."""
        if not self._mixer_ready:
            return
        try:
            import pygame

            with self._lock:
                if pygame.mixer.music.get_busy():
                    pygame.mixer.music.stop()
                pygame.mixer.music.unload()
                self._playing = False
        except Exception:
            pass

    def shutdown(self) -> None:
        self.stop()
        try:
            import pygame

            if self._mixer_ready:
                pygame.mixer.quit()
        except Exception:
            pass
        self._mixer_ready = False
        for f in self._tmp.glob("say_*.mp3"):
            try:
                f.unlink()
            except OSError:
                pass
