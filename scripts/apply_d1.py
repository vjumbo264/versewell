#!/usr/bin/env python3
"""
Apply VerseWell import SQL to Cloudflare D1 via the Cloudflare API (task-04/05).

No local wrangler CLI needed — works from CI or a phone (Termux) with only
python3 + the two Cloudflare credentials as environment variables:

  CLOUDFLARE_API_TOKEN   (API token with D1 edit permission)
  CLOUDFLARE_ACCOUNT_ID
  D1_DATABASE_ID         (written to .d1_database_id by provision step / CI)

What it does:
  1. Ensures the schema exists (runs schema.sql statements).
  2. Reads the current versions(code -> source_sha256) map from D1 and writes
     it to IMPORT_STATE so scripts/import.py can skip unchanged files.
  3. Runs scripts/import.py.
  4. Uploads each produced *.sql to D1, chunked to stay under API limits.

QUOTA (D1 free tier): ~100k rows WRITTEN per day per account, reset at
midnight UTC. A full import of all versions is ~300k rows, so it cannot
finish in one day. This script therefore stops gracefully when the quota
error is hit (versions only become visible to the API after their
completion marker lands, so a partial day is always resumable), and the
GitHub Actions workflow runs on a daily schedule — the import completes
automatically over successive days. --all likewise stops at the quota
boundary; just re-run (or wait for the next scheduled run).

Usage:
  python3 scripts/apply_d1.py --state-only   # just write IMPORT_STATE json
  python3 scripts/apply_d1.py                # full import of new/changed files
  python3 scripts/apply_d1.py --all          # force re-import everything
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error


class QuotaExhausted(Exception):
    """Raised when D1's free-tier daily row-write limit is hit."""


def _is_quota_error(msg):
    return "row write limit" in msg or "daily row" in msg

API = "https://api.cloudflare.com/client/v4"
TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "")
ACCOUNT = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
DB_ID = os.environ.get("D1_DATABASE_ID") or (
    open(os.path.join(os.path.dirname(__file__), "..", ".d1_database_id")).read().strip()
    if os.path.exists(os.path.join(os.path.dirname(__file__), "..", ".d1_database_id"))
    else ""
)
OUT_DIR = os.environ.get("IMPORT_OUT", "/tmp/versewell_import")
STATE_FILE = os.environ.get("IMPORT_STATE", "/tmp/versewell_import/state.json")
# D1 rejects oversized statements with SQLITE_TOOBIG; 90 KB per statement is
# comfortably under its limit (900 KB chunks failed with HTTP 400 / 7500).
MAX_SQL_BYTES = 90_000


def d1_query(sql, params=None):
    url = f"{API}/accounts/{ACCOUNT}/d1/database/{DB_ID}/query"
    body = json.dumps({"sql": sql, "params": params or []}).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
            if not data.get("success"):
                msg = json.dumps(data.get("errors"))
                if _is_quota_error(msg):
                    raise QuotaExhausted(msg)
                raise RuntimeError(f"D1 query failed: {data.get('errors')}")
            return data["result"][0]
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:400]
            if _is_quota_error(detail):
                raise QuotaExhausted(detail) from e
            if e.code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"D1 HTTP {e.code}: {detail}") from e
    raise RuntimeError("unreachable")


