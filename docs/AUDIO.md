# VerseWell Audio Narration Architecture

## 1. Scope & Translation Set
Audio narration is generated and provided exclusively for 5 translations across the complete New Testament (260 chapters each):
1. **KJV** (King James Version) — Voice: `en-US-ChristopherNeural` (Authoritative, resonant male)
2. **NIV** (New International Version) — Voice: `en-US-JennyNeural` (Expressive, natural female)
3. **TLB** (The Living Bible) — Voice: `en-US-GuyNeural` (Warm, conversational male)
4. **GW** (GOD'S WORD Translation) — Voice: `en-US-AriaNeural` (Clear, articulate female)
5. **VOICE** (The Voice) — Voice: `en-US-EricNeural` (Contemplative male)

## 2. Audio Mastering Specifications
- Format: AAC in `.m4a` container
- Bitrate: 48 kbps mono, 24,000 Hz
- Loudness Target: -16 LUFS (EBU R128 standard via ffmpeg loudnorm)
- Faststart: `+faststart` MP4 atom moved to beginning of file for instant streaming & seeking
- Timing: Lead-in (`"<Book>, chapter <N>."`), followed by 700ms pause, verses separated by 350ms pause.

## 3. Storage & Delivery (Zero KV/R2 Cost)
Audio assets are NOT stored in the git repository. They are stored as GitHub Release assets in `vjumbo264/versewell` tagged `audio-<code>`.
- Asset naming: `<book-slug>-<chapter:03d>.m4a` (e.g. `matthew-001.m4a`)
- Manifest: `manifest.json` per release with per-chapter durations, hashes, bytes, and per-verse start/end timestamps.
- Streaming Endpoint: `GET /api/v1/audio/{version}/{book}/{chapter}`
  - Supports HTTP Range headers (`206 Partial Content`)
  - Long-term immutable caching (`Cache-Control: public, max-age=31536000, immutable`)
  - Fast proxy with Cloudflare Edge caching
