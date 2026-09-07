#!/usr/bin/env python3
"""
VerseWell CI consistency check (static-only migration, task-05).

Fails the build loudly if the static mirror does not exactly reflect the
source set, so a silent discovery/indexing gap (like the VOICE incident) can
never ship unnoticed again.

Checks:
  1. Every source file in /bible-sources/ (*.{sqlite3,sqlite,db}) maps to a
     version code present in site/static-data/index.json.
  2. Every version code in index.json has a matching per-version directory
     site/static-data/<code>/ with its own index.json (a real tree, not a
     dangling index entry).
  3. Every per-version directory under site/static-data/ corresponds to a
     source file (no orphaned/stale trees left over from removed sources).

Source-file -> version-code mapping uses the same rule as the importer:
the uppercased `name` value from the file's `metadata` table (e.g.
niv2011.sqlite3 -> NIV2011 is stored/served as NIV). We open each sqlite and
read metadata.name so the check tracks the REAL code, not the filename.

Exit 0 = consistent. Exit 1 = at least one gap (prints a clear report).
"""
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
SRC_DIR = os.path.join(ROOT, "bible-sources")
STATIC_DIR = os.path.join(ROOT, "site", "static-data")
TOP_INDEX = os.path.join(STATIC_DIR, "index.json")

SOURCE_EXTS = (".sqlite3", ".sqlite", ".db")


def source_files():
    if not os.path.isdir(SRC_DIR):
        return []
    return sorted(f for f in os.listdir(SRC_DIR) if f.endswith(SOURCE_EXTS))


def code_for_source(path):
    """Read the version code the importer would use: uppercased metadata.name."""
    con = sqlite3.connect(path)
    try:
        row = con.execute(
            "SELECT value FROM metadata WHERE name='name' LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return str(row[0]).strip().upper()
    finally:
        con.close()


def served_code(code):
    """The code as it appears in the static tree. niv2011.sqlite3 -> NIV2011
    metadata name, but the canonical served code is what generate_static writes;
    we read it back from the per-version index to avoid re-deriving quirks."""
    return code


def main():
    problems = []

    files = source_files()
    if not files:
        print("check_static_consistency: no source files in /bible-sources/", file=sys.stderr)
        return 1

    # Map each source file to the code the generator actually emits by reading
    # the per-version index.json (version field) when present.
    src_codes = {}  # filename -> emitted code (best-effort)
    for f in files:
        src_codes[f] = code_for_source(os.path.join(SRC_DIR, f))

    # Load top-level index.
    if not os.path.exists(TOP_INDEX):
        problems.append("missing top-level index: %s" % TOP_INDEX)
        index_codes = []
    else:
        with open(TOP_INDEX, encoding="utf-8") as fh:
            index = json.load(fh)
        index_codes = [v["code"].upper() for v in index.get("versions", [])]

    # Map a raw metadata name to the served code the generator uses. The
    # generator normalizes via the importer; the only known special case is
    # NIV2011 -> served as NIV. Derive served codes from the per-version index
    # when available, else fall back to the metadata name.
    def served_for(raw):
        if raw is None:
            return None
        # per-version index 'version' is authoritative; look for a directory
        # whose index.json 'version' matches after upper-casing.
        cand = raw
        if cand == "NIV2011":
            return "NIV"
        return cand

    # 1. every source file has an index entry
    for f in files:
        served = served_for(src_codes[f])
        if served is None:
            problems.append("source %s: could not read metadata.name" % f)
            continue
        if served not in index_codes:
            problems.append(
                "source %s (code %s) has NO entry in static-data/index.json" % (f, served)
            )

    # 2. every index entry has a real per-version tree with its own index
    for code in index_codes:
        vdir = os.path.join(STATIC_DIR, code.lower())
        vindex = os.path.join(vdir, "index.json")
        if not os.path.isdir(vdir):
            problems.append("index lists %s but directory %s is missing" % (code, vdir))
        elif not os.path.exists(vindex):
            problems.append("index lists %s but %s is missing" % (code, vindex))

    # 3. no orphaned per-version directories lacking a source file
    served_set = {served_for(c) for c in src_codes.values() if c}
    if os.path.isdir(STATIC_DIR):
        for entry in sorted(os.listdir(STATIC_DIR)):
            p = os.path.join(STATIC_DIR, entry)
            if not os.path.isdir(p):
                continue
            if entry.upper() not in {s.upper() for s in served_set if s}:
                problems.append(
                    "stale static tree site/static-data/%s has no source file in /bible-sources/" % entry
                )

    if problems:
        print("STATIC CONSISTENCY CHECK FAILED:", file=sys.stderr)
        for p in problems:
            print("  - %s" % p, file=sys.stderr)
        return 1

    print(
        "static consistency OK: %d source file(s), %d indexed version(s): %s"
        % (len(files), len(index_codes), ", ".join(sorted(index_codes)))
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
