#!/usr/bin/env python3
"""
VerseWell chapter-audio generator (48k m4a, parallel, incremental-commit).

Feature contract (see AUDIO_STATE.json for the closed allowlist and the
per-version voice assignment table):

  * Only versions listed in AUDIO_STATE.json.audio_enabled_versions ever get
    audio. Anything else is silently a no-op (safety rail: a new version
    dropped into /bible-sources/ will NEVER auto-inherit narration).
  * For each in-scope chapter, synthesize the plain verse text through
    Microsoft Edge TTS (edge-tts, no API key), then normalize + master with
    ClipForge's calibrated speech_clarity_v1 chain (highpass 70 Hz, EQ +1.5 dB
    @ 3 kHz, acompressor 1.5:1, two-pass EBU R128 loudnorm targeting
    -16 LUFS / 7 LU / -1.5 dBTP, alimiter 0.84). Final format: M4A (AAC)
    48 kbps mono 24 kHz — ~1.9 MB per average chapter, ~4.5 GB for the full
    13.5k-chapter allowlist (vs ~40+ GB for WAV-era estimates).
  * Output: site/static-data/{version_lower}/{book_slug}/{chapter}.m4a,
    COMMITTED TO GIT INCREMENTALLY every --commit-interval successes
    (default 100). Every push auto-triggers deploy.yml, so audio goes live
    as it renders — never all at once at the end of a 6h job, and a crash
    loses at most one small batch (the previous revision died at ~5h50m
    with NOTHING committed).
  * Idempotent resume: any chapter whose committed .m4a exists is skipped,
    so re-runs pick up exactly where the last slice stopped. --dry-run
    prints remaining=N for the workflow's planning steps.
  * Parallel: --jobs N worker threads synthesize concurrently (Edge TTS is
    remote; throughput scales with workers until rate limits).
  * Soft stop: when --stop-file PATH appears (workflow watchdog touches it
    at the 5h00m mark), no new chapters are started; in-flight work
    finishes, the last partial batch is committed + pushed, and the script
    exits 0 so the workflow can refresh JSON and re-dispatch itself BEFORE
    the 6h Actions hard kill.
  * --finalize: refresh every audio_url field (via generate_static.py).
    The workflow then git-rms this script + the workflow file so no audio
    GENERATION artifacts remain on main.

Ported from ClipForge pipeline/stage_b/voiceover.py — the reference
implementation. Overrides for VerseWell:

  * rate = +0% (calm long-form Scripture reading pace; ClipForge's +20% is
    tuned for brisk short-video narration and is NOT appropriate here).
  * Final container = M4A/AAC 48 kbps (git storage; ClipForge emits WAV for
    its own video pipeline). AAC 48k is transparent for the mastered speech
    signal and plays natively in every browser's <audio> element.
  * Voice per version comes from AUDIO_STATE.json (not from a global
    Settings file), so each version gets a distinct, alternating-gender
    narrator.

Usage:
  python3 scripts/generate_audio.py                     # render all missing
  python3 scripts/generate_audio.py --versions KJV NLT  # subset
  python3 scripts/generate_audio.py --max-chapters 500  # partial run
  python3 scripts/generate_audio.py --jobs 6            # parallelism
  python3 scripts/generate_audio.py --stop-file /tmp/softstop
  python3 scripts/generate_audio.py --dry-run           # prints remaining=N
  python3 scripts/generate_audio.py --finalize          # refresh audio_url JSON

The static-mirror JSON must exist first (the narration text source), and
generate_static.py decides each chapter's audio_url from the same sibling
.m4a presence this script writes — the two never disagree.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATE_PATH = ROOT / "AUDIO_STATE.json"
STATIC_DIR = ROOT / "site" / "static-data"

# ---------------------------------------------------------------------------
# Speech-clarity mastering chain — LIFTED VERBATIM from ClipForge
# pipeline/stage_b/voiceover.py (VOICE_CLARITY_* constants). Do not deviate;
# the whole point is narration consistency with ClipForge's calibrated output.
# ---------------------------------------------------------------------------
VOICE_CLARITY_PRESET_NAME = "speech_clarity_v1"
VOICE_CLARITY_TARGET_I_LUFS = -16.0
VOICE_CLARITY_TARGET_LRA_LU = 7.0
VOICE_CLARITY_TARGET_TP_DBTP = -1.5
VOICE_CLARITY_PRE_FILTERS = (
    "highpass=f=70:p=2,"
    "equalizer=f=3000:t=q:w=1.1:g=1.5,"
    "acompressor=threshold=0.125:ratio=1.5:attack=15:release=120:makeup=1.0"
)
VOICE_CLARITY_FINAL_LIMITER = "alimiter=limit=0.84:attack=5:release=50:level=0"

# Internal WAV contract, as ClipForge (before final AAC encode).
SAMPLE_RATE_HZ = 24000
CHANNELS = 1

# Edge-TTS retry contract — ClipForge values.
EDGE_TTS_MAX_ATTEMPTS = 3
EDGE_TTS_BACKOFF_S = 2.0

# VerseWell narration overrides (see AUDIO_STATE.json).
TTS_RATE = "+0%"
TTS_VOLUME = "+0%"
TTS_PITCH = "+0Hz"

# Final storage format: M4A (AAC) 48 kbps mono 24 kHz. AAC at 48 kbps is
# transparent for the loudness-normalized speech_clarity_v1 master (speech
# sits well under the ~10 kHz bandwidth 24 kHz preserves) and the full
# 13,528-chapter allowlist lands at ~4.5 GB — inside GitHub's 5 GB soft cap
# and Cloudflare Pages' 25 MiB/file, 20k-files-per-deploy limits (audio adds
# ~13.5k files total, deployed incrementally). M4A/AAC plays natively in
# every browser's <audio> element (Safari included, unlike opus).
M4A_BITRATE = "48k"

# Commit + push generated audio after this many successes (incremental
# publish — each push auto-deploys the site, so audio appears as it renders).
DEFAULT_COMMIT_INTERVAL = 100
DEFAULT_JOBS = 4


# ---------------------------------------------------------------------------
# Small utils
# ---------------------------------------------------------------------------
def _run(cmd, description):
    """Wrap subprocess.run so failures surface a readable message."""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        stderr_tail = "\n".join((proc.stderr or "").splitlines()[-25:])
        raise RuntimeError(f"{description} failed (rc={proc.returncode}):\n{stderr_tail}")
    return proc


def _require_ffmpeg():
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is required but not on PATH.")


def _git(*args, check=True):
    return subprocess.run(["git", *args], cwd=ROOT, check=check,
                          capture_output=True, text=True)


def _can_git():
    return shutil.which("git") is not None and (ROOT / ".git").is_dir()


def publish_batch(new_rels, reason):
    """Stage the new .m4a files, refresh audio_url JSON, commit + push.

    Called from the main thread only (between drain/refill cycles), so no
    locking is needed against the render workers.
    """
    if not new_rels:
        return
    print(f"\n>> publish ({reason}): +{len(new_rels)} file(s)", flush=True)

    # Refresh audio_url fields FIRST so the same commit that adds the bytes
    # also exposes them (generate_static.py keys off sibling .m4a presence).
    _run([sys.executable, str(HERE / "generate_static.py")],
         "audio_url refresh (generate_static.py)")

    if not _can_git() or os.environ.get("AUDIO_NO_GIT") == "1":
        print(">> no git (local dry run) — skipping commit/push", flush=True)
        return

    _git("add", "site/static-data")
    if _git("diff", "--cached", "--quiet", check=False).returncode == 0:
        print(">> nothing staged (already committed)", flush=True)
        return
    _git("commit", "-m",
         f"audio: +{len(new_rels)} chapter m4a(s) rendered ({reason})")
    # Rebase-tolerant push: a concurrent deploy/other commit may have landed.
    _git("pull", "--rebase", "origin", "main", check=False)
    push = _git("push", "origin", "HEAD:main", check=False)
    if push.returncode != 0:
        # Retry once after a fresh rebase — do NOT lose the commit.
        _git("pull", "--rebase", "origin", "main", check=False)
        push = _git("push", "origin", "HEAD:main", check=False)
    if push.returncode != 0:
        raise RuntimeError(f"git push failed: {push.stderr[-300:]}")
    print(">> committed + pushed (deploy.yml will pick it up)", flush=True)


# ---------------------------------------------------------------------------
# Edge TTS synthesis (ClipForge contract)
# ---------------------------------------------------------------------------
async def _save_edge_mp3(text, voice, destination):
    import edge_tts  # delayed import — only needed when synthesizing

    communicator = edge_tts.Communicate(
        text=text,
        voice=voice,
        rate=TTS_RATE,
        volume=TTS_VOLUME,
        pitch=TTS_PITCH,
    )
    await communicator.save(str(destination))


def _synthesize_wav(text, voice, wav_path):
    """Edge TTS -> raw MP3 -> normalized 24kHz mono s16le WAV.

    Mirrors ClipForge's synthesize_edge_tts_to_wav: same 3-attempt retry
    with linear backoff, same ffmpeg WAV normalization command shape.
    """
    if not text.strip():
        raise RuntimeError("empty narration text")
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    last_error = None
    with tempfile.TemporaryDirectory(prefix="versewell_edge_tts_") as tmp:
        mp3_path = Path(tmp) / "voice.mp3"
        for attempt in range(1, EDGE_TTS_MAX_ATTEMPTS + 1):
            try:
                if mp3_path.exists():
                    mp3_path.unlink()
                asyncio.run(_save_edge_mp3(text, voice, mp3_path))
                if not mp3_path.is_file() or mp3_path.stat().st_size == 0:
                    raise RuntimeError("Edge TTS returned empty audio")
                _run(
                    [
                        "ffmpeg", "-hide_banner", "-nostdin", "-y",
                        "-i", str(mp3_path),
                        "-ar", str(SAMPLE_RATE_HZ), "-ac", str(CHANNELS),
                        "-c:a", "pcm_s16le", str(wav_path),
                    ],
                    "Edge TTS WAV normalization",
                )
                if not wav_path.is_file() or wav_path.stat().st_size <= 44:
                    raise RuntimeError("WAV normalize produced no audio")
                return
            except Exception as e:
                last_error = e
                if attempt < EDGE_TTS_MAX_ATTEMPTS:
                    delay = EDGE_TTS_BACKOFF_S * attempt
                    print(
                        f"    edge-tts attempt {attempt}/{EDGE_TTS_MAX_ATTEMPTS} "
                        f"failed ({voice}); retrying in {delay:.0f}s",
                        flush=True,
                    )
                    time.sleep(delay)
        raise RuntimeError(
            f"Edge TTS failed after {EDGE_TTS_MAX_ATTEMPTS} attempts for {voice}: {last_error}"
        )


def _measure_loudness(input_wav):
    """First pass of ClipForge's two-pass loudnorm — measure the raw take."""
    filter_chain = (
        f"{VOICE_CLARITY_PRE_FILTERS},"
        f"loudnorm=I={VOICE_CLARITY_TARGET_I_LUFS}:"
        f"LRA={VOICE_CLARITY_TARGET_LRA_LU}:"
        f"TP={VOICE_CLARITY_TARGET_TP_DBTP}:print_format=json"
    )
    proc = _run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(input_wav),
         "-af", filter_chain, "-f", "null", "-"],
        "voiceover loudness measurement",
    )
    matches = re.findall(r'\{\s*"input_i".*?\n\}', proc.stderr, flags=re.DOTALL)
    if not matches:
        raise RuntimeError("loudnorm did not return parseable JSON")
    measured = json.loads(matches[-1])
    return {
        "measured_I": float(measured["input_i"]),
        "measured_LRA": float(measured["input_lra"]),
        "measured_TP": float(measured["input_tp"]),
        "measured_thresh": float(measured["input_thresh"]),
        "offset": float(measured["target_offset"]),
    }


