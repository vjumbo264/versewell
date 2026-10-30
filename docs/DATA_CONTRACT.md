# VerseWell Data Contract

## 1. Overview
VerseWell is a public scripture platform whose single source of truth is the static JSON tree in `site/static-data/`.
This document defines the strict API and data contract between VerseWell and client consumers (including the Android application and the Audio Generation Pipeline).

## 2. Scripture Structure
Each chapter is served at `/api/v1/versions/:version/:book/:chapter` (and statically at `/static-data/:version_slug/:book_slug/:chapter.json`).

```json
{
  "version": "KJV",
  "book": "Matthew",
  "book_name": "Matthew",
  "chapter": 1,
  "intros": [
    {
      "start_verse": 1,
      "end_verse": 17,
      "text": "Section introduction text here"
    }
  ],
  "verses": [
    {
      "verse": 1,
      "text": "The book of the generation of Jesus Christ, the son of David, the son of Abraham.",
      "heading_before": "The Genealogy of Jesus the Messiah",
      "footnotes": [
        {
          "marker": "a",
          "text": "Or is an account of the origin"
        }
      ]
    }
  ],
  "navigation": {
    "previous": null,
    "next": {"book": "Matthew", "chapter": 2}
  }
}
```

## 3. Invariants & Rules
1. **Clean Verse Text**:
   - `text` contains ONLY the pure scripture words of the verse.
   - Section intros, headings, chapter labels, verse numbers, and footnote references must NEVER be concatenated into or pollute `text`.
   - Audio generation pipelines synthesize ONLY `text`, ensuring spoken audio matches scripture without spurious text.

2. **Headings**:
   - Any editorial section heading preceding a verse is stored in `heading_before`.
   - If no heading precedes the verse, `heading_before` is omitted or `null`.

3. **Footnotes**:
   - Per-verse footnotes are represented as an array of objects: `[{"marker": string, "text": string}]`.
   - Client applications must gracefully handle both object format `{"marker": "...", "text": "..."}` and legacy string format `"..."`.

4. **Section Introductions**:
   - Introductions spanning verse ranges are stored in `intros: [{"start_verse": int, "end_verse": int, "text": string}]`.
   - Intros are never part of verse `text`.

5. **Audio Pipeline Contract**:
   - The audio generation pipeline processes only the 5 approved New Testament translations: `KJV`, `NIV`, `TLB`, `GW`, `VOICE`.
   - Each translation has exactly 260 NT chapters (Matthew 1 through Revelation 22).
   - Generated chapter audio files are hosted as release assets on `vjumbo264/versewell` tagged `audio-<code>`.
   - Audio streaming API: `GET /api/v1/audio/:version/:book/:chapter` with HTTP 206 Partial Content (Range) support.
