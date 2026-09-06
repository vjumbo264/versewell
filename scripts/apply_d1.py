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
MAX_SQL_BYTES = 900_000  # stay safely under D1 /query body limits


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
                raise RuntimeError(f"D1 query failed: {data.get('errors')}")
            return data["result"][0]
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:400]
            if e.code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"D1 HTTP {e.code}: {detail}") from e
    raise RuntimeError("unreachable")


def ensure_schema():
    schema = open(os.path.join(os.path.dirname(__file__), "..", "schema.sql")).read()
    # split on statement boundaries; schema.sql uses one statement per block
    stmts = [s.strip() for s in schema.split(";") if s.strip() and not s.strip().startswith("--")]
    for raw in stmts:
        stmt = "\n".join(l for l in raw.splitlines() if not l.strip().startswith("--")).strip()
        if stmt:
            d1_query(stmt + ";")


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
    chunks = split_sql(path)
    for i, chunk in enumerate(chunks):
        # strip transaction wrappers for per-chunk execution
        d1_query(chunk)
        print(f"  {os.path.basename(path)} chunk {i + 1}/{len(chunks)} applied")


def main():
    if not (TOKEN and ACCOUNT and DB_ID):
        print("ERROR: need CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, D1_DATABASE_ID", file=sys.stderr)
        return 2
    os.makedirs(OUT_DIR, exist_ok=True)
    print("ensuring schema…")
    ensure_schema()
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
    for item in manifest["imported"]:
        print(f"applying {item['code']} ({item['verses']} verses)…")
        apply_file(item["sql"])
    print("skipped (unchanged):", [s["code"] for s in manifest["skipped"]])
    print("errors:", manifest["errors"])
    return 1 if manifest["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