def _master_and_encode_m4a(raw_wav, out_m4a):
    """Second loudnorm pass + limiter, encoded straight to final M4A/AAC."""
    m = _measure_loudness(raw_wav)
    loudnorm = (
        f"loudnorm=I={VOICE_CLARITY_TARGET_I_LUFS}:"
        f"LRA={VOICE_CLARITY_TARGET_LRA_LU}:"
        f"TP={VOICE_CLARITY_TARGET_TP_DBTP}:"
        f"measured_I={m['measured_I']}:"
        f"measured_LRA={m['measured_LRA']}:"
        f"measured_TP={m['measured_TP']}:"
        f"measured_thresh={m['measured_thresh']}:"
        f"offset={m['offset']}:linear=true:print_format=summary"
    )
    filter_chain = f"{VOICE_CLARITY_PRE_FILTERS},{loudnorm},{VOICE_CLARITY_FINAL_LIMITER}"
    out_m4a.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_m4a.with_suffix(".tmp.m4a")
    try:
        _run(
            [
                "ffmpeg", "-hide_banner", "-nostdin", "-y",
                "-i", str(raw_wav),
                "-af", filter_chain,
                "-ar", str(SAMPLE_RATE_HZ), "-ac", str(CHANNELS),
                "-c:a", "aac", "-b:a", M4A_BITRATE,
                "-movflags", "+faststart",
                str(tmp),
            ],
            "voiceover master + m4a encode",
        )
        if not tmp.is_file() or tmp.stat().st_size == 0:
            raise RuntimeError("m4a encode produced no audio")
        tmp.replace(out_m4a)
    finally:
        if tmp.exists():
            tmp.unlink()


