/* VerseWell reading site — hash-routed SPA, dogfoods the public API. */
'use strict';

const API_BASE = 'https://versewell-api.vjumbo264.workers.dev';
const view = document.getElementById('view');
const switcher = document.getElementById('version-switcher');

const state = { versions: [], books: {}, defaultVersion: null };
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const encRef = (s) => encodeURIComponent(s);

/* ---------------- theme ---------------- */
(function initTheme() {
  const saved = localStorage.getItem('vw-theme');
  const theme = saved || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  document.documentElement.dataset.theme = theme;
})();
document.getElementById('theme-toggle').addEventListener('click', () => {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  localStorage.setItem('vw-theme', next);
});

/* ---------------- API ---------------- */
async function api(path) {
  const res = await fetch(API_BASE + path);
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const msg = data && data.error ? data.error.message : `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return data;
}

async function loadVersions() {
  if (state.versions.length) return;
  const data = await api('/api/v1/versions');
  state.versions = data.versions;
  state.defaultVersion =
    localStorage.getItem('vw-version') ||
    (state.versions.find((v) => v.code === 'NIV2011') || state.versions[0] || {}).code;
  renderSwitcher();
}

function renderSwitcher() {
  switcher.innerHTML = state.versions
    .map((v) => `<option value="${esc(v.code)}"${v.code === currentVersion() ? ' selected' : ''}>${esc(v.code)}</option>`)
    .join('');
}
function currentVersion() {
  return localStorage.getItem('vw-version') || state.defaultVersion;
}
switcher.addEventListener('change', () => {
  localStorage.setItem('vw-version', switcher.value);
  const parts = location.hash.split('/').filter(Boolean);
  if (parts[0] === 'v' && parts.length >= 3) {
    location.hash = `#/v/${switcher.value}/${parts.slice(2).join('/')}`;
  } else {
    route();
  }
});

async function loadBooks(version) {
  if (!state.books[version]) {
    const data = await api(`/api/v1/versions/${version}/books`);
    state.books[version] = data.books;
  }
  return state.books[version];
}

function setActiveTab(name) {
  document.querySelectorAll('.tabs a').forEach((a) => a.classList.toggle('active', a.dataset.tab === name));
}

function showError(err) {
  view.innerHTML = `<div class="error-box"><strong>Something went wrong.</strong><br>${esc(err.message)}</div>`;
}

/* ---------------- Home ---------------- */
async function renderHome(params) {
  setActiveTab('home');
  await loadVersions();
  const version = params.get('v') || currentVersion();

  if (params.get('book')) {
    const books = await loadBooks(version);
    const p = decodeURIComponent(params.get('book')).replace(/[-_+]/g, ' ').toLowerCase();
    const book = books.find((b) => b.book.toLowerCase() === p || b.book_name.toLowerCase() === p);
    if (book) return renderChapterPicker(version, book);
  }

  const cards = state.versions
    .map(
      (v) => `
    <a class="version-card" href="#/?v=${esc(v.code)}">
      <div class="code">${esc(v.code)}</div>
      <div class="name">${esc(v.name)}</div>
      <div class="badges">
        ${v.has_footnotes ? '<span class="badge">Footnotes</span>' : ''}
        ${v.has_intros ? '<span class="badge">Intros</span>' : ''}
      </div>
    </a>`
    )
    .join('');

  view.innerHTML = `
    <h1>Read the Bible</h1>
    <p class="lede">Choose a translation, then a book and chapter. Section introductions and footnotes appear right in the text.</p>
    <div class="version-grid">${cards}</div>
    <h2>Books — ${esc(version)}</h2>
    <p class="muted">Loading books…</p>`;

  const books = await loadBooks(version);
  const ot = books.filter((b) => b.book_order <= 39);
  const nt = books.filter((b) => b.book_order > 39);
  const grid = (list) =>
    `<div class="book-grid">${list
      .map((b) => `<a href="#/?v=${esc(version)}&book=${encRef(b.book_name)}">${esc(b.book_name)}</a>`)
      .join('')}</div>`;
  view.innerHTML = `
    <h1>Read the Bible</h1>
    <p class="lede">Choose a translation, then a book and chapter. Section introductions and footnotes appear right in the text.</p>
    <div class="version-grid">${cards}</div>
    <h2>Old Testament — ${esc(version)}</h2>${grid(ot)}
    <h2>New Testament</h2>${grid(nt)}`;
}

