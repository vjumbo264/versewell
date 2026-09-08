# VerseWell

**Chapter audio narration** is available for a fixed set of 12 versions (AMP, CEV, GW, KJV, MSG, NIV, NKJV, NLT, NLV, TLB, TPT, VOICE), each with its own distinct Edge-TTS narrator voice. Narration is generated offline by the manually-triggered `generate-audio` GitHub Actions workflow and committed as 48 kbps M4A files under `site/static-data/{version}/{book}/{chapter}.m4a` (incrementally, as the parallel render proceeds — each batch push auto-deploys); chapter JSON exposes it via `audio_url` (`null` when unavailable). The scope is a closed allowlist in `AUDIO_STATE.json` — versions added later never automatically get audio. See `ARCHITECTURE.md` §8 for the full contract.

A free, public Bible platform with two faces over one shared dataset:

- **Reading site** — https://versewell.pages.dev — a clean, mobile-first Bible reader with section introductions and footnotes rendered inline as part of the reading experience.
- **Public REST API** — https://versewell-api.vjumbo264.workers.dev — the same data, for any developer, app, or website. No API key, no signup, CORS open to all origins. The website itself is just a client of this API.

All translations are produced by the operator's church and published here with redistribution rights confirmed.

---

## Using the website

Open https://versewell.pages.dev and:

1. **Pick a translation** on the home page (or use the version switcher in the top bar at any time).
2. **Pick a book**, then a chapter.
3. **Read.** Section introductions appear as a bordered block above the verses they cover. Verses with footnotes show small `[n]` markers — tap one to jump to the note at the bottom of the chapter.
4. **Navigate** with the ← Previous / Next → links at the top and bottom of each chapter.
5. **Search** from the Search tab (searches the currently selected translation).
6. Dark/light mode: the ◐ button in the top bar. Your choice is remembered.

## Using the API

Base URL: `https://versewell-api.vjumbo264.workers.dev`

| Endpoint | Description |
|---|---|
| `GET /api/v1/versions` | List all available Bible versions |
| `GET /api/v1/versions/{version}/books` | List books of a version (with chapter counts) |
| `GET /api/v1/versions/{version}/{book}` | Book metadata |
| `GET /api/v1/versions/{version}/{book}/{chapter}` | Full chapter: verses + section intro + footnotes |
| `GET /api/v1/versions/{version}/{book}/{chapter}/{verse}` | A single verse, with its footnotes |
| `GET /api/v1/versions/{version}/search?q=…` | Search verse text (first 50 matches) |
| `GET /api/v1/versions/{version}/random` | A random verse |

- `{version}` is the version code (`KJV`, `CEV`, `NIV2011`, …) — case-insensitive; get the list from `/api/v1/versions`.
- `{book}` accepts an OSIS code (`Gen`, `John`) or full name (`Genesis`, `1%20John`).
- Chapter and verse responses nest footnotes and section intros by default; add `?notes=false` for a lean text-only response.
- Errors: `{"error": {"code": "…", "message": "…"}}` with 400/404 as appropriate.
- Successful reads are cacheable for 24 h (`Cache-Control: public, max-age=86400`).

Full interactive documentation with example requests/responses lives on the site's **API** tab: https://versewell.pages.dev/#/docs

## Adding a new Bible version (no manual database work)

Everything is designed to be doable from a phone browser or Termux — no local CLI tooling is ever required.

1. Get the new translation as a `.sqlite3` file in the same export format as the existing sources (tables `books`, `verses`, `annotations`, `chapters`, `metadata` — see `ARCHITECTURE.md`).
2. Drop it into the [`/bible-sources/`](bible-sources/) folder of this repository (GitHub web UI: *Add file → Upload files* works fine from a phone; or `git push` from Termux).
3. Push to `main`. That's it.

On the next deploy, GitHub Actions runs `scripts/generate_static.py`, which scans `/bible-sources/`, normalizes each `.sqlite` file in memory (handling the known per-source quirks documented in `ARCHITECTURE.md`), and regenerates the static JSON tree under `site/static-data/`. A consistency check then **fails the build loudly** if any source file has no static entry, so a version can never silently fail to appear.

The new version then shows up automatically in the version picker, the API, search, and the reader — zero code changes, zero database work, zero waiting on any quota or schedule.

> **No database, no quota:** VerseWell is backed entirely by static JSON files. There is no database and no usage limit of any kind. Adding a version is exactly "drop a file in `/bible-sources/` and push."

## Repository layout

```
bible-sources/     one .sqlite3 per Bible version (the single source of truth)
schema.sql         normalized in-memory schema the generator builds the tree from (no live DB)
scripts/import.py  source .sqlite3 -> normalized records (per-file quirks handled)
scripts/generate_static.py  builds the site/static-data/ JSON tree from the sources
scripts/check_static_consistency.py  CI guard: fails build on any source<->mirror gap
worker/            Cloudflare Worker implementing /api/v1/* (reads the static JSON tree)
site/              static reading site (vanilla JS SPA) + the static-data/ tree
.github/workflows/deploy.yml  generate static tree -> consistency check -> deploy worker + site
ARCHITECTURE.md    source-file inventory, per-file schema quirks, mapping decisions
BUILD_STATE.json   build progress checkpoint (project memory across sessions)
```

## Infrastructure

- **Storage:** none — no database, no KV, no quota-limited service. The static JSON tree under `site/static-data/` (served by Pages) is the single source of truth.
- **API:** Cloudflare Worker `versewell-api` (a thin read-only layer that fetches the static JSON tree at request time), deployed by GitHub Actions via wrangler.
- **Site:** Cloudflare Pages project `versewell` (static files from `/site`, including `static-data/`), deployed by the same workflow.
- **CI/CD:** GitHub Actions — push to `main` → regenerate the static tree → consistency check → deploy worker → deploy site. No scheduled jobs, no import resume.
- **Secrets:** `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` are stored as GitHub Actions secrets.
- Free tier only; production URLs are the free `*.pages.dev` / `*.workers.dev` subdomains.
