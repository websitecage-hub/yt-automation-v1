"""Orchestrator: advance one video through the SCALED stage machine.

`python -m pipeline.run` is the produce workflow's single entry point. It is a
crash-only state machine: the repo is the database, each video is one JSON file
in data/videos/ carrying a `stage`, and every stage saves before the next
begins -- so a killed run resumes exactly where it stopped on the next call.

Stages: idea -> voice -> srt -> beats -> visuals -> render -> upload ->
thumbnail -> done. The order is load-bearing: voice must exist before srt can
time it, srt before beats can snap to a word onset, and beats before visuals
knows which stills to buy. Producing (idea..render) needs the creative/voice/
image keys; the YouTube stages degrade to logged no-ops when credentials are
absent (see pipeline.upload.has_yt), so a keyless DRY_RUN still renders an mp4.
"""

import io
import json
import os
import subprocess
import sys
import time
from concurrent import futures
from datetime import datetime, timezone

from pipeline import adapters, llm, render, srt, thumbnail, topics, upload
from pipeline import beats as beat_engine
from pipeline import qc

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIDEOS = os.path.join(REPO, "data", "videos")
WORK = os.path.join(REPO, "work")
NICHE = os.path.join(REPO, "config", "niche.md")
STRATEGY = os.path.join(REPO, "data", "strategy.json")
STATUS = os.path.join(REPO, "STATUS.md")
PAUSE = os.path.join(REPO, "data", "PAUSE")

STAGES = ["idea", "voice", "srt", "beats", "visuals", "render", "upload",
          "thumbnail", "qc", "done"]
SHELF = 5                 # stop producing once this many finished, unpublished videos wait
CAPTION_PAD = 0.4         # scene duration = last word end + this much tail
ART_WORKERS = 4           # concurrent image calls; each takes ~30s, the host allows 4
STILL_W, STILL_H = 1920, 1080
AUDIO_EXT = ("mp3", "wav", "m4a", "ogg", "flac")
IMAGE_EXT = ("jpg", "jpeg", "png", "webp", "gif")
RUN_BUDGET_MIN = int(os.getenv("RUN_BUDGET_MIN", "300"))

# ---------------------------------------------------------------- store helpers

def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _slug(text):
    keep = "".join(c.lower() if c.isalnum() else "-" for c in str(text))
    while "--" in keep:
        keep = keep.replace("--", "-")
    return keep.strip("-")[:48] or "lesson"


def _read(path, default=""):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return default


def load_jobs():
    """Every job JSON in data/videos, ordered by filename."""
    jobs = []
    if not os.path.isdir(VIDEOS):
        return jobs
    for name in sorted(os.listdir(VIDEOS)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(VIDEOS, name), encoding="utf-8") as fh:
                jobs.append(json.load(fh))
        except (OSError, json.JSONDecodeError) as e:
            print("[run] skipping unreadable %s: %s" % (name, e))
    return jobs


