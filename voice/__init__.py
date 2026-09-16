"""Voice input and output. Platform-adjacent: not imported by core/."""

from .stt import Listener, MicUnavailable, list_devices
from .tts import Speaker, detect_language, strip_for_speech

__all__ = [
    "Listener",
    "MicUnavailable",
    "Speaker",
    "detect_language",
    "list_devices",
    "strip_for_speech",
]