async function renderChapterPicker(version, book) {
  const links = Array.from({ length: book.chapters }, (_, i) => i + 1)
    .map((c) => `<a href="#/v/${esc(version)}/${encRef(book.book)}/${c}">${c}</a>`)
    .join('');
  view.innerHTML = `
    <h1>${esc(book.book_name)}</h1>
    <p class="reader-sub">${esc(version)} · ${book.chapters} chapters · <a href="#/?v=${esc(version)}">← all books</a></p>
    <div class="chapter-grid">${links}</div>`;
}

/* ---------------- Reader ---------------- */
async function renderReader(version, bookParam, chapterStr) {
  setActiveTab('home');
  await loadVersions();
  const chapter = parseInt(chapterStr, 10);
  if (!Number.isInteger(chapter) || chapter < 1) {
    view.innerHTML = `<div class="error-box">Invalid chapter “${esc(chapterStr)}”.</div>`;
    return;
  }
  view.innerHTML = `<p class="muted loading">Loading ${esc(bookParam)} ${chapter}…</p>`;

  const data = await api(`/api/v1/versions/${version}/${encRef(bookParam)}/${chapter}`);

  const intro = data.intro
    ? `<div class="section-intro"><span class="intro-label">Introduction · ${esc(data.book_name)}</span>${esc(data.intro.text)}</div>`
    : '';

  let footnoteIndex = [];
  const versesHtml = data.versions ? '' : data.verses
    .map((v) => {
      const notes = v.footnotes || [];
      notes.forEach((n, i) => footnoteIndex.push({ verse: v.verse, marker: n.marker, text: n.text, n: footnoteIndex.length + 1 }));
      const markers = notes
        .map((n, i) => {
          const num = footnoteIndex.length - notes.length + i + 1;
          return `<button class="fn-marker" data-fn="${num}" title="Footnote ${num}">[${num}]</button>`;
        })
        .join('');
      return `<p class="verse"><span class="verse-num">${v.verse}</span>${esc(v.text)}${markers}</p>`;
    })
    .join('');

  const fnPanel = footnoteIndex.length
    ? `<div class="footnotes-panel"><h2>Footnotes</h2>${footnoteIndex
        .map(
          (f) =>
            `<p class="fn-item" id="fn-${f.n}"><span class="fn-ref">${f.n}. ${esc(data.book_name)} ${data.chapter}:${f.verse}</span> — ${esc(f.text)}</p>`
        )
        .join('')}</div>`
    : '';

  const nav = (target, label) =>
    target
      ? `<a href="#/v/${esc(version)}/${encRef(target.book)}/${target.chapter}">${label}</a>`
      : `<a class="disabled">${label}</a>`;

  view.innerHTML = `
    <div class="reader-head">
      <h1>${esc(data.book_name)} ${data.chapter}</h1>
      <span class="reader-sub">${esc(version)}</span>
    </div>
    <div class="chapter-nav">${nav(data.navigation.prev, '← Previous')}${nav(data.navigation.next, 'Next →')}</div>
    <div class="reader-body">${intro}${versesHtml}</div>
    ${fnPanel}
    <div class="chapter-nav">${nav(data.navigation.prev, '← Previous')}${nav(data.navigation.next, 'Next →')}</div>`;

  view.querySelectorAll('.fn-marker').forEach((btn) =>
    btn.addEventListener('click', () => {
      const item = view.querySelector(`#fn-${btn.dataset.fn}`);
      if (!item) return;
      view.querySelectorAll('.fn-item.highlight').forEach((el) => el.classList.remove('highlight'));
      item.classList.add('highlight');
      item.scrollIntoView({ behavior: 'smooth', block: 'center' });
    })
  );
  renderSwitcher();
  window.scrollTo(0, 0);
}

