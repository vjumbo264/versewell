#!/usr/bin/env python3
"""
VerseWell importer (task-04).

Reads every *.sqlite3 file in /bible-sources/, maps it onto the unified
schema (see ARCHITECTURE.md), and emits SQL statements that can be applied to
Cloudflare D1 (via scripts/apply_d1.py or `wrangler d1 execute --file`).

Usage:
  python3 scripts/import.py                 # import all new/changed sources
  python3 scripts/import.py --all           # force re-import everything
  python3 scripts/import.py amp.sqlite3     # import one file

Change detection: each file's SHA-256 is compared against
versions.source_sha256 already in D1 (state read through the D1 API by
apply_d1.py and passed in via STATE_FILE). New or changed files are
(re)imported; unchanged files are skipped, so deploys are incremental.

Output: writes /tmp/versewell_import/<code>.sql plus a manifest.json listing
what was imported/skipped. SQL is emitted with parameterized-escaped literals
(single-quote doubling) — the source files are trusted church exports, and
this is a one-way batch import, but escaping is done uniformly regardless.
"""
import hashlib
import html
import json
import os
import re
import sqlite3
import sys

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "bible-sources")
OUT_DIR = os.environ.get("IMPORT_OUT", "/tmp/versewell_import")
STATE_FILE = os.environ.get("IMPORT_STATE", "")  # JSON: {code: sha256} already in D1

# Books present in each source in canonical order (from the source's own
# `books` table — we never hardcode the canon).
BATCH = 100  # rows per INSERT statement. Merged-text versions (MSG) can
# exceed D1's per-statement size limit (SQLITE_TOOBIG) at larger batches,
# even when the overall chunk size is small. 100 keeps every statement small.


def esc(s):
    """SQL literal. Ints/numbers stay numeric; strings are quoted+escaped.
    Newlines/tabs are flattened so every emitted statement is single-line
    (keeps statement splitting in apply_d1.py unambiguous)."""
    if s is None:
        return "NULL"
    if isinstance(s, (int, float)):
        return str(s)
    t = str(s).replace("\r", " ").replace("\n", " ").replace("\t", " ")
    t = re.sub(r" {2,}", " ", t)
    return "'" + t.replace("'", "''") + "'"


def strip_html(raw):
    """HTML snippet -> readable plain text. Keeps inline words, drops tags."""
    if not raw:
        return ""
    t = re.sub(r"(?is)<(script|style).*?</\\1>", " ", raw)
    t = re.sub(r"(?i)</(p|div|h[1-6]|li|br|tr)>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n\n", t)
    return t.strip()


def norm_code(name, filename):
    code = (name or os.path.splitext(filename)[0]).strip().upper()
    code = re.sub(r"[^A-Z0-9]", "", code)
    return code


def load_meta(con):
    return {r[0]: r[1] for r in con.execute("SELECT name, value FROM metadata")}


def heading_sets(con):
    """chapter ref -> set of section-heading strings from the chapter HTML."""
    out = {}
    for ref, content in con.execute(
        "SELECT reference_osis, content FROM chapters "
        "WHERE reference_osis NOT LIKE '%.int'"
    ):
        hs = set()
        for m in re.finditer(r"(?is)<h[2-4][^>]*>(.*?)</h[2-4]>", content or ""):
            t = re.sub(r"<[^>]+>", "", m.group(1))
            t = re.sub(r"\s+", " ", t).strip()
            if t:
                hs.add(t)
        out[ref] = hs
    return out


def strip_heading(text, headings):
    if not text or "\n" not in text or not headings:
        return text
    first, rest = text.split("\n", 1)
    if first.strip() in headings and rest.strip():
        return rest.strip()
    return text


def parse_fen_target(content):
    """BibleGateway-style note: target ref lives in title=\"Go to Book C:V\"."""
    m = re.search(r'title="Go to ([^"]+?)\s+(\d+):(\d+)"', content or "")
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3))


def parse_usfm_target(link):
    """USFM-style note: 'Gen.1.1!f.1' -> (osis, 1, 1). Handles ranges 'Mark.16.19-Mark.16.20!f.1'."""
    m = re.match(r"^([A-Za-z0-9]+)\.(\d+)\.(\d+)(?:-[A-Za-z0-9.]+)?![fx]\.\d+$", link or "")
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3))


