# VerseWell Audio Narration Architecture & Manifest Specification

## 1. Translation & Voice Matrix (Complete New Testament — 260 Chapters Each)
Audio narration is built and served exclusively for these 5 translations:

| Code | Translation | Primary Edge Neural Voice | Secondary Edge Fallback | Offline CPU Fallback (Piper) |
|---|---|---|---|---|
| `KJV` | King James Version | `en-US-ChristopherNeural` (Male) | `en-US-RogerNeural` | `en_US-lessac-medium` |
| `NIV` | New International Version | `en-US-JennyNeural` (Female) | `en-US-AriaNeural` | `en_US-amy-medium` |
| `TLB` | The Living Bible | `en-US-GuyNeural` (Male) | `en-US-SteffanNeural` | `en_US-lessac-medium` |
| `GW` | GOD'S WORD Translation | `en-US-AriaNeural` (Female) | `en-US-JennyNeural` | `en_US-amy-medium` |
| `VOICE` | The Voice | `en-US-EricNeural` (Male) | `en-US-ChristopherNeural` | `en_US-lessac-medium` |

All other translations return `"audio": { "available": false }` on `GET /api/v1/versions`.

## 2. Clean Text & Synthesis Rules
- Source: `site/static-data/<version>/<book-slug>/<chapter>.json` (`verses[].text` only).
- Never speaks section introductions (`intros`), editorial headings (`heading_before`), footnote markers/text (`footnotes`), or verse numbers.
- Speaks lead-in `"<Book Name>, chapter <N>."`, followed by a `700 ms` silence pause, and `350 ms` pauses between verses.
- Preserves textual-bracket Scripture sentences (e.g., `VOICE` Matthew 17:21) while stripping short editorial/omission bracket tags (`[a]`, `[Verse omitted]`).
- Bounded concurrency (`Semaphore(5)`) synthesizes up to 5 verses concurrently per chapter with retries, exponential backoff, secondary Edge voice fallback, and local deterministic **Piper** ONNX neural TTS fallback.

## 3. Mastering & Verification Invariants
- **Encoding**: Mono (`1` channel), `24,000 Hz`, EBU R128 loudness normalized (`-16 LUFS`, `-1.5 dBTP`), AAC in `.m4a` at `48 kbps` with `+faststart` atom for immediate HTTP Range seeking.
- **Verification (`verify_audio`)**:
  1. File exists and `size >= 8,000` bytes.
  2. `ffprobe` duration vs chapter word count falls within `70–255 WPM`.
  3. `ffmpeg volumedetect` mean volume `> -52.0 dB` (non-silent).
  4. `ffmpeg silencedetect` confirms no abnormal interior silence stretch `>= 10.0s`.
  5. SHA-256 computed and recorded.
- Any chapter failing verification is immediately deleted, never uploaded, and causes the workflow job to exit `1` (red).

## 4. Storage & `manifest.json` Schema
Assets are published to GitHub Releases in `vjumbo264/versewell` under tags `audio-kjv`, `audio-niv`, `audio-tlb`, `audio-gw`, and `audio-voice` (`<book-slug>-<chapter:03d>.m4a` + `manifest.json`).

```json
{
  "version": "KJV",
  "voice": "en-US-ChristopherNeural",
  "total_chapters": 260,
  "total_bytes": 375085200,
  "chapters": [
    {
      "file": "matthew-001.m4a",
      "book": "Matthew",
      "book_slug": "matthew",
      "chapter": 1,
      "bytes": 1412890,
      "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "duration_ms": 234510,
      "engine": "edge-tts",
      "voice": "en-US-ChristopherNeural",
      "word_count": 489,
      "verses": [
        { "verse": 1, "start_ms": 2180, "end_ms": 6420 }
      ]
    }
  ]
}
```

## 5. Worker API Endpoints (`versewell-api.vjumbo264.workers.dev`)
- `GET /api/v1/versions`: includes `audio` metadata object (`available`, `chapters`, `total_bytes`, `voice`, `manifest_url`).
- `GET /api/v1/audio/{version}/manifest`: returns the release `manifest.json` (cached via Cloudflare Cache API).
- `GET /api/v1/audio/{version}/{book}/{chapter}`: streams `.m4a` with full `Range: bytes=...` (`206 Partial Content`), `Accept-Ranges: bytes`, `Content-Type: audio/mp4`, and `Cache-Control: public, max-age=31536000, immutable` (plus `ETag` keyed by chapter `sha256`).