def ensure_schema():
    path = os.path.join(os.path.dirname(__file__), "..", "schema.sql")
    # strip comment lines BEFORE splitting on ';' (comments may contain ';')
    lines = [l for l in open(path).read().splitlines() if not l.strip().startswith("--")]
    stmts = [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
    for stmt in stmts:
        d1_query(stmt + ";")
    # Self-heal: the verses table was first created with
    # REFERENCES versions(code), but the import inserts the versions row LAST
    # (atomic completion marker), so the enforced FK rejects every verse
    # INSERT. CREATE TABLE IF NOT EXISTS above is a no-op on the existing
    # table, so detect the stale FK and rebuild the table without it.
    try:
        row = d1_query("SELECT sql FROM sqlite_master WHERE name='verses';")
        ddl = (row.get("results") or [{}])[0].get("sql") or ""
    except Exception:
        return
    if "REFERENCES" not in ddl.upper():
        return
    print("stale FK detected on verses — rebuilding table without it…")
    for stmt in (
        "ALTER TABLE verses RENAME TO verses_old;",
        "CREATE TABLE verses (version TEXT NOT NULL, book TEXT NOT NULL, book_name TEXT NOT NULL, "
        "book_order INTEGER NOT NULL, chapter INTEGER NOT NULL, verse INTEGER NOT NULL, "
        "text TEXT NOT NULL, PRIMARY KEY (version, book, chapter, verse));",
        "INSERT INTO verses SELECT * FROM verses_old;",
        "DROP TABLE verses_old;",
        "CREATE INDEX IF NOT EXISTS idx_verses_lookup ON verses(version, book, chapter, verse);",
    ):
        d1_query(stmt)
    print("verses table rebuilt (no FK).")


def current_state():
    try:
        res = d1_query("SELECT code, source_sha256 FROM versions;")
        return {r["code"]: r["source_sha256"] for r in res.get("results", [])}
    except Exception:
        return {}


def split_sql(path):
    """Split a .sql file into chunks of whole statements under MAX_SQL_BYTES.

    import.py emits multi-row INSERTs where row lines end with ',\n' and the
    final row line of each statement ends with ';\n'. We accumulate lines into
    a statement buffer and close it only when the buffer ends with ';' AND we
    are not inside a quoted string. String state is tracked incrementally,
    treating a doubled '' as an escaped quote (never toggling state). Robust to
    semicolons and quotes inside text values."""
    stmts = []
    buf = []
    in_string = False

    def scan(line):
        nonlocal in_string
        i = 0
        n = len(line)
        while i < n:
            ch = line[i]
            if ch == "'":
                if in_string and i + 1 < n and line[i + 1] == "'":
                    i += 2  # escaped quote inside a string literal
                    continue
                in_string = not in_string
            i += 1

    for line in open(path):
        buf.append(line)
        scan(line)
        if not in_string and line.rstrip().endswith(";"):
            stmts.append("".join(buf))
            buf = []
    if buf:
        stmts.append("".join(buf))

    # D1's /query API rejects explicit transaction control (BEGIN/COMMIT/
    # SAVEPOINT -> error 7500); each /query call is already atomic per call.
    # PRAGMA foreign_keys is also unnecessary for our append-only import.
    skip = {"begin transaction;", "begin;", "commit;", "rollback;",
            "pragma foreign_keys=off;", "pragma foreign_keys=on;"}
    stmts = [s for s in stmts if s.strip().lower() not in skip]

    chunks, cur, size = [], [], 0
    for s in stmts:
        if size + len(s.encode()) > MAX_SQL_BYTES and cur:
            chunks.append("".join(cur))
            cur, size = [], 0
        cur.append(s)
        size += len(s.encode())
    if cur:
        chunks.append("".join(cur))
    return chunks


def apply_file(path):
    """Apply one version's .sql, atomically w.r.t. the change-detector.

    The `INSERT INTO versions` statement is the completion marker —
    current_state() reads versions.source_sha256 to decide which files to
    skip. If we applied it inline (near the top of the file) and the upload
    later aborted, the version would be left PARTIALLY imported but marked
    done, and every future deploy would skip it. So we hold the versions
    statement back and apply it only after every other chunk succeeds.
    A mid-import abort then leaves the version absent from `versions`,
    guaranteeing the next run re-imports it from scratch."""
    chunks = split_sql(path)
    data_chunks, version_stmt = [], None
    for chunk in chunks:
        if version_stmt is None and "INSERT INTO versions" in chunk:
            # split the versions statement out of its chunk (it sits right
            # after the DELETEs, which must run first anyway)
            idx = chunk.find("INSERT INTO versions")
            head, tail = chunk[:idx], chunk[idx:]
            end = tail.find(";\n")
            if end == -1:
                end = tail.rfind(";") + 1
            else:
                end += 2
            version_stmt = tail[:end]
            remainder = tail[end:]
            if head.strip():
                data_chunks.append(head)
            if remainder.strip():
                data_chunks.append(remainder)
        else:
            data_chunks.append(chunk)
    for i, chunk in enumerate(data_chunks):
        d1_query(chunk)
        print(f"  {os.path.basename(path)} chunk {i + 1}/{len(data_chunks)} applied", flush=True)
    if version_stmt:
        d1_query(version_stmt)
        print(f"  {os.path.basename(path)} versions row committed (completion marker)")
    else:
        print(f"  WARNING: no INSERT INTO versions found in {path}")


def main():
    if not (TOKEN and ACCOUNT and DB_ID):
        print("ERROR: need CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, D1_DATABASE_ID", file=sys.stderr)
        return 2
    os.makedirs(OUT_DIR, exist_ok=True)
    print("ensuring schema…")
    try:
        ensure_schema()
    except QuotaExhausted as e:
        # Quota already spent before we even start (e.g. earlier runs today).
        # Nothing can be written, so skip the import entirely but let the
        # deploy steps of the workflow proceed — exit 0 with a notice.
        print(f"D1 daily write quota already exhausted before import: {e}")
        print("::notice::D1 daily write quota reached — import resumes on next scheduled run")
        return 0
    print("reading current versions state…")
    state = current_state()
    json.dump(state, open(STATE_FILE, "w"))
    print(f"  {len(state)} versions already in D1")
    if "--state-only" in sys.argv:
        return 0

    args = [sys.executable, os.path.join(os.path.dirname(__file__), "import.py")]
    if "--all" in sys.argv:
        args.append("--all")
    subprocess.run(args, check=True, env={**os.environ, "IMPORT_OUT": OUT_DIR, "IMPORT_STATE": STATE_FILE})

    manifest = json.load(open(os.path.join(OUT_DIR, "manifest.json")))
    quota_hit = False
    for item in manifest["imported"]:
        print(f"applying {item['code']} ({item['verses']} verses)…")
        try:
            apply_file(item["sql"])
        except QuotaExhausted as e:
            # Daily row-write budget spent. Any version whose completion
            # marker (INSERT INTO versions) did not land is still treated as
            # unimported, so the next run resumes it automatically.
            print(f"\nD1 daily write quota exhausted: {e}")
            print("Stopping here; remaining versions resume on the next run "
                  "(scheduled daily, or manual re-run). This is expected on "
                  "the free tier and is NOT a build failure.")
            quota_hit = True
            break
    print("skipped (unchanged):", [s["code"] for s in manifest["skipped"]])
    print("errors:", manifest["errors"])
    if manifest["errors"]:
        return 1
    # Quota stop: exit 0 so CI marks the run successful (import continues
    # tomorrow via the scheduled workflow) but surfaces the note in the log.
    if quota_hit:
        print("::notice::D1 daily write quota reached — import resumes on next run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
