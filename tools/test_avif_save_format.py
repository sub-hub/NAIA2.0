#!/usr/bin/env python3
"""AVIF save format keeps NAI generation metadata (text chunks and the stealth alpha copy)."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
from PIL.PngImagePlugin import PngInfo


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import result_image_payload_service as result_images  # noqa: E402
from core.api_service import _embed_stealth_pnginfo  # noqa: E402
from core.headless_result_service import HeadlessHistoryItem, HeadlessResultStore  # noqa: E402
from core.headless_save_service import HeadlessSaveService  # noqa: E402
from utils.avif_image import avif_save_available, png_bytes_to_avif, read_png_text  # noqa: E402
from utils.image_info import ImageMetadataExtractor, extract_embedded_metadata  # noqa: E402

PROMPT = "1girl, solo, black hair, 한글 태그"
NAI_CHUNKS = {
    "Title": "NovelAI generated image",
    "Description": PROMPT,
    "Software": "NovelAI",
    "Source": "NovelAI Diffusion V4.5 4BDE2A90",
    "Generation time": "1.234",
    "Comment": json.dumps({"prompt": PROMPT, "uc": "lowres", "steps": 28, "seed": 1234,
                           "sampler": "k_euler_ancestral", "scale": 5.0}, ensure_ascii=False),
}


def make_nai_png() -> bytes:
    """A small NAI-like PNG: gradient + black block, text chunks and the stealth alpha copy."""
    image = Image.new("RGB", (128, 160))
    image.putdata([((x * 3) % 256, (y * 5) % 256, 128) if x < 80 else (0, 0, 0)
                   for y in range(160) for x in range(128)])
    image = _embed_stealth_pnginfo(image, {k: v for k, v in NAI_CHUNKS.items() if k != "Title"})
    info = PngInfo()
    for key, value in NAI_CHUNKS.items():
        info.add_itxt(key, value) if not value.isascii() else info.add_text(key, value)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", pnginfo=info)
    return buffer.getvalue()


def stealth(image: Image.Image) -> str | None:
    with contextlib.redirect_stdout(io.StringIO()):
        return ImageMetadataExtractor._read_stealth_pnginfo(image)


def make_item(png_bytes: bytes) -> HeadlessHistoryItem:
    with Image.open(io.BytesIO(png_bytes)) as opened:
        opened.load()
        image = opened.copy()
    webp = io.BytesIO()
    image.save(webp, format="WEBP", quality=85)
    return HeadlessHistoryItem(image=image, raw_bytes=png_bytes, webp_bytes=webp.getvalue(),
                               generation_params={}, prompt_context={})


def make_service(save_dir: Path, store: HeadlessResultStore, auto_save_state: dict) -> HeadlessSaveService:
    context = SimpleNamespace(
        result_store=store,
        save_directory_state={"base_path": str(save_dir), "use_timestamp_folder": False, "save_counter": 1,
                              "filename_format": "number_only", "classification_method": "none",
                              "classification_rules": ""},
        auto_save_state=auto_save_state,
        session_timestamp="test-session",
        remote_options={},
        save_remote_ui_state=lambda: None,
        _coerce_bool=lambda v: str(v).lower() in {"1", "true", "yes", "on"} if isinstance(v, str) else bool(v),
        _coerce_int=lambda v, default=0, minimum=None, maximum=None: int(v) if v is not None else default,
        _module_state_payload=lambda module_id, state: {"module_id": module_id, **state},
        _output_root=lambda: save_dir,
    )
    return HeadlessSaveService(context)


def main() -> int:
    assert avif_save_available(), "this Pillow build cannot write AVIF with aom"
    evidence: dict[str, object] = {}
    png_bytes = make_nai_png()
    with Image.open(io.BytesIO(png_bytes)) as source:
        source.load()
        source_rgba = source.copy()
    stealth_ref = stealth(source_rgba)
    assert stealth_ref, "fixture must carry the stealth copy"

    # 1. Encoder: text chunks stored, alpha plane (stealth) bit-exact, color lossy but close.
    avif_bytes = png_bytes_to_avif(png_bytes)
    assert result_images.image_media_type_for_bytes(avif_bytes) == "image/avif"
    with Image.open(io.BytesIO(avif_bytes)) as avif:
        avif.load()
        assert avif.format == "AVIF" and avif.mode == "RGBA", (avif.format, avif.mode)
        assert read_png_text(avif) == NAI_CHUNKS
        assert avif.getchannel("A").tobytes() == source_rgba.getchannel("A").tobytes()
        assert stealth(avif) == stealth_ref
        # The stored comment must not be misread as a WebUI "parameters" block.
        assert ImageMetadataExtractor._extract_from_exif(avif) is None
        # NovelAI Inspect reads non-PNG metadata as `JSON.parse(UserComment)` and needs a
        # top-level "Comment"; a wrapped object makes it report no metadata (checked 2026-11-22).
        comment = avif.getexif().get_ifd(0x8769)[0x9286]
        assert comment[:8] == b"ASCII\x00\x00\x00", comment[:8]
        assert json.loads(comment[8:]) == NAI_CHUNKS
    evidence["avif_bytes"] = len(avif_bytes)
    evidence["png_bytes"] = len(png_bytes)

    # The first AVIF build wrapped the chunks as {"naia_png_text": {...}}; those files still read.
    legacy = Image.Exif()
    legacy.get_ifd(0x8769)[0x9286] = b"ASCII\x00\x00\x00" + json.dumps({"naia_png_text": NAI_CHUNKS}).encode()
    with Image.open(io.BytesIO(avif_bytes)) as avif:
        avif.load()
        buffer = io.BytesIO()
        avif.save(buffer, format="AVIF", exif=legacy.tobytes(), quality=50)
    with Image.open(buffer) as old:
        old.load()
        assert read_png_text(old) == NAI_CHUNKS
    # Some other tool's JSON comment is not mistaken for PNG chunks.
    other = Image.Exif()
    other.get_ifd(0x8769)[0x9286] = b"ASCII\x00\x00\x00" + b'{"foo": "bar", "n": 1}'
    plain = io.BytesIO()
    source_rgba.convert("RGB").save(plain, format="JPEG", exif=other.tobytes())
    with Image.open(plain) as jpeg:
        assert read_png_text(jpeg) == {}

    # 2. Reader: everything read from the PNG reads the same from the AVIF
    #    (the AVIF only adds its container keys: exif, timestamp, duration).
    with contextlib.redirect_stdout(io.StringIO()):
        from_png = extract_embedded_metadata(png_bytes)
        from_avif = extract_embedded_metadata(avif_bytes)
    assert from_png.get("prompt") == PROMPT, from_png.get("prompt")
    assert {k: from_avif.get(k) for k in from_png} == from_png, sorted(
        k for k in from_png if from_avif.get(k) != from_png[k])
    evidence["prompt_from_avif"] = from_avif.get("prompt")

    with tempfile.TemporaryDirectory(prefix="naia-avif-save-test-") as temp_dir:
        save_dir = Path(temp_dir)

        # 3. Settings: old configs that only had save_as_webp keep their format.
        legacy = make_service(save_dir, HeadlessResultStore(max_items=10), {"save_as_webp": True})
        assert legacy.save_format() == "webp"
        assert make_service(save_dir, HeadlessResultStore(max_items=10), {}).save_format() == "png"

        store = HeadlessResultStore(max_items=10)
        item = make_item(png_bytes)
        store._items = [item]
        store._set_latest_item(item)
        state: dict = {"save_as_webp": False}
        service = make_service(save_dir, store, state)
        payload = service.set_auto_save_param("save_format", "avif")
        assert payload is not None and payload["save_format"] == "avif" and payload["save_as_webp"] is False
        assert service.set_auto_save_param("save_format", "gif") is None
        assert state["save_format"] == "avif"

        # 4. Auto save writes .avif and the file reads back with its NAI metadata.
        result = service.save_history_item(item)
        saved = Path(result["path"])
        assert saved.suffix == ".avif", saved
        assert extract_embedded_metadata(saved.read_bytes()).get("prompt") == PROMPT
        evidence["saved_file"] = saved.name

        # 5. Converting the saved AVIF back to PNG (download/copy as PNG) restores the chunks.
        png_again, name = result_images.image_file_to_png_payload(saved)
        assert name.endswith(".png")
        with Image.open(io.BytesIO(png_again)) as back:
            back.load()
            for key, value in NAI_CHUNKS.items():
                assert back.text.get(key) == value, key
            assert stealth(back) == stealth_ref
        evidence["png_roundtrip_chunks"] = sorted(NAI_CHUNKS)

        # 6. Legacy checkbox key still switches the format.
        service.set_auto_save_param("save_as_webp", "true")
        assert state["save_format"] == "webp"
        assert result_images.image_media_type_for_path(saved) == "image/avif"

    print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
