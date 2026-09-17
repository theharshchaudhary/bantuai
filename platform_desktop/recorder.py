"""Capture for meeting notes: the microphone and the PC's own output, mixed, in chunks.

`soundcard` (314KB) provides WASAPI loopback - the other side of a Zoom or Meet
call as the PC plays it. `sounddevice` has no loopback option. Verified live
2026-09-17: loopback came back verbatim through Whisper, and the built-in
microphone also hears the call through the speakers, which is why speakers are
told apart by comparing the two tracks' loudness rather than by transcribing
each (that would also spend the audio quota twice).

Audio never touches the disk: each chunk is handed over as WAV bytes and dropped.
"""

from __future__ import annotations

import io
import logging
import queue
import threading
import time
import warnings
import wave
from typing import Any, Callable

from core.meetings import AudioChunk, Segment
from core.providers.base import AuthError, RateLimited

log = logging.getLogger("bantu.recorder")

RATE = 16_000          # what Whisper wants: 16kHz mono
FRAME_S = 0.5
CHUNK_S = 120          # ~3.8MB of WAV, far under the 25MB request limit
#: A chunk ends at the quietest moment in its last stretch, not at a fixed second.
#: Found live: a fixed cut split "August fourteenth" and Whisper heard "August 5".
PAUSE_SEARCH_FRACTION = 0.2
PAUSE_SEARCH_MAX_S = 15.0
#: A microphone this quiet for the first seconds is probably not the one in use.
SILENT_MIC_RMS = 0.0002
SILENT_MIC_CHECK_S = 8.0


