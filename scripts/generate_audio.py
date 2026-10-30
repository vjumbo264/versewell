#!/usr/bin/env python3
"""
VerseWell Audio Generation Pipeline (Section 6 implementation)
Synthesizes clean New Testament audio for 5 translations: KJV, NIV, TLB, GW, VOICE.

- Reads static chapter JSON files
- Extracts clean verse text only (no intros, headings, footnotes, or numbers)
- Synthesizes per verse with Edge-TTS, computes exact timings
- Assembles chapter audio with silence pauses, normalizes to -16 LUFS AAC in .m4a 48kbps
- Verifies duration, word count, non-silence, and file integrity
- Emits manifest.json and uploads assets to GitHub Releases
"""

import os
import sys
import json
import asyncio
import subprocess
import argparse
import hashlib
import tempfile
import re

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
STATIC_DIR = os.path.join(ROOT, "site", "static-data")

NT_BOOKS = [
    ("matthew", 28), ("mark", 16), ("luke", 24), ("john", 21),
    ("acts", 28), ("romans", 16), ("1-corinthians", 16), ("2-corinthians", 13),
    ("galatians", 6), ("ephesians", 6), ("philippians", 4), ("colossians", 4),
    ("1-thessalonians", 5), ("2-thessalonians", 3), ("1-timothy", 6), ("2-timothy", 4),
    ("titus", 3), ("philemon", 1), ("hebrews", 13), ("james", 5),
    ("1-peter", 5), ("2-peter", 3), ("1-john", 5), ("2-john", 1),
    ("3-john", 1), ("jude", 1), ("revelation", 22)
]

VOICE_CONFIG = {
    "kjv": {
        "primary": "en-US-ChristopherNeural",
        "fallback": "en-US-EricNeural",
        "title": "King James Version"
    },
    "niv": {
        "primary": "en-US-JennyNeural",
        "fallback": "en-US-AriaNeural",
        "title": "New International Version"
    },
    "tlb": {
        "primary": "en-US-GuyNeural",
        "fallback": "en-US-ChristopherNeural",
        "title": "The Living Bible"
    },
    "gw": {
        "primary": "en-US-AriaNeural",
        "fallback": "en-US-JennyNeural",
        "title": "GOD'S WORD Translation"
    },
    "voice": {
        "primary": "en-US-EricNeural",
        "fallback": "en-US-GuyNeural",
        "title": "The Voice"
    }
}

def clean_spoken_text(text: str) -> str:
    """Normalize text for natural TTS reading."""
    t = text
    # Smart quotes and dashes
    t = t.replace("“", "\"").replace("”", "\"").replace("’", "'").replace("‘", "'")
    t = t.replace("—", ", ").replace("–", ", ")
    # Normalize LORD / GOD in all caps to Lord / God for natural speech cadence
    t = re.sub(r"\bLORD\b", "Lord", t)
    t = re.sub(r"\bGOD\b", "God", t)
    # Remove bracketed editorial annotations [like this]
    t = re.sub(r"\[.*?\]", "", t)
    # Remove extra whitespace
    t = re.sub(r"\s+", " ", t).strip()
    return t