/* ---------------- Search ---------------- */
async function renderSearch(params) {
  setActiveTab('search');
  await loadVersions();
  const version = params.get('v') || currentVersion();
  const q = params.get('q') || '';

  view.innerHTML = `
    <h1>Search</h1>
    <p class="lede">Searching <strong>${esc(version)}</strong>. Try “love”, “faith”, “light”…</p>
    <form class="search-bar" id="search-form">
      <input id="search-input" type="search" placeholder="Search ${esc(version)}…" value="${esc(q)}" autofocus>
      <button type="submit">Search</button>
    </form>
    <div id="search-results">${q ? '<p class="muted loading">Searching…</p>' : ''}</div>`;

  document.getElementById('search-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const nq = document.getElementById('search-input').value.trim();
    location.hash = `#/search?v=${esc(version)}&q=${encodeURIComponent(nq)}`;
  });

  if (!q) return;
  try {
    const data = await api(`/api/v1/versions/${version}/search?q=${encodeURIComponent(q)}`);
    const rx = new RegExp('(' + q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'ig');
    const items = data.results
      .map(
        (r) => `
      <a class="result-item" href="#/v/${esc(version)}/${encRef(r.book)}/${r.chapter}">
        <div class="ref">${esc(r.book_name)} ${r.chapter}:${r.verse}</div>
        <div class="text">${esc(r.text).replace(rx, '<mark>$1</mark>')}</div>
      </a>`
      )
      .join('');
    document.getElementById('search-results').innerHTML =
      `<p class="muted">${data.count} result${data.count === 1 ? '' : 's'}${data.count === data.limit ? ' (first ' + data.limit + ')' : ''}</p>` +
      (items || '<p class="muted">No matches found.</p>');
  } catch (err) {
    document.getElementById('search-results').innerHTML = `<div class="error-box">${esc(err.message)}</div>`;
  }
}

