#!/usr/bin/env python3
"""
VerseWell static-JSON generator — builds the platform's SINGLE SOURCE OF
TRUTH (static-only architecture; there is no database).

Reads every /bible-sources/*.sqlite file and writes a plain-JSON tree of the
whole library into site/static-data/, which Cloudflare Pages serves as static
assets and the Worker API reads at request time. This is the ONLY content
pipeline: adding a new .sqlite to /bible-sources/ and pushing is the entire
process required to make a version live on both the site and the API.

Usage:
  python3 scripts/generate_static.py                # regenerate every version
  python3 scripts/generate_static.py kjv.sqlite3    # one file only

Single mapping, no duplication: this script does NOT reimplement any
source->unified mapping. It imports scripts/import.py and runs the exact
same import_file() over an in-memory SQLite DB shaped by the root schema.sql,
then reads rows back out. Every quirk handled there (CEV +1 chapter shift,
MSG merged verses, fen- vs !f. footnotes, the global per-book fen counter,
section-heading stripping, intro extraction, primary-key dedup) is inherited
automatically.

Outputs per version: per-chapter files in the Worker API's chapter-response
shape, a per-version index.json (books+slugs), and a compact single-file
search.json the Worker's /search route scans in one fetch. The top-level
site/static-data/index.json lists every version. JSON shapes intentionally
reuse the Worker API response shapes (see ARCHITECTURE.md) so a consumer can
swap between /api/v1/... and /static-data/... without code changes.

Idempotency: each version's rows are rebuilt from scratch each run; a JSON
file is only (re)written when its content differs, so re-running against an
unchanged source produces zero git diffs.
"""
import importlib.util
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
SRC_DIR = os.path.join(ROOT, "bible-sources")
OUT_DIR = os.path.join(ROOT, "site", "static-data")  # Pages deploys site/ only — mirror must live inside it
SCHEMA = os.path.join(ROOT, "schema.sql")
IMPORTER = os.path.join(HERE, "import.py")

RESERVED_BOOK_SLUGS = {"books", "search", "random", "index"}  # mirror the API's reserved segments

# ---------------------------------------------------------------------------
# Canonical Old/New Testament identity (Part 2 fix).
#
# A book's testament is a property of the BOOK ITSELF (its OSIS code in the
# 66-book canon), NEVER of the version it appears in. The previous client-side
# rule (book_order <= 39) broke for incomplete versions whose source assigns
# its own 1..N book numbers — e.g. TPT numbers Psalms=1 .. Revelation=30, so
# Matthew..Revelation (order 4..30) were mis-filed under 'Old Testament'.
# The generator now stamps every book with a canonical `testament` field so
# clients never have to re-derive this from version-local ordering.
# ---------------------------------------------------------------------------
CANONICAL_OT_OSIS = {
    "Gen", "Exod", "Lev", "Num", "Deut", "Josh", "Judg", "Ruth",
    "1Sam", "2Sam", "1Kgs", "2Kgs", "1Chr", "2Chr", "Ezra", "Neh",
    "Esth", "Job", "Ps", "Prov", "Eccl", "Song", "Isa", "Jer", "Lam",
    "Ezek", "Dan", "Hos", "Joel", "Amos", "Obad", "Jonah", "Mic",
    "Nah", "Hab", "Zeph", "Hag", "Zech", "Mal",
}
CANONICAL_NT_OSIS = {
    "Matt", "Mark", "Luke", "John", "Acts", "Rom", "1Cor", "2Cor",
    "Gal", "Eph", "Phil", "Col", "1Thess", "2Thess", "1Tim", "2Tim",
    "Titus", "Phlm", "Heb", "Jas", "1Pet", "2Pet", "1John", "2John",
    "3John", "Jude", "Rev",
}


def testament_of(osis_book):
    """Canonical OT/NT classification by OSIS code — independent of any
    version's completeness or its own book numbering."""
    if osis_book in CANONICAL_NT_OSIS:
        return "NT"
    return "OT"  # OT set and any defensive default (all 66 are covered above)


def load_audio_allowlist():
    """Closed audio scope from AUDIO_STATE.json. A version absent from this
    list NEVER gets an audio_url, even if MP3 files somehow exist for it."""
    try:
        with open(os.path.join(ROOT, "AUDIO_STATE.json"), encoding="utf-8") as fh:
            return set(json.load(fh).get("audio_enabled_versions", []))
    except OSError:
        return set()


AUDIO_ALLOWLIST = load_audio_allowlist()


