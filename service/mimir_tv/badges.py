"""Badges drawn onto a title's poster, so every Jellyfin app (Wholphin, Swiftfin) shows what mimir-tv's page shows on
its cards: the language of a catalogue title, the streaming apps of an On my apps title. Text labels in a colour
each, never the apps' own logos.

The poster as fetched is kept beside it as .poster-original.jpg (Jellyfin ignores dot files) and poster.jpg is drawn
from it; .poster-badges records which badges are on it, so a poster is drawn again only when they change."""

import io
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ORIGINAL, DRAWN = ".poster-original.jpg", ".poster-badges"
LANGUAGE = "#263238"  # a language badge's colour
APPS = {  # app name -> badge colour; others get the language colour
    "Netflix": "#E50914",
    "Prime Video": "#00A8E1",
    "Peacock": "#111111",
    "YouTube": "#FF0000",
    "Apple TV+": "#111111",
    "ESPN+": "#C8102E",
    "Sling TV": "#1F6FEB",
    "Hulu": "#1CE783",
    "Max": "#002BE7",
    "Paramount+": "#0064FF",
}


def draw(poster: bytes, labels: list[tuple[str, str]]) -> bytes:
    """The poster with each (text, colour) label as a pill, stacked from its bottom-left corner up."""
    img = Image.open(io.BytesIO(poster)).convert("RGB")
    w, h = img.size
    size = max(12, w // 14)
    font = ImageFont.load_default(size=size)
    pen = ImageDraw.Draw(img)
    pad, gap, y = size // 3, size // 4, h - size // 2
    for text, colour in reversed(labels):
        left, top, right, bottom = pen.textbbox((0, 0), text, font=font)
        bw, bh = right - left + 2 * pad, bottom - top + 2 * pad
        x, y = size // 2, y - bh
        pen.rounded_rectangle((x, y, x + bw, y + bh), radius=bh // 2, fill=colour)
        pen.text((x + pad - left, y + pad - top), text, font=font, fill="white")
        y -= gap
    out = io.BytesIO()
    img.save(out, "JPEG", quality=88)
    return out.getvalue()


def apply(folder: Path, labels: list[tuple[str, str]]) -> bool:
    """poster.jpg with these badges, drawn from the kept original (the first time, poster.jpg itself is the
    original). True when poster.jpg was drawn again."""
    poster, original, drawn = folder / "poster.jpg", folder / ORIGINAL, folder / DRAWN
    if not original.exists():
        if not poster.exists():
            return False
        poster.replace(original)
    want = json.dumps(labels)
    if poster.exists() and drawn.exists() and drawn.read_text() == want:
        return False
    try:
        poster.write_bytes(draw(original.read_bytes(), labels) if labels else original.read_bytes())
    except OSError:  # not an image Pillow reads: the poster as it came
        poster.write_bytes(original.read_bytes())
    drawn.write_text(want)
    return True


def for_language(language: str | None) -> list[tuple[str, str]]:
    return [(language.upper(), LANGUAGE)] if language else []


def for_apps(names: list[str]) -> list[tuple[str, str]]:
    return [(n, APPS.get(n, LANGUAGE)) for n in names]
