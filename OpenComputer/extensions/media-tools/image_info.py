"""ImageInfo — extract dimensions, format, and EXIF metadata via PIL."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


class PILUnavailableError(RuntimeError):
    """Pillow not installed — install via ``pip install Pillow``."""


@dataclass(frozen=True)
class ImageMetadata:
    """Result of inspecting an image file."""

    path: str
    format: str
    mode: str
    width: int
    height: int
    exif: dict[str, Any]


def inspect_image(path: Path) -> ImageMetadata:
    """Open the image, return metadata. Raises FileNotFoundError if missing."""
    try:
        from PIL import ExifTags, Image
    except ImportError as e:
        raise PILUnavailableError(
            "Pillow is not installed. Install with 'pip install Pillow'."
        ) from e

    if not path.exists():
        raise FileNotFoundError(str(path))

    with Image.open(path) as img:
        img.load()  # force decode so we hit truncation before returning
        exif_raw = getattr(img, "_getexif", lambda: None)() or {}
        exif: dict[str, Any] = {}
        for tag_id, value in exif_raw.items():
            tag_name = ExifTags.TAGS.get(tag_id, str(tag_id))
            try:
                # Some EXIF values are bytes — coerce to str when reasonable.
                if isinstance(value, bytes):
                    try:
                        value = value.decode("utf-8", errors="replace")
                    except Exception:  # noqa: BLE001
                        value = repr(value)
                exif[tag_name] = value
            except Exception:  # noqa: BLE001
                continue

        return ImageMetadata(
            path=str(path),
            format=img.format or "",
            mode=img.mode or "",
            width=img.width,
            height=img.height,
            exif=exif,
        )


__all__ = ["ImageMetadata", "PILUnavailableError", "inspect_image"]
