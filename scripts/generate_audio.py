#!/usr/bin/env python3
"""
VerseWell Audio Generation Pipeline (Contract fixes_oct30_v1, Section 6)
Synthesizes verified New Testament chapter audio for 5 translations: KJV, NIV, TLB, GW, VOICE.
- Reads clean static chapter JSON (site/static-data/) — verse text only (never intros, headings, footnotes, or verse numbers)
- Preserves textual-bracket Scripture verses (e.g. VOICE Matthew 17:21) while stripping short editorial/omission tags
- Synthesizes per verse with bounded concurrency (Semaphore=5)
- Ordered fallback chain per translation: primary Edge neural voice -> secondary Edge neural voice -> offline local Piper neural TTS
- Concatenates with 700ms pause after lead-in and 350ms pause between verses, computing exact per-verse start/end ms
- Masters to mono -16 LUFS AAC in .m4a at 48kbps with +faststart
- Verifies size, ffprobe duration vs WPM, mean volume, silence stretches, and SHA-256; deletes & fails (exit 1) on any invalid chapter
- Reconciles manifest.json with actual GitHub Release assets and writes Actions step summary
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
STATIC_DIR = os.path.join(ROOT, "site", "static-data")
PIPER_CACHE_DIR = "/tmp/piper_models"
MAX_JOB_SECONDS = int(5.0 * 3600)  # Graceful slice boundary before 6h GitHub Actions timeout

NT_BOOKS = [
    ("matthew", 28), ("mark", 16), ("luke", 24), ("john", 21),
    ("acts", 28), ("romans", 16), ("1-corinthians", 16), ("2-corinthians", 13),
    ("galatians", 6), ("ephesians", 6), ("philippians", 4), ("colossians", 4),
    ("1-thessalonians", 5), ("2-thessalonians", 3), ("1-timothy", 6), ("2-timothy", 4),
    ("titus", 3), ("philemon", 1), ("hebrews", 13), ("james", 5),
    ("1-peter", 5), ("2-peter", 3), ("1-john", 5), ("2-john", 1),
    ("3-john", 1), ("jude", 1), ("revelation", 22),
]

VOICE_CONFIG = {
    "kjv": {
        "primary": "en-US-ChristopherNeural",
        "fallback_edge": "en-US-RogerNeural",
        "piper_model": "en_US-lessac-medium",
        "piper_url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/en_US-lessac-medium.onnx",
        "title": "King James Version",
    },
    "niv": {
        "primary": "en-US-JennyNeural",
        "fallback_edge": "en-US-AriaNeural",
        "piper_model": "en_US-amy-medium",
        "piper_url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/amy/medium/en_US-amy-medium.onnx",
        "title": "New International Version",
    },
    "tlb": {
        "primary": "en-US-GuyNeural",
        "fallback_edge": "en-US-SteffanNeural",
        "piper_model": "en_US-lessac-medium",
        "piper_url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/en_US-lessac-medium.onnx",
        "title": "The Living Bible",
    },
    "gw": {
        "primary": "en-US-AriaNeural",
        "fallback_edge": "en-US-JennyNeural",
        "piper_model": "en_US-amy-medium",
        "piper_url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/amy/medium/en_US-amy-medium.onnx",
        "title": "GOD'S WORD Translation",
    },
    "voice": {
        "primary": "en-US-EricNeural",
        "fallback_edge": "en-US-ChristopherNeural",
        "piper_model": "en_US-lessac-medium",
        "piper_url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/en_US-lessac-medium.onnx",
        "title": "The Voice",
    },
}

SHORT_BRACKET_RE = re.compile(
    r"\[(?:\d+|[a-zA-Z]{1,2}|lit\.?|or\b[^\]]{0,25}|—|-|\s*verse[s]?\s*\d+[^\]]*omitted[^\]]*)\]",
    re.IGNORECASE,
)


def clean_spoken_text(text: str) -> str:
    """Normalize pure verse text for natural TTS without wiping bracketed Scripture verses."""
    t = text or ""
    t = t.replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'")
    t = t.replace("—", ", ").replace("–", ", ")
    t = re.sub(r"\bLORD\b", "Lord", t)
    t = re.sub(r"\bGOD\b", "God", t)
    t = re.sub(r"\bSelah\.?\b", "", t)
    # Remove only short editorial/footnote/omission bracket markers; unwrap textual brackets around real verses
    t = SHORT_BRACKET_RE.sub("", t)
    t = t.replace("[", "").replace("]", "")
    t = re.sub(r"\s+", " ", t).strip()
    return t


def get_audio_duration_ms(file_path: str) -> int:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        file_path,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return int(round(float(res.stdout.strip()) * 1000))


def verify_audio(file_path: str, word_count: int) -> dict:
    """Strict verification: readable size, duration/WPM plausibility, non-silence, no long dead silence, sha256."""
    if not os.path.exists(file_path):
        raise ValueError("Output file missing")
    size = os.path.getsize(file_path)
    if size < 8000:
        raise ValueError(f"File too small ({size} bytes)")

    dur_ms = get_audio_duration_ms(file_path)
    dur_sec = dur_ms / 1000.0
    if dur_sec <= 1.0:
        raise ValueError(f"Audio duration implausibly short ({dur_sec:.2f}s)")

    wpm = (max(word_count, 1) / dur_sec) * 60.0
    if wpm < 70.0 or wpm > 255.0:
        raise ValueError(f"Implausible speech rate: {wpm:.1f} WPM (duration={dur_sec:.1f}s, words={word_count})")

    cmd = [
        "ffmpeg", "-i", file_path,
        "-af", "volumedetect,silencedetect=noise=-50dB:d=10.0",
        "-f", "null", "/dev/null",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    m = re.search(r"mean_volume:\s*([-\d\.]+)\s*dB", res.stderr)
    if not m:
        raise ValueError("Could not determine mean volume")
    mean_vol = float(m.group(1))
    if mean_vol < -52.0:
        raise ValueError(f"Audio is nearly silent: {mean_vol} dB")

    if "silence_duration:" in res.stderr:
        for sm in re.finditer(r"silence_duration:\s*([\d\.]+)", res.stderr):
            sdur = float(sm.group(1))
            if sdur >= 10.0:
                raise ValueError(f"Detected abnormal dead silence run of {sdur:.1f}s")

    sha = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            sha.update(chunk)

    return {
        "size": size,
        "duration_ms": dur_ms,
        "mean_volume_db": mean_vol,
        "wpm": round(wpm, 1),
        "sha256": sha.hexdigest(),
    }


def ensure_piper_model(cfg: dict) -> str:
    os.makedirs(PIPER_CACHE_DIR, exist_ok=True)
    onnx_path = os.path.join(PIPER_CACHE_DIR, f"{cfg['piper_model']}.onnx")
    json_path = onnx_path + ".json"
    if not os.path.exists(onnx_path) or os.path.getsize(onnx_path) < 1_000_000:
        urllib.request.urlretrieve(cfg["piper_url"], onnx_path)
    if not os.path.exists(json_path) or os.path.getsize(json_path) < 100:
        urllib.request.urlretrieve(cfg["piper_url"] + ".json", json_path)
    return onnx_path


async def synthesize_edge(text: str, voice: str, out_wav: str):
    import edge_tts
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_mp3:
        tmp_mp3_path = tmp_mp3.name
    try:
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(tmp_mp3_path)
        if not os.path.exists(tmp_mp3_path) or os.path.getsize(tmp_mp3_path) < 128:
            raise RuntimeError("edge-tts produced empty mp3")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", tmp_mp3_path, "-ar", "24000", "-ac", "1", out_wav],
            check=True,
        )
    finally:
        if os.path.exists(tmp_mp3_path):
            os.remove(tmp_mp3_path)


def synthesize_piper_sync(text: str, cfg: dict, out_wav: str):
    model_path = ensure_piper_model(cfg)
    raw_wav = out_wav + ".piper.wav"
    try:
        subprocess.run(
            [sys.executable, "-m", "piper", "--model", model_path, "--output_file", raw_wav],
            input=text.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
        )
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", raw_wav, "-ar", "24000", "-ac", "1", out_wav],
            check=True,
        )
    finally:
        if os.path.exists(raw_wav):
            os.remove(raw_wav)


async def synthesize_with_fallback(text: str, cfg: dict, out_wav: str) -> tuple[str, str]:
    """Synthesize text using primary Edge voice -> secondary Edge voice -> offline Piper fallback."""
    if not re.search(r"[A-Za-z0-9]", text):
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", "0.25", out_wav],
            check=True,
        )
        return ("edge-tts", cfg["primary"])

    # 1. Primary Edge voice (3 attempts)
    for attempt in range(3):
        try:
            await synthesize_edge(text, cfg["primary"], out_wav)
            return ("edge-tts", cfg["primary"])
        except Exception:
            await asyncio.sleep(0.8 * (attempt + 1))

    # 2. Secondary Edge voice (2 attempts)
    for attempt in range(2):
        try:
            await synthesize_edge(text, cfg["fallback_edge"], out_wav)
            return ("edge-tts", cfg["fallback_edge"])
        except Exception:
            await asyncio.sleep(1.5 * (attempt + 1))

    # 3. Local deterministic Piper neural TTS fallback
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, synthesize_piper_sync, text, cfg, out_wav)
    return ("piper", cfg["piper_model"])


async def synthesize_chapter(version: str, book_slug: str, chapter: int, cfg: dict, out_m4a: str) -> dict:
    ch_file = os.path.join(STATIC_DIR, version.lower(), book_slug, f"{chapter}.json")
    if not os.path.exists(ch_file):
        raise FileNotFoundError(f"Missing chapter file: {ch_file}")

    with open(ch_file, "r", encoding="utf-8") as f:
        ch_data = json.load(f)

    book_name = ch_data.get("book_name", book_slug.replace("-", " ").title())
    verses = ch_data.get("verses", [])
    if not verses:
        raise ValueError(f"No verses in {ch_file}")

    with tempfile.TemporaryDirectory() as work_dir:
        silence_lead = os.path.join(work_dir, "silence_lead.wav")
        silence_verse = os.path.join(work_dir, "silence_verse.wav")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", "0.7", silence_lead], check=True)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", "0.35", silence_verse], check=True)

        sem = asyncio.Semaphore(5)
        lead_text = f"{book_name}, chapter {chapter}."
        lead_wav = os.path.join(work_dir, "lead.wav")

        async def run_unit(txt: str, path: str):
            async with sem:
                return await synthesize_with_fallback(txt, cfg, path)

        tasks = [run_unit(lead_text, lead_wav)]
        prepared_verses = []
        for idx, v in enumerate(verses):
            v_num = v["verse"]
            clean_t = clean_spoken_text(v.get("text", ""))
            v_wav = os.path.join(work_dir, f"v_{idx:03d}_{v_num}.wav")
            prepared_verses.append((v_num, clean_t, v_wav))
            tasks.append(run_unit(clean_t, v_wav))

        results = await asyncio.gather(*tasks)
        lead_engine, lead_voice = results[0]

        lead_dur = get_audio_duration_ms(lead_wav)
        concat_list = [lead_wav, silence_lead]
        verse_timings = []
        current_time_ms = lead_dur + 700
        total_words = len(lead_text.split())

        engines_used = {lead_engine}
        voices_used = {lead_voice}

        for (v_num, clean_t, v_wav), (eng, vc) in zip(prepared_verses, results[1:]):
            engines_used.add(eng)
            voices_used.add(vc)
            total_words += len(clean_t.split())
            v_dur = get_audio_duration_ms(v_wav)
            verse_timings.append({
                "verse": v_num,
                "start_ms": current_time_ms,
                "end_ms": current_time_ms + v_dur,
            })
            concat_list.append(v_wav)
            concat_list.append(silence_verse)
            current_time_ms += v_dur + 350

        concat_txt = os.path.join(work_dir, "concat.txt")
        with open(concat_txt, "w", encoding="utf-8") as cf:
            for item in concat_list:
                cf.write(f"file '{item}'\n")

        subprocess.run([
            "ffmpeg", "-y", "-v", "error",
            "-f", "concat", "-safe", "0", "-i", concat_txt,
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-c:a", "aac", "-b:a", "48k", "-ac", "1", "-ar", "24000",
            "-movflags", "+faststart",
            out_m4a,
        ], check=True)

        try:
            v_res = verify_audio(out_m4a, total_words)
        except Exception:
            if os.path.exists(out_m4a):
                os.remove(out_m4a)
            raise

        primary_eng = "piper" if "piper" in engines_used else "edge-tts"
        primary_vc = cfg["primary"] if cfg["primary"] in voices_used else sorted(voices_used)[0]

        return {
            "file": os.path.basename(out_m4a),
            "book": book_name,
            "book_slug": book_slug,
            "chapter": chapter,
            "bytes": v_res["size"],
            "sha256": v_res["sha256"],
            "duration_ms": v_res["duration_ms"],
            "engine": primary_eng,
            "voice": primary_vc,
            "word_count": total_words,
            "verses": verse_timings,
        }


def get_release_asset_names(tag: str, repo: str) -> set[str]:
    if not os.environ.get("GH_TOKEN"):
        return set()
    try:
        res = subprocess.run(
            ["gh", "release", "view", tag, "-R", repo, "--json", "assets"],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(res.stdout)
        return {a["name"] for a in data.get("assets", []) if a.get("size", 0) > 8000}
    except Exception:
        return set()


def write_step_summary(v_code: str, done_count: int, total_target: int, total_bytes: int, failed: list[str], mode: str):
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    status_str = "PASS" if not failed else f"FAIL ({len(failed)} failed)"
    mb = total_bytes / (1024 * 1024)
    lines = [
        f"### Audio Pipeline Summary — `{v_code.upper()}` ({mode})",
        "",
        "| Translation | Completed | Target | Size (MB) | Status |",
        "|---|---:|---:|---:|---|",
        f"| `{v_code.upper()}` | {done_count} | {total_target} | {mb:.2f} MB | **{status_str}** |",
        "",
    ]
    if failed:
        lines.append("**Failures:** " + ", ".join(failed[:10]))
    with open(summary_path, "a", encoding="utf-8") as sf:
        sf.write("\n".join(lines) + "\n")


async def main():
    parser = argparse.ArgumentParser(description="VerseWell Audio Generator")
    parser.add_argument("--version", required=True, choices=["kjv", "niv", "tlb", "gw", "voice"])
    parser.add_argument("--pilot", action="store_true", help="Run isolated 2-chapter pilot verification")
    parser.add_argument("--chapters", type=int, default=260, help="Max chapters to process")
    parser.add_argument("--offset", type=int, default=0, help="Chapter offset")
    parser.add_argument("--simulate-failure", action="store_true", help="Deliberately fail verification without publishing")
    args = parser.parse_args()

    start_wall = time.time()
    v_code = args.version.lower()
    cfg = VOICE_CONFIG[v_code]
    tag = f"audio-{v_code}"
    repo = os.environ.get("GITHUB_REPOSITORY", "vjumbo264/versewell")

    all_chapters = [(b, ch) for b, cnt in NT_BOOKS for ch in range(1, cnt + 1)]

    # Deliberate failure test: isolated directory, never touches release assets, exits non-zero
    if args.simulate_failure:
        bad_dir = tempfile.mkdtemp(prefix=f"audio_failtest_{v_code}_")
        bad_path = os.path.join(bad_dir, "matthew-001.m4a")
        with open(bad_path, "wb") as f:
            f.write(b"deliberately_invalid_audio_payload")
        try:
            verify_audio(bad_path, 150)
        except Exception as e:
            if os.path.exists(bad_path):
                os.remove(bad_path)
            write_step_summary(v_code, 0, 1, 0, [f"Deliberate failure caught: {e}"], "failure-test")
            print(f"EXPECTED DELIBERATE FAILURE CAUGHT (nothing published): {e}", file=sys.stderr)
            sys.exit(1)
        sys.exit(1)

    # Pilot mode: isolated directory, tests chapter 1 + Matthew 17 (bracketed-verse regression test) without overwriting production manifest
    if args.pilot:
        pilot_dir = f"/tmp/audio_pilot_{v_code}"
        os.makedirs(pilot_dir, exist_ok=True)
        pilot_targets = [("matthew", 1), ("matthew", 17)] if v_code == "voice" else [("philemon", 1), ("3-john", 1)]
        pilot_bytes = 0
        for b_slug, ch in pilot_targets:
            out_p = os.path.join(pilot_dir, f"{b_slug}-{ch:03d}.m4a")
            entry = await synthesize_chapter(v_code, b_slug, ch, cfg, out_p)
            pilot_bytes += entry["bytes"]
            print(f"PILOT OK {v_code.upper()} {b_slug} {ch}: {entry['duration_ms']/1000:.1f}s, {entry['bytes']}B, engine={entry['engine']}, voice={entry['voice']}")
        write_step_summary(v_code, len(pilot_targets), len(pilot_targets), pilot_bytes, [], "pilot")
        return

    out_dir = f"/tmp/audio_out_{v_code}"
    os.makedirs(out_dir, exist_ok=True)
    manifest_file = os.path.join(out_dir, "manifest.json")
    manifest_data = {"version": v_code.upper(), "voice": cfg["primary"], "chapters": []}

    for p in [f"audio_{v_code}/manifest.json", manifest_file]:
        if os.path.exists(p):
            try:
                loaded = json.load(open(p, encoding="utf-8"))
                if isinstance(loaded.get("chapters"), list):
                    manifest_data["chapters"] = loaded["chapters"]
                    break
            except Exception:
                pass

    # Reconcile manifest against actual GitHub Release assets so missing .m4a files are never skipped
    live_assets = get_release_asset_names(tag, repo)
    if live_assets:
        manifest_data["chapters"] = [
            c for c in manifest_data["chapters"]
            if c.get("file") in live_assets and c.get("bytes", 0) > 8000 and c.get("sha256")
        ]

    by_key = {(c["book_slug"], c["chapter"]): c for c in manifest_data["chapters"] if c.get("book_slug") and c.get("chapter")}
    target_chapters = all_chapters[args.offset : args.offset + args.chapters]
    failures = []
    timed_out_gracefully = False

    print(f"=== Audio Pipeline {v_code.upper()}: {len(by_key)}/260 already verified on release ===")

    for b_slug, ch in target_chapters:
        if (b_slug, ch) in by_key:
            continue
        if time.time() - start_wall >= MAX_JOB_SECONDS:
            print("Reached graceful slice time limit (5h); saving state for chained continuation.")
            timed_out_gracefully = True
            break

        asset_name = f"{b_slug}-{ch:03d}.m4a"
        out_path = os.path.join(out_dir, asset_name)
        print(f"Synthesizing {v_code.upper()} {b_slug} {ch} -> {asset_name}...")
        try:
            entry = await synthesize_chapter(v_code, b_slug, ch, cfg, out_path)
            by_key[(b_slug, ch)] = entry
            # Keep chapters sorted in canonical NT order
            order_idx = {pair: i for i, pair in enumerate(all_chapters)}
            manifest_data["chapters"] = [by_key[k] for k in sorted(by_key.keys(), key=lambda x: order_idx.get(x, 9999))]
            with open(manifest_file, "w", encoding="utf-8") as mf:
                json.dump(manifest_data, mf, indent=2)

            if os.environ.get("GH_TOKEN"):
                subprocess.run(
                    ["gh", "release", "upload", tag, out_path, manifest_file, "--clobber", "-R", repo],
                    check=True,
                )
                # Remove local uploaded .m4a to keep runner disk small and prevent re-uploading already uploaded chapters
                os.remove(out_path)
            print(f"  OK {asset_name}: {entry['duration_ms']/1000:.1f}s ({entry['engine']}/{entry['voice']}) [{len(by_key)}/260]")
        except Exception as e:
            if os.path.exists(out_path):
                os.remove(out_path)
            err_msg = f"{b_slug} {ch}: {e}"
            print(f"  ERROR {err_msg}", file=sys.stderr)
            failures.append(err_msg)

    order_idx = {pair: i for i, pair in enumerate(all_chapters)}
    manifest_data["chapters"] = [by_key[k] for k in sorted(by_key.keys(), key=lambda x: order_idx.get(x, 9999))]
    manifest_data["total_chapters"] = len(manifest_data["chapters"])
    manifest_data["total_bytes"] = sum(c.get("bytes", 0) for c in manifest_data["chapters"])
    with open(manifest_file, "w", encoding="utf-8") as mf:
        json.dump(manifest_data, mf, indent=2)
    if os.environ.get("GH_TOKEN") and os.path.exists(manifest_file):
        subprocess.run(["gh", "release", "upload", tag, manifest_file, "--clobber", "-R", repo], check=False)

    write_step_summary(v_code, len(manifest_data["chapters"]), len(target_chapters), manifest_data["total_bytes"], failures, "production")

    if failures:
        print(f"FATAL: {len(failures)} chapter(s) failed verification in {v_code.upper()}. Failing workflow.", file=sys.stderr)
        sys.exit(1)
    if not timed_out_gracefully:
        missing_in_slice = [f"{b}-{c}" for (b, c) in target_chapters if (b, c) not in by_key]
        if missing_in_slice:
            print(f"FATAL: Missing chapters in target slice: {missing_in_slice[:10]}", file=sys.stderr)
            sys.exit(1)

    print(f"Run complete for {v_code.upper()}: {len(manifest_data['chapters'])}/260 verified.")


if __name__ == "__main__":
    asyncio.run(main())
