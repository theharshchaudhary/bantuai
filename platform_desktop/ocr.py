"""Screen reading via the OCR engine built into Windows.

Free, offline, ~13ms, and nothing to install — the engine ships with the OS.
Critically it returns a bounding box per word, which is what lets "click Save"
work as a local lookup instead of a vision request.

Read the screen with this before spending a Gemini vision call: Gemini's free
tier is 20 requests/day/model, this is unlimited.
"""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.tools.registry import Tier, ToolError, ToolRegistry


@dataclass
class Word:
    text: str
    x: int
    y: int
    w: int
    h: int

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2


def _grab(region: str = "") -> Any:
    from PIL import ImageGrab

    box = None
    if region:
        try:
            parts = [int(p.strip()) for p in region.split(",")]
        except ValueError as e:
            raise ToolError(f"region must be 'left,top,right,bottom' in pixels: {e}") from e
        if len(parts) != 4:
            raise ToolError("region must have exactly four numbers: left,top,right,bottom")
        box = tuple(parts)
    img = ImageGrab.grab(bbox=box, all_screens=True)
    return img


async def _recognize(png_path: str) -> tuple[str, list[Word]]:
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.storage import FileAccessMode, StorageFile

    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise ToolError(
            "Windows OCR has no language pack for your profile. Add an English "
            "language pack in Settings > Time & Language."
        )
    f = await StorageFile.get_file_from_path_async(png_path)
    stream = await f.open_async(FileAccessMode.READ)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    result = await engine.recognize_async(bitmap)

    words: list[Word] = []
    for line in result.lines:
        for w in line.words:
            r = w.bounding_rect
            words.append(Word(w.text, int(r.x), int(r.y), int(r.width), int(r.height)))
    return result.text or "", words


def ocr_screen(region: str = "") -> tuple[str, list[Word]]:
    """Capture and OCR. Shared by the tools below and, later, by GUI control."""
    img = _grab(region)
    tmp = Path(tempfile.gettempdir()) / "bantu_ocr.png"
    img.save(tmp, format="PNG")
    try:
        return asyncio.run(_recognize(str(tmp)))
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"OCR failed: {type(e).__name__}: {e}") from e


# --- tools ------------------------------------------------------------------


def register(reg: ToolRegistry) -> None:
    @reg.register(tier=Tier.AUTO, category="screen")
    def read_screen(region: str = "") -> str:
        """Read all text currently visible on screen, using local OCR.

        Free and unlimited — always try this before look_at_screen, which costs a
        scarce vision request. Good for error dialogs, app text, and anything
        written down. It cannot describe images, icons or layout.

        Args:
            region: Optional 'left,top,right,bottom' pixel box. Whole screen if omitted.
        """
        text, words = ocr_screen(region)
        if not text.strip():
            return "No text found on screen."
        return f"{len(words)} words read from screen:\n\n{text}"

    @reg.register(tier=Tier.AUTO, category="screen")
    def find_on_screen(text: str) -> str:
        """Locate on-screen text and return its pixel coordinates.

        Use this to find a button or label before clicking it. Matching is
        case-insensitive and partial.

        Args:
            text: The word or phrase to look for.
        """
        _, words = ocr_screen()
        needle = text.strip().lower()
        if not needle:
            raise ToolError("nothing to search for")

        hits = [w for w in words if needle in w.text.lower()]
        if not hits:
            # Try to match a phrase spread across adjacent words.
            joined = " ".join(w.text.lower() for w in words)
            if needle in joined:
                return (
                    f"{text!r} appears on screen but is split across words; "
                    f"search for a single word from it instead."
                )
            return f"{text!r} was not found on screen."

        lines = [
            f"- {w.text!r} at ({w.center[0]}, {w.center[1]}), box {w.w}x{w.h}" for w in hits[:10]
        ]
        return f"Found {len(hits)} match(es):\n" + "\n".join(lines)
