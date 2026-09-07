# VerseWell — Architecture

VerseWell is a public Bible platform with two read-only faces over one shared
Cloudflare D1 database:

1. **REST API** (Cloudflare Worker) — `/api/v1/*`
2. **Reading website** (Cloudflare Pages, static) — dogfoods the public API

No authentication, no accounts, no per-user state. Free tier only.
Production URLs: `https://versewell-api.<subdomain>.workers.dev` (API) and
`https://versewell.pages.dev` (site).

---

## 1. Source data inventory (task-02)

Source: Google Drive folder `1DWkrc33kGOCTR0LFZhMzANFi_IryA5wn`
(fetched via public `embeddedfolderview` listing + `uc?export=download&id=` links —
no API key needed, works from CI and from a phone browser).

11 `.sqlite` files, all valid SQLite 3 databases. Every file shares the same
logical layout (an And Bible / bible-app style export), with per-file quirks:

| file | code (metadata `name`) | full name (metadata `fullname`) | verses | annotations | book intros (`*.int` chapters) |
|---|---|---|---|---|---|
| amp.sqlite3 | AMP | Amplified Bible | 31,103 | 4,498 | no |
| cev.sqlite3 | CEV | Contemporary English Version (US Version) | 28,980 | 8,489 | **yes — all 66 books** |
| gw.sqlite3 | GW | GOD'S WORD Translation | 31,084 | 1,348 | no |
| kjv.sqlite3 | KJV | King James Version | 31,102 | 1 | no |
| msg.sqlite3.db | MSG | The Message | 13,118 | 1 | no |
| niv2011.sqlite3 | NIV | New International Version | 31,102 | 55,779 | no |
| nkjv.sqlite3 | NKJV | New King James Version | 31,102 | 41,259 | no |
| nlt.sqlite3 | NLT | New Living Translation | 31,064 | 4,810 | no |
| nlv.sqlite3 | NLV | New Life Version | 31,102 | 481 | no |
| tlb.sqlite3 | TLB | Living Bible | 28,071 | 2,406 | no |
| voice.sqlite3 | VOICE | The Voice | 30,199 | 1,592 | no |

### Common source schema (all files)

- `books(number PK, osis, human, chapters)` — 66 rows; canonical OSIS book
  codes (`Gen`, `Exod`, `2Chr`, …), human names, chapter count.
  **CEV quirk:** `chapters` includes the `.int` pseudo-chapter (Genesis = 51).
- `verses(id PK, book CHAR(7), verse REAL, unformatted TEXT)` —
  `verse` is a float `chapter + verse/1000` (e.g. `1.001` = ch 1 v 1,
  `22.021` = ch 22 v 21). `unformatted` is plain verse text, sometimes with a
  leading section heading followed by `\n` (e.g. `"The Creation\nIn the
  beginning…"`). Importer must strip a leading heading line.
- `annotations(id PK, osis, link, content)` — footnotes and cross-references
  as HTML snippets (see §3).
- `chapters(id PK, reference_osis, reference_human, content, prev/next…)` —
  full-chapter HTML **plus, in CEV only, `Xxx.int` rows holding book-level
  introductions**. We do not import the chapter HTML; we import only the
  `.int` intro content (HTML stripped to text).
- `metadata(id PK, name, value)` — `name` (code), `fullname`, `date`,
  sometimes `copyright`, `url`, `css`. Used to populate `versions`.
- `android_metadata` — irrelevant.

### Per-file quirks found (task-02 verification)

1. **CEV chapter offset.** `verses.verse` is shifted **+1 chapter** in CEV
   (no chapter 1; Genesis runs 2.001–51.xxx, John to 22.xxx, Revelation to
   23.xxx). Verified: CEV `John 4.016` = the John 3:16 text
   ("God loved the people of this world so much…"). Importer subtracts 1 from
   the chapter for CEV. CEV footnote/crossref `verse_id`s (`Gen.1.1!f.1`)
   are **already in true reference space** — no shift applied to annotations.
2. **MSG merged verses.** `msg` has only 13,118 rows; consecutive verses are
   merged into single rows keyed by the first verse (e.g. Gen 1 has verses
   1, 3, 6, 9, 11…). Handled naturally by the schema; consumers must not
   assume contiguous verse numbers.
