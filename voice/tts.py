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
import queue
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


#: Replies are spoken a sentence at a time. Measured 2026-09-17: edge-tts takes
#: ~1.1s before a short line exists as audio, but a 66-word reply synthesized as
#: one file took up to 9.6s, all of it silent. Now the first sentence plays after
#: about a second whatever the length, while the next is synthesized behind it.
_SENTENCE_END = re.compile(r"(?<=[.!?।॥])\s+")
#: Shorter sentences join the next one: "OK." is not worth its own request, and
#: a gap after every two words sounds broken.
_MIN_CHUNK = 40

_DONE = object()


def split_sentences(text: str, min_chars: int = _MIN_CHUNK) -> list[str]:
    """Break text into speakable chunks of at least `min_chars`, in order."""
    chunks: list[str] = []
    cur = ""
    for part in _SENTENCE_END.split(text):
        part = part.strip()
        if not part:
            continue
        cur = f"{cur} {part}".strip()
        if len(cur) >= min_chars:
            chunks.append(cur)
            cur = ""
    if cur:
        if chunks:
            chunks[-1] = f"{chunks[-1]} {cur}"
        else:
            chunks.append(cur)
    return chunks


class Speaker:
    """Speaks text aloud, and can be interrupted.

    `speak` returns at once. Synthesis and playback run on a background thread,
    because both happened on the caller's thread before, and the caller was the
    HUD's UI thread: the orb froze for as long as edge-tts took.
    """

    def __init__(self, settings):
        self.settings = settings
        self._lock = threading.Lock()
        self._mixer_ready = False
        self._tmp = Path(tempfile.gettempdir()) / "bantu_tts"
        self._tmp.mkdir(exist_ok=True)
        self._seq = 0
        #: Bumped by stop(). Every thread of an older utterance sees the change and exits.
        self._gen = 0
        #: The utterance still synthesizing or playing, or 0.
        self._active = 0

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

    def _mixer_play(self, path: Path) -> None:
        import pygame

        pygame.mixer.music.load(str(path))
        pygame.mixer.music.play()

    def _mixer_busy(self) -> bool:
        if not self._mixer_ready:
            return False
        try:
            import pygame

            return bool(pygame.mixer.music.get_busy())
        except Exception:
            return False

    def _mixer_halt(self) -> None:
        if not self._mixer_ready:
            return
        try:
            import pygame

            if pygame.mixer.music.get_busy():
                pygame.mixer.music.stop()
            pygame.mixer.music.unload()
        except Exception:
            pass

    # --- synthesis ----------------------------------------------------------

    def synthesize(self, text: str, voice: str, rate: str = "+4%") -> Path | None:
        """Render text to an mp3 file. Returns None if synthesis fails."""
        with self._lock:
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
        """Start saying something. Returns False if there is nothing to say or no audio."""
        spoken = strip_for_speech(text)
        if not spoken:
            return False
        if not getattr(self.settings, "voice_enabled", True):
            return False
        if not self._ensure_mixer():
            return False

        code = lang or detect_language(spoken)
        voice = self.settings.voice_for(code)
        rate = getattr(self.settings, "voice_rate", "+4%")

        self.stop()  # never overlap two replies
        with self._lock:
            self._gen += 1
            gen = self._active = self._gen
        worker = threading.Thread(
            target=self._say, args=(gen, split_sentences(spoken), voice, rate), name="bantu-speech", daemon=True
        )
        worker.start()
        if blocking:
            worker.join(timeout=300)
        return True

    def _say(self, gen: int, chunks: list[str], voice: str, rate: str) -> None:
        """Synthesize ahead on one thread, play in order on this one."""
        ready: queue.Queue = queue.Queue()

        def produce() -> None:
            for chunk in chunks:
                if gen != self._gen:
                    break
                ready.put(self.synthesize(chunk, voice, rate))
            ready.put(_DONE)

        threading.Thread(target=produce, name="bantu-speech-synth", daemon=True).start()
        try:
            while gen == self._gen:
                path = ready.get()
                if path is _DONE:
                    break
                if path is None:
                    continue  # one sentence failed to synthesize; say the rest
                with self._lock:
                    if gen != self._gen:
                        break
                    try:
                        self._mixer_play(path)
                    except Exception as e:
                        log.warning("playback failed: %s", e)
                        break
                while gen == self._gen and self._mixer_busy():
                    time.sleep(0.03)
                with self._lock:
                    if gen == self._gen:
                        self._mixer_halt()  # unload, so the file can be removed
                try:
                    path.unlink()
                except OSError:
                    pass
        finally:
            with self._lock:
                if self._active == gen:
                    self._active = 0

    def is_speaking(self) -> bool:
        """True from the moment speak() is called until the last sentence ends."""
        return self._active != 0 or self._mixer_busy()

    def wait(self, timeout: float = 120.0) -> None:
        deadline = time.time() + timeout
        while self.is_speaking() and time.time() < deadline:
            time.sleep(0.05)

    def stop(self) -> None:
        """Cut speech immediately, including sentences not yet played. This is barge-in."""
        with self._lock:
            self._gen += 1
            self._active = 0
            self._mixer_halt()

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
