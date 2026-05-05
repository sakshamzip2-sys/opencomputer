# media-tools

All-local image + audio tooling. Three tools, no paid APIs:

- `ImageInfo(path)` — Pillow-based inspection (dimensions, format, EXIF).
- `TTSGenerate(text, out_path, voice?)` — edge-tts synthesis to MP3.
- `AudioTranscribe(path, model?)` — local STT via mlx-whisper (Apple
  Silicon) or pywhispercpp (cross-platform).

## Install + enable

```bash
oc plugin enable media-tools
```

Required deps:
- `Pillow` for ImageInfo (install with `pip install Pillow`)
- `edge-tts` for TTSGenerate (already a core OC dep)
- One of:
  - `mlx-whisper` (Apple Silicon, fast)
  - `pywhispercpp` (cross-platform)

If a backend is missing the tool returns `{"error": "..."}` rather
than raising — keeps the agent loop responsive.

## MVP scope (2026-05-05)

Shipped:
- ImageInfo with PIL EXIF parsing.
- TTSGenerate via edge-tts.
- AudioTranscribe with auto backend selection.

Explicitly out of scope (open issues / future PRs):
- **Image generation** (DALL·E / SD / Midjourney) — needs paid APIs +
  heavy model weights. Add via a separate plugin if you want it.
- **Streaming TTS** — current synthesis writes to file. Real-time
  streaming would need a different transport.
- **Video processing** (ffmpeg pipelines).
- **Custom voice cloning**.

## Tests

```bash
pytest tests/test_media_tools_*.py -v
```

Tests use a real PNG fixture for ImageInfo; TTS + STT tests check the
unavailable-backend error path (no real network / model load).
