/**
 * VerseWell REST API worker (static-only migration, task-06).
 *
 * The API is now backed ENTIRELY by the static JSON tree the generator writes
 * to site/static-data/ and Cloudflare Pages serves at
 *   https://versewell.pages.dev/static-data/
 * There is NO database, NO KV, and no quota-limited storage anywhere in this
 * path — the Worker is a thin read-only layer over those public static files.
 *
 * Routes (all under /api/v1/), response shapes unchanged from the D1 version:
 *   GET /api/v1/versions
 *   GET /api/v1/versions/:version/books
 *   GET /api/v1/versions/:version/:book
 *   GET /api/v1/versions/:version/:book/:chapter
 *   GET /api/v1/versions/:version/:book/:chapter/:verse
 *   GET /api/v1/versions/:version/search?q=...
 *   GET /api/v1/versions/:version/random
 *
 * Chapter/verse responses nest `intros[]` (plus back-compat `intro`) and
 * per-verse `footnotes`; pass ?notes=false to omit them. CORS is open (public API). Errors use a
 * consistent shape: {"error": {code, message}}.
 *
 * Fetch strategy: the canonical tree lives on the Pages origin (the only copy
 * of the data). The Worker fetches the specific JSON file(s) it needs per
 * request and caches parsed bodies in-module (per-isolate) with a short TTL,
 * so warm isolates serve repeat reads with zero extra network hops. This
 * keeps the Worker free-tier friendly (no asset-size limit pressure from the
 * ~60MB tree) and guarantees the API and the website read the SAME bytes.
 */

const STATIC_BASE = 'https://versewell.pages.dev/static-data';

const CORS_HEADERS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
  'Access-Control-Max-Age': '86400',
};

const SEARCH_LIMIT = 50;
const CACHE_TTL_MS = 5 * 60 * 1000; // per-isolate cache lifetime

// In-module (per-isolate) cache: path -> { t, data }.
const cache = new Map();

async function fetchJson(path) {
  const hit = cache.get(path);
  const now = Date.now();
  if (hit && now - hit.t < CACHE_TTL_MS) return hit.data;

  const res = await fetch(`${STATIC_BASE}${path}`, {
    headers: { Accept: 'application/json' },
    cf: { cacheTtl: 300, cacheEverything: true },
  });
  if (res.status === 404) {
    cache.set(path, { t: now, data: null });
    return null;
  }
  if (!res.ok) throw new Error(`static fetch failed: ${res.status} for ${path}`);
  const ct = res.headers.get('content-type') || '';
  if (!ct.includes('application/json')) {
    // Pages SPA fallback returns text/html for unknown paths — treat as miss.
    cache.set(path, { t: now, data: null });
    return null;
  }
  const data = await res.json();
  cache.set(path, { t: now, data });
  return data;
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      'content-type': 'application/json; charset=utf-8',
      'Cache-Control': status === 200 ? 'public, max-age=86400' : 'no-store',
      ...CORS_HEADERS,
    },
  });
}

function fail(status, code, message) {
  return json({ error: { code, message } }, status);
}
function badRequest(message) {
  return fail(400, 'bad_request', message);
}
function notFound(message) {
  return fail(404, 'not_found', message);
}

/* ---------- static-tree accessors ---------- */

async function getTopIndex() {
  return fetchJson('/index.json'); // { versions: [...] } or null
}

async function getVersion(code) {
  const idx = await getTopIndex();
  if (!idx) return null;
  const up = String(code).toUpperCase();
  return idx.versions.find((v) => v.code.toUpperCase() === up) || null;
}

async function getVersionIndex(versionCode) {
  // { version, name, books: [{book, book_name, book_order, chapters, verse_count, slug}] }
  return fetchJson(`/${versionCode.toLowerCase()}/index.json`);
}

function normalizeBookParam(raw) {
  return decodeURIComponent(raw).replace(/[-_+]/g, ' ').replace(/\s+/g, ' ').trim().toLowerCase();
}

function findBook(books, param) {
  const p = normalizeBookParam(param);
  return (
    books.find((b) => b.book.toLowerCase() === p) ||
    books.find((b) => b.book_name.toLowerCase() === p) ||
    null
  );
}

function bookForChapter(versionIndex, bookRow) {
  return bookRow;
}

/* ---------- route handlers (static-backed) ---------- */

