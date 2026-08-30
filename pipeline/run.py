"""Orchestrator: advance one video through the SCALED stage machine.

`python -m pipeline.run` is the produce workflow's single entry point. It is a
crash-only state machine: the repo is the database, each video is one JSON file
in data/videos/ carrying a `stage`, and every stage saves before the next
begins -- so a killed run resumes exactly where it stopped on the next call.

Stages: idea -> voice -> srt -> visuals -> html -> render -> upload -> thumbnail
-> done. Producing (idea..render) needs the creative/code/voice/image keys; the
YouTube stages degrade to logged no-ops when credentials are absent (see
pipeline.upload.has_yt), so a keyless DRY_RUN still renders a finished mp4.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

from pipeline import adapters, htmlgen, llm, render, srt, topics, upload

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIDEOS = os.path.join(REPO, "data", "videos")
WORK = os.path.join(REPO, "work")
NICHE = os.path.join(REPO, "config", "niche.md")
STRATEGY = os.path.join(REPO, "data", "strategy.json")
STATUS = os.path.join(REPO, "STATUS.md")
PAUSE = os.path.join(REPO, "data", "PAUSE")

STAGES = ["idea", "voice", "srt", "visuals", "html", "render", "upload", "thumbnail", "done"]
SHELF = 5                 # stop producing once this many finished, unpublished videos wait
CAPTION_PAD = 0.4         # scene duration = last word end + this much tail
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
        if j.get("stage") not in (None, "done"):
            return j
    return None


def shelf_count(jobs):
    return sum(1 for j in jobs if j.get("stage") == "done" and not j.get("published"))

# ---------------------------------------------------------------- media helper

def _ffmpeg(*args):
    subprocess.run(["ffmpeg", "-y", *args], check=True, capture_output=True, text=True)


def _save_media(data, fmt, dst, kind):
    """Write provider bytes to dst, transcoding to mp3/jpg with ffmpeg if needed."""
    want = "mp3" if kind == "audio" else "jpg"
    if fmt == want or (want == "jpg" and fmt == "jpeg"):
        with open(dst, "wb") as fh:
            fh.write(data)
        return dst
    tmp = "%s.%s" % (dst, fmt or "bin")
    with open(tmp, "wb") as fh:
        fh.write(data)
    try:
        _ffmpeg("-i", tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return dst


# ---------------------------------------------------------------- stages

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
                 ("{perf}", "(no performance data yet)"),
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
        "thumbnail_prompt": str(doc.get("thumbnail_prompt", "")),
        "verdict": str(doc.get("verdict", "")),
        "extra_credit": str(doc.get("extra_credit", "")),
        "scenes": scenes,
    }
    print("[run] idea: LESSON #%03d %r (%d scenes)" % (n, job["title"], len(scenes)))
    return job

def stage_voice(job, work_dir):
    for i, scene in enumerate(job.get("scenes") or []):
        dst = os.path.join(work_dir, "v%s_%d.mp3" % (job["id"], i))
        if os.path.exists(dst):
            continue
        data = adapters.tts(str(scene.get("text", "")))
        _save_media(data, adapters.last_format("voice"), dst, "audio")
    job["stage"] = "srt"
    return job


def stage_srt(job, work_dir):
    """Whisper is the only clock: each scene's dur is its last word end + a tail."""
    for i, scene in enumerate(job.get("scenes") or []):
        mp3 = os.path.join(work_dir, "v%s_%d.mp3" % (job["id"], i))
        words = srt.word_timeline(mp3)
        with open(os.path.join(work_dir, "w%s_%d.json" % (job["id"], i)), "w", encoding="utf-8") as fh:
            json.dump(words, fh)
        end = max((float(w.get("e", 0)) for w in words if isinstance(w, dict)), default=0.0)
        if end:
            scene["dur"] = round(end + CAPTION_PAD, 3)
        else:
            scene["dur"] = round(max(2.0, len(str(scene.get("text", "")).split()) / 2.5), 3)
    job["stage"] = "visuals"
    return job


def stage_visuals(job, work_dir):
    for i, scene in enumerate(job.get("scenes") or []):
        dst = os.path.join(work_dir, "i%s_%d.jpg" % (job["id"], i))
        if os.path.exists(dst):
            continue
        data = adapters.image(str(scene.get("image_prompt", "")), "1920x1080")
        _save_media(data, adapters.last_format("image"), dst, "image")
    job["stage"] = "html"
    return job


def stage_html(job, work_dir):
    for i in range(len(job.get("scenes") or [])):
        htmlgen.generate_scene(job, i, work_dir)
    job["stage"] = "render"
    return job


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
    job["video_id"] = upload.upload_video(job, job.get("mp4"))
    job["stage"] = "thumbnail"
    return job


def stage_thumbnail(job, work_dir):
    thumb = upload.make_thumb(job, work_dir)
    if thumb and job.get("video_id"):
        upload.set_thumbnail(job["video_id"], thumb)
    job["thumb"] = thumb
    job["stage"] = "done"
    return job


STAGE_FN = {
    "voice": stage_voice, "srt": stage_srt, "visuals": stage_visuals, "html": stage_html,
    "render": stage_render, "upload": stage_upload, "thumbnail": stage_thumbnail,
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
    """Run stages until done or the budget runs out. Saves after every stage."""
    os.makedirs(WORK, exist_ok=True)
    while job.get("stage") != "done":
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
