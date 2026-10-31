/**
 * VerseWell REST API worker with Audio Narration Support.
 *
 * Supported Routes (all under /api/v1/):
 *   GET /api/v1/versions
 *   GET /api/v1/versions/:version/books
 *   GET /api/v1/versions/:version/:book
 *   GET /api/v1/versions/:version/:book/:chapter
 *   GET /api/v1/versions/:version/:book/:chapter/:verse
 *   GET /api/v1/versions/:version/search?q=...
 *   GET /api/v1/versions/:version/random
 *   GET /api/v1/audio/:version/manifest
 *   GET /api/v1/audio/:version/:book/:chapter
 */

const STATIC_BASE = 'https://versewell.pages.dev/static-data';
const AUDIO_RELEASE_BASE = 'https://github.com/vjumbo264/versewell/releases/download';

const CORS_HEADERS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
  'Access-Control-Allow-Headers': 'Range, Content-Type, Authorization',
  'Access-Control-Expose-Headers': 'Content-Range, Content-Length, Accept-Ranges, ETag',
  'Access-Control-Max-Age': '86400',
};

const SEARCH_LIMIT = 50;
const CACHE_TTL_MS = 5 * 60 * 1000; // per-isolate cache lifetime

const AUDIO_CONFIG = {
  kjv: { voice: 'en-US-ChristopherNeural', title: 'King James Version' },
  niv: { voice: 'en-US-JennyNeural', title: 'New International Version' },
  tlb: { voice: 'en-US-GuyNeural', title: 'The Living Bible' },
  gw: { voice: 'en-US-AriaNeural', title: "GOD'S WORD Translation" },
  voice: { voice: 'en-US-EricNeural', title: 'The Voice' },
};

// In-module cache: path -> { t, data }.
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
    cache.set(path, { t: now, data: null });
    return null;
  }

  const data = await res.json();
  cache.set(path, { t: now, data });
  return data;
}

function json(data, status = 200, customHeaders = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      'content-type': 'application/json; charset=utf-8',
      'Cache-Control': status === 200 ? 'public, max-age=86400' : 'no-store',
      ...CORS_HEADERS,
      ...customHeaders,
    },
  });
}

function fail(status, code, message) {
  return json({ error: { code, message } }, status);
}

function notFound(message) {
  return fail(404, 'not_found', message);
}

function badRequest(message) {
  return fail(400, 'bad_request', message);
}

function serverError(message) {
  return fail(500, 'server_error', message);
}

async function getTopIndex() {
  return fetchJson('/index.json');
}

async function getVersionIndex(versionCode) {
  return fetchJson(`/${versionCode.toLowerCase()}/index.json`);
}

async function getVersion(versionParam) {
  const top = await getTopIndex();
  if (!top || !top.versions) return null;
  const target = versionParam.toLowerCase();
  return top.versions.find((v) => v.code.toLowerCase() === target) || null;
}

function findBook(books, bookParam) {
  const decoded = decodeURIComponent(bookParam).trim().toLowerCase();
  const slug = decoded.replace(/\s+/g, '-').replace(/[^a-z0-9\-]/g, '');
  return (
    books.find((b) => b.slug === slug) ||
    books.find((b) => b.book.toLowerCase() === decoded) ||
    books.find((b) => b.book_name.toLowerCase() === decoded) ||
    null
  );
}

/* ---------- Audio Narration Handlers ---------- */

async function getCachedAudioManifest(code) {
  const key = `audio_manifest_${code}`;
  const hit = cache.get(key);
  const now = Date.now();
  if (hit && now - hit.t < 60 * 1000) return hit.data; // 60s cache

  try {
    const res = await fetch(`${AUDIO_RELEASE_BASE}/audio-${code}/manifest.json`, {
      headers: { 'User-Agent': 'VerseWell-API' },
      cf: { cacheTtl: 60, cacheEverything: true },
    });
    if (res.ok) {
      const data = await res.json();
      cache.set(key, { t: now, data });
      return data;
    }
  } catch (_) {}
  return null;
}

