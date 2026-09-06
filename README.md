# VerseWell

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

On the next deploy, GitHub Actions runs `scripts/apply_d1.py`, which:

- hashes every file in `/bible-sources/` and compares it to `versions.source_sha256` in D1 — files already imported unchanged are skipped;
- maps the new file onto the unified schema (`versions`, `verses`, `footnotes`, `section_intros`) via `scripts/import.py`, handling the known per-source quirks documented in `ARCHITECTURE.md`;
- writes the data to D1 in chunks, committing the `versions` row **last** as the completion marker, so a version only becomes visible to the API/site once it is fully imported (a version can never appear half-loaded).

The new version then shows up automatically in the version picker, the API, search, and the reader — zero code changes, zero manual D1 work.

> **Free-tier note:** Cloudflare D1's free tier allows roughly 100k row-writes per day per account. A large multi-version import spans several days; the deploy workflow also runs on a daily schedule (02:17 UTC) and automatically resumes where the quota cut it off. Nothing to do but wait.

## Repository layout

```
bible-sources/     one .sqlite3 per Bible version (source of truth for imports)
schema.sql         unified D1 schema (versions / verses / footnotes / section_intros)
scripts/import.py  source .sqlite3 -> unified-schema SQL (per-file quirks handled)
scripts/apply_d1.py applies import SQL to D1 via the Cloudflare API (quota-aware)
scripts/deploy_pages.sh  deploys /site to Cloudflare Pages (wrangler wrapper)
worker/            Cloudflare Worker implementing /api/v1/*
site/              static reading site (vanilla JS SPA, dogfoods the API)
.github/workflows/deploy.yml  import + deploy on push to main, plus daily import resume
ARCHITECTURE.md    source-file inventory, per-file schema quirks, mapping decisions
BUILD_STATE.json   build progress checkpoint (project memory across sessions)
```

## Infrastructure

- **Database:** Cloudflare D1 (`versewell-db`), one unified schema for all versions.
- **API:** Cloudflare Worker `versewell-api` (bound to D1), deployed by GitHub Actions via wrangler.
- **Site:** Cloudflare Pages project `versewell` (static files from `/site`), deployed by the same workflow.
- **CI/CD:** GitHub Actions — push to `main` → import new/changed sources into D1 → deploy worker → deploy site. A daily scheduled run resumes quota-limited imports.
- **Secrets:** `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` are stored as GitHub Actions secrets. The D1 database id is tracked in `.d1_database_id`.
- Free tier only; production URLs are the free `*.pages.dev` / `*.workers.dev` subdomains.
