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
| voice.sqlite3 | VOICE | The Voice | 30,199 | 1,592 | **yes — 756 chapters carry `long-aside` commentary blocks** |

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
5. **VOICE long-aside commentary (corrects earlier 'no intros' claim).**
   756 VOICE chapters embed `<div class="long-aside">` commentary/intro
   blocks in the chapter HTML, and the same prose is *also* concatenated
   into the anchored verse's `unformatted` text. That is the root cause of
   the two intro bugs: the prose was merged into verse text on the site, and
   no intros were ever extracted for VOICE. The importer now lifts each
   aside into `section_intros` (anchored at its verse via the inner span's
   `class="text Book-C-V"`, in source chapter numbering) and strips it from
   the verse text. An aside that fails to match its verse text prefix logs a
   loud WARNING at generation time — a schema drift can never fail silently
   again. Direct inspection confirmed the remaining files (AMP, GW, KJV,
   MSG, NIV2011, NKJV, NLT, NLV, TLB) genuinely have no intro data (no
   `.int` rows, no sub-verse rows, no long-aside blocks, no intro-like
   annotations); AMP's single 'introduction' annotation is an ordinary
   footnote.

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
| `section_intros.*` | Two real per-file shapes (verified by direct inspection, not assumed): (1) CEV `chapters` rows with `reference_osis` = `Xxx.int` — book intros, stored as `chapter=1, start_verse=1, end_verse=<last verse of ch 1>`; (2) VOICE `long-aside` blocks (quirk 5) — stored anchored at their verse, `start_verse=end_verse=<anchor verse>`. A chapter may carry multiple intros; chapter JSON exposes `intros[]` plus the singular `intro` for back-compat. |

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