def save_job(job):
    os.makedirs(VIDEOS, exist_ok=True)
    path = os.path.join(VIDEOS, "%s.json" % job["id"])
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(job, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return path


def next_lesson(jobs):
    """Lesson numbers only ever climb: max seen + 1, starting at 1."""
    best = 0
    for j in jobs:
        try:
            best = max(best, int(j.get("lesson") or j.get("n") or 0))
        except (TypeError, ValueError):
            pass
    return best + 1


def in_progress(jobs):
    for j in jobs:
        if j.get("stage") not in (None, "done", "qc_failed"):
            return j
    return None


def shelf_count(jobs):
    return sum(1 for j in jobs if j.get("stage") == "done" and not j.get("published"))

# ---------------------------------------------------------------- media helper

def _ffmpeg(*args):
    subprocess.run(["ffmpeg", "-y", *args], check=True, capture_output=True, text=True)


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _find(work_dir, stem, exts):
    """First work/{stem}.{ext} that exists, or None. Providers pick the container."""
    for ext in exts:
        path = os.path.join(work_dir, "%s.%s" % (stem, ext))
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path
    return None


def _voice_path(job, work_dir, i):
    """Scene i's narration file, whatever container the TTS host returned."""
    return _find(work_dir, "v%s_%d" % (job.get("id", ""), i), AUDIO_EXT)


def _beat_art(job, work_dir, i, j, kind):
    """Beat (i, j)'s still, if it has already been bought. Resume skips those."""
    head = {"scene": "s", "photo": "p", "meme": "m"}.get(kind)
    if not head:
        return None
    return _find(work_dir, "%s%s_%d_%d" % (head, job.get("id", ""), i, j), IMAGE_EXT)


def audio_seconds(path):
    """Duration of an audio file via ffprobe, or 0.0 when it cannot be read.

    Only a fallback -- Whisper is the clock (C4). This exists so a scene whose
    transcription failed still gets a duration that matches its real audio
    instead of a guess from word count.
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            check=True, capture_output=True, text=True).stdout
        return round(_num(out.strip()), 3)
    except (OSError, subprocess.CalledProcessError, ValueError):
        return 0.0


def save_still(data, fmt, dst):
    """Provider image bytes -> a 1920x1080 JPEG at dst, cover-cropped with Pillow.

    Pillow rather than ffmpeg because Pillow is a declared dependency and ffmpeg
    is only guaranteed on the runner. The crop is `cover`, never a stretch: the
    art host returns 3:2 and a squashed subject is instantly visible on screen.
    Unreadable bytes are written through untouched -- compose can still show
    whatever the browser manages to decode.
    """
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        img = img.convert("RGB")
        sw, sh = img.size
        scale = max(STILL_W / float(sw or 1), STILL_H / float(sh or 1))
        img = img.resize((max(1, int(sw * scale + 0.5)), max(1, int(sh * scale + 0.5))),
                         Image.LANCZOS)
        left = (img.size[0] - STILL_W) // 2
        top = (img.size[1] - STILL_H) // 2
        img.crop((left, top, left + STILL_W, top + STILL_H)).save(dst, "JPEG", quality=90)
        return dst
    except Exception as e:
        raw = "%s.%s" % (os.path.splitext(dst)[0], fmt or "bin")
        print("[run] Pillow could not normalise %s (%s) -- keeping the raw %s"
              % (os.path.basename(dst), e, fmt or "bytes"))
        with open(raw, "wb") as fh:
            fh.write(data)
        return raw


# ---------------------------------------------------------------- stages

def _perf_summary():
    """What the SCRIPT model gets as {perf}: real numbers, not a placeholder.

    B3: this was hardcoded to "(no performance data yet)" even after learn.py
    had written strategy. Now it reports the strategy file's notes plus the
    last finished episodes' titles/lanes, or the placeholder only when the
    channel genuinely has no history yet.
    """
    try:
        strategy = json.loads(_read(STRATEGY, "{}") or "{}")
    except ValueError:
        strategy = {}
    notes = str(strategy.get("notes") or "").strip()
    jobs = [j for j in load_jobs() if j.get("stage") == "done"]
    jobs.sort(key=lambda j: str(j.get("created", "")))
    recent = ["#%s %s [%s]" % (j.get("lesson", "?"), str(j.get("title", ""))[:50],
                               j.get("lane", "?")) for j in jobs[-5:]]
    parts = []
    if notes:
        parts.append("PLAYBOOK NOTES: " + notes[:800])
    if recent:
        parts.append("LAST FINISHED: " + " | ".join(recent))
    return " ".join(parts) or "(no performance data yet)"


def stage_idea(jobs):
    """Pick a topic, write the 16-scene script, return a fresh job at 'voice'."""
    topics.refill(lambda s, u: llm.llm(s, u, json_out=True))
    picked = topics.pick()
    if not picked:
        raise RuntimeError("topic bank is empty and refill failed")
    used = ", ".join(sorted({j.get("topic", "") for j in jobs if j.get("topic")})) or "(none yet)"
    n = next_lesson(jobs)
    system = llm.prompt("SCRIPT", "system")
    user = llm.prompt("SCRIPT", "user")
    for k, v in (("{niche}", _read(NICHE, "SCALED -- biology as power stats")),
                 ("{strategy}", (_read(STRATEGY, "{}").strip() or "{}")),
                 ("{perf}", _perf_summary()),
                 ("{used}", used),
                 ("{topic}", picked["topic"]),
                 ("{n}", str(n))):
        user = user.replace(k, v)
    doc = llm.llm(system, user, json_out=True)
    if isinstance(doc, str):
        doc = llm.parse_json(doc)
    scenes = doc.get("scenes") or []
    if not scenes:
        raise RuntimeError("script came back with no scenes")
    job = {
        "id": "%s-%s" % (datetime.now(timezone.utc).strftime("%Y%m%d"), _slug(picked["topic"])),
        "lesson": n,
        "topic": picked["topic"],
        "lane": picked.get("lane", ""),
        "created": _now(),
        "stage": "voice",
        "title": str(doc.get("title", "")),
        "description": str(doc.get("description", "")),
        "tags": doc.get("tags") or [],
        "thumb_words": str(doc.get("thumb_words", "")),
        "staircase": doc.get("staircase") or [],
        "thumbnail_prompt": str(doc.get("thumbnail_prompt", "")),
        "verdict": str(doc.get("verdict", "")),
        "extra_credit": str(doc.get("extra_credit", "")),
        "scenes": scenes,
    }
    print("[run] idea: LESSON #%03d %r (%d scenes)" % (n, job["title"], len(scenes)))
    return job

def stage_voice(job, work_dir):
    """One narration clip per scene, in Croc's cloned voice.

    The provider's own container is kept (wav, mp3, ...) instead of transcoding:
    the browser plays either, the renderer's ffmpeg accepts either, and skipping
    the convert step removes a whole class of failure from the critical path.
    """
    for i, scene in enumerate(job.get("scenes") or []):
        if _voice_path(job, work_dir, i):
            continue
        data = adapters.tts(str(scene.get("text", "")))
        fmt = adapters.last_format("voice") or "mp3"
        dst = os.path.join(work_dir, "v%s_%d.%s" % (job["id"], i, fmt))
        with open(dst, "wb") as fh:
            fh.write(data)
        print("[run] voice %d/%d -> %s (%d bytes)"
              % (i + 1, len(job.get("scenes") or []), os.path.basename(dst), len(data)))
    job["stage"] = "srt"
    return job


def stage_srt(job, work_dir):
    """Whisper is the only clock: each scene's dur is its last word end + a tail.

    Degrades twice over. No word list (no GROQ_KEY, a whisper outage) falls back
    to the narration file's real length, which keeps the cut in sync and only
    costs the word-snapping; no audio at all falls back to a reading-speed
    estimate, which keeps the episode buildable.
    """
    for i, scene in enumerate(job.get("scenes") or []):
        wpath = os.path.join(work_dir, "w%s_%d.json" % (job.get("id", ""), i))
        audio = _voice_path(job, work_dir, i)
        try:
            reuse = os.path.getsize(wpath) > 2
        except OSError:
            reuse = False
        if reuse:
            # R3/C3: a previous run already paid Whisper for this scene. The
            # w-file is the done-marker; only a 2-byte "[]" (a recorded
            # failure) or a missing file re-transcribes.
            try:
                with open(wpath, encoding="utf-8") as fh:
                    words = json.load(fh)
            except (OSError, ValueError):
                words = []
        else:
            words = []
            if audio:
                try:
                    words = srt.word_timeline(audio)
                except Exception as e:
                    print("[run] whisper failed on scene %d (%s) -- beats fall on an even grid" % (i, e))
            with open(wpath, "w", encoding="utf-8") as fh:
                json.dump(words, fh)

        end = max((float(w.get("e", 0)) for w in words if isinstance(w, dict)), default=0.0)
        if not end and audio:
            end = audio_seconds(audio) or 0.0
        if not end:
            end = max(2.0, len(str(scene.get("text", "")).split()) / 2.5)
        scene["dur"] = round(end + CAPTION_PAD, 3)
    print("[run] clock: %d scenes, %.1fs total" % (len(job.get("scenes") or []),
                                                   sum(_num(s.get("dur")) for s in job["scenes"])))
    job["stage"] = "beats"
    return job


def stage_beats(job, work_dir):
    """Ask the director WHAT each scene shows; Python has already decided WHEN.

    The model never sees a timestamp and never writes motion -- it fills slots.
    Every reply is validated, given exactly one chance to repair itself, and then
    forced legal by autofix, so a bad reply costs one extra call at worst.
    """
    scenes = job.get("scenes") or []
    system = llm.prompt("BEATS", "system")
    template = llm.prompt("BEATS", "user")
    staircase = ", ".join(str(x) for x in (job.get("staircase") or [])) or "(none)"
    for i, scene in enumerate(scenes):
        if scene.get("beats"):
            continue
        words = srt.scene_words(job, i, work_dir)
        n = beat_engine.beat_count(scene.get("dur"))
        user = template
        for k, v in (("{i+1}", str(i + 1)), ("{title}", str(job.get("title", ""))),
                     ("{act}", str(scene.get("act", ""))),
                     ("{heading}", str(scene.get("heading", ""))),
                     ("{text}", str(scene.get("text", ""))),
                     ("{staircase}", staircase), ("{n}", str(n))):
            user = user.replace(k, v)

        specs = _beat_specs(system, user, i)
        problems = beat_engine.validate(specs)
        if problems:
            print("[run] scene %d beats rejected (%s) -- one repair attempt"
                  % (i, "; ".join(problems[:3])))
            specs = _beat_specs(system, user + "\n\nYour last reply was invalid: "
                               + "; ".join(problems) + ". Return corrected JSON only.", i)
        scene["beats"] = beat_engine.build(scene, words, specs, n)
        kinds = ", ".join("%s%s" % (b["kind"][0], "" if b["kind"] != "img" else "*")
                          for b in scene["beats"])
        print("[run] beats %d/%d: %d [%s]" % (i + 1, len(scenes), len(scene["beats"]), kinds))
    job["stage"] = "visuals"
    return job


def _beat_specs(system, user, i):
    """The director's beat list for one scene, or [] when it cannot be parsed.

    [] is survivable: build() pads with zooms, so the scene still gets its pacing
    and merely loses the model's content choices.
    """
    try:
        doc = llm.llm(system, user, json_out=True)
        if isinstance(doc, str):
            doc = llm.parse_json(doc)
        specs = doc.get("beats") if isinstance(doc, dict) else doc
        return [b for b in (specs or []) if isinstance(b, dict)]
    except Exception as e:
        print("[run] scene %d beat call failed (%s) -- Python fills the slots" % (i, e))
        return []


def stage_visuals(job, work_dir):
    """Render every img and meme beat. Four at a time -- each call takes ~30s.

    Art failures are logged and skipped, never raised: compose degrades a beat
    with no file into a punch-in on whatever is already on screen, so a partial
    art outage still produces a finished episode.
    """
    jobs_todo = []
    for i, scene in enumerate(job.get("scenes") or []):
        for j, beat in enumerate(scene.get("beats") or []):
            kind = beat.get("kind")
            if kind not in ("scene", "photo", "meme") or not beat.get("prompt"):
                continue
            if _beat_art(job, work_dir, i, j, kind):
                continue
            jobs_todo.append((i, j, kind, str(beat["prompt"])))

    if not jobs_todo:
        print("[run] visuals: nothing to render")
        job["stage"] = "render"
        return job

    print("[run] visuals: %d beats to render (%d scene, %d photo, %d meme)"
          % (len(jobs_todo), sum(1 for x in jobs_todo if x[2] == "scene"),
             sum(1 for x in jobs_todo if x[2] == "photo"),
             sum(1 for x in jobs_todo if x[2] == "meme")))
    done = {"ok": 0, "fail": 0}
    with futures.ThreadPoolExecutor(max_workers=ART_WORKERS) as pool:
        pending = {pool.submit(_render_beat_art, job, work_dir, *task): task for task in jobs_todo}
        for fut in futures.as_completed(pending):
            i, j, kind, _p = pending[fut]
            try:
                fut.result()
                done["ok"] += 1
            except Exception as e:
                done["fail"] += 1
                print("[run] %s beat %d.%d failed: %s" % (kind, i, j, e))
    print("[run] visuals: %d rendered, %d failed" % (done["ok"], done["fail"]))
    job["stage"] = "render"
    return job


def _render_beat_art(job, work_dir, i, j, kind, prompt_text):
    """One beat's full frame.

    scene: the one cartoon world. Cast re-staging first (same faces, same
    room); when the beat carries baked words (text/value) or the cast has no
    cached URL, a from-text world frame is painted instead -- the host draws
    short copy correctly, and a missing short caption beats a missing picture.
    meme: Croc re-posed (cast edit -> same-croc edit -> text fallback).
    photo: raw irony with the caption/counter baked in like a macro.
    """
    try:
        beat = ((job.get("scenes") or [])[i].get("beats") or [])[j]
    except (IndexError, AttributeError):
        beat = {}
    beat = beat if isinstance(beat, dict) else {}
    if kind == "scene":
        text, value, label = (beat.get("text", ""), beat.get("value", ""),
                              beat.get("label", ""))
        dst = os.path.join(work_dir, "s%s_%d_%d.jpg" % (job["id"], i, j))
        if not (str(text).strip() or str(value).strip()):
            try:
                data = adapters.cast_edit(prompt_text, "guy")
                print("[run] scene %d.%d via cast edit" % (i, j))
                return save_still(data, adapters.last_format("image") or "png", dst)
            except Exception as e:
                print("[run] scene %d.%d cast edit failed (%s) -- painting" % (i, j, e))
        data = adapters.image_world(prompt_text, text, value, label, "1920x1080")
        fmt = adapters.last_format("image") or "jpg"
    elif kind == "meme":
        dst = os.path.join(work_dir, "m%s_%d_%d.jpg" % (job["id"], i, j))
        try:
            data = adapters.cast_edit(prompt_text, "croc")
            print("[run] meme %d.%d via cast edit (croc)" % (i, j))
            return save_still(data, adapters.last_format("image") or "png", dst)
        except Exception as e:
            print("[run] meme %d.%d cast edit failed (%s) -- text fallback" % (i, j, e))
            try:
                data = adapters.meme_croc(prompt_text)
                print("[run] meme %d.%d via same-croc edit" % (i, j))
            except Exception as e2:
                print("[run] meme %d.%d croc-edit failed (%s) -- text fallback" % (i, j, e2))
                data = adapters.meme_img(prompt_text)
        fmt = adapters.last_format("image") or "png"
    elif kind == "photo":
        data = adapters.image_plain(prompt_text, "1920x1080",
                                    beat.get("caption", ""), beat.get("counter", ""))
        fmt = adapters.last_format("image") or "jpg"
        dst = os.path.join(work_dir, "p%s_%d_%d.jpg" % (job["id"], i, j))
    else:
        raise RuntimeError("no art path for kind %r" % kind)
    return save_still(data, fmt, dst)


def stage_render(job, work_dir):
    job["mp4"] = render.render_video(job, work_dir)
    job["stage"] = "upload"
    return job


def stage_upload(job, work_dir):
    mp4 = job.get("mp4")
    if not (mp4 and os.path.exists(mp4)):
        # A fresh runner lost work/ between runs -- rebuild the mp4 before upload.
        print("[run] mp4 missing at upload -- re-rendering")
        job["mp4"] = render.render_video(job, work_dir)
    if job.get("video_id"):
        print("[run] already uploaded as %s -- skipping" % job["video_id"])
    else:
        # R1: adopt an orphan from a kill between upload and save before
        # uploading anything new. The pre-upload save means a crash from here
        # on still leaves a searchable marker on the channel.
        found = upload.find_upload(job.get("id"))
        if found:
            job["video_id"] = found
        else:
            job["upload_started_at"] = _now()
            save_job(job)
            job["video_id"] = upload.upload_video(job, job.get("mp4"))
    job["stage"] = "thumbnail"
    return job


def stage_thumbnail(job, work_dir):
    """The hybrid thumbnail: the model paints the scene, Pillow owns every word."""
    thumb = thumbnail.make_thumbnail(job, work_dir)
    if thumb and job.get("video_id"):
        upload.set_thumbnail(job["video_id"], thumb)
    job["thumb"] = thumb
    job["stage"] = "qc"
    return job


def stage_qc(job, work_dir):
    """R7: numeric gates before anything ships. Failure quarantines the job.

    A quarantined job (stage "qc_failed") is invisible to in_progress() and to
    publish -- it waits for an operator, it never uploads, and it never loops.
    """
    ok, fails, warns = qc.check(job, work_dir)
    for w in warns:
        print("[run] qc warn: %s" % w)
    if ok:
        print("[run] qc passed")
        job.pop("qc_reasons", None)
        job["stage"] = "done"
    else:
        for f in fails:
            print("[run] qc FAIL: %s" % f)
        job["qc_reasons"] = fails
        job["stage"] = "qc_failed"
    return job


STAGE_FN = {
    "voice": stage_voice, "srt": stage_srt, "beats": stage_beats, "visuals": stage_visuals,
    "render": stage_render, "upload": stage_upload, "thumbnail": stage_thumbnail,
    "qc": stage_qc,
}

# ---------------------------------------------------------------- driver

def write_status(job=None):
    jobs = load_jobs()
    lines = ["# SCALED status", "", "_updated %s_" % _now(), "",
             "shelf (done, unpublished): %d/%d" % (shelf_count(jobs), SHELF), "",
             "| lesson | stage | title |", "| --- | --- | --- |"]
    for j in sorted(jobs, key=lambda x: x.get("lesson", 0)):
        lines.append("| %s | %s | %s |"
                     % (j.get("lesson", "?"), j.get("stage", "?"),
                        str(j.get("title", ""))[:60].replace("|", "/")))
    lines += ["", "providers: %s" % json.dumps(getattr(llm, "PROVIDERS_HEALTH", {}))]
    with open(STATUS, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def advance(job, deadline):
    """Run stages until done, quarantined, or the budget runs out.

    Saves after every stage. A quarantined job (qc_failed) stops the machine
    the same way a finished one does -- it just isn't DONE.
    """
    os.makedirs(WORK, exist_ok=True)
    while job.get("stage") not in ("done", "qc_failed"):
        if time.time() >= deadline:
            print("[run] budget reached at stage %s -- resuming next run" % job.get("stage"))
            return False
        stage = job.get("stage")
        fn = STAGE_FN.get(stage)
        if not fn:
            raise RuntimeError("unknown stage %r" % stage)
        print("[run] %s: %s" % (job["id"], stage))
        fn(job, WORK)
        save_job(job)
        write_status(job)
    if job.get("stage") == "qc_failed":
        print("[run] %s QUARANTINED (see qc_reasons) -- needs an operator" % job.get("id"))
        return True
    print("[run] %s DONE (LESSON #%03d)" % (job["id"], job.get("lesson", 0)))
    return True


def main():
    if os.path.exists(PAUSE):
        print("[run] data/PAUSE present -- nothing to do")
        return 0
    deadline = time.time() + RUN_BUDGET_MIN * 60
    jobs = load_jobs()
    job = in_progress(jobs)
    if job:
        print("[run] resuming %s at stage %s" % (job["id"], job.get("stage")))
    else:
        if shelf_count(jobs) >= SHELF:
            print("[run] shelf full (%d/%d) -- not producing" % (shelf_count(jobs), SHELF))
            write_status()
            return 0
        job = stage_idea(jobs)
        save_job(job)
        write_status(job)
    try:
        advance(job, deadline)
    except Exception as e:                          # noqa: BLE001 - log, persist, exit non-zero
        print("[run] stage %s failed: %s" % (job.get("stage"), e))
        write_status(job)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
