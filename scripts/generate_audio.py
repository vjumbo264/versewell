#!/usr/bin/env python3
"""
VerseWell chapter-audio generator.

Feature contract (see AUDIO_STATE.json for the closed allowlist and the
per-version voice assignment table):

  * Only versions listed in AUDIO_STATE.json.audio_enabled_versions ever get
    audio. Anything else is silently a no-op (safety rail: a new version
    dropped into /bible-sources/ will NEVER auto-inherit narration).
  * For each in-scope chapter, synthesize the plain verse text through
    Microsoft Edge TTS (edge-tts, no API key), then normalize + master with
    ClipForge's calibrated speech_clarity_v1 chain (highpass 70 Hz, EQ +1.5 dB
    @ 3 kHz, acompressor 1.5:1, two-pass EBU R128 loudnorm targeting
    -16 LUFS / 7 LU / -1.5 dBTP, alimiter 0.84). Final format MP3 24 kbps
    mono 24 kHz.
  * Output: site/static-data/{version_lower}/{book_slug}/{chapter}.mp3
    (colocated with the existing per-chapter JSON files). The static mirror
    generator adds an `audio_url` field to each chapter JSON pointing to the
    same-directory MP3 (or null).
  * Idempotent: --skip-existing (default) means already-committed MP3s are
    NOT re-synthesized, so a workflow re-run resumes an interrupted job
    exactly where it stopped, and re-running to completion is a no-op.

Ported from ClipForge pipeline/stage_b/voiceover.py — the reference
implementation. Overrides for VerseWell:

  * rate = +0% (calm long-form Scripture reading pace; ClipForge's +20% is
    tuned for brisk short-video narration and is NOT appropriate here).
  * Final container = MP3 24 kbps (repo storage; ClipForge emits WAV).
  * Voice per version comes from AUDIO_STATE.json (not from a global
    Settings file), so each version gets a distinct, alternating-gender
    narrator.

Usage:
  python3 scripts/generate_audio.py                     # all in-scope
  python3 scripts/generate_audio.py --versions KJV NLT  # subset
  python3 scripts/generate_audio.py --max-chapters 500  # partial run
  python3 scripts/generate_audio.py --dry-run           # planning only

The static-mirror JSON must have been generated first (deploy.yml runs
generate_static.py before this) so verse text and per-book slugs exist.
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

# Internal WAV contract, as ClipForge (before final MP3 encode).
SAMPLE_RATE_HZ = 24000
CHANNELS = 1

# Edge-TTS retry contract — ClipForge values.
EDGE_TTS_MAX_ATTEMPTS = 3
EDGE_TTS_BACKOFF_S = 2.0

# VerseWell narration overrides (see AUDIO_STATE.json).
TTS_RATE = "+0%"
TTS_VOLUME = "+0%"
TTS_PITCH = "+0Hz"

# Final storage format. 24 kbps mono 24 kHz MP3 is a speech-only encode:
# spoken voice sits well under the ~10 kHz bandwidth 24 kHz preserves, and the
# speech_clarity_v1 master (highpass 70 Hz + presence EQ + comp + loudnorm +
# limiter) keeps the signal dense, so 24 kbps stays intelligible while cutting
# repo/Pages storage ~4x vs 96 kbps. Full-allowlist estimate drops from
# ~40 GB to ~10 GB; see AUDIO_STATE.json.
MP3_BITRATE = "24k"


# ---------------------------------------------------------------------------
# ffmpeg helpers
# ---------------------------------------------------------------------------
def _run(cmd, description):
    """Wrap subprocess.run so ffmpeg failures surface a readable message."""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        # ffmpeg writes progress + errors to stderr; keep the tail only.
        stderr_tail = "\n".join(proc.stderr.splitlines()[-25:])
        raise RuntimeError(f"{description} failed (rc={proc.returncode}):\n{stderr_tail}")
    return proc


def _require_ffmpeg():
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is required but not on PATH.")


async def _save_edge_mp3(text, voice, destination):
    # Delayed import — only needed when actually synthesizing.
    import edge_tts

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
    matches = re.findall(r"\{\s*\"input_i\".*?\n\}", proc.stderr, flags=re.DOTALL)
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


def _master_and_encode_mp3(raw_wav, out_mp3):
    """Second loudnorm pass + limiter, encoded straight to final MP3."""
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
    out_mp3.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_mp3.with_suffix(".tmp.mp3")
    try:
        _run(
            [
                "ffmpeg", "-hide_banner", "-nostdin", "-y",
                "-i", str(raw_wav),
                "-af", filter_chain,
                "-ar", str(SAMPLE_RATE_HZ), "-ac", str(CHANNELS),
                "-c:a", "libmp3lame", "-b:a", MP3_BITRATE,
                str(tmp),
            ],
            "voiceover master + mp3 encode",
        )
        if not tmp.is_file() or tmp.stat().st_size == 0:
            raise RuntimeError("mp3 encode produced no audio")
        tmp.replace(out_mp3)
    finally:
        if tmp.exists():
            tmp.unlink()


# ---------------------------------------------------------------------------
# Verse-text -> narration text
# ---------------------------------------------------------------------------
_VERSE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _chapter_narration(chapter_json):
    """Build the spoken narration string for a chapter.

    We speak the plain verse text only (no footnote markers, no intros —
    intros are optional prose that would make chapters much longer and
    inconsistent across versions). Verse numbers are NOT spoken because
    that makes for a jarring listening experience for Scripture.
    """
    verses = chapter_json.get("verses") or []
    parts = []
    for v in verses:
        text = (v.get("text") or "").strip()
        if not text:
            continue
        # Ensure a sentence-ending pause between verses even if the source
        # verse text doesn't end with punctuation (Edge TTS uses punctuation
        # for prosody).
        if text[-1] not in ".!?…\":;":
            text = text + "."
        parts.append(text)
    return " ".join(parts)


# ---------------------------------------------------------------------------
# State + plan
# ---------------------------------------------------------------------------
def _load_state():
    with open(STATE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _iter_chapters(version_code):
    """Yield (book_slug, chapter_int, chapter_json_path) for every chapter
    JSON of `version_code` under site/static-data/{lower}/."""
    vdir = STATIC_DIR / version_code.lower()
    idx_path = vdir / "index.json"
    if not idx_path.is_file():
        return
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
            yield slug, ch, entry


def _voice_for(state, version_code):
    for row in state["voice_assignment"]["table"]:
        if row["version"] == version_code:
            return row["voice"]
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Generate VerseWell chapter audio.")
    ap.add_argument("--versions", nargs="*", default=None,
                    help="Subset of allowlisted versions (default: all).")
    ap.add_argument("--max-chapters", type=int, default=None,
                    help="Stop after N newly-generated chapters this run.")
    ap.add_argument("--skip-existing", action="store_true", default=True,
                    help="Skip chapters whose target MP3 already exists (default on).")
    ap.add_argument("--force", action="store_true",
                    help="Regenerate even if the target MP3 exists (overrides --skip-existing).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Only report what would be generated.")
    args = ap.parse_args()

    state = _load_state()
    allowlist = set(state["audio_enabled_versions"])
    if args.versions:
        requested = [v.upper() for v in args.versions]
        # Enforce the closed allowlist rail — silently drop anything not on it.
        planned = [v for v in requested if v in allowlist]
        rejected = [v for v in requested if v not in allowlist]
        for v in rejected:
            print(f"skip {v}: not on audio_enabled_versions allowlist", flush=True)
    else:
        planned = sorted(allowlist)

    _require_ffmpeg()

    total_generated = 0
    total_skipped = 0
    for version in planned:
        voice = _voice_for(state, version)
        if not voice:
            print(f"skip {version}: no voice assignment", flush=True)
            continue
        print(f"\n== {version}  voice={voice} rate={TTS_RATE} ==", flush=True)
        for slug, ch, ch_json in _iter_chapters(version):
            out_mp3 = ch_json.parent / (ch_json.stem + ".mp3")
            if out_mp3.exists() and not args.force:
                total_skipped += 1
                continue
            with open(ch_json, encoding="utf-8") as fh:
                data = json.load(fh)
            narration = _chapter_narration(data)
            if not narration.strip():
                # Empty chapter (shouldn't happen) — nothing to synthesize.
                continue
            label = f"{version}/{slug}/{ch}"
            if args.dry_run:
                print(f"  DRY  {label}  ({len(narration)} chars)", flush=True)
                total_generated += 1
                if args.max_chapters and total_generated >= args.max_chapters:
                    print(f"\n(dry-run) reached --max-chapters={args.max_chapters}", flush=True)
                    return 0
                continue
            print(f"  synth {label}  ({len(narration)} chars)", flush=True)
            with tempfile.TemporaryDirectory(prefix="versewell_ch_") as tmp:
                raw_wav = Path(tmp) / "raw.wav"
                try:
                    _synthesize_wav(narration, voice, raw_wav)
                    _master_and_encode_mp3(raw_wav, out_mp3)
                except Exception as e:
                    print(f"    FAILED {label}: {e}", flush=True)
                    if out_mp3.exists():
                        out_mp3.unlink()
                    continue
            total_generated += 1
            if args.max_chapters and total_generated >= args.max_chapters:
                print(f"\nreached --max-chapters={args.max_chapters}", flush=True)
                break
        if args.max_chapters and total_generated >= args.max_chapters:
            break

    print(f"\nDone. generated={total_generated} skipped_existing={total_skipped}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
