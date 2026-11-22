# Image save formats and generation metadata

How a generated image is held, saved and read back, and where its generation
metadata (prompt, seed, sampler, workflow) lives. Read this before changing a save
path, adding a save format, or adding code that opens an image and reads its
metadata.

---

## Terms

| Term | Meaning |
|------|---------|
| **text chunks** | PNG `tEXt`/`iTXt` entries. NAI writes `Title`, `Description` (prompt), `Software`, `Source` (model), `Generation_time`, `Comment` (JSON with every parameter). WebUI writes `parameters`; ComfyUI writes `prompt`/`workflow`; NAIA adds `naia_generation_params`, `naia_prompt_context`, `naia_api_metadata`. |
| **stealth** | NAI's hidden second copy of the same chunks, gzip-compressed JSON stored in the lowest bit of the alpha channel, column-major, after the ASCII signature `stealth_pngcomp`. Reader: `ImageMetadataExtractor._read_stealth_pnginfo` (`utils/image_info.py`); writer: `_embed_stealth_pnginfo` (`core/api_service.py`). Needs RGBA and height >= 120. |
| **alpha 254** | Because of stealth, almost every pixel of a NAI result has alpha 254 or 255, so the image counts as semi-transparent. Encoders that treat semi-transparent pixels specially change the color (see the WebP issue below). |
| **preview WebP** | The lossy WebP made for every result to send to the browser. It is not the original and has no metadata. |

The two copies normally hold the same values, but key names differ:
the PNG chunk is `Generation_time` and the stealth JSON uses `Generation time`.

---

## What a history item holds

`HeadlessHistoryItem` (`core/headless_result_service.py`):

| Field | Content |
|-------|---------|
| `raw_bytes` | Original bytes from the backend. For NAI this is the PNG with text chunks and stealth, untouched. |
| `webp_bytes` | Preview WebP from `_image_to_webp` (quality 85, method 0, no metadata). Sent over the WebSocket and used for thumbnails. |
| `png_payload_override` | Only for ComfyUI results that were not PNG: a PNG with the metadata embedded, so `raw_bytes` can keep the original. |
| `image` | Decoded PIL image. |
| `filepath` | Saved file, empty while unsaved. `quicksave_path` is the Ctrl+S copy. |

`result_image_payload_service.history_item_png_payload(item)` is the one place that
picks "the PNG with metadata" for an item (override, then raw PNG, then saved PNG,
then re-encode).

---

## Save paths

All in `HeadlessSaveService` (`core/headless_save_service.py`). The format comes from
`save_format()` (`png` / `webp` / `avif`); configs written before `save_format`
existed only have `save_as_webp` and are read through it.

| Path | Entry | Notes |
|------|-------|-------|
| Auto save | `save_history_item` | Called after each generation in a worker thread (`generation_runner._auto_save_generated_history_item`), and by Grok, Director, upscale and history-limit eviction. |
| Save all unsaved | `save_unsaved_history` | `/api/history/unsaved/save-all` (the "일괄 저장" button). |
| Selected items | `save_history_items` | `/api/history/selected/save`, reached through the `WebSessionContext.save_history_items` wrapper. |
| Ctrl+S quick save | `quicksave_item` | Copies or moves the saved file if there is one; otherwise encodes from memory with the current format. |

`_encode_history_item` turns an item into bytes for a format:

| Format | Bytes written | Metadata |
|--------|---------------|----------|
| `png` | `history_item_png_payload` | Everything, unchanged. |
| `webp` | `item.webp_bytes` (the preview) | None. Lossy q85. |
| `avif` | `utils/avif_image.png_bytes_to_avif` | Text chunks in EXIF, stealth in lossless alpha. About 1/6 of the PNG. |

---

## AVIF metadata contract

`utils/avif_image.py`. Encoder settings: quality 92, speed 6, 4:4:4, codec `aom`,
`advanced={"alpha:lossless": "1"}` (alpha only; color stays lossy). Chosen on 16 NAI
results: SSIMULACRA2 90+ on all of them.

1. **Text chunks go into EXIF UserComment as one flat JSON object**, ASCII charset:
   `{"Title": ..., "Description": ..., "Comment": ...}`. Do not wrap it. NovelAI's
   Inspect page reads non-PNG metadata as `JSON.parse(UserComment)` and needs a
   top-level `Comment`; given any other non-empty object it shows no metadata and
   never looks at the alpha copy. Files from the first AVIF build used
   `{"naia_png_text": {...}}`; `read_png_text` still accepts that shape.
2. **The alpha plane must stay lossless**, or stealth is destroyed.
3. **Any code that opens an image and reads its metadata must call
   `restore_png_text(image)` first.** It copies the EXIF chunks back into
   `image.info`, so the existing PNG-oriented parsing works unchanged. Current callers:
   - `ImageMetadataExtractor.extract_metadata` (also behind `extract_embedded_metadata`)
   - `_build_input_metadata_payload` (`app/backend/server/result_display_routes.py`)
   - `extract_comfyui_workflow_metadata_from_image` (`utils/comfyui_png_metadata.py`)
   - `pil_image_to_png_bytes` (`core/result_image_payload_service.py`), which covers
     PNG download of a saved file, `image_bytes_to_png_payload` and imported images
   A new reader that skips it will report "no metadata" for AVIF files.
4. `read_png_text` accepts only a JSON object of strings that contains a known chunk
   name (`TEXT_PNG_KEYS` plus `Generation_time`), so another tool's JSON comment is not
   taken for chunks. `ImageMetadataExtractor._extract_from_exif` skips such a comment
   instead of parsing it as WebUI `parameters`.

`avif_save_available()` checks that Pillow has the aom encoder. Without it the AVIF
button is disabled and a save that asks for AVIF writes PNG.

---

## How other tools read metadata

NovelAI Inspect (`https://novelai.net/inspect`, read from its bundle 2026-11-22):

1. PNG text chunks.
2. If none: EXIF UserComment, parsed as JSON and used as the metadata object.
3. Only if that is empty: stealth from the alpha channel. Decoding is its own code for
   PNG/WebP/JPEG and the browser's canvas for anything else.

Its signature check (whether NAI made the image) supports only PNG and WebP, and its
file picker offers PNG, JPEG and WebP; AVIF has to be dragged in.

---

## Known issues

- **Selected-items save always writes WebP.** `WebSessionContext.save_history_items`
  defaults `save_as_webp=True` and passes it on, so the setting is ignored even though
  the service was changed to follow it (2026-08-05). `tools/test_selected_history_actions.py`
  calls the service directly and does not catch it.
- **The preview WebP damages dark areas.** `_image_to_webp` encodes without
  `exact=True`; with alpha 254 libwebp rewrites the color of semi-transparent pixels,
  turning black hair or outlines into gray blocks (worst image PSNR 25 dB). Adding
  `exact=True` fixes it at the same size and speed. "Save as WebP" writes this preview.
- **`Generation_time` is dropped when converting to PNG.** `build_pnginfo` copies only
  `TEXT_PNG_KEYS`, which has `Generation time` but not NAI's `Generation_time`.