async function handleAudioManifest(version) {
  const v = version.toLowerCase();
  const cfg = AUDIO_CONFIG[v];
  if (!cfg) {
    return notFound(
      `Audio is not supported for version '${version}'. Only KJV, NIV, TLB, GW, and VOICE have audio narration.`
    );
  }

  const manifest = await getCachedAudioManifest(v);
  if (manifest) {
    return json(manifest, 200, { 'Cache-Control': 'public, max-age=120' });
  }

  return json(
    {
      version: v.toUpperCase(),
      voice: cfg.voice,
      title: cfg.title,
      chapters: [],
      total_bytes: 0,
      available: false,
      message: 'Audio generation in progress. Manifest will populate as chapters complete.',
    },
    200,
    { 'Cache-Control': 'public, max-age=60' }
  );
}

async function handleAudioStream(request, version, book, chapterStr) {
  const v = version.toLowerCase();
  if (!AUDIO_CONFIG[v]) {
    return notFound(`Audio is not supported for version '${version}'. Only KJV, NIV, TLB, GW, and VOICE have audio narration.`);
  }

  const ch = Number(chapterStr);
  if (!Number.isInteger(ch) || ch < 1) {
    return badRequest(`Invalid chapter '${chapterStr}'.`);
  }

  // Resolve book via canonical static index (supports OSIS '1Cor', human '1 Corinthians', or slug '1-corinthians')
  let bookSlug = decodeURIComponent(book).toLowerCase().trim().replace(/\s+/g, '-').replace(/[^a-z0-9\-]/g, '');
  try {
    const versionIndex = await getVersionIndex(v);
    if (versionIndex && Array.isArray(versionIndex.books)) {
      const matched = findBook(versionIndex.books, book);
      if (matched && matched.slug) {
        bookSlug = matched.slug;
      }
    }
  } catch (_) {}

  const chPadded = String(ch).padStart(3, '0');
  const assetName = `${bookSlug}-${chPadded}.m4a`;
  const assetUrl = `${AUDIO_RELEASE_BASE}/audio-${v}/${assetName}`;

  // Look up sha256 from cached manifest so Cache API & ETag are immutably keyed by content hash
  let chapterSha = null;
  let manifestEntry = null;
  try {
    const manifest = await getCachedAudioManifest(v);
    if (manifest && Array.isArray(manifest.chapters)) {
      manifestEntry = manifest.chapters.find((c) => c.file === assetName) || null;
      if (manifestEntry && manifestEntry.sha256) {
        chapterSha = manifestEntry.sha256;
      }
    }
  } catch (_) {}

  const etag = chapterSha ? `"${chapterSha}"` : null;
  const inm = request.headers.get('if-none-match');
  if (etag && inm && inm === etag) {
    return new Response(null, {
      status: 304,
      headers: {
        ETag: etag,
        'Cache-Control': 'public, max-age=31536000, immutable',
        ...CORS_HEADERS,
      },
    });
  }

  const baseHeaders = {
    'Content-Type': 'audio/mp4',
    'Accept-Ranges': 'bytes',
    'Cache-Control': chapterSha ? 'public, max-age=31536000, immutable' : 'public, max-age=3600',
    ...(etag ? { ETag: etag } : {}),
    ...CORS_HEADERS,
  };

  const rangeHeader = request.headers.get('range');

  try {
    // 1. Check Cloudflare Cache API keyed by sha256
    const edgeCache = typeof caches !== 'undefined' && caches.default ? caches.default : null;
    const cacheKeyUrl = `https://audio-cache.versewell.internal/${v}/${assetName}?sha=${chapterSha || 'live'}`;
    const cacheKey = new Request(cacheKeyUrl, { method: 'GET' });

    let fullBytes = null;
    if (edgeCache) {
      const cachedRes = await edgeCache.match(cacheKey);
      if (cachedRes && cachedRes.ok) {
        if (request.method === 'HEAD' && !rangeHeader) {
          const len = cachedRes.headers.get('content-length') || (manifestEntry ? String(manifestEntry.bytes) : '0');
          return new Response(null, { status: 200, headers: { ...baseHeaders, 'Content-Length': len } });
        }
        fullBytes = await cachedRes.arrayBuffer();
      }
    }

    // 2. If not in Cache API yet, fetch from GitHub Release
    if (!fullBytes) {
      // Fast path for HEAD without Range
      if (request.method === 'HEAD' && !rangeHeader) {
        const headRes = await fetch(assetUrl, {
          method: 'HEAD',
          headers: { 'User-Agent': 'VerseWell-API' },
          redirect: 'follow',
        });
        if (headRes.status === 404) {
          return notFound(`Audio chapter '${assetName}' for version '${v.toUpperCase()}' not found or not yet generated.`);
        }
        if (!headRes.ok) {
          return serverError(`Upstream error fetching audio metadata: ${headRes.status}`);
        }
        const len = headRes.headers.get('content-length') || (manifestEntry ? String(manifestEntry.bytes) : '0');
        return new Response(null, { status: 200, headers: { ...baseHeaders, 'Content-Length': len } });
      }

      // If client sent Range, try upstream Range first for low memory/latency when cold
      if (rangeHeader && rangeHeader.startsWith('bytes=')) {
        const upRangeRes = await fetch(assetUrl, {
          method: request.method === 'HEAD' ? 'HEAD' : 'GET',
          headers: { 'User-Agent': 'VerseWell-API', Range: rangeHeader },
          redirect: 'follow',
          cf: { cacheEverything: true, cacheTtl: 86400 },
        });
        if (upRangeRes.status === 404) {
          return notFound(`Audio chapter '${assetName}' for version '${v.toUpperCase()}' not found or not yet generated.`);
        }
        if (upRangeRes.status === 416) {
          const cr = upRangeRes.headers.get('content-range') || 'bytes */*';
          return new Response(null, { status: 416, headers: { 'Content-Range': cr, ...CORS_HEADERS } });
        }
        if (upRangeRes.status === 206) {
          const resHeaders = new Headers(baseHeaders);
          if (upRangeRes.headers.has('content-length')) {
            resHeaders.set('Content-Length', upRangeRes.headers.get('content-length'));
          }
          if (upRangeRes.headers.has('content-range')) {
            resHeaders.set('Content-Range', upRangeRes.headers.get('content-range'));
          }
          return new Response(request.method === 'HEAD' ? null : upRangeRes.body, { status: 206, headers: resHeaders });
        }
        if (!upRangeRes.ok) {
          return serverError(`Upstream error fetching audio: ${upRangeRes.status}`);
        }
        fullBytes = await upRangeRes.arrayBuffer();
      } else {
        const upFullRes = await fetch(assetUrl, {
          method: 'GET',
          headers: { 'User-Agent': 'VerseWell-API' },
          redirect: 'follow',
          cf: { cacheEverything: true, cacheTtl: 86400 },
        });
        if (upFullRes.status === 404) {
          return notFound(`Audio chapter '${assetName}' for version '${v.toUpperCase()}' not found or not yet generated.`);
        }
        if (!upFullRes.ok) {
          return serverError(`Upstream error fetching audio: ${upFullRes.status}`);
        }
        fullBytes = await upFullRes.arrayBuffer();
      }

      // Populate Cache API with full immutable payload when sha is known
      if (edgeCache && fullBytes && fullBytes.byteLength > 0) {
        const toCache = new Response(fullBytes.slice(0), {
          status: 200,
          headers: { ...baseHeaders, 'Content-Length': String(fullBytes.byteLength) },
        });
        try {
          await edgeCache.put(cacheKey, toCache);
        } catch (_) {}
      }
    }

    const totalLength = fullBytes.byteLength;

    // 3. Serve byte-range slice if requested
    if (rangeHeader && rangeHeader.startsWith('bytes=')) {
      const spec = rangeHeader.slice(6).trim();
      const dashIdx = spec.indexOf('-');
      let start = 0;
      let end = totalLength - 1;
      if (dashIdx === 0) {
        const suffix = parseInt(spec.slice(1), 10);
        if (!Number.isFinite(suffix) || suffix <= 0) {
          return new Response(null, { status: 416, headers: { 'Content-Range': `bytes */${totalLength}`, ...CORS_HEADERS } });
        }
        start = Math.max(0, totalLength - suffix);
      } else {
        const startStr = spec.slice(0, dashIdx);
        const endStr = dashIdx >= 0 ? spec.slice(dashIdx + 1) : '';
        start = parseInt(startStr, 10);
        end = endStr ? parseInt(endStr, 10) : totalLength - 1;
      }
      if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || start > end || start >= totalLength) {
        return new Response(null, {
          status: 416,
          headers: { 'Content-Range': `bytes */${totalLength}`, ...CORS_HEADERS },
        });
      }
      if (end >= totalLength) end = totalLength - 1;
      const chunk = fullBytes.slice(start, end + 1);
      return new Response(request.method === 'HEAD' ? null : chunk, {
        status: 206,
        headers: {
          ...baseHeaders,
          'Content-Range': `bytes ${start}-${end}/${totalLength}`,
          'Content-Length': String(chunk.byteLength),
        },
      });
    }

    return new Response(request.method === 'HEAD' ? null : fullBytes, {
      status: 200,
      headers: {
        ...baseHeaders,
        'Content-Length': String(totalLength),
      },
    });
  } catch (err) {
    return serverError(`Failed to stream audio narration: ${err.message}`);
  }
}

