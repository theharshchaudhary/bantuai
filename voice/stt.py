"""Speech in.

Groq's Whisper is the default: free at 2,000 requests/day on its own quota
pool, and far more accurate than the offline option — which matters most for
Hindi and Nepali. Windows' built-in SpeechRecognizer takes over when there is
no network or the quota is gone, so voice degrades rather than dying.

Silence detection is a plain energy gate rather than webrtcvad: no native build
to fail on Windows, and it only has to answer "has this person stopped talking".
"""

from __future__ import annotations

import io
import logging
import tempfile
import threading
import time
import wave
from pathlib import Path

log = logging.getLogger("bantu.stt")

SAMPLE_RATE = 16000        # what Whisper expects
CHANNELS = 1
BLOCK = 1024

#: RMS below this counts as silence. Set from a short ambient sample at start,
#: because a fixed threshold is wrong in every room but one.
DEFAULT_SILENCE_RMS = 0.012
SILENCE_TO_STOP = 1.0      # seconds of quiet that end an utterance
MAX_UTTERANCE = 30.0       # hard ceiling, also Whisper's sweet spot
MIN_UTTERANCE = 0.35       # below this it is a cough or a key click


class MicUnavailable(RuntimeError):
    pass


def _rms(block) -> float:
    import numpy as np

    return float(np.sqrt(np.mean(np.square(block, dtype="float64")))) if block.size else 0.0


def list_devices() -> list[tuple[int, str]]:
    import sounddevice as sd

    return [
        (i, d["name"])
        for i, d in enumerate(sd.query_devices())
        if d["max_input_channels"] > 0
    ]


def calibrate(seconds: float = 0.6, device: int | None = None) -> float:
    """Sample the room and derive a silence threshold from it."""
    import numpy as np
    import sounddevice as sd

    try:
        rec = sd.rec(
            int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=CHANNELS,
            dtype="float32", device=device,
        )
        sd.wait()
    except Exception as e:
        raise MicUnavailable(f"no usable microphone: {e}") from e
    ambient = _rms(np.asarray(rec).flatten())
    # Sit well above the noise floor, but never below a sane minimum.
    return max(DEFAULT_SILENCE_RMS, ambient * 3.0)


def record_utterance(
    device: int | None = None,
    threshold: float | None = None,
    max_seconds: float = MAX_UTTERANCE,
    on_start=None,
    stop_event: threading.Event | None = None,
) -> bytes | None:
    """Record until the speaker stops. Returns WAV bytes, or None if nothing was said."""
    import numpy as np
    import sounddevice as sd

    gate = threshold if threshold is not None else DEFAULT_SILENCE_RMS
    frames: list = []
    started = False
    quiet_for = 0.0
    began = time.time()

    try:
        stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="float32",
            blocksize=BLOCK, device=device,
        )
    except Exception as e:
        raise MicUnavailable(f"could not open the microphone: {e}") from e

    with stream:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            if time.time() - began > max_seconds:
                break
            try:
                block, _ = stream.read(BLOCK)
            except Exception as e:
                log.warning("microphone read failed: %s", e)
                break
            arr = np.asarray(block).flatten()
            loud = _rms(arr) > gate

            if loud:
                if not started:
                    started = True
                    if on_start:
                        try:
                            on_start()
                        except Exception:
                            pass
                quiet_for = 0.0
                frames.append(arr.copy())
            elif started:
                quiet_for += BLOCK / SAMPLE_RATE
                frames.append(arr.copy())
                if quiet_for >= SILENCE_TO_STOP:
                    break

    if not frames:
        return None
    audio = np.concatenate(frames)
    if len(audio) / SAMPLE_RATE < MIN_UTTERANCE:
        return None

    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("int16")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


# --- transcription ----------------------------------------------------------


def transcribe_groq(wav_bytes: bytes, api_key: str, language: str | None = None) -> str:
    """Transcribe with Groq's Whisper. Raises on failure so the caller can fall back."""
    from groq import Groq

    client = Groq(api_key=api_key, timeout=45.0)
    kwargs = {
        "file": ("speech.wav", wav_bytes, "audio/wav"),
        "model": "whisper-large-v3-turbo",
        "response_format": "text",
    }
    if language:
        kwargs["language"] = language
    result = client.audio.transcriptions.create(**kwargs)
    return (result if isinstance(result, str) else getattr(result, "text", "")).strip()


def transcribe_windows(wav_bytes: bytes) -> str:
    """Offline fallback using the recogniser built into Windows.

    English-only in practice; there is no Nepali pack, and Hindi depends on what
    the user has installed. Better than silence when the network is gone.
    """
    import asyncio

    tmp = Path(tempfile.gettempdir()) / "bantu_stt.wav"
    tmp.write_bytes(wav_bytes)

    async def go() -> str:
        from winsdk.windows.media.speechrecognition import SpeechRecognizer
        from winsdk.windows.storage import StorageFile

        rec = SpeechRecognizer()
        await rec.compile_constraints_async()
        f = await StorageFile.get_file_from_path_async(str(tmp))
        stream = await f.open_read_async()
        result = await rec.recognize_async(stream) if hasattr(rec, "recognize_async") else None
        return (getattr(result, "text", "") or "").strip()

    try:
        return asyncio.run(go())
    except Exception as e:
        raise RuntimeError(f"Windows speech recognition failed: {e}") from e


class Listener:
    """Records a phrase and returns text, preferring Groq and degrading offline."""

    def __init__(self, settings, get_key):
        self.settings = settings
        self._get_key = get_key
        self.device = getattr(settings, "mic_device", None)
        self._threshold: float | None = None

    def ensure_calibrated(self) -> float:
        if self._threshold is None:
            try:
                self._threshold = calibrate(device=self.device)
                log.info("silence threshold %.4f", self._threshold)
            except MicUnavailable:
                self._threshold = DEFAULT_SILENCE_RMS
                raise
        return self._threshold

    def listen(self, on_start=None, stop_event: threading.Event | None = None) -> str:
        """Capture one utterance and transcribe it. Returns '' if nothing was said."""
        try:
            threshold = self.ensure_calibrated()
        except MicUnavailable:
            threshold = DEFAULT_SILENCE_RMS

        wav = record_utterance(
            device=self.device, threshold=threshold,
            on_start=on_start, stop_event=stop_event,
        )
        if not wav:
            return ""

        key = self._get_key("groq")
        if key:
            try:
                return transcribe_groq(wav, key)
            except Exception as e:
                log.warning("Groq transcription failed, falling back offline: %s", e)
        try:
            return transcribe_windows(wav)
        except Exception as e:
            log.warning("offline transcription failed too: %s", e)
            return ""