Chapter/verse responses embed `intros[]` (all intros in the chapter, plus the
back-compat singular `intro`) and per-verse `footnotes`; `?notes=false` omits
them. CORS `*`, JSON error shape
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
— intros as always-visible bordered blocks rendered inline before the verse
range they introduce; footnotes as ONE tappable marker per verse (grouping all
of that verse's notes) with inline expand/collapse at that verse's position —
only one panel open at a time, no bottom-of-chapter list — prev/next chapter,
version switcher), Search, API Docs
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
book, book_name, chapter, intros, verses[], navigation`); indexes mirror
`/versions` and `/versions/{v}/books`. `scripts/generate_static.py` reuses
`scripts/import.py`'s real `import_file()` against an in-memory SQLite DB
shaped by `schema.sql`, so output is byte-for-byte identical to what the API
serves. Idempotent: files are only rewritten on content change. A new version
dropped into `/bible-sources/` is readable via this path — and therefore via
the Worker and the website — on the very next Pages deploy.

---

## §8. Chapter audio narration (fixed-scope feature)

Each chapter of a **fixed, closed set of versions** has an AI-narrated audio
file, generated once offline and committed into the repo alongside the static
JSON mirror — never generated on demand, never regenerated on deploy.

### Scope (closed allowlist)

`AUDIO_STATE.json.audio_enabled_versions` is the permanent allowlist, captured
from the exact contents of `/bible-sources/` when the feature was built:
`AMP, CEV, GW, KJV, MSG, NIV, NKJV, NLT, NLV, TLB, TPT, VOICE` (12 versions,
VOICE included after `voice.sqlite3` was moved out of staging).
**A version added to `/bible-sources/` later NEVER automatically receives
audio** — `scripts/generate_audio.py` enforces the allowlist at runtime, and
`generate_static.py` only emits a non-null `audio_url` for allowlisted codes.

### Synthesis (ported verbatim from ClipForge `pipeline/stage_b/voiceover.py`)

- Engine: Microsoft Edge TTS via `edge-tts` (`edge_tts.Communicate`), no API key.
- Retry: 3 attempts, linear backoff (2 s × attempt).
- Normalize: ffmpeg → 24 kHz mono `pcm_s16le` WAV.
- Mastering: ClipForge's `speech_clarity_v1` chain unchanged — highpass 70 Hz
  (2 poles), +1.5 dB presence EQ at 3 kHz (Q 1.1), acompressor 1.5:1
  (threshold 0.125, attack 15, release 120), two-pass EBU R128 loudnorm
  targeting **-16 LUFS / 7 LU / -1.5 dBTP**, final `alimiter` 0.84.
- VerseWell overrides: **rate `+0%`** (calm long-form Scripture pace — NOT
  ClipForge's brisk `+20%`), final storage format **M4A (AAC) 48 kbps mono 24 kHz**
  — a speech-only encode that keeps narration intelligible while cutting
  full-allowlist repo storage ~4x vs 96 kbps (~40 GB → ~10 GB).

### Voice assignment (distinct voice per version, alternating gender)

Versions sorted alphabetically; voices assigned Male, Female, Male, …
Calm narrator/conversational styles only (energetic/casual voices excluded):

| version | voice | gender |
|---|---|---|
| AMP | en-US-AndrewNeural | M |
| CEV | en-US-AvaNeural | F |
| GW | en-US-ChristopherNeural | M |
| KJV | en-GB-SoniaNeural | F |
| MSG | en-US-EricNeural | M |
| NIV | en-US-JennyNeural | F |
| NKJV | en-GB-RyanNeural | M |
| NLT | en-US-MichelleNeural | F |
| NLV | en-US-DavisNeural | M |
| TLB | en-US-AriaNeural | F |
| TPT | en-US-RogerNeural | M |
| VOICE | en-US-NancyNeural | F |

### Storage, trigger, API surface

- Files: `site/static-data/{version_lower}/{book_slug}/{chapter}.m4a`
  (colocated with the chapter JSON, deployed by Pages with the same tree).
  M4A/AAC 48 kbps keeps the full 13,528-chapter allowlist near ~4.5 GB —
  inside GitHub's 5 GB repo soft cap and Pages' 25 MiB/file limit.
- Trigger: `.github/workflows/generate-audio.yml` — **workflow_dispatch
  only**, idempotent (any chapter whose committed `.m4a` exists is skipped),
  **parallel** (`--jobs N` concurrent Edge TTS workers, default 4), supports
  partial runs via `versions` / `max_chapters` inputs.
- **Incremental publish (as-it-renders):** every `--commit-interval`
  successes (default 100) the generator refreshes `audio_url` JSON, commits
  the new `.m4a` files and pushes; each push auto-triggers `deploy.yml`, so
  audio appears on the live site continuously and a crash loses at most one
  small batch — resume state IS the git tree, no separate manifest file.
- **Soft-stop at 5h00m:** a watchdog touches a stop-file at minute 300 so the
  run winds down, commits its final partial batch and exits 0 long before
  the 6 h hard kill (an earlier revision died at ~5h50m with nothing
  committed, losing the whole run and never dispatching the next slice).
- **Auto-resume (hands-free to completion):** if chapters remain when the
  slice ends, the workflow re-dispatches itself via the `AUDIO_RESUME_TOKEN`
  secret (fine-grained PAT, `actions:write`) with input `auto=1`, chaining
  slices until the render is complete. Without the secret the chain stops
  with a warning and an operator re-triggers manually.
- **Self-cleanup:** when a resume-chain run finds 0 chapters remaining it
  refreshes `audio_url` fields, then `git rm`s the workflow file and
  `scripts/generate_audio.py`, commits and pushes — removing all audio
  GENERATION artifacts from `main` while keeping the rendered `.m4a` files
  and the `audio_url` JSON the live site needs. A manual dispatch
  (`auto != 1`) never self-deletes.
- Chapter JSON (static mirror and Worker) carries `audio_url` — the M4A path,
  or `null` for non-allowlisted versions / not-yet-generated chapters. The
  Reader renders an `<audio>` player only when `audio_url` is non-null.

### Canonical OT/NT classification (Part 2)

Every book in a version's `index.json` carries a `testament` field (`OT`/`NT`)
derived from the fixed canonical OSIS lists in `generate_static.py` — never
from `book_order`, which is version-local (TPT numbers its 30 books 1..30
starting at Psalms, which previously mis-filed Matt..Rev under 'Old
Testament'). Clients group strictly by `testament`.