def load_importer():
    spec = importlib.util.spec_from_file_location("vw_import", IMPORTER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_unified_db(importer, path):
    """Run the real importer into an in-memory SQLite DB with the unified schema."""
    con = sqlite3 = __import__("sqlite3").connect(":memory:")
    con.executescript(open(SCHEMA, encoding="utf-8").read())
    con.row_factory = __import__("sqlite3").Row
    buf = io.StringIO()
    meta = importer.import_file(path, buf)
    con.executescript(buf.getvalue())
    return con, meta


def book_slug(book_name):
    """URL slug for a book, matching the Worker API's normalizeBookParam
    (lowercase, spaces/punctuation -> '-') so a static path resolves to the
    same book the API's /versions/{v}/{book} would."""
    import re
    s = re.sub(r"[\s_+]+", "-", str(book_name).strip().lower())
    s = re.sub(r"[^a-z0-9-]", "", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s


def write_if_changed(path, data):
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n"
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            if fh.read() == payload:
                return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(payload)
    return True


def navigation(books_rows, book, chapter, chapter_count):
    """Mirror worker/index.js chapterNavigation: prev/next across book edges."""
    idx = next(i for i, b in enumerate(books_rows) if b["book"] == book)
    prev = None
    nxt = None
    if chapter > 1:
        prev = {"book": book, "chapter": chapter - 1}
    elif idx > 0:
        pb = books_rows[idx - 1]
        prev = {"book": pb["book"], "chapter": pb["chapters"]}
    if chapter < chapter_count:
        nxt = {"book": book, "chapter": chapter + 1}
    elif idx < len(books_rows) - 1:
        nxt = {"book": books_rows[idx + 1]["book"], "chapter": 1}
    return {"prev": prev, "next": nxt}


def generate_version(importer, path, out_root):
    con, meta = build_unified_db(importer, path)
    code = meta["code"]
    ver = con.execute(
        "SELECT code, name, language, description, has_footnotes, has_intros, verse_count FROM versions WHERE code=?",
        (code,),
    ).fetchone()

    books_rows = [
        dict(r)
        for r in con.execute(
            "SELECT book, book_name, book_order, MAX(chapter) AS chapters, COUNT(*) AS verse_count "
            "FROM verses WHERE version=? GROUP BY book, book_name, book_order ORDER BY book_order",
            (code,),
        )
    ]
    # Resolve slug collisions deterministically (e.g. two books slugging equal,
    # or a slug colliding with a reserved word) by suffixing the OSIS code.
    used = set(RESERVED_BOOK_SLUGS)
    for b in books_rows:
        s = book_slug(b["book_name"])
        if s in used:
            s = book_slug(b["book"])
        base, n = s, 2
        while s in used:
            s = "%s-%d" % (base, n)
            n += 1
        b["slug"] = s
        used.add(s)
        b["testament"] = testament_of(b["book"])

    changed = 0
    version_dir = os.path.join(out_root, code.lower())

    # Per-chapter files, in the Worker API's exact chapter-response shape.
    for b in books_rows:
        ch_range = [
            r[0]
            for r in con.execute(
                "SELECT DISTINCT chapter FROM verses WHERE version=? AND book=? ORDER BY chapter",
                (code, b["book"]),
            )
        ]
        for ch in ch_range:
            verses = []
            fn_rows = {}
            for fr in con.execute(
                "SELECT verse, marker, note_text FROM footnotes WHERE version=? AND book=? AND chapter=? ORDER BY verse, marker",
                (code, b["book"], ch),
            ):
                fn_rows.setdefault(fr["verse"], []).append({"marker": fr["marker"], "text": fr["note_text"]})
            for vr in con.execute(
                "SELECT verse, text FROM verses WHERE version=? AND book=? AND chapter=? ORDER BY verse",
                (code, b["book"], ch),
            ):
                v = {"verse": vr["verse"], "text": vr["text"]}
                if vr["verse"] in fn_rows:
                    v["footnotes"] = fn_rows[vr["verse"]]
                verses.append(v)
            # A chapter may carry MULTIPLE intros (book intro at ch 1 plus
            # mid-chapter asides) — never LIMIT 1, that silently drops data.
            intro_rows = con.execute(
                "SELECT start_verse, end_verse, intro_text FROM section_intros "
                "WHERE version=? AND book=? AND chapter=? ORDER BY start_verse",
                (code, b["book"], ch),
            ).fetchall()
            intros = [
                {"start_verse": r["start_verse"], "end_verse": r["end_verse"], "text": r["intro_text"]}
                for r in intro_rows
            ]
            # audio_url: committed narration MP3 for allowlisted versions
            # only, null otherwise (never a dangling link).
            mp3_path = os.path.join(version_dir, b["slug"], "%d.mp3" % ch)
            audio_url = (
                "/static-data/%s/%s/%d.mp3" % (code.lower(), b["slug"], ch)
                if code in AUDIO_ALLOWLIST and os.path.exists(mp3_path)
                else None
            )
            payload = {
                "version": code,
                "book": b["book"],
                "book_name": b["book_name"],
                "chapter": ch,
                "audio_url": audio_url,
                "intros": intros or None,
                "verses": verses,
                "navigation": navigation(books_rows, b["book"], ch, b["chapters"]),
            }
            changed += write_if_changed(
                os.path.join(version_dir, b["slug"], "%d.json" % ch), payload
            )

    # Per-version index: the /versions/{v}/books response shape, plus the slug
    # each book's chapters live under in the static tree.
    version_index = {
        "version": code,
        "name": ver["name"],
        "books": [
            {
                "book": b["book"],
                "book_name": b["book_name"],
                "book_order": b["book_order"],
                "chapters": b["chapters"],
                "verse_count": b["verse_count"],
                "slug": b["slug"],
                "testament": b["testament"],
            }
            for b in books_rows
        ],
    }
    changed += write_if_changed(os.path.join(version_dir, "index.json"), version_index)

    # Compact single-file search index: every verse's text plus its location,
    # so the Worker's /search route can scan a version with ONE fetch instead
    # of one fetch per chapter (a thousand+ subrequests would blow the Workers
    # free-tier request limit). Shape: {version, entries:[{book,book_name,
    # chapter,verse,text}]}. ~1-2 MB gzip per version — trivially cacheable.
    search_entries = [
        {
            "book": r["book"],
            "book_name": r["book_name"],
            "chapter": r["chapter"],
            "verse": r["verse"],
            "text": r["text"],
        }
        for r in con.execute(
            "SELECT v.book, b.book_name, v.chapter, v.verse, v.text "
            "FROM verses v JOIN (SELECT DISTINCT book, book_name, book_order FROM verses WHERE version=?) b "
            "  ON b.book = v.book "
            "WHERE v.version=? ORDER BY b.book_order, v.chapter, v.verse",
            (code, code),
        )
    ]
    changed += write_if_changed(
        os.path.join(version_dir, "search.json"), {"version": code, "entries": search_entries}
    )

    intro_count = con.execute(
        "SELECT COUNT(*) FROM section_intros WHERE version=?", (code,)
    ).fetchone()[0]
    # Sanity check vs the importer's own count — a mismatch means the
    # normalized DB lost intros between extraction and output.
    if intro_count != meta.get("intros", 0):
        print(
            "WARNING %s: importer extracted %d intros but unified DB holds %d"
            % (code, meta.get("intros", 0), intro_count),
            file=sys.stderr,
        )

    top = {
        "code": ver["code"],
        "name": ver["name"],
        "language": ver["language"],
        "description": ver["description"],
        "has_footnotes": bool(ver["has_footnotes"]),
        "has_intros": bool(ver["has_intros"]),
        "verse_count": ver["verse_count"],
    }
    con.close()
    return code, top, changed, intro_count


def main():
    importer = load_importer()
    only = [a for a in sys.argv[1:] if not a.startswith("--")]
    files = sorted(
        f for f in os.listdir(SRC_DIR) if f.endswith((".sqlite3", ".sqlite", ".db"))
    )
    if only:
        files = [f for f in files if f in only]
    if not files:
        print("no source files found", file=sys.stderr)
        return 1

    os.makedirs(OUT_DIR, exist_ok=True)
    top_versions = []
    total_changed = 0
    for f in files:
        code, top, changed, intro_count = generate_version(importer, os.path.join(SRC_DIR, f), OUT_DIR)
        top_versions.append(top)
        total_changed += changed
        print("static mirror: %-8s %6d verses  %5d intros  (%d file(s) written)" % (code, top["verse_count"], intro_count, changed))

    top_versions.sort(key=lambda v: v["code"])
    total_changed += write_if_changed(os.path.join(OUT_DIR, "index.json"), {"versions": top_versions})
    print("top-level index: %d version(s); %d file(s) changed total" % (len(top_versions), total_changed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
