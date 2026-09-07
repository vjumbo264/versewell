/**
 * VerseWell REST API worker (task-07).
 *
 * Routes (all under /api/v1/):
 *   GET /api/v1/versions
 *   GET /api/v1/versions/:version/books
 *   GET /api/v1/versions/:version/:book
 *   GET /api/v1/versions/:version/:book/:chapter
 *   GET /api/v1/versions/:version/:book/:chapter/:verse
 *   GET /api/v1/versions/:version/search?q=...
 *   GET /api/v1/versions/:version/random
 *
 * Chapter/verse responses nest `intro` and per-verse `footnotes`; pass
 * ?notes=false to omit them. CORS is open (public API). All SQL is
 * parameterized. Errors use a consistent shape: {"error": {code, message}}.
 */

const CORS_HEADERS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
  'Access-Control-Max-Age': '86400',
};

const SEARCH_LIMIT = 50;

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

async function getVersion(db, code) {
  return db
    .prepare('SELECT code, name, language, description, has_footnotes, has_intros, verse_count FROM versions WHERE code = ?1')
    .bind(code.toUpperCase())
    .first();
}

async function listBooks(db, versionCode) {
  const { results } = await db
    .prepare(
      `SELECT book, book_name, book_order, MAX(chapter) AS chapters, COUNT(*) AS verse_count
       FROM verses WHERE version = ?1
       GROUP BY book, book_name, book_order ORDER BY book_order`
    )
    .bind(versionCode)
    .all();
  return results;
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

async function getFootnotes(db, version, book, chapter) {
  const { results } = await db
    .prepare(
      'SELECT verse, marker, note_text FROM footnotes WHERE version = ?1 AND book = ?2 AND chapter = ?3 ORDER BY verse, marker'
    )
    .bind(version, book, chapter)
    .all();
  const byVerse = {};
  for (const row of results) {
    (byVerse[row.verse] = byVerse[row.verse] || []).push({ marker: row.marker, text: row.note_text });
  }
  return byVerse;
}

async function getIntro(db, version, book, chapter) {
  const row = await db
    .prepare(
      'SELECT start_verse, end_verse, intro_text FROM section_intros WHERE version = ?1 AND book = ?2 AND chapter = ?3 ORDER BY start_verse LIMIT 1'
    )
    .bind(version, book, chapter)
    .first();
  if (!row) return null;
  return { start_verse: row.start_verse, end_verse: row.end_verse, text: row.intro_text };
}

async function chapterNavigation(db, version, bookRow, chapter) {
  let prev = null;
  let next = null;
  if (chapter > 1) {
    prev = { book: bookRow.book, chapter: chapter - 1 };
  } else {
    const p = await db
      .prepare(
        `SELECT book, MAX(chapter) AS mc FROM verses WHERE version = ?1 AND book_order < ?2
         GROUP BY book, book_order ORDER BY book_order DESC LIMIT 1`
      )
      .bind(version, bookRow.book_order)
      .first();
    if (p) prev = { book: p.book, chapter: p.mc };
  }
  if (chapter < bookRow.chapters) {
    next = { book: bookRow.book, chapter: chapter + 1 };
  } else {
    const n = await db
      .prepare(
        `SELECT book FROM verses WHERE version = ?1 AND book_order > ?2
         GROUP BY book, book_order ORDER BY book_order LIMIT 1`
      )
      .bind(version, bookRow.book_order)
      .first();
    if (n) next = { book: n.book, chapter: 1 };
  }
  return { prev, next };
}

async function handleChapter(db, versionRow, bookRow, chapter, wantNotes) {
  const { results: verses } = await db
    .prepare('SELECT verse, text FROM verses WHERE version = ?1 AND book = ?2 AND chapter = ?3 ORDER BY verse')
    .bind(versionRow.code, bookRow.book, chapter)
    .all();
  if (!verses.length) {
    return notFound(`Chapter ${chapter} not found in ${bookRow.book_name} (${versionRow.code}).`);
  }

  let footnotes = {};
  let intro = null;
  if (wantNotes) {
    [footnotes, intro] = await Promise.all([
      getFootnotes(db, versionRow.code, bookRow.book, chapter),
      getIntro(db, versionRow.code, bookRow.book, chapter),
    ]);
  }
  const navigation = await chapterNavigation(db, versionRow.code, bookRow, chapter);

  return json({
    version: versionRow.code,
    book: bookRow.book,
    book_name: bookRow.book_name,
    chapter,
    ...(wantNotes ? { intro } : {}),
    verses: verses.map((v) => ({
      verse: v.verse,
      text: v.text,
      ...(wantNotes && footnotes[v.verse] ? { footnotes: footnotes[v.verse] } : {}),
    })),
    navigation,
  });
}

async function handleVerse(db, versionRow, bookRow, chapter, verseNum, wantNotes) {
  const row = await db
    .prepare('SELECT verse, text FROM verses WHERE version = ?1 AND book = ?2 AND chapter = ?3 AND verse = ?4')
    .bind(versionRow.code, bookRow.book, chapter, verseNum)
    .first();
  if (!row) {
    return notFound(`Verse ${bookRow.book_name} ${chapter}:${verseNum} not found (${versionRow.code}).`);
  }
  let notes = [];
  let intro = null;
  if (wantNotes) {
    const { results } = await db
      .prepare(
        'SELECT marker, note_text FROM footnotes WHERE version = ?1 AND book = ?2 AND chapter = ?3 AND verse = ?4 ORDER BY marker'
      )
      .bind(versionRow.code, bookRow.book, chapter, verseNum)
      .all();
    notes = results.map((r) => ({ marker: r.marker, text: r.note_text }));
    intro = await getIntro(db, versionRow.code, bookRow.book, chapter);
    if (intro && !(intro.start_verse <= verseNum && verseNum <= intro.end_verse)) intro = null;
  }
  return json({
    version: versionRow.code,
    book: bookRow.book,
    book_name: bookRow.book_name,
    chapter,
    verse: row.verse,
    text: row.text,
    ...(wantNotes ? { footnotes: notes, intro } : {}),
  });
}

function escapeLike(q) {
  return q.replace(/[\\%_]/g, (c) => '\\' + c);
}

async function handleSearch(db, versionRow, url) {
  const q = (url.searchParams.get('q') || '').trim();
  if (!q) return badRequest('Missing required query parameter: q');
  if (q.length > 200) return badRequest('Query too long (max 200 characters).');
  const { results } = await db
    .prepare(
      `SELECT book, book_name, chapter, verse, text FROM verses
       WHERE version = ?1 AND text LIKE ?2 ESCAPE '\\'
       ORDER BY book_order, chapter, verse LIMIT ?3`
    )
    .bind(versionRow.code, `%${escapeLike(q)}%`, SEARCH_LIMIT)
    .all();
  return json({
    version: versionRow.code,
    query: q,
    count: results.length,
    limit: SEARCH_LIMIT,
    results: results.map((r) => ({
      book: r.book,
      book_name: r.book_name,
      chapter: r.chapter,
      verse: r.verse,
      text: r.text,
    })),
  });
}

async function handleRandom(db, versionRow, wantNotes) {
  const row = await db
    .prepare(
      'SELECT book, book_name, chapter, verse, text FROM verses WHERE version = ?1 ORDER BY RANDOM() LIMIT 1'
    )
    .bind(versionRow.code)
    .first();
  if (!row) return notFound(`Version ${versionRow.code} has no verses.`);
  let notes = [];
  if (wantNotes) {
    const { results } = await db
      .prepare(
        'SELECT marker, note_text FROM footnotes WHERE version = ?1 AND book = ?2 AND chapter = ?3 AND verse = ?4 ORDER BY marker'
      )
      .bind(versionRow.code, row.book, row.chapter, row.verse)
      .all();
    notes = results.map((r) => ({ marker: r.marker, text: r.note_text }));
  }
  return json({
    version: versionRow.code,
    book: row.book,
    book_name: row.book_name,
    chapter: row.chapter,
    verse: row.verse,
    text: row.text,
    ...(wantNotes ? { footnotes: notes } : {}),
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
      const { results } = await env.DB.prepare(
        'SELECT code, name, language, description, has_footnotes, has_intros, verse_count, imported_at FROM versions ORDER BY code'
      ).all();
      return json({
        versions: results.map((v) => ({
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
    const versionRow = await getVersion(env.DB, versionParam);
    if (!versionRow) return notFound(`Unknown version '${versionParam}'.`);

    // GET /api/v1/versions/:version/search?q=...
    // Path segments: ['api','v1','versions',':version','search'] -> length 5.
    if (segments.length === 5 && segments[4] === 'search') {
      return handleSearch(env.DB, versionRow, url);
    }
    // GET /api/v1/versions/:version/random
    if (segments.length === 5 && segments[4] === 'random') {
      return handleRandom(env.DB, versionRow, wantNotes);
    }
    // NOTE: 'books', 'search' and 'random' are reserved path segments and never book names.

    // GET /api/v1/versions/:version/books
    if (segments.length === 5 && segments[4] === 'books') {
      const books = await listBooks(env.DB, versionRow.code);
      return json({ version: versionRow.code, books });
    }

    const books = await listBooks(env.DB, versionRow.code);

    if (segments.length === 4) {
      return badRequest("Expected 'books', 'search', 'random', or a book name after the version.");
    }

    const bookRow = findBook(books, segments[4]);
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
      return handleChapter(env.DB, versionRow, bookRow, chapter, wantNotes);
    }

    const verseNum = Number(segments[6]);
    if (!Number.isInteger(verseNum) || verseNum < 1) {
      return badRequest(`Invalid verse '${decodeURIComponent(segments[6])}'.`);
    }

    // GET /api/v1/versions/:version/:book/:chapter/:verse
    if (segments.length === 7) {
      return handleVerse(env.DB, versionRow, bookRow, chapter, verseNum, wantNotes);
    }

    return notFound('Unknown route.');
  },
};
