"""Looking at the screen with a vision model.

Deliberately separate from ocr.py, and deliberately expensive-sounding in its
description: Gemini's free tier allows about 120 vision calls a day in total,
while Windows OCR is unlimited. The model should reach for OCR first and this
only when the question genuinely needs *seeing* rather than *reading*.
"""

from __future__ import annotations

import io
from typing import Any

from core.providers.base import Image, Message
from core.tools.registry import Tier, ToolError, ToolRegistry

#: Screens are wide; downscaling keeps the image inside one billing block.
MAX_EDGE = 1600


def capture(region: str = "") -> Image:
    from PIL import ImageGrab

    box = None
    if region:
        parts = [p.strip() for p in region.split(",")]
        if len(parts) != 4 or not all(p.lstrip("-").isdigit() for p in parts):
            raise ToolError("region must be 'left,top,right,bottom' in pixels")
        box = tuple(int(p) for p in parts)

    img = ImageGrab.grab(bbox=box, all_screens=True)
    if max(img.size) > MAX_EDGE:
        ratio = MAX_EDGE / max(img.size)
        img = img.resize((int(img.width * ratio), int(img.height * ratio)))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Image(buf.getvalue(), "image/png")


def register(reg: ToolRegistry, router: Any, settings: Any) -> None:
    @reg.register(tier=Tier.AUTO, category="screen")
    def look_at_screen(question: str, region: str = "") -> str:
        """Look at the screen and answer a question about what it shows.

        COSTS A SCARCE REQUEST. Try read_screen first — it is free and unlimited
        and handles anything written as text. Use this only for genuinely visual
        questions: layout, images, charts, icons, colours, or "what is this?".

        Args:
            question: What you want to know about what is on screen.
            region: Optional 'left,top,right,bottom' pixel box. Whole screen if omitted.
        """
        img = capture(region)
        try:
            resp = router.chat(
                [Message.user(question, [img])],
                system="Answer concisely about the screenshot. State only what you can see.",
                max_output_tokens=int(getattr(settings, "max_output_tokens", 1024)),
                needs_vision=True,
            )
        except Exception as e:
            raise ToolError(
                f"could not look at the screen: {e}. Windows OCR via read_screen "
                f"still works offline and without quota."
            ) from e
        return resp.text.strip() or "(the model returned nothing about the image)"