def to_wav(samples: Any, rate: int = RATE) -> bytes:
    import numpy as np

    pcm = (np.clip(np.asarray(samples, dtype="float32").reshape(-1), -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def split_point(loudness: list[float], search_frames: int) -> int:
    """How many frames to keep: up to and including the quietest frame in the last stretch."""
    n = len(loudness)
    if n == 0:
        return 0
    start = max(0, n - max(1, search_frames))
    quietest = min(range(start, n), key=lambda i: (loudness[i], -i))
    return quietest + 1


def microphone_name(settings: Any) -> str | None:
    """The name of the microphone chosen in Settings (a sounddevice index), if any."""
    index = getattr(settings, "mic_device", None)
    if index is None:
        return None
    try:
        import sounddevice as sd

        return str(sd.query_devices(index)["name"])
    except Exception:
        return None


def _find_microphone(name: str | None):
    import soundcard as sc

    if name:
        # sounddevice names can be cut short (MME stops at 31 characters): match on the start.
        key = name.strip().lower()[:24]
        for mic in sc.all_microphones():
            if mic.name.lower().startswith(key) or key in mic.name.lower():
                return mic
        log.warning("microphone %r not found for meeting notes; using the default", name)
    return sc.default_microphone()


class _Track(threading.Thread):
    """Reads fixed-length frames from one device into a queue until told to stop."""

    def __init__(self, label: str, device: Any, frame_len: int):
        super().__init__(name=f"bantu-meeting-{label}", daemon=True)
        self.label, self.device, self.frame_len = label, device, frame_len
        self.frames: queue.Queue = queue.Queue()
        self.error: Exception | None = None
        self.stopping = threading.Event()

    def run(self) -> None:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # "data discontinuity" whenever nothing is playing
                with self.device.recorder(samplerate=RATE, channels=1, blocksize=self.frame_len) as rec:
                    while not self.stopping.is_set():
                        self.frames.put(rec.record(numframes=self.frame_len)[:, 0].copy())
        except Exception as e:
            self.error = e
            log.warning("%s track stopped: %s", self.label, e)


class MeetingRecorder:
    def __init__(self, on_chunk: Callable[[AudioChunk], None], mic_name: str | None = None,
                 on_warning: Callable[[str], None] = lambda text: None, chunk_s: float = CHUNK_S):
        self.on_chunk = on_chunk
        self.mic_name = mic_name
        self.on_warning = on_warning
        self.chunk_frames = max(1, int(chunk_s / FRAME_S))
        self._frame_len = int(RATE * FRAME_S)
        self._paused = threading.Event()
        self._stopping = threading.Event()
        self._mic: _Track | None = None
        self._call: _Track | None = None
        self._mixer: threading.Thread | None = None

    def start(self) -> None:
        import soundcard as sc

        mic = _find_microphone(self.mic_name)
        self._mic = _Track("mic", mic, self._frame_len)
        try:
            loopback = sc.get_microphone(id=str(sc.default_speaker().name), include_loopback=True)
            self._call = _Track("call", loopback, self._frame_len)
        except Exception as e:  # no loopback: record the room only
            log.warning("no loopback device: %s", e)
            self.on_warning("The computer's own audio cannot be captured, so only the microphone is recorded.")
        self._mic.start()
        if self._call:
            self._call.start()
        time.sleep(0.3)
        if self._mic.error:
            raise RuntimeError(f"the microphone could not be opened ({self._mic.error})")
        self._mixer = threading.Thread(target=self._mix, name="bantu-meeting-mixer", daemon=True)
        self._mixer.start()

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def stop(self) -> None:
        """Stop capturing and hand over the last partial chunk before returning."""
        for track in (self._mic, self._call):
            if track:
                track.stopping.set()
        for track in (self._mic, self._call):
            if track:
                track.join(timeout=3)
        self._stopping.set()
        if self._mixer:
            self._mixer.join(timeout=10)

    def _next(self, track: _Track | None, timeout: float):
        import numpy as np

        if track is None:
            return np.zeros(self._frame_len, dtype="float32")
        try:
            return track.frames.get(timeout=timeout)
        except queue.Empty:
            return None

    def _mix(self) -> None:
        import numpy as np

        zeros = np.zeros(self._frame_len, dtype="float32")
        samples, mic_rms, call_rms = [], [], []
        chunk_offset = elapsed = 0.0
        warned_silent = False

        def flush(final: bool) -> None:
            nonlocal samples, mic_rms, call_rms, chunk_offset
            keep = len(samples)
            if not final:
                search = min(int(PAUSE_SEARCH_MAX_S / FRAME_S), max(1, int(len(samples) * PAUSE_SEARCH_FRACTION)))
                keep = split_point([m + c for m, c in zip(mic_rms, call_rms)], search)
            if samples and (not final or keep * FRAME_S >= 1.0):
                self.on_chunk(AudioChunk(to_wav(np.concatenate(samples[:keep])), chunk_offset, FRAME_S,
                                         mic_rms[:keep], call_rms[:keep]))
            if final:
                chunk_offset = elapsed
                samples, mic_rms, call_rms = [], [], []
            else:
                # What follows the pause starts the next chunk, so no word is cut in two.
                chunk_offset += keep * FRAME_S
                samples, mic_rms, call_rms = samples[keep:], mic_rms[keep:], call_rms[keep:]

        while True:
            done = self._stopping.is_set()
            mic = self._next(self._mic, 0.2 if done else 2.0)
            if mic is None:
                if done:
                    break
                if self._mic and self._mic.error:
                    self.on_warning("The microphone stopped working, so the recording has stopped.")
                    break
                continue
            call = self._next(self._call, 0.2)
            call = zeros if call is None else call
            elapsed += FRAME_S
            if self._paused.is_set():
                flush(final=True)  # paused time is not recorded; the transcript keeps real times
                continue
            m_rms = float(np.sqrt(np.mean(np.square(mic))))
            c_rms = float(np.sqrt(np.mean(np.square(call))))
            if not warned_silent and elapsed >= SILENT_MIC_CHECK_S and max(mic_rms + [m_rms]) < SILENT_MIC_RMS:
                warned_silent = True
                self.on_warning("The microphone seems silent. If you are not on headphones with a separate mic, "
                                "choose the right one in Settings.")
            samples.append(np.clip(mic + call, -1.0, 1.0))
            mic_rms.append(m_rms)
            call_rms.append(c_rms)
            if len(samples) >= self.chunk_frames:
                flush(final=False)
        flush(final=True)


def groq_segments_transcriber(get_key: Callable[[], str | None]) -> Callable[[bytes], list[Segment]]:
    """Timed segments from Groq Whisper (turbo: faster and, on Hindi, more accurate than large-v3)."""

    def transcribe(wav: bytes, prompt: str = "") -> list[Segment]:
        import groq

        key = get_key()
        if not key:
            raise AuthError("no Groq key: meeting transcription needs one")
        client = groq.Groq(api_key=key, timeout=120.0, max_retries=0)
        try:
            options = {"prompt": prompt} if prompt else {}
            result = client.audio.transcriptions.create(
                file=("chunk.wav", wav, "audio/wav"), model="whisper-large-v3-turbo",
                response_format="verbose_json", timestamp_granularities=["segment"], **options)
        except groq.RateLimitError as e:
            retry = None
            try:
                retry = float(e.response.headers.get("retry-after") or 0) or None
            except (AttributeError, ValueError):
                pass
            raise RateLimited(f"transcription limit: {e}", retry_after=retry) from e
        except groq.AuthenticationError as e:
            raise AuthError(f"Groq rejected the key: {e}") from e
        segments = getattr(result, "segments", None) or []
        out = []
        for s in segments:
            get = s.get if isinstance(s, dict) else (lambda k, _s=s: getattr(_s, k, None))
            out.append(Segment(float(get("start") or 0), float(get("end") or 0), str(get("text") or "")))
        return out

    return transcribe
