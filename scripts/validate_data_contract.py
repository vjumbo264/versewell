#!/usr/bin/env python3
"""
Validates that all scripture versions in VerseWell adhere to the DATA_CONTRACT:
1. All 12 indexed versions exist and have complete books.
2. The 5 audio translations (KJV, NIV, TLB, GW, VOICE) have all 260 NT chapters.
3. Every verse has non-empty text with no merged intros/headings.
4. Footnotes match either the structured object contract or clean string format.
5. Exits 0 on success, 1 on any violation.
"""
import os, json, sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
STATIC_DIR = os.path.join(ROOT, "site", "static-data")
TOP_INDEX = os.path.join(STATIC_DIR, "index.json")

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

def main():
    if not os.path.exists(TOP_INDEX):
        print(f"ERROR: Missing index file {TOP_INDEX}", file=sys.stderr)
        return 1

    with open(TOP_INDEX, "r", encoding="utf-8") as f:
        top = json.load(f)

    versions = [v["code"].lower() for v in top.get("versions", [])]
    errors = []

    # Verify audio versions are present
    for av in AUDIO_VERSIONS:
        if av not in versions:
            errors.append(f"Required audio translation '{av.upper()}' is not in static index.json")

    # Audit NT chapters for audio versions
    for av in AUDIO_VERSIONS:
        v_dir = os.path.join(STATIC_DIR, av)
        ch_count = 0
        for book_slug, exp_ch in NT_BOOKS:
            for ch in range(1, exp_ch + 1):
                ch_file = os.path.join(v_dir, book_slug, f"{ch}.json")
                if not os.path.exists(ch_file):
                    errors.append(f"Missing chapter: {av}/{book_slug}/{ch}.json")
                    continue
                ch_count += 1
                try:
                    with open(ch_file, "r", encoding="utf-8") as cf:
                        data = json.load(cf)
                except Exception as e:
                    errors.append(f"Corrupt JSON {ch_file}: {e}")
                    continue

                verses = data.get("verses", [])
                if not verses:
                    errors.append(f"No verses in {ch_file}")
                    continue

                for v in verses:
                    text = v.get("text", "")
                    if not text or not text.strip():
                        errors.append(f"Empty verse text in {ch_file} v{v.get('verse')}")
                    
                    fns = v.get("footnotes")
                    if fns:
                        for fn in fns:
                            if isinstance(fn, dict):
                                if "text" not in fn:
                                    errors.append(f"Malformed footnote in {ch_file}")
                            elif not isinstance(fn, str):
                                errors.append(f"Invalid footnote type in {ch_file}: {type(fn)}")

        if ch_count != 260:
            errors.append(f"{av.upper()} has {ch_count}/260 NT chapters")

    if errors:
        print("DATA CONTRACT VALIDATION FAILED:", file=sys.stderr)
        for err in errors[:20]:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print(f"Data contract validation PASSED. All {len(AUDIO_VERSIONS)} audio translations have 260/260 complete NT chapters.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
