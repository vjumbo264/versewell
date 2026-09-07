# VerseWell — Architecture

VerseWell is a public Bible platform with two read-only faces over **one shared
source of truth: the static JSON tree** generated from the source `.sqlite`
files and served by Cloudflare Pages:

1. **REST API** (Cloudflare Worker) — `/api/v1/*` — a thin read-only layer that
   fetches the static JSON files at request time and re-shapes them per route.
2. **Reading website** (Cloudflare Pages, static) — dogfoods the public API and
   the static tree directly.

There is **no database, no KV, and no quota-limited / paid-tier storage service
anywhere in this architecture**. The static JSON files under `site/static-data/`
are the single, permanent source of truth for both the website and the Worker
API. No authentication, no accounts, no per-user state. Free tier only.
Production URLs: `https://versewell-api.<subdomain>.workers.dev` (API) and
`https://versewell.pages.dev` (site).

---

## 1. Source data inventory

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
  beginning…"`). The generator strips a leading heading line.
- `annotations(id PK, osis, link, content)` — footnotes and cross-references
  as HTML snippets (see §2).
- `chapters(id PK, reference_osis, reference_human, content, prev/next…)` —
  full-chapter HTML **plus, in CEV only, `Xxx.int` rows holding book-level
  introductions**. We do not keep the chapter HTML; we keep only the `.int`
  intro content (HTML stripped to text).
- `metadata(id PK, name, value)` — `name` (code), `fullname`, `date`,
  sometimes `copyright`, `url`, `css`. Used to populate the version index.
- `android_metadata` — irrelevant.

### Per-file quirks found

1. **CEV chapter offset.** `verses.verse` is shifted **+1 chapter** in CEV
   (no chapter 1; Genesis runs 2.001–51.xxx, John to 22.xxx, Revelation to
   23.xxx). Verified: CEV `John 4.016` = the John 3:16 text
   ("God loved the people of this world so much…"). The generator subtracts 1
   from the chapter for CEV. CEV footnote/crossref `verse_id`s (`Gen.1.1!f.1`)
   are **already in true reference space** — no shift applied to annotations.
2. **MSG merged verses.** `msg` has only 13,118 rows; consecutive verses are
   merged into single rows keyed by the first verse (e.g. Gen 1 has verses
   1, 3, 6, 9, 11…). Handled naturally; consumers must not assume contiguous
   verse numbers.
3. **Annotation styles** (`link` column):
   - `fen-XXX-{n}{letter}` (AMP, GW, NIV, NKJV, NLT, NLV, TLB, VOICE) —
     footnote; `n` is a **global per-book verse counter**. The authoritative
     target reference is inside `content` HTML: `title="Go to Book C:V"`.
     Prefix `cen-` = cross-reference, `fen-` = footnote. Only `fen-` kept as
     footnotes; `cen-` skipped.
   - `Book.C.V!f.N` USFM style (CEV, KJV, MSG) — `!f.` = footnote,
     `!x.` = cross-reference (skipped). Target verse is in the link itself.
4. **Leading section headings** in `verses.unformatted` (AMP `"The
   Creation\n…"`, GW, NIV `"The Beginning\n…"`, NKJV, NLT, NLV). The generator
   strips a leading heading line when the verse is the first of a chapter or
   the heading pattern is detected. Headings are not currently exposed
   separately; that is a possible future enhancement.

---

## 2. Normalized shape (in-memory `schema.sql`)

At build time the generator opens each `.sqlite` source and normalizes it into
an **in-memory** SQLite database shaped by [`schema.sql`](schema.sql), purely
as an intermediate representation. **No live database is ever created or
queried at runtime.** One schema fits every version; footnote/intro tables are
simply empty for versions lacking them. The generator then walks this in-memory
DB and writes plain JSON files to `site/static-data/`.

`schema.sql` defines four tables — `versions`, `verses`, `footnotes`,
`section_intros` — used only to structure the generator's in-memory model:

| unified | source |
|---|---|
| `versions.code` | `metadata.name` uppercased (`niv2011` → `NIV2011` → served as `NIV`, `voice` → `VOICE`, `msg` → `MSG`) |
| `versions.name` | `metadata.fullname` |
| `versions.description` | `metadata.copyright` (HTML stripped), if present |
| `versions.has_footnotes` | count of footnote-type annotations > 0 |
| `versions.has_intros` | any `Xxx.int` chapter rows (CEV only) |
| `verses.*` | `verses` table; `book` joined to `books` for name/order; **CEV: chapter − 1** |
| `footnotes.*` | `annotations` where link is `fen-*` or `*!f.*`; target parsed from `title="Go to Book C:V"` (fen style) or from the link (USFM style); HTML stripped |
| `section_intros.*` | CEV `chapters` rows with `reference_osis` = `Xxx.int`; stored as `chapter=1, start_verse=1, end_verse=<last verse of ch 1>` so they render as the bordered intro block before chapter 1 of the book |