def get_audio_duration_ms(file_path: str) -> int:
    """Get audio duration in milliseconds using ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        file_path
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return int(float(res.stdout.strip()) * 1000)

def verify_audio(file_path: str, word_count: int) -> dict:
    """Rigorous verification: file size, duration vs words, mean volume."""
    size = os.path.getsize(file_path)
    if size < 10000:
        raise ValueError(f"File too small ({size} bytes)")
    
    dur_ms = get_audio_duration_ms(file_path)
    dur_sec = dur_ms / 1000.0
    if dur_sec <= 0:
        raise ValueError("Audio duration is zero")
    
    # Words per minute
    wpm = (word_count / dur_sec) * 60
    if wpm < 80 or wpm > 250:
        raise ValueError(f"Implausible speech rate: {wpm:.1f} WPM (duration={dur_sec:.1f}s, words={word_count})")
    
    # Volume check (mean volume must not be dead silence)
    cmd = [
        "ffmpeg", "-i", file_path,
        "-af", "volumedetect",
        "-f", "null", "/dev/null"
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    m = re.search(r"mean_volume:\s*([-\d\.]+)\s*dB", res.stderr)
    if not m:
        raise ValueError("Could not determine mean volume")
    mean_vol = float(m.group(1))
    if mean_vol < -55.0:
        raise ValueError(f"Audio is nearly silent: {mean_vol} dB")
    
    # SHA-256
    sha = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            sha.update(chunk)
            
    return {
        "size": size,
        "duration_ms": dur_ms,
        "mean_volume_db": mean_vol,
        "wpm": round(wpm, 1),
        "sha256": sha.hexdigest()
    }

async def synthesize_chunk(text: str, voice: str, out_wav: str):
    """Synthesize a single text chunk with edge-tts and convert to WAV."""
    import edge_tts
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_mp3:
        tmp_mp3_path = tmp_mp3.name
    try:
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(tmp_mp3_path)
        # Convert to 24kHz mono PCM WAV
        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-i", tmp_mp3_path,
            "-ar", "24000", "-ac", "1",
            out_wav
        ]
        subprocess.run(cmd, check=True)
    finally:
        if os.path.exists(tmp_mp3_path):
            os.remove(tmp_mp3_path)

async def synthesize_chapter(version: str, book_slug: str, chapter: int, voice: str, out_m4a: str):
    """Synthesizes all verses in a chapter, concatenates with pauses, and encodes to mastered AAC m4a."""
    ch_file = os.path.join(STATIC_DIR, version.lower(), book_slug, f"{chapter}.json")
    if not os.path.exists(ch_file):
        raise FileNotFoundError(f"Missing chapter file: {ch_file}")
    
    with open(ch_file, "r", encoding="utf-8") as f:
        ch_data = json.load(f)
        
    book_name = ch_data.get("book_name", book_slug.capitalize())
    verses = ch_data.get("verses", [])
    if not verses:
        raise ValueError(f"No verses in {ch_file}")
        
    with tempfile.TemporaryDirectory() as work_dir:
        lead_text = f"{book_name}, chapter {chapter}."
        lead_wav = os.path.join(work_dir, "lead.wav")
        await synthesize_chunk(lead_text, voice, lead_wav)
        lead_dur = get_audio_duration_ms(lead_wav)
        
        # Silence generators
        silence_lead = os.path.join(work_dir, "silence_lead.wav")
        silence_verse = os.path.join(work_dir, "silence_verse.wav")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", "0.7", silence_lead], check=True)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", "0.35", silence_verse], check=True)
        
        concat_list = [lead_wav, silence_lead]
        verse_timings = []
        current_time_ms = lead_dur + 700
        total_words = len(lead_text.split())
        
        for idx, v in enumerate(verses):
            v_num = v["verse"]
            raw_text = v["text"]
            clean_text = clean_spoken_text(raw_text)
            total_words += len(clean_text.split())
            
            v_wav = os.path.join(work_dir, f"v_{v_num}.wav")
            # Retry synthesis up to 3 times
            for attempt in range(3):
                try:
                    await synthesize_chunk(clean_text, voice, v_wav)
                    break
                except Exception as e:
                    if attempt == 2:
                        raise e
                    await asyncio.sleep(1.0 * (attempt + 1))
            
            v_dur = get_audio_duration_ms(v_wav)
            verse_timings.append({
                "verse": v_num,
                "start_ms": current_time_ms,
                "end_ms": current_time_ms + v_dur
            })
            
            concat_list.append(v_wav)
            concat_list.append(silence_verse)
            current_time_ms += v_dur + 350
            
        # Create ffmpeg concat demuxer input
        concat_txt = os.path.join(work_dir, "concat.txt")
        with open(concat_txt, "w") as cf:
            for item in concat_list:
                cf.write(f"file '{item}'\n")
                
        # Encode to mastered AAC m4a (-16 LUFS, 48kbps mono, +faststart)
        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-f", "concat", "-safe", "0", "-i", concat_txt,
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-c:a", "aac", "-b:a", "48k", "-ac", "1", "-ar", "24000",
            "-movflags", "+faststart",
            out_m4a
        ]
        subprocess.run(cmd, check=True)
        
        # Verification
        v_res = verify_audio(out_m4a, total_words)
        
        return {
            "file": os.path.basename(out_m4a),
            "book": book_name,
            "book_slug": book_slug,
            "chapter": chapter,
            "bytes": v_res["size"],
            "sha256": v_res["sha256"],
            "duration_ms": v_res["duration_ms"],
            "engine": "edge-tts",
            "voice": voice,
            "word_count": total_words,
            "verses": verse_timings
        }

async def main():
    parser = argparse.ArgumentParser(description="VerseWell Audio Generator")
    parser.add_argument("--version", required=True, choices=["kjv", "niv", "tlb", "gw", "voice"])
    parser.add_argument("--pilot", action="store_true", help="Run 2-chapter pilot")
    parser.add_argument("--chapters", type=int, default=260, help="Number of chapters to generate in this run")
    parser.add_argument("--offset", type=int, default=0, help="Chapter offset to start from")
    parser.add_argument("--simulate-failure", action="store_true", help="Deliberately fail verification for testing")
    args = parser.parse_args()

    v_code = args.version.lower()
    config = VOICE_CONFIG[v_code]
    voice = config["primary"]

    print(f"=== Audio Pipeline: {v_code.upper()} ===")
    print(f"Voice: {voice} ({config['title']})")

    # Flatten all 260 NT chapters
    all_chapters = []
    for b_slug, count in NT_BOOKS:
        for ch in range(1, count + 1):
            all_chapters.append((b_slug, ch))

    if args.pilot:
        target_chapters = all_chapters[:2]
    else:
        target_chapters = all_chapters[args.offset : args.offset + args.chapters]

    print(f"Target count: {len(target_chapters)} chapters.")

    os.makedirs(f"/tmp/audio_out_{v_code}", exist_ok=True)
    manifest_file = f"/tmp/audio_out_{v_code}/manifest.json"
    manifest_data = {"version": v_code.upper(), "voice": voice, "chapters": []}

    # Load existing manifest if present (from previous workflow or check_release step)
    existing_candidates = [
        f"audio_{v_code}/manifest.json",
        manifest_file
    ]
    for p in existing_candidates:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    if isinstance(loaded.get("chapters"), list) and loaded["chapters"]:
                        manifest_data["chapters"] = loaded["chapters"]
                        print(f"Loaded existing manifest with {len(manifest_data['chapters'])} chapters from {p}")
                        break
            except Exception as e:
                print(f"Note reading {p}: {e}")

    completed_set = {
        (c.get("book_slug"), c.get("chapter"))
        for c in manifest_data["chapters"]
        if c.get("book_slug") and c.get("chapter")
    }

    tag = f"audio-{v_code}"
    repo = os.environ.get("GITHUB_REPOSITORY", "vjumbo264/versewell")

    for b_slug, ch in target_chapters:
        asset_name = f"{b_slug}-{ch:03d}.m4a"
        out_path = f"/tmp/audio_out_{v_code}/{asset_name}"

        if (b_slug, ch) in completed_set and not args.simulate_failure:
            print(f"  Skipping {v_code.upper()} {b_slug} {ch} (already in manifest).")
            continue

        print(f"Synthesizing {v_code.upper()} {b_slug} {ch} -> {asset_name}...")
        
        if args.simulate_failure:
            # Create a deliberate bad chapter (silent / 0 bytes) to prove CI goes red
            with open(out_path, "wb") as f:
                f.write(b"corrupt")
            verify_audio(out_path, 100) # will throw and fail

        entry = await synthesize_chapter(v_code, b_slug, ch, voice, out_path)
        manifest_data["chapters"].append(entry)
        completed_set.add((b_slug, ch))
        print(f"  OK: {entry['duration_ms']/1000:.1f}s, {entry['bytes']} bytes, SHA: {entry['sha256'][:10]}...")

        # Save manifest after each chapter
        with open(manifest_file, "w", encoding="utf-8") as mf:
            json.dump(manifest_data, mf, indent=2)

        # Upload immediately to release if token available
        if os.environ.get("GH_TOKEN"):
            try:
                subprocess.run(
                    ["gh", "release", "upload", tag, out_path, manifest_file, "--clobber", "-R", repo],
                    check=False
                )
            except Exception as e:
                print(f"Live upload warning: {e}")

    with open(manifest_file, "w", encoding="utf-8") as mf:
        json.dump(manifest_data, mf, indent=2)

    print(f"Completed run. Total chapters in manifest: {len(manifest_data['chapters'])}/260.")

if __name__ == "__main__":
    asyncio.run(main())
