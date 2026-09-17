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
import threading
from dataclasses import dataclass
from pathlib import Path

from core.tools.registry import Tier, ToolError, ToolRegistry

from .paths import parse_region


@dataclass
class Word:
    text: str
    x: int
    y: int
    w: int
    h: int
    line: int = 0

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2


@dataclass
class Match:
    """One or more adjacent words that together matched a query."""

    text: str
    x: int
    y: int
    w: int
    h: int
    #: OCR line the words came from, so neighbours on the same line can be found.
    line: int = -1

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2


def _virtual_origin() -> tuple[int, int]:
    """Top-left of the desktop. Negative when a monitor sits left of or above the primary."""
    import ctypes
    import sys

    if sys.platform != "win32":
        return 0, 0
    u = ctypes.windll.user32
    return u.GetSystemMetrics(76), u.GetSystemMetrics(77)


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
    for li, line in enumerate(result.lines):
        for w in line.words:
            r = w.bounding_rect
            words.append(Word(w.text, int(r.x), int(r.y), int(r.width), int(r.height), li))
    return result.text or "", words


#: Windows OCR rejects images larger than this on either side.
_OCR_MAX_SIDE = 9000


def capture_words(
    bbox: tuple[int, int, int, int] | None = None, scale: int = 1
) -> tuple[str, list[Word]]:
    """Capture a screen area and OCR it, returning words in ABSOLUTE screen coordinates.

    OCR boxes are relative to the captured image. They are shifted by where that
    image sits on the desktop, so a word's centre is a point you can click. PIL
    treats a bbox as absolute when all_screens is set, and without one the image
    starts at the virtual-screen origin — which is negative on some layouts.
    """
    from PIL import ImageGrab

    img = ImageGrab.grab(bbox=bbox, all_screens=True)
    ox, oy = (bbox[0], bbox[1]) if bbox else _virtual_origin()
    # Small UI text is where Windows OCR struggles: at 1x it read "Duplicate" as
    # "Dupicate". Upscaling helps, but coordinates must be scaled back down.
    scale = max(1, min(int(scale), _OCR_MAX_SIDE // max(img.width, img.height, 1)))
    if scale > 1:
        from PIL import Image as _PILImage

        img = img.resize((img.width * scale, img.height * scale), _PILImage.LANCZOS)
    tmp = Path(tempfile.gettempdir()) / f"bantu_ocr_{threading.get_ident()}_{scale}.png"
    img.save(tmp, format="PNG")
    try:
        text, words = asyncio.run(_recognize(str(tmp)))
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"OCR failed: {type(e).__name__}: {e}") from e
    for w in words:
        w.x = ox + round(w.x / scale)
        w.y = oy + round(w.y / scale)
        w.w = max(1, round(w.w / scale))
        w.h = max(1, round(w.h / scale))
    return text, words


def ocr_screen(region: str = "") -> tuple[str, list[Word]]:
    """Read the whole screen, or a 'left,top,right,bottom' region of it."""
    return capture_words(parse_region(region))


def find_matches(words: list[Word], needle: str, max_span: int = 8) -> list[Match]:
    """Locate a word or a multi-word phrase.

    OCR splits text at every space, so a single-word search misses anything a
    person would actually say ("Save As", "Sign in with Google"). This slides a
    window over consecutive words within a line and merges their boxes, which is
    what makes clicking by name work without a vision call.
    """
    target = " ".join(needle.lower().split())
    if not target:
        return []

    out: list[Match] = []
    seen: set[tuple[int, int, int, int]] = set()

    for start in range(len(words)):
        parts: list[Word] = []
        for offset in range(max_span):
            i = start + offset
            if i >= len(words) or words[i].line != words[start].line:
                break
            parts.append(words[i])
            joined = " ".join(p.text for p in parts).lower()
            if target in joined:
                x = min(p.x for p in parts)
                y = min(p.y for p in parts)
                w = max(p.x + p.w for p in parts) - x
                h = max(p.y + p.h for p in parts) - y
                key = (x, y, w, h)
                if key not in seen:
                    seen.add(key)
                    out.append(Match(" ".join(p.text for p in parts), x, y, w, h, words[start].line))
                break
            # Stop growing once the window can no longer become a prefix match.
            if len(joined) > len(target) + 40:
                break
    return out


# --- tools ------------------------------------------------------------------


def register(reg: ToolRegistry) -> None:
    reg.describe_category("screen", "read the text on screen, find where some text is, or ask a vision question about what the screen shows")

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

        Handles multi-word phrases such as 'Save As' or 'Sign in', not just
        single words. Use it to find a button or label before clicking it.
        Matching is case-insensitive and partial.

        Args:
            text: The word or phrase to look for.
        """
        needle = text.strip()
        if not needle:
            raise ToolError("nothing to search for")

        _, words = ocr_screen()
        hits = find_matches(words, needle)
        if not hits:
            return (
                f"{text!r} is not visible on screen. Use read_screen to see what is "
                f"actually there, or look_at_screen if the target is an icon rather than text."
            )
        lines = [
            f"- {h.text!r} at ({h.center[0]}, {h.center[1]}), box {h.w}x{h.h}" for h in hits[:10]
        ]
        more = f"\n... and {len(hits) - 10} more" if len(hits) > 10 else ""
        return f"Found {len(hits)} match(es) for {text!r}:\n" + "\n".join(lines) + more