3. **Annotation styles** (`link` column):
   - `fen-XXX-{n}{letter}` (AMP, GW, NIV, NKJV, NLT, NLV, TLB, VOICE) —
     footnote; `n` is a **global per-book verse counter** (verified: AMP
     Gen 2:4 → `fen-AMP-35a`, i.e. 34 verses in Gen 1 + verse 4 of Gen 2).
     The authoritative target reference is inside `content` HTML:
     `title="Go to Book C:V"`. Prefix `cen-` = cross-reference, `fen-` =
     footnote. Only `fen-` imported as footnotes; `cen-` skipped.
   - `Book.C.V!f.N` USFM style (CEV, KJV, MSG) — `!f.` = footnote,
     `!x.` = cross-reference (skipped). Target verse is in the link itself.
4. **Leading section headings** in `verses.unformatted` (AMP `"The
   Creation\n…"`, GW, NIV `"The Beginning\n…"`, NKJV, NLT, NLV). Importer
   strips a leading heading line when the verse is the first of a chapter or
   the heading pattern is detected (short first line followed by `\n`).
   Headings are not currently exposed separately; that is a possible future
   enhancement (recorded in BUILD_STATE decisions).

---

## 2. Unified D1 schema (task-03)

One schema fits every version; footnote/intro tables are simply empty for
versions lacking them. File: [`schema.sql`](schema.sql).

```sql
CREATE TABLE IF NOT EXISTS versions (
  code        TEXT PRIMARY KEY,      -- e.g. 'AMP', 'NIV2011' (uppercased, unique per file)
  name        TEXT NOT NULL,         -- display name from metadata.fullname
  language    TEXT NOT NULL DEFAULT 'en',
  description TEXT,                  -- copyright/attribution text if present
  has_footnotes INTEGER NOT NULL DEFAULT 0,
  has_intros    INTEGER NOT NULL DEFAULT 0,
  source_file   TEXT NOT NULL,       -- filename in /bible-sources/
  source_sha256 TEXT NOT NULL,       -- change detection for re-import
  verse_count   INTEGER NOT NULL DEFAULT 0,
  imported_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS verses (
  version TEXT NOT NULL REFERENCES versions(code),
  book    TEXT NOT NULL,             -- OSIS code, e.g. 'Gen'
  book_name TEXT NOT NULL,           -- human name, e.g. 'Genesis'
  book_order INTEGER NOT NULL,       -- 1..66 for sorting
  chapter INTEGER NOT NULL,
  verse   INTEGER NOT NULL,
  text    TEXT NOT NULL,
  PRIMARY KEY (version, book, chapter, verse)
);

CREATE TABLE IF NOT EXISTS footnotes (
  version TEXT NOT NULL,
  book    TEXT NOT NULL,
  chapter INTEGER NOT NULL,
  verse   INTEGER NOT NULL,
  marker  TEXT,                      -- e.g. 'a', 'b' or '1' — source ordering hint
  note_text TEXT NOT NULL,           -- HTML stripped to readable text (inline refs kept as text)
  PRIMARY KEY (version, book, chapter, verse, marker)
);

CREATE TABLE IF NOT EXISTS section_intros (
  version   TEXT NOT NULL,
  book      TEXT NOT NULL,
  chapter   INTEGER NOT NULL,        -- chapter where the intro is displayed (1 for book intros)
  start_verse INTEGER NOT NULL,
  end_verse   INTEGER NOT NULL,
  intro_text TEXT NOT NULL,
  PRIMARY KEY (version, book, chapter, start_verse)
);

CREATE INDEX IF NOT EXISTS idx_verses_lookup ON verses(version, book, chapter, verse);
CREATE INDEX IF NOT EXISTS idx_footnotes_lookup ON footnotes(version, book, chapter, verse);
CREATE INDEX IF NOT EXISTS idx_intros_lookup ON section_intros(version, book, chapter);
```

### Source → unified mapping

| unified | source |
|---|---|
| `versions.code` | `metadata.name` uppercased (`niv2011` → `NIV2011`, `voice` → `VOICE`, `msg` → `MSG`) |
| `versions.name` | `metadata.fullname` |
| `versions.description` | `metadata.copyright` (HTML stripped), if present |
| `versions.has_footnotes` | count of footnote-type annotations > 0 |
| `versions.has_intros` | any `Xxx.int` chapter rows (CEV only) |
| `verses.*` | `verses` table; `book` joined to `books` for name/order; **CEV: chapter − 1** |
| `footnotes.*` | `annotations` where link is `fen-*` or `*!f.*`; target parsed from `title="Go to Book C:V"` (fen style) or from the link (USFM style); HTML stripped |
| `section_intros.*` | CEV `chapters` rows with `reference_osis` = `Xxx.int`; stored as `chapter=1, start_verse=1, end_verse=<last verse of ch 1>` so they render as the bordered intro block before chapter 1 of the book |

