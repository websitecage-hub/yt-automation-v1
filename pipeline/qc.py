"""QC gate — numeric checks before anything ships. No LLM, no network.

Every failure mode the factory has actually produced (silent renders, 3-second
mp4s, wordless thumbnails, even-grid beats from dead Whisper) is caught here as
*bad output*, not as an exception. A failing job quarantines
(stage "qc_failed" + reasons) instead of uploading; warnings never block.

Kept dependency-light on purpose: stdlib + ffprobe (already required) + Pillow
(already required). All functions take (job, work_dir) and return plain data so
the rules are unit-testable without ffmpeg on the box.
"""
import json
import os
import subprocess

MIN_DURATION = 45.0          # shorter than this is a broken render, not a short
MAX_DURATION = 15 * 60.0     # longer than this never finishes uploading in budget
MIN_ART_COVERAGE = 0.70      # fraction of visual beats that must have files
MIN_THUMB_BYTES = 30 * 1024  # below this the thumbnail is the painted fallback
AUDIO_EXT = ("mp3", "wav", "m4a", "ogg", "flac")
IMAGE_EXT = ("jpg", "jpeg", "png", "webp", "gif")
VISUAL_KINDS = ("img", "photo", "doodle", "meme")
_KIND_HEAD = {"img": "i", "photo": "p", "doodle": "d", "meme": "m"}


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def total_duration(job):
    """Sum of scene durations, the same number compose renders."""
    return round(sum(_num(s.get("dur")) for s in (job or {}).get("scenes") or []), 3)


def _find(work_dir, stem, exts):
    for ext in exts:
        path = os.path.join(work_dir, "%s.%s" % (stem, ext))
        try:
            if os.path.getsize(path) > 0:
                return path
        except OSError:
            continue
    return None


def missing_narration(job, work_dir):
    """Scene indexes with no voice file on disk."""
    missing = []
    for i in range(len((job or {}).get("scenes") or [])):
        if not _find(work_dir, "v%s_%d" % (job.get("id", ""), i), AUDIO_EXT):
            missing.append(i)
    return missing


def art_coverage(job, work_dir):
    """(have, want) visual beats with files present. Zero-want counts as full."""
    have = want = 0
    for i, scene in enumerate((job or {}).get("scenes") or []):
        for j, beat in enumerate((scene or {}).get("beats") or []):
            if not isinstance(beat, dict) or beat.get("kind") not in VISUAL_KINDS:
                continue
            if not beat.get("prompt"):
                continue
            want += 1
            stem = "%s%s_%d_%d" % (_KIND_HEAD[beat["kind"]], job.get("id", ""), i, j)
            if _find(work_dir, stem, IMAGE_EXT):
                have += 1
    return have, want


def thumb_status(job, work_dir):
    """(exists_big_enough, bytes or 0) for the episode thumbnail."""
    path = (job or {}).get("thumb") or ""
    candidates = [path] if path else []
    candidates.append(os.path.join(work_dir, "%s_thumb.jpg" % (job or {}).get("id", "")))
    for cand in candidates:
        try:
            size = os.path.getsize(cand)
        except OSError:
            continue
        if size > 0:
            return size >= MIN_THUMB_BYTES, size
    return False, 0


def whisper_gaps(job, work_dir):
    """Scene indexes whose word file is missing or empty.

    A gap means beats fell on an even grid. Fails the gate only when Whisper
    was actually available (GROQ_KEY set) -- otherwise it is the documented
    fallback, and failing would quarantine every keyless DRY_RUN.
    """
    gaps = []
    for i in range(len((job or {}).get("scenes") or [])):
        path = os.path.join(work_dir, "w%s_%d.json" % (job.get("id", ""), i))
        try:
            with open(path, encoding="utf-8") as fh:
                words = json.load(fh)
        except (OSError, ValueError):
            words = []
        if not [w for w in (words or []) if isinstance(w, dict)]:
            gaps.append(i)
    return gaps


def loudness_notes(job, work_dir):
    """Warn-only: scenes far from -14 LUFS integrated (cheap single-pass read).

    Read-only measurement -- the voice itself is never touched (operator rule:
    the clone ships as the host returns it). Missing ffmpeg or unreadable audio
    yields no notes rather than failures.
    """
    notes = []
    try:
        import shutil
        if not shutil.which("ffmpeg"):
            return notes
        for i in range(len((job or {}).get("scenes") or [])):
            audio = _find(work_dir, "v%s_%d" % (job.get("id", ""), i), AUDIO_EXT)
            if not audio:
                continue
            out = subprocess.run(
                ["ffmpeg", "-nostats", "-i", audio, "-filter_complex",
                 "loudnorm=print_format=json", "-f", "null", "-"],
                capture_output=True, text=True, timeout=120)
            integrated = None
            for line in (out.stderr or "").splitlines():
                line = line.strip()
                if '"input_i"' in line:
                    try:
                        integrated = float(line.split(":")[1].strip().rstrip(","))
                    except (ValueError, IndexError):
                        pass
            if integrated is not None and abs(integrated - (-14.0)) > 6.0:
                notes.append("scene %d loudness %.1f LUFS (target -14)" % (i, integrated))
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return notes


def check(job, work_dir):
    """(ok, fails, warns) for one finished episode. ok=False quarantines."""
    fails, warns = [], []
    job = job or {}

    total = total_duration(job)
    if total < MIN_DURATION:
        fails.append("duration %.1fs below %.0fs minimum -- broken render?" % (total, MIN_DURATION))
    elif total > MAX_DURATION:
        fails.append("duration %.1fs above %.0fmin maximum" % (total, MAX_DURATION / 60.0))

    missing = missing_narration(job, work_dir)
    if missing:
        fails.append("scenes %s have no narration audio" % ",".join(map(str, missing)))

    have, want = art_coverage(job, work_dir)
    if want and have / float(want) < MIN_ART_COVERAGE:
        fails.append("art coverage %d/%d below %d%%" % (have, want, int(MIN_ART_COVERAGE * 100)))

    big_enough, size = thumb_status(job, work_dir)
    if size <= 0:
        fails.append("no thumbnail file")
    elif not big_enough:
        fails.append("thumbnail only %d bytes -- painted fallback?" % size)

    gaps = whisper_gaps(job, work_dir)
    if gaps:
        if (os.getenv("GROQ_KEY") or "").strip():
            fails.append("scenes %s have no word timings (even-grid beats)"
                         % ",".join(map(str, gaps)))
        else:
            warns.append("scenes %s on ffprobe fallback timing (no GROQ_KEY)"
                         % ",".join(map(str, gaps)))

    warns.extend(loudness_notes(job, work_dir))
    return (not fails), fails, warns