/* ---------- Scripture Text Handlers ---------- */

async function handleChapter(versionRow, versionIndex, bookRow, chapter, wantNotes) {
  const data = await fetchJson(
    `/${versionRow.code.toLowerCase()}/${bookRow.slug}/${chapter}.json`
  );
  if (!data) {
    return notFound(`Chapter ${chapter} not found in ${bookRow.book_name} (${versionRow.code}).`);
  }

  const verses = data.verses.map((v) =>
    wantNotes ? v : { verse: v.verse, text: v.text }
  );

  return json({
    version: versionRow.code,
    book: data.book,
    book_name: data.book_name,
    chapter: data.chapter,
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
  const idx = await fetchJson(`/${versionRow.code.toLowerCase()}/search.json`);
  const entries = (idx && idx.entries) || [];
  const results = [];

  for (const e of entries) {
    if (e.text.toLowerCase().includes(needle)) {
      results.push({
        version: versionRow.code,
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
    results,
  });
}

async function handleRandom(versionRow, versionIndex, wantNotes) {
  const books = versionIndex.books;
  if (!books.length) return notFound(`Version ${versionRow.code} has no verses.`);

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

/* ---------- Main Export ---------- */

export default {
  async fetch(request, env) {
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      return fail(405, 'method_not_allowed', 'Only GET and HEAD are supported.');
    }

    const url = new URL(request.url);
    const segments = url.pathname.split('/').filter(Boolean); // e.g. ['api','v1','audio',...]

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
          'GET /api/v1/audio/:version/manifest',
          'GET /api/v1/audio/:version/:book/:chapter',
        ],
        docs: 'See the VerseWell site /docs page for full documentation.',
      });
    }

    if (segments[0] !== 'api' || segments[1] !== 'v1') {
      return notFound('Unknown route.');
    }

    // Route: Audio API (/api/v1/audio/...)
    if (segments[2] === 'audio') {
      if (segments.length === 5 && segments[4] === 'manifest') {
        return handleAudioManifest(segments[3]);
      }
      if (segments.length === 6) {
        return handleAudioStream(request, segments[3], segments[4], segments[5]);
      }
      return notFound("Unknown audio route. Expected '/api/v1/audio/:version/manifest' or '/api/v1/audio/:version/:book/:chapter'.");
    }

    if (segments[2] !== 'versions') {
      return notFound('Unknown route.');
    }

    const wantNotes = url.searchParams.get('notes') !== 'false';

    // GET /api/v1/versions
    if (segments.length === 3) {
      const idx = await getTopIndex();
      const versions = (idx && idx.versions) || [];

      const versionsWithAudio = await Promise.all(
        versions
          .slice()
          .sort((a, b) => a.code.localeCompare(b.code))
          .map(async (v) => {
            const code = v.code.toLowerCase();
            const config = AUDIO_CONFIG[code];
            let audioMeta = { available: false };

            if (config) {
              let chapterCount = 0;
              let totalBytes = 0;
              try {
                const manifest = await getCachedAudioManifest(code);
                if (manifest && Array.isArray(manifest.chapters)) {
                  chapterCount = manifest.chapters.length;
                  totalBytes = manifest.chapters.reduce((sum, c) => sum + (c.bytes || 0), 0);
                }
              } catch (_) {}

              audioMeta = {
                available: true,
                chapters: chapterCount,
                total_bytes: totalBytes,
                voice: config.voice,
                manifest_url: `${url.origin}/api/v1/audio/${code}/manifest`,
              };
            }

            return {
              code: v.code,
              name: v.name,
              language: v.language,
              description: v.description,
              has_footnotes: !!v.has_footnotes,
              has_intros: !!v.has_intros,
              verse_count: v.verse_count,
              audio: audioMeta,
            };
          })
      );

      return json({ versions: versionsWithAudio });
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
          slug: b.slug,
          testament: b.testament,
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
        slug: bookRow.slug,
        testament: bookRow.testament,
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
