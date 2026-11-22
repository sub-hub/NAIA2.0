"""AVIF save format that keeps NAI generation metadata.

A generated result is a PNG whose metadata lives in two places:
  1. PNG text chunks (NAI ``Comment``/``Description``/..., WebUI ``parameters``,
     ComfyUI ``prompt``/``workflow``, NAIA ``naia_*``).
  2. NAI's hidden copy in the alpha channel's lowest bits (``stealth_pngcomp``).

AVIF has no text chunks, so (1) goes into EXIF UserComment as one flat JSON object
(``{"Comment": ..., "Description": ...}``) and ``restore_png_text`` puts it back into
``image.info`` when the file is read. The flat shape is what NovelAI's own Inspect page
reads from non-PNG images: it parses UserComment as JSON and uses the object as the
metadata, and it only falls back to the alpha copy when that object is empty. (2)
survives because the alpha plane is encoded losslessly (aom ``alpha:lossless``) while
color stays lossy.

Settings were picked on 16 NAI V4.5/V5 results (2026-11-22): quality 92 at speed 6
scored SSIMULACRA2 90+ on every image (visually lossless) at about 16% of the PNG size,
encoding in about 0.2 s per image with the default thread count.
"""

from __future__ import annotations

import io
import json
from typing import Any

from PIL import Image

AVIF_EXTENSION = ".avif"
AVIF_SAVE_OPTIONS: dict[str, Any] = {
    "quality": 92,
    "speed": 6,
    "subsampling": "4:4:4",
    # `alpha:lossless` is an aom option; another encoder would reject it.
    "codec": "aom",
    "advanced": {"alpha:lossless": "1"},
}
# First AVIF build (2026-11-22, not released) wrapped the chunks under this key. Still read so
# files saved then keep their metadata; NovelAI Inspect cannot read that shape.
_LEGACY_WRAPPER_KEY = "naia_png_text"
_EXIF_USER_COMMENT = 0x9286
_EXIF_IFD = 0x8769
_ASCII_PREFIX = b"ASCII\x00\x00\x00"


def avif_save_available() -> bool:
    """True when this Pillow build can write AVIF with the aom encoder."""
    try:
        from PIL import _avif  # type: ignore[attr-defined]

        return bool(_avif.encoder_codec_available("aom"))
    except Exception:
        return False


def _png_text_chunks(image: Image.Image) -> dict[str, str]:
    text = getattr(image, "text", None)
    if not isinstance(text, dict):
        return {}
    return {str(key): str(value) for key, value in text.items() if value is not None}


def _exif_with_png_text(chunks: dict[str, str]) -> bytes:
    exif = Image.Exif()
    # ASCII JSON keeps it half the size of UTF-16 "UNICODE" comments (NAI Comment is ~8 KB).
    payload = json.dumps(chunks, ensure_ascii=True, separators=(",", ":"))
    exif.get_ifd(_EXIF_IFD)[_EXIF_USER_COMMENT] = _ASCII_PREFIX + payload.encode("ascii")
    return exif.tobytes()


def png_bytes_to_avif(png_bytes: bytes) -> bytes:
    """Encode a PNG (with its text chunks and stealth alpha) as AVIF."""
    with Image.open(io.BytesIO(png_bytes)) as opened:
        opened.load()
        chunks = _png_text_chunks(opened)
        icc_profile = opened.info.get("icc_profile")
        image = opened
        if image.mode not in ("RGB", "RGBA"):
            has_alpha = "A" in image.mode or "transparency" in image.info
            image = image.convert("RGBA" if has_alpha else "RGB")
        save_kwargs: dict[str, Any] = dict(AVIF_SAVE_OPTIONS)
        if chunks:
            save_kwargs["exif"] = _exif_with_png_text(chunks)
        if icc_profile:
            save_kwargs["icc_profile"] = icc_profile
        buffer = io.BytesIO()
        image.save(buffer, format="AVIF", **save_kwargs)
        return buffer.getvalue()


def read_png_text(image: Image.Image) -> dict[str, str]:
    """PNG text chunks that ``png_bytes_to_avif`` stored in EXIF, or ``{}``."""
    if "exif" not in getattr(image, "info", {}):
        return {}
    try:
        raw = image.getexif().get_ifd(_EXIF_IFD).get(_EXIF_USER_COMMENT)
    except Exception:
        return {}
    if isinstance(raw, bytes):
        raw = raw[8:] if raw[:8] == _ASCII_PREFIX else raw
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str) or not raw.lstrip("\x00 ").startswith("{"):
        return {}
    try:
        data = json.loads(raw.strip("\x00 "))
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    if isinstance(data.get(_LEGACY_WRAPPER_KEY), dict):
        data = data[_LEGACY_WRAPPER_KEY]
    # Only a JSON object of text chunks counts - not some other tool's JSON comment.
    if not all(isinstance(value, str) for value in data.values()):
        return {}
    from utils.comfyui_png_metadata import TEXT_PNG_KEYS

    known = set(TEXT_PNG_KEYS) | {"Generation_time"}
    if not known.intersection(data):
        return {}
    return {str(key): value for key, value in data.items()}


def restore_png_text(image: Image.Image) -> Image.Image:
    """Put the stored PNG text chunks back into ``image.info`` (existing keys win)."""
    for key, value in read_png_text(image).items():
        image.info.setdefault(key, value)
    return image