async function handleChapter(versionRow, versionIndex, bookRow, chapter, wantNotes) {
  const data = await fetchJson(
    `/${versionRow.code.toLowerCase()}/${bookRow.slug}/${chapter}.json`
  );
  if (!data) {
    return notFound(`Chapter ${chapter} not found in ${bookRow.book_name} (${versionRow.code}).`);
  }
  // Static chapter payload matches the API shape; honor ?notes=false by
  // stripping the intro and per-verse footnotes the file always embeds.
  const verses = data.verses.map((v) =>
    wantNotes ? v : { verse: v.verse, text: v.text }
  );
  return json({
    version: versionRow.code,
    book: data.book,
    book_name: data.book_name,
    chapter: data.chapter,
    // audio_url: committed narration M4A (AAC 48k) path, or null when the
    // version is outside the fixed audio allowlist / not yet generated.
    audio_url: data.audio_url ?? null,
    ...(wantNotes
      ? {
          intro: (data.intros && data.intros[0]) ?? data.intro ?? null,
          intros: data.intros ?? (data.intro ? [data.intro] : null),
        }
      : {}),
    verses,
    navigation: data.navigation,
  });
}

async function handleVerse(versionRow, bookRow, chapter, verseNum, wantNotes) {
  const data = await fetchJson(
    `/${versionRow.code.toLowerCase()}/${bookRow.slug}/${chapter}.json`
  );
  if (!data) {
    return notFound(`Chapter ${chapter} not found in ${bookRow.book_name} (${versionRow.code}).`);
  }
  const v = data.verses.find((x) => x.verse === verseNum);
  if (!v) {
    return notFound(`Verse ${bookRow.book_name} ${chapter}:${verseNum} not found (${versionRow.code}).`);
  }
  const allIntros = data.intros ?? (data.intro ? [data.intro] : []);
  let intro = null;
  if (wantNotes) {
    intro =
      allIntros.find((it) => it.start_verse <= verseNum && verseNum <= it.end_verse) || null;
  }
  return json({
    version: versionRow.code,
    book: data.book,
    book_name: data.book_name,
    chapter: data.chapter,
    verse: v.verse,
    text: v.text,
    ...(wantNotes
      ? { footnotes: v.footnotes || [], intro, intros: allIntros.length ? allIntros : null }
      : {}),
  });
}

async function handleSearch(versionRow, versionIndex, url) {
  const q = (url.searchParams.get('q') || '').trim();
  if (!q) return badRequest('Missing required query parameter: q');
  if (q.length > 200) return badRequest('Query too long (max 200 characters).');
  const needle = q.toLowerCase();
  // Single fetch of the compact per-version search index (search.json) —
  // avoids one subrequest per chapter, which would exceed the Workers
  // free-tier request limit for a 1,000+ chapter version.
  const idx = await fetchJson(`/${versionRow.code.toLowerCase()}/search.json`);
  const entries = (idx && idx.entries) || [];
  const results = [];
  for (const e of entries) {
    if (e.text && e.text.toLowerCase().includes(needle)) {
      results.push({
        book: e.book,
        book_name: e.book_name,
        chapter: e.chapter,
        verse: e.verse,
        text: e.text,
      });
      if (results.length >= SEARCH_LIMIT) break;
    }
  }
  return json({
    version: versionRow.code,
    query: q,
    count: results.length,
    limit: SEARCH_LIMIT,
    results,
  });
}

async function handleRandom(versionRow, versionIndex, wantNotes) {
  const books = versionIndex.books;
  if (!books.length) return notFound(`Version ${versionRow.code} has no verses.`);
  // Pick a random book, then a random chapter, then a random verse.
  const b = books[Math.floor(Math.random() * books.length)];
  const ch = 1 + Math.floor(Math.random() * b.chapters);
  const data = await fetchJson(`/${versionRow.code.toLowerCase()}/${b.slug}/${ch}.json`);
  if (!data || !data.verses.length) {
    return notFound(`Version ${versionRow.code} has no verses.`);
  }
  const v = data.verses[Math.floor(Math.random() * data.verses.length)];
  return json({
    version: versionRow.code,
    book: data.book,
    book_name: data.book_name,
    chapter: data.chapter,
    verse: v.verse,
    text: v.text,
    ...(wantNotes ? { footnotes: v.footnotes || [] } : {}),
  });
}