# ---------------------------------------------------------------------------
# Verse-text -> narration text
# ---------------------------------------------------------------------------
def _chapter_narration(chapter_json):
    """Spoken narration string: plain verse text only (no footnotes, no
    intros, no verse numbers — jarring for long-form Scripture listening)."""
    verses = chapter_json.get("verses") or []
    parts = []
    for v in verses:
        text = (v.get("text") or "").strip()
        if not text:
            continue
        if text[-1] not in ".!?…\";":
            text = text + "."
        parts.append(text)
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------
def _load_state():
    with open(STATE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _voice_for(state, version_code):
    for row in state["voice_assignment"]["table"]:
        if row["version"] == version_code:
            return row["voice"]
    return None


def plan_chapters(state, requested_versions):
    """All in-scope chapters as dicts, in deterministic render order."""
    allowlist = set(state["audio_enabled_versions"])
    if requested_versions:
        planned = [v.upper() for v in requested_versions]
        for v in planned:
            if v not in allowlist:
                print(f"skip {v}: not on audio_enabled_versions allowlist", flush=True)
        planned = [v for v in planned if v in allowlist]
    else:
        planned = sorted(allowlist)

    chapters = []
    for version in planned:
        voice = _voice_for(state, version)
        if not voice:
            print(f"skip {version}: no voice assignment", flush=True)
            continue
        vdir = STATIC_DIR / version.lower()
        idx_path = vdir / "index.json"
        if not idx_path.is_file():
            continue
        with open(idx_path, encoding="utf-8") as fh:
            idx = json.load(fh)
        for b in idx.get("books", []):
            slug = b["slug"]
            book_dir = vdir / slug
            if not book_dir.is_dir():
                continue
            for entry in sorted(book_dir.iterdir()):
                if entry.suffix != ".json":
                    continue
                try:
                    ch = int(entry.stem)
                except ValueError:
                    continue
                chapters.append({
                    "version": version,
                    "voice": voice,
                    "slug": slug,
                    "chapter": ch,
                    "json_path": entry,
                    "out_m4a": entry.parent / (entry.stem + ".m4a"),
                    "rel": f"{version.lower()}/{slug}/{ch}.m4a",
                    "label": f"{version}/{slug}/{ch}",
                })
    return chapters


# ---------------------------------------------------------------------------
# Per-chapter render (thread worker)
# ---------------------------------------------------------------------------
def render_chapter(item):
    """Full pipeline for one chapter -> committed-tree .m4a. Returns rel."""
    out_m4a = item["out_m4a"]
    if out_m4a.exists() and out_m4a.stat().st_size > 0:
        return item["rel"]  # already rendered (idempotent)
    with open(item["json_path"], encoding="utf-8") as fh:
        data = json.load(fh)
    narration = _chapter_narration(data)
    if not narration.strip():
        raise RuntimeError("empty narration text")
    with tempfile.TemporaryDirectory(prefix="versewell_ch_") as tmp:
        raw_wav = Path(tmp) / "raw.wav"
        _synthesize_wav(narration, item["voice"], raw_wav)
        _master_and_encode_m4a(raw_wav, out_m4a)
    return item["rel"]


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_dry_run(args, state):
    chapters = plan_chapters(state, args.versions)
    remaining = [
        c for c in chapters
        if args.force or not (c["out_m4a"].exists() and c["out_m4a"].stat().st_size > 0)
    ]
    done = len(chapters) - len(remaining)
    print(f"total_in_scope={len(chapters)} done={done}", flush=True)
    print(f"remaining={len(remaining)}", flush=True)
    if args.verbose:
        for c in remaining[:50]:
            print(f"  TODO {c['label']}", flush=True)
    return 0


def cmd_generate(args, state):
    _require_ffmpeg()
    chapters = plan_chapters(state, args.versions)
    todo = [
        c for c in chapters
        if args.force or not (c["out_m4a"].exists() and c["out_m4a"].stat().st_size > 0)
    ]
    if args.max_chapters:
        todo = todo[: args.max_chapters]
    print(
        f"plan: {len(todo)} chapter(s) to render this slice, "
        f"jobs={args.jobs}, commit_interval={args.commit_interval}",
        flush=True,
    )
    if not todo:
        print("generated=0 failed=0", flush=True)
        return 0

    stop_file = Path(args.stop_file) if args.stop_file else None

    def stop_requested():
        return bool(stop_file and stop_file.exists())

    generated = 0
    failed = 0
    batch = []
    pending = {}
    t_start = time.time()

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        idx = 0
        while idx < len(todo) and len(pending) < args.jobs and not stop_requested():
            pending[pool.submit(render_chapter, todo[idx])] = todo[idx]
            idx += 1
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                item = pending.pop(fut)
                try:
                    rel = fut.result()
                    generated += 1
                    batch.append(rel)
                    if generated % 10 == 0:
                        rate = generated / max(time.time() - t_start, 1) * 60
                        print(
                            f"  [{generated}/{len(todo)}] {item['label']} ok "
                            f"({rate:.1f} ch/min, {failed} failed)",
                            flush=True,
                        )
                except Exception as e:
                    failed += 1
                    print(f"  FAILED {item['label']}: {e}", flush=True)
            # Publish as we go — outside the per-future loop so the JSON
            # refresh + git ops happen once per batch, on the main thread.
            if len(batch) >= args.commit_interval:
                publish_batch(batch, reason=f"{generated} rendered")
                batch = []
            while idx < len(todo) and len(pending) < args.jobs and not stop_requested():
                pending[pool.submit(render_chapter, todo[idx])] = todo[idx]
                idx += 1
            if stop_requested() and idx < len(todo):
                if not getattr(cmd_generate, "_stop_logged", False):
                    print(
                        "\n>> soft-stop file detected — finishing in-flight "
                        "chapters, publishing, then exiting so the workflow "
                        "can commit + re-dispatch before the 6h hard kill",
                        flush=True,
                    )
                    cmd_generate._stop_logged = True
                idx = len(todo)  # submit nothing more; drain remaining futures

    if batch:
        publish_batch(batch, reason="slice end")
    mins = (time.time() - t_start) / 60
    print(
        f"\nDone. generated={generated} failed={failed} in {mins:.1f} min "
        f"(stop={'yes' if stop_requested() else 'no'})",
        flush=True,
    )
    return 0


def cmd_finalize(args, state):
    """Render complete: refresh audio_url fields one last time."""
    _run([sys.executable, str(HERE / "generate_static.py")],
         "final generate_static.py audio_url refresh")
    print("finalized: audio_url fields refreshed", flush=True)
    return 0


def main():
    ap = argparse.ArgumentParser(description="Generate VerseWell chapter audio.")
    ap.add_argument("--versions", nargs="*", default=None,
                    help="Subset of allowlisted versions (default: all).")
    ap.add_argument("--max-chapters", type=int, default=None,
                    help="Stop after N newly-generated chapters this run.")
    ap.add_argument("--jobs", type=int, default=DEFAULT_JOBS,
                    help=f"Parallel synthesis workers (default {DEFAULT_JOBS}).")
    ap.add_argument("--commit-interval", type=int, default=DEFAULT_COMMIT_INTERVAL,
                    help=f"Commit + push every N successes (default "
                         f"{DEFAULT_COMMIT_INTERVAL}; each push auto-deploys).")
    ap.add_argument("--stop-file", default=None,
                    help="When this file appears, stop starting new chapters, "
                         "publish, and exit 0 (workflow soft-stop).")
    ap.add_argument("--force", action="store_true",
                    help="Regenerate even chapters whose .m4a already exists.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Only report what would be generated (prints remaining=N).")
    ap.add_argument("--finalize", action="store_true",
                    help="Refresh audio_url fields, then exit.")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    state = _load_state()

    if args.finalize:
        return cmd_finalize(args, state)
    if args.dry_run:
        return cmd_dry_run(args, state)
    return cmd_generate(args, state)


if __name__ == "__main__":
    sys.exit(main())