def note_marker(link, n):
    m = re.search(r"([a-zA-Z])$", link or "")
    if m:
        return m.group(1)
    m = re.search(r"!f\.(\d+)$", link or "")
    return m.group(1) if m else str(n)


def import_file(path, out_sql):
    filename = os.path.basename(path)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    meta = load_meta(con)
    code = norm_code(meta.get("name"), filename)
    fullname = strip_html(meta.get("fullname") or code)
    description = strip_html(meta.get("copyright") or "") or None

    books = {}   # osis -> dict(human, number)
    for r in cur.execute("SELECT number, osis, human FROM books ORDER BY number"):
        books[r["osis"]] = {"human": r["human"], "number": r["number"]}
    book_by_human = {}
    for osis, b in books.items():
        book_by_human[b["human"].lower()] = osis

    is_cev = code == "CEV"
    chapter_shift = 1 if is_cev else 0
    headings = heading_sets(con)

    sha = hashlib.sha256(open(path, "rb").read()).hexdigest()

    verses, footnotes, intros = [], [], []
    seen_verse_keys = set()
    seen_fn_keys = set()

    # --- verses ---
    for r in cur.execute("SELECT book, verse, unformatted FROM verses ORDER BY id"):
        raw = float(r["verse"])
        ch = int(raw)
        vnum = int(round((raw - ch) * 1000))
        ch -= chapter_shift
        if ch < 1 or vnum < 1:
            continue
        text = (r["unformatted"] or "").strip()
        if not text:
            continue
        # heading lookup uses the ORIGINAL (pre-shift) chapter ref, because the
        # source chapters table is in the source's own numbering
        src_ch = ch + chapter_shift
        text = strip_heading(text, headings.get(f"{r['book']}.{src_ch}"))
        text = re.sub(r"[ \t]+", " ", text).strip()
        if not text:
            continue
        b = books.get(r["book"])
        if not b:
            continue
        # Some source files contain genuine duplicate rows for one reference
        # (e.g. CEV has two Mark 17.009 rows = Mark 16:9 after the chapter
        # shift: the main text and a trailing summary line). Keep the first
        # row in source order; never emit two rows for one primary key.
        vkey = (r["book"], ch, vnum)
        if vkey in seen_verse_keys:
            continue
        seen_verse_keys.add(vkey)
        verses.append((code, r["book"], b["human"], b["number"], ch, vnum, text))

    # --- footnotes ---
    fen_style = False
    for r in cur.execute("SELECT link, content FROM annotations"):
        link, content = r["link"] or "", r["content"] or ""
        if link.startswith("cen-") or "!x." in link:
            continue  # cross-references: skipped (decision D1)
        if link.startswith("fen-") or link.startswith("fn-") or "!f." in link:
            pass
        else:
            continue
        if link.startswith("fen-"):
            fen_style = True
            tgt = parse_fen_target(content)
            if not tgt:
                continue
            human, ch, vnum = tgt
            osis = book_by_human.get(human.lower())
            if not osis:
                continue
        else:
            tgt = parse_usfm_target(link)
            if not tgt:
                continue
            osis, ch, vnum = tgt
            if osis not in books:
                continue
        # CEV annotation refs are already in true reference space — no shift.
        note = strip_html(content)
        # drop the leading back-reference ("Genesis 1:1 " / "1:1 " / "1.1,2 ")
        note = re.sub(r"^([A-Za-z ]+)?\d+[:.]\d+([,\-\u2013]\d+)*\s*", "", note).strip()
        if not note:
            continue
        marker = note_marker(link, len(footnotes) + 1)
        # Guarantee (book, chapter, verse, marker) uniqueness: a verse can be
        # annotated twice by the source (e.g. CEV Mark 16:9), which would
        # otherwise collide on the primary key.
        fkey = (osis, ch, vnum, marker)
        if fkey in seen_fn_keys:
            suffix = 2
            while (osis, ch, vnum, f"{marker}.{suffix}") in seen_fn_keys:
                suffix += 1
            marker = f"{marker}.{suffix}"
            fkey = (osis, ch, vnum, marker)
        seen_fn_keys.add(fkey)
        footnotes.append((code, osis, ch, vnum, marker, note))

    # --- section intros (CEV book-level *.int chapters) ---
    for r in cur.execute(
        "SELECT reference_osis, content FROM chapters WHERE reference_osis LIKE '%.int'"
    ):
        osis = r["reference_osis"].split(".")[0]
        if osis not in books:
            continue
        text = strip_html(r["content"])
        text = re.sub(r"^Introduction\s*", "", text, flags=re.I).strip()
        if not text:
            continue
        last_v = max([v[5] for v in verses if v[1] == osis and v[4] == 1] or [1])
        intros.append((code, osis, 1, 1, last_v, text))

    # intros reference book by osis; keep verses' chapter-1 max as end range

    # --- emit SQL ---
    w = out_sql.write
    w(f"PRAGMA foreign_keys=OFF;\n")
    w(f"BEGIN TRANSACTION;\n")
    w(f"DELETE FROM footnotes WHERE version={esc(code)};\n")
    w(f"DELETE FROM section_intros WHERE version={esc(code)};\n")
    w(f"DELETE FROM verses WHERE version={esc(code)};\n")
    w(f"DELETE FROM versions WHERE code={esc(code)};\n")
    w(
        "INSERT INTO versions (code, name, language, description, has_footnotes, "
        "has_intros, source_file, source_sha256, verse_count) VALUES ("
        f"{esc(code)}, {esc(fullname)}, 'en', {esc(description)}, "
        f"{1 if footnotes else 0}, {1 if intros else 0}, {esc(filename)}, "
        f"{esc(sha)}, {len(verses)});\n"
    )

    def emit(table, cols, rows):
        # Batch by both count and accumulated bytes so a few very long rows
        # (merged MSG paragraphs) can never push one INSERT over D1's
        # per-statement limit.
        MAX_STMT_BYTES = 60_000
        i = 0
        while i < len(rows):
            batch, size = [], 0
            while i < len(rows) and len(batch) < BATCH:
                row_sql = "(" + ", ".join(esc(c) for c in rows[i]) + ")"
                n = len(row_sql.encode())
                if batch and size + n > MAX_STMT_BYTES:
                    break
                batch.append(row_sql)
                size += n
                i += 1
            vals = ",\n".join(batch)
            w(f"INSERT INTO {table} ({cols}) VALUES\n{vals};\n")

    emit("verses", "version, book, book_name, book_order, chapter, verse, text", verses)
    emit("footnotes", "version, book, chapter, verse, marker, note_text", footnotes)
    emit("section_intros", "version, book, chapter, start_verse, end_verse, intro_text", intros)
    w("COMMIT;\n")

    con.close()
    return {
        "code": code,
        "name": fullname,
        "file": filename,
        "sha256": sha,
        "verses": len(verses),
        "footnotes": len(footnotes),
        "intros": len(intros),
    }


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    force = "--all" in sys.argv
    only = [a for a in sys.argv[1:] if not a.startswith("--")]

    existing = {}
    if STATE_FILE and os.path.exists(STATE_FILE):
        existing = json.load(open(STATE_FILE))

    files = sorted(
        f for f in os.listdir(SRC_DIR)
        if f.endswith((".sqlite3", ".sqlite", ".db"))
    )
    manifest = {"imported": [], "skipped": [], "errors": []}
    for f in files:
        if only and f not in only:
            continue
        path = os.path.join(SRC_DIR, f)
        sha = hashlib.sha256(open(path, "rb").read()).hexdigest()
        con = sqlite3.connect(path)
        code = norm_code(load_meta(con).get("name"), f)
        con.close()
        if not force and existing.get(code) == sha:
            manifest["skipped"].append({"code": code, "file": f, "reason": "unchanged sha256"})
            continue
        try:
            sql_path = os.path.join(OUT_DIR, f"{code}.sql")
            with open(sql_path, "w") as out:
                result = import_file(path, out)
            result["sql"] = sql_path
            manifest["imported"].append(result)
            print(f"imported {f}: {result['verses']} verses, "
                  f"{result['footnotes']} footnotes, {result['intros']} intros")
        except Exception as e:  # noqa: BLE001
            manifest["errors"].append({"file": f, "error": str(e)})
            print(f"ERROR importing {f}: {e}", file=sys.stderr)

    with open(os.path.join(OUT_DIR, "manifest.json"), "w") as mf:
        json.dump(manifest, mf, indent=2)
    print(json.dumps({k: len(v) for k, v in manifest.items()}))
    return 1 if manifest["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