---

## 3. Version-add workflow (auto-discovery, static-only)

The **entire** process to publish a new version live everywhere:

1. Drop `<code>.sqlite3` into [`/bible-sources/`](bible-sources/).
2. Push to `main`.

That's it. No manual steps, no import job, no quota, no waiting.

GitHub Actions (`deploy.yml`) then runs `scripts/generate_static.py`, which
scans `/bible-sources/`, normalizes each file in memory, and regenerates the
static JSON tree in `site/static-data/` (idempotent — only changed files are
rewritten). It then runs `scripts/check_static_consistency.py`, which **fails
the build loudly** if any source file has no corresponding static entry (or
any stale static tree has no source), so a silent discovery/indexing gap can
never ship unnoticed. Finally it deploys the Worker and the Pages site.

---

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
successful reads.

**How the Worker reads data (static-only, task-06):** the canonical static
tree lives on the Pages origin (`https://versewell.pages.dev/static-data/`),
the only copy of the data. The Worker fetches the specific JSON file(s) it
needs per request and caches parsed bodies **in-isolate** with a short TTL
(plus Cloudflare edge cache), so warm isolates serve repeat reads with zero
extra network hops. This keeps the Worker free-tier friendly — the ~60 MB
tree cannot be bundled into a Worker asset bundle — and guarantees the API
and the website read the **same bytes**. Search (`?q=`) is a static text scan
over a generator-emitted compact per-version `search.json`, so a query costs
**one** fetch, not one-per-chapter (which would exceed the Workers free-tier
subrequest limit). There is no SQL, no `LIKE`, no database.

## 5. Website (Pages, static)

Vanilla HTML/CSS/JS (no framework, no build step) in `/site/`. Pages: Home
(version picker + book/chapter navigator), Reader (`#/v/:version/:book/:chapter`
— intros as bordered blocks, footnotes as tappable superscript markers with an
expandable panel, prev/next chapter, version switcher), Search, API Docs
(`#/docs`). Dark/light mode via `prefers-color-scheme` + toggle. The site
reads the same static tree (and the public API) — no special backend access.

## 6. Infrastructure

| resource | name | created via |
|---|---|---|
| Worker | `versewell-api` | Cloudflare API + GitHub Actions |
| Pages project | `versewell` | Cloudflare API (Direct Upload via Actions) |
| GitHub secrets | `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` | GitHub API |

There is **no database resource** — the previous Cloudflare D1 database
(`versewell-db`) was deleted during the static-only migration. Names of
secrets/resources are documented; secret **values** live only in GitHub
Actions secrets, never in the repo.

---

## §7. Static JSON tree (`/static-data/`)

The **single source of truth** for all Bible content. At build time the
generator reads the `/bible-sources/*.sqlite*` files and writes plain JSON
into the repo at `site/static-data/` (the Pages project deploys only the
`site/` directory); Cloudflare Pages serves them as static assets at the
public `/static-data/...` URLs shown below. No database, no Worker compute for
direct access, no API key, no rate limit.

### Directory layout
```
/static-data/
  index.json                  <- {"versions":[...]}  (same fields as GET /api/v1/versions)
  {version}/                  <- lowercase version code, e.g. kjv
    index.json                <- {"version","name","books":[...]} (as GET /versions/{v}/books, plus a "slug" per book)
    search.json               <- compact per-version verse index used for ?q= search
    {book-slug}/              <- e.g. john
      {chapter}.json          <- e.g. 3.json (as GET /versions/{v}/{book}/{chapter})
```
### URL / book-slug rule
`{book-slug}` = the book's `book_name` lowercased, spaces/`_`/`+`/punctuation
collapsed to single `-` (e.g. `1-samuel`) — exactly the Worker API's
`normalizeBookParam`, so `/static-data/kjv/1-samuel/3.json` and
`/api/v1/versions/KJV/1 Samuel/3` address the same chapter. Collisions /
reserved words (`books|search|random|index`) are de-duplicated with a numeric
suffix; the authoritative slug per book is in that version's `index.json`.

### Shape parity & generator
Each `{chapter}.json` matches the Worker chapter response verbatim (`version,
book, book_name, chapter, intro, verses[], navigation`); indexes mirror
`/versions` and `/versions/{v}/books`. `scripts/generate_static.py` reuses
`scripts/import.py`'s real `import_file()` against an in-memory SQLite DB
shaped by `schema.sql`, so output is byte-for-byte identical to what the API
serves. Idempotent: files are only rewritten on content change. A new version
dropped into `/bible-sources/` is readable via this path — and therefore via
the Worker and the website — on the very next Pages deploy.