export default {
  async fetch(request, env) {
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }
    if (request.method !== 'GET') {
      return fail(405, 'method_not_allowed', 'Only GET is supported.');
    }

    const url = new URL(request.url);
    const segments = url.pathname.split('/').filter(Boolean); // e.g. ['api','v1','versions',...]

    if (url.pathname === '/' || url.pathname === '/api/v1' || url.pathname === '/api/v1/') {
      return json({
        name: 'VerseWell API',
        version: 'v1',
        endpoints: [
          'GET /api/v1/versions',
          'GET /api/v1/versions/:version/books',
          'GET /api/v1/versions/:version/:book',
          'GET /api/v1/versions/:version/:book/:chapter',
          'GET /api/v1/versions/:version/:book/:chapter/:verse',
          'GET /api/v1/versions/:version/search?q=...',
          'GET /api/v1/versions/:version/random',
        ],
        docs: 'See the VerseWell site /docs page for full documentation.',
      });
    }

    if (segments[0] !== 'api' || segments[1] !== 'v1' || segments[2] !== 'versions') {
      return notFound('Unknown route.');
    }

    const wantNotes = url.searchParams.get('notes') !== 'false';

    // GET /api/v1/versions
    if (segments.length === 3) {
      const idx = await getTopIndex();
      const versions = (idx && idx.versions) || [];
      return json({
        versions: versions
          .slice()
          .sort((a, b) => a.code.localeCompare(b.code))
          .map((v) => ({
            code: v.code,
            name: v.name,
            language: v.language,
            description: v.description,
            has_footnotes: !!v.has_footnotes,
            has_intros: !!v.has_intros,
            verse_count: v.verse_count,
          })),
      });
    }

    const versionParam = segments[3];
    const versionRow = await getVersion(versionParam);
    if (!versionRow) return notFound(`Unknown version '${versionParam}'.`);
    const versionIndex = await getVersionIndex(versionRow.code);
    if (!versionIndex) return notFound(`Unknown version '${versionParam}'.`);

    // GET /api/v1/versions/:version/search?q=...
    if (segments.length === 5 && segments[4] === 'search') {
      return handleSearch(versionRow, versionIndex, url);
    }
    // GET /api/v1/versions/:version/random
    if (segments.length === 5 && segments[4] === 'random') {
      return handleRandom(versionRow, versionIndex, wantNotes);
    }
    // NOTE: 'books', 'search' and 'random' are reserved path segments and never book names.

    // GET /api/v1/versions/:version/books
    if (segments.length === 5 && segments[4] === 'books') {
      return json({
        version: versionRow.code,
        books: versionIndex.books.map((b) => ({
          book: b.book,
          book_name: b.book_name,
          book_order: b.book_order,
          chapters: b.chapters,
          verse_count: b.verse_count,
        })),
      });
    }

    if (segments.length === 4) {
      return badRequest("Expected 'books', 'search', 'random', or a book name after the version.");
    }

    const bookRow = findBook(versionIndex.books, segments[4]);
    if (!bookRow) return notFound(`Unknown book '${decodeURIComponent(segments[4])}' for version ${versionRow.code}.`);

    // GET /api/v1/versions/:version/:book
    if (segments.length === 5) {
      return json({
        version: versionRow.code,
        book: bookRow.book,
        book_name: bookRow.book_name,
        book_order: bookRow.book_order,
        chapters: bookRow.chapters,
        verse_count: bookRow.verse_count,
      });
    }

    const chapter = Number(segments[5]);
    if (!Number.isInteger(chapter) || chapter < 1) {
      return badRequest(`Invalid chapter '${decodeURIComponent(segments[5])}'.`);
    }

    // GET /api/v1/versions/:version/:book/:chapter
    if (segments.length === 6) {
      return handleChapter(versionRow, versionIndex, bookRow, chapter, wantNotes);
    }

    const verseNum = Number(segments[6]);
    if (!Number.isInteger(verseNum) || verseNum < 1) {
      return badRequest(`Invalid verse '${decodeURIComponent(segments[6])}'.`);
    }

    // GET /api/v1/versions/:version/:book/:chapter/:verse
    if (segments.length === 7) {
      return handleVerse(versionRow, bookRow, chapter, verseNum, wantNotes);
    }

    return notFound('Unknown route.');
  },
};