---

## 3. Version-add workflow (auto-discovery)

- `/bible-sources/<code>.sqlite3` — one file per version, committed to the
  repo. (`msg.sqlite3.db` is stored as `msg.sqlite3` for consistency.)
- The importer (`scripts/import.py`) computes each file's SHA-256 and
  compares it to `versions.source_sha256` in D1: **new or changed files are
  (re)imported; unchanged files are skipped** — so deploys are incremental
  and a new version goes live by dropping one file into the folder and
  pushing. No code changes, no manual D1 work.
- GitHub Actions (`deploy.yml`) runs the importer against remote D1 via the
  Cloudflare API on every push that touches `bible-sources/` or the app code.

## 4. API surface (Worker, `/api/v1/`)

```
GET /api/v1/versions
GET /api/v1/versions/:version/books
GET /api/v1/versions/:version/:book
GET /api/v1/versions/:version/:book/:chapter
GET /api/v1/versions/:version/:book/:chapter/:verse
GET /api/v1/versions/:version/search?q=...
GET /api/v1/versions/:version/random
```

Chapter/verse responses embed `intro` (when a section intro covers them) and
per-verse `footnotes`; `?notes=false` omits them. CORS `*`, JSON error shape
`{"error": {"code", "message"}}`, `Cache-Control: public, max-age=86400` on
successful reads. SQL is parameterized throughout; search uses `LIKE`.

## 5. Website (Pages, static)

Vanilla HTML/CSS/JS (no framework, no build step) in `/site/`. Pages: Home
(version picker + book/chapter navigator), Reader (`#/v/:version/:book/:chapter`
— intros as bordered blocks, footnotes as tappable superscript markers with an
expandable panel, prev/next chapter, version switcher), Search, API Docs
(`#/docs`). Dark/light mode via `prefers-color-scheme` + toggle. The site
calls only the public API — no special backend access.

## 6. Infrastructure

| resource | name | created via |
|---|---|---|
| D1 database | `versewell-db` | Cloudflare API |
| Worker | `versewell-api` | Cloudflare API + GitHub Actions |
| Pages project | `versewell` | Cloudflare API (Direct Upload via Actions) |
| GitHub secrets | `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` | GitHub API |

Names of secrets/resources are documented; secret **values** live only in
GitHub Actions secrets / Cloudflare bindings, never in the repo.

---

## §7. Static JSON mirror (`/static-data/`)

A **database-free** way to read the exact same Bible text. At build time a generator reads the same `/bible-sources/*.sqlite*` files the D1 importer reads and writes plain JSON into the repo; Cloudflare Pages serves them as static assets. No D1, no Worker, no API key, no rate limit — the answer to D1's free-tier daily row-write quota throttling new-version imports.

### Directory layout
```
/static-data/
  index.json                  <- {"versions":[...]}  (same fields as GET /api/v1/versions)
  {version}/                  <- lowercase version code, e.g. kjv
    index.json                <- {"version","name","books":[...]} (as GET /versions/{v}/books, plus a "slug" per book)
    {book-slug}/              <- e.g. john
      {chapter}.json          <- e.g. 3.json (as GET /versions/{v}/{book}/{chapter})
```
### URL / book-slug rule
`{book-slug}` = the book's `book_name` lowercased, spaces/`_`/`+`/punctuation collapsed to single `-` (e.g. `1-samuel`) — exactly the Worker API's `normalizeBookParam`, so `/static-data/kjv/1-samuel/3.json` and `/api/v1/versions/KJV/1 Samuel/3` address the same chapter. Collisions / reserved words (`books|search|random|index`) are de-duplicated with a numeric suffix; the authoritative slug per book is in that version's `index.json`.
### Shape parity & generator
Each `{chapter}.json` is the Worker chapter response verbatim (`version, book, book_name, chapter, intro, verses[], navigation`); indexes mirror `/versions` and `/versions/{v}/books`. `scripts/generate_static.py` reuses `scripts/import.py`'s real `import_file()` against an in-memory SQLite DB shaped by `schema.sql`, so output is byte-for-byte identical to the D1/Worker path (verified: static KJV John 3 == live API, 36 verses + navigation). Idempotent: files are only rewritten on content change (verified: 2nd run = 0 writes). A new version dropped into `/bible-sources/` is readable via this path on the very next Pages deploy, independent of its D1 import state.
