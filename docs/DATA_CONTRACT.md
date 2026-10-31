# VerseWell Data Contract

## 1. Architecture & Single Source of Truth
VerseWell serves 12 church-produced Bible translations exclusively from static JSON (`site/static-data/`) generated deterministically by `scripts/generate_static.py` from `bible-sources/*.sqlite3`. Neither Cloudflare D1 nor KV is used. Both `versewell.pages.dev` and `versewell-api.vjumbo264.workers.dev` read this exact static tree.

## 2. Book List Contract (`/api/v1/versions/:version/books` & `/static-data/:v/index.json`)
Every book object carries canonical `testament` (`"OT"` or `"NT"`) and URL `slug` so translations with partial Old Testament coverage (such as `TPT` with 30 books or `MSG`) never misclassify New Testament books under Old Testament:

```json
{
  "book": "Matt",
  "book_name": "Matthew",
  "book_order": 4,
  "chapters": 28,
  "verse_count": 1071,
  "slug": "matthew",
  "testament": "NT"
}
```

## 3. Chapter Payload Contract (`/api/v1/versions/:version/:book/:chapter`)
```json
{
  "version": "NIV",
  "book": "Matt",
  "book_name": "Matthew",
  "chapter": 1,
  "intro": null,
  "intros": null,
  "verses": [
    {
      "verse": 1,
      "text": "This is the genealogy of Jesus the Messiah the son of David, the son of Abraham:",
      "heading_before": "The Genealogy of Jesus the Messiah",
      "footnotes": [
        { "marker": "a", "text": "Or is an account of the origin" }
      ]
    }
  ],
  "navigation": {
    "prev": { "book": "Mal", "chapter": 4 },
    "next": { "book": "Matt", "chapter": 2 }
  }
}
```

### Strict Invariants (Enforced by `scripts/validate_data_contract.py` in CI)
1. **Pure Verse Text**: `verses[].text` contains ONLY spoken scripture prose. Section introductions, editorial headings, HTML tags, verse numbers, and footnote markers are never merged into `text`.
2. **Section Introductions**: Stored in `intros` (`[{"start_verse": int, "end_verse": int, "text": string}]` when present, or `null` when absent). Verified live on `CEV` (66 book intros) and `VOICE` (44 long-aside intros).
3. **Editorial Headings**: Stored in `verses[].heading_before` (`string`, omitted when absent).
4. **Footnotes**: Canonical API shape is `[{"marker": string, "text": string}]`. Android clients (`BundledFootnoteListSerializer`) additionally tolerate plain string arrays `["..."]` for backward compatibility with older cached bundles.

## 4. Verified New Testament Counts for the 5 Audio Translations
All 5 audio translations have **260 / 260** New Testament chapters (Matthew 1 – Revelation 22):
- **KJV**: 260 chapters, 7,957 verses (Textus Receptus baseline)
- **NIV**: 260 chapters, 7,941 verses (16 standard critical-text omissions footnoted, e.g. Matt 17:21, 18:11)
- **GW**: 260 chapters, 7,943 verses (standard critical-text omissions + 3 John 1:15)
- **VOICE**: 260 chapters, 7,755 verse rows + 5 NT section intros (combines poetic/dialogue verse pairs into anchored rows)
- **TLB**: 260 chapters, 7,557 verse rows (paraphrase combining multi-verse spans into anchored rows)
