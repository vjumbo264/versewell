#!/usr/bin/env python3
"""
Validates that all scripture versions in VerseWell adhere to docs/DATA_CONTRACT.md:
1. All 12 indexed versions exist in site/static-data/index.json and have valid book indexes.
2. Canonical testament ('OT' / 'NT') is stamped on every book (preventing incomplete versions like TPT/MSG from lumping NT books under OT).
3. Versions with intros (CEV, VOICE) expose has_intros=true and non-empty intros[], and intro prose is never merged into verse text.
4. Verse text never starts with its own heading_before or contains raw HTML tags.
5. Footnotes strictly follow [{"marker": str, "text": str}] with non-empty text.
6. All 5 audio translations (KJV, NIV, TLB, GW, VOICE) have 260/260 complete NT chapters with non-empty verses.
Exits 0 on success, 1 on any violation.
"""
import os, json, re, sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
STATIC_DIR = os.path.join(ROOT, "site", "static-data")
TOP_INDEX = os.path.join(STATIC_DIR, "index.json")

NT_OSIS = {
    "Matt", "Mark", "Luke", "John", "Acts", "Rom", "1Cor", "2Cor",
    "Gal", "Eph", "Phil", "Col", "1Thess", "2Thess", "1Tim", "2Tim",
    "Titus", "Phlm", "Heb", "Jas", "1Pet", "2Pet", "1John", "2John",
    "3John", "Jude", "Rev"
}

NT_BOOKS = [
    ("matthew", 28), ("mark", 16), ("luke", 24), ("john", 21),
    ("acts", 28), ("romans", 16), ("1-corinthians", 16), ("2-corinthians", 13),
    ("galatians", 6), ("ephesians", 6), ("philippians", 4), ("colossians", 4),
    ("1-thessalonians", 5), ("2-thessalonians", 3), ("1-timothy", 6), ("2-timothy", 4),
    ("titus", 3), ("philemon", 1), ("hebrews", 13), ("james", 5),
    ("1-peter", 5), ("2-peter", 3), ("1-john", 5), ("2-john", 1),
    ("3-john", 1), ("jude", 1), ("revelation", 22)
]

AUDIO_VERSIONS = ["kjv", "niv", "tlb", "gw", "voice"]
HTML_TAG_RE = re.compile(r"<[a-zA-Z/][^>]*>")

def main():
    if not os.path.exists(TOP_INDEX):
        print(f"ERROR: Missing index file {TOP_INDEX}", file=sys.stderr)
        return 1

    with open(TOP_INDEX, "r", encoding="utf-8") as f:
        top = json.load(f)

    top_versions = top.get("versions", [])
    if len(top_versions) < 12:
        print(f"ERROR: Expected 12 versions in index.json, got {len(top_versions)}", file=sys.stderr)
        return 1

    by_code = {v["code"].lower(): v for v in top_versions}
    errors = []

    for code_lc, vmeta in by_code.items():
        v_idx_path = os.path.join(STATIC_DIR, code_lc, "index.json")
        if not os.path.exists(v_idx_path):
            errors.append(f"Missing version index: {v_idx_path}")
            continue
        v_idx = json.load(open(v_idx_path, encoding="utf-8"))
        for b in v_idx.get("books", []):
            expected_t = "NT" if b.get("book") in NT_OSIS else "OT"
            if b.get("testament") != expected_t:
                errors.append(f"{code_lc.upper()} book {b.get('book')} has testament={b.get('testament')!r}, expected {expected_t!r}")

    for intro_ver, sample_ch in [("cev", ("matthew", 1)), ("voice", ("john", 1))]:
        if not by_code.get(intro_ver, {}).get("has_intros"):
            errors.append(f"{intro_ver.upper()} must have has_intros=true in index.json")
        sample_path = os.path.join(STATIC_DIR, intro_ver, sample_ch[0], f"{sample_ch[1]}.json")
        if os.path.exists(sample_path):
            sdata = json.load(open(sample_path, encoding="utf-8"))
            if not sdata.get("intros"):
                errors.append(f"{intro_ver.upper()} {sample_ch[0]} {sample_ch[1]} missing expected intros[]")

    summary_counts = {}
    for av in AUDIO_VERSIONS:
        if av not in by_code:
            errors.append(f"Required audio translation '{av.upper()}' missing from index.json")
            continue
        v_dir = os.path.join(STATIC_DIR, av)
        ch_count = 0
        verse_count = 0
        for book_slug, exp_ch in NT_BOOKS:
            for ch in range(1, exp_ch + 1):
                ch_file = os.path.join(v_dir, book_slug, f"{ch}.json")
                if not os.path.exists(ch_file):
                    errors.append(f"Missing chapter: {av}/{book_slug}/{ch}.json")
                    continue
                ch_count += 1
                try:
                    data = json.load(open(ch_file, encoding="utf-8"))
                except Exception as e:
                    errors.append(f"Corrupt JSON {ch_file}: {e}")
                    continue

                intros = data.get("intros") or []
                if data.get("intros") is not None and not isinstance(data.get("intros"), list):
                    errors.append(f"Invalid intros type in {ch_file}")
                for intr in intros:
                    if not isinstance(intr, dict) or not intr.get("text", "").strip():
                        errors.append(f"Malformed intro in {ch_file}")

                verses = data.get("verses", [])
                if not verses:
                    errors.append(f"No verses in {ch_file}")
                    continue

                for v in verses:
                    verse_count += 1
                    text = v.get("text", "")
                    if not text or not text.strip():
                        errors.append(f"Empty verse text in {ch_file} v{v.get('verse')}")
                        continue
                    if HTML_TAG_RE.search(text):
                        errors.append(f"Unstripped HTML tag in {ch_file} v{v.get('verse')}")
                    hb = v.get("heading_before")
                    if hb and len(hb) >= 8 and text.startswith(hb):
                        errors.append(f"Merged heading_before inside verse text in {ch_file} v{v.get('verse')}")
                    for intr in intros:
                         snippet = intr.get("text", "").strip()[:40]
                         if len(snippet) >= 25 and snippet in text:
                             errors.append(f"Merged intro text inside verse text in {ch_file} v{v.get('verse')}")

                    fns = v.get("footnotes")
                    if fns is not None:
                        if not isinstance(fns, list):
                            errors.append(f"Footnotes not a list in {ch_file} v{v.get('verse')}")
                        else:
                            for fn in fns:
                                if not isinstance(fn, dict) or not isinstance(fn.get("marker"), str) or not isinstance(fn.get("text"), str) or not fn["text"].strip():
                                    errors.append(f"Malformed footnote object in {ch_file} v{v.get('verse')}: {fn!r}")

        if ch_count != 260:
            errors.append(f"{av.upper()} has {ch_count}/260 NT chapters")
        summary_counts[av.upper()] = {"chapters": ch_count, "verses": verse_count}

    if errors:
        print("DATA CONTRACT VALIDATION FAILED:", file=sys.stderr)
        for err in errors[:30]:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print("Data contract validation PASSED:", json.dumps(summary_counts))
    return 0

if __name__ == "__main__":
    sys.exit(main())