/* ---------------- API docs (task-09) ---------------- */
function renderDocs() {
  setActiveTab('docs');
  const base = API_BASE;
  view.innerHTML = `
  <h1>VerseWell API</h1>
  <p class="lede">A free, open, read-only REST API for every Bible version on VerseWell — the same API this website uses.
  No API key, no signup, no rate-limit games. CORS is enabled for all origins: call it from any app or website.</p>
  <p>Base URL: <code>${base}</code> · All responses are JSON. · Successful reads are cacheable (<code>Cache-Control: public, max-age=86400</code>).</p>

  <h2>Endpoints</h2>
  <table class="doc-table">
    <tr><th>Endpoint</th><th>Description</th></tr>
    <tr><td><code>GET /api/v1/versions</code></td><td>List all available Bible versions.</td></tr>
    <tr><td><code>GET /api/v1/versions/{version}/books</code></td><td>List books of a version (with chapter counts).</td></tr>
    <tr><td><code>GET /api/v1/versions/{version}/{book}</code></td><td>Book metadata (chapter count, verse count).</td></tr>
    <tr><td><code>GET /api/v1/versions/{version}/{book}/{chapter}</code></td><td>Full chapter: verses plus any section intro and footnotes.</td></tr>
    <tr><td><code>GET /api/v1/versions/{version}/{book}/{chapter}/{verse}</code></td><td>A single verse, with its footnotes.</td></tr>
    <tr><td><code>GET /api/v1/versions/{version}/search?q=…</code></td><td>Search verse text (first ${50} matches).</td></tr>
    <tr><td><code>GET /api/v1/versions/{version}/random</code></td><td>A random verse.</td></tr>
  </table>

  <h2>Parameters</h2>
  <table class="doc-table">
    <tr><th>Parameter</th><th>Where</th><th>Notes</th></tr>
    <tr><td><code>{version}</code></td><td>path</td><td>Version code, e.g. <code>KJV</code>, <code>NIV2011</code>, <code>CEV</code>. Case-insensitive. Get the full list from <code>/api/v1/versions</code>.</td></tr>
    <tr><td><code>{book}</code></td><td>path</td><td>OSIS code (<code>Gen</code>, <code>John</code>) or full name (<code>Genesis</code>, <code>1%20John</code>). Case-insensitive.</td></tr>
    <tr><td><code>{chapter}</code>, <code>{verse}</code></td><td>path</td><td>1-based integers.</td></tr>
    <tr><td><code>notes</code></td><td>query</td><td><code>?notes=false</code> omits section intros and footnotes for a lean, text-only response. Default is <code>true</code>.</td></tr>
    <tr><td><code>q</code></td><td>query (search)</td><td>Search text, max 200 characters.</td></tr>
  </table>

  <h2>Examples</h2>
  <div class="doc-endpoint">
    <span class="method">GET</span><span class="path">/api/v1/versions</span>
    <pre>curl ${base}/api/v1/versions</pre>
    <pre>{
  "versions": [
    { "code": "KJV", "name": "King James Version", "language": "en",
      "has_footnotes": false, "has_intros": false, "verse_count": 31102 },
    …
  ]
}</pre>
  </div>
  <div class="doc-endpoint">
    <span class="method">GET</span><span class="path">/api/v1/versions/CEV/Genesis/1</span>
    <pre>curl ${base}/api/v1/versions/CEV/Genesis/1</pre>
    <pre>{
  "version": "CEV", "book": "Gen", "book_name": "Genesis", "chapter": 1,
  "intro": { "start_verse": 1, "end_verse": 31,
             "text": "Genesis describes many beginnings. …" },
  "verses": [
    { "verse": 1, "text": "In the beginning God created the heavens and the earth.",
      "footnotes": [ { "marker": "1", "text": "the heavens and the earth: …" } ] },
    …
  ],
  "navigation": { "prev": null, "next": { "book": "Gen", "chapter": 2 } }
}</pre>
  </div>
  <div class="doc-endpoint">
    <span class="method">GET</span><span class="path">/api/v1/versions/KJV/John/3/16</span>
    <pre>curl "${base}/api/v1/versions/KJV/John/3/16?notes=false"</pre>
    <pre>{
  "version": "KJV", "book": "John", "book_name": "John",
  "chapter": 3, "verse": 16,
  "text": "For God so loved the world, that he gave his only begotten Son, …"
}</pre>
    <p class="muted">With <code>notes=false</code> the <code>footnotes</code> and <code>intro</code> fields are omitted entirely.</p>
  </div>
  <div class="doc-endpoint">
    <span class="method">GET</span><span class="path">/api/v1/versions/NIV2011/search?q=light</span>
    <pre>curl "${base}/api/v1/versions/NIV2011/search?q=light"</pre>
    <pre>{
  "version": "NIV2011", "query": "light", "count": 50, "limit": 50,
  "results": [
    { "book": "Gen", "book_name": "Genesis", "chapter": 1, "verse": 3,
      "text": "And God said, \\"Let there be light,\\" and there was light." },
    …
  ]
}</pre>
  </div>

  <h2>Errors</h2>
  <p>Errors use a consistent JSON shape with a conventional HTTP status code:</p>
  <pre>HTTP 404
{ "error": { "code": "not_found", "message": "Chapter 99 not found in Genesis (KJV)." } }

HTTP 400
{ "error": { "code": "bad_request", "message": "Invalid chapter 'abc'." } }</pre>

  <h2>Fair use</h2>
  <p class="muted">This is a free public reference API running on free-tier infrastructure. There are no keys or hard limits;
  please be reasonable — cache responses where you can, and don't hammer it. The translations remain the property of their
  respective holders and are shared here with permission.</p>`;
}

/* ---------------- router ---------------- */
async function route() {
  const hash = location.hash.slice(1) || '/';
  const [pathPart, queryPart] = hash.split('?');
  const params = new URLSearchParams(queryPart || '');
  const parts = pathPart.split('/').filter(Boolean);
  try {
    if (parts[0] === 'v' && parts.length >= 4) {
      return await renderReader(parts[1], parts[2], parts[3]);
    }
    if (parts[0] === 'search') return await renderSearch(params);
    if (parts[0] === 'docs') return renderDocs();
    return await renderHome(params);
  } catch (err) {
    showError(err);
  }
}
window.addEventListener('hashchange', route);
route();
