"""Publishing for SCALED — take one finished video public, on a schedule.

A run bakes and uploads a video as *private*; publishing is a separate, later
step so the channel drips rather than dumps. publish_one() takes the single
oldest finished-but-unpublished job, flips it to public, and pins Professor
Croc's extra-credit fact as the first comment.

The job JSON files under data/videos/ ARE the queue and the state: the same
read-modify-write-to-disk discipline as topics.py, so a crash never loses a
decision. When has_yt() is False (no creds, or DRY_RUN) every network call is a
logged no-op, but the job is still marked published and saved — the queue must
drain the same way online or off, or a dry run would republish forever.
"""
import glob
import json
import os
from datetime import datetime, timezone

from pipeline.upload import has_yt

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------- job store
# Shared with comments.py and learn.py so the three publishing-side modules
# read and write the queue exactly the same way.

def videos_root(videos_dir="data/videos"):
    """Absolute path to the queue dir; a relative one hangs off the repo root."""
    return videos_dir if os.path.isabs(videos_dir) else os.path.join(REPO, videos_dir)


def read_job(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_job(path, job):
    """Same formatting every time so diffs stay readable (mirrors topics.save)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(job, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return path


def _all_jobs(videos_dir="data/videos"):
    """Every readable job in the queue as (job, path) pairs. Junk is skipped."""
    out = []
    for path in glob.glob(os.path.join(videos_root(videos_dir), "*.json")):
        try:
            job = read_job(path)
        except (OSError, json.JSONDecodeError) as e:
            print("[publish] %s unreadable (%s) -- skipped" % (path, e))
            continue
        if isinstance(job, dict):
            out.append((job, path))
    return out


def _order_key(job, path):
    """Chronological-ish key: explicit timestamp, else the date-prefixed id, else name."""
    return str(job.get("created_at") or job.get("id") or os.path.basename(path))


def published_jobs(videos_dir="data/videos"):
    """Published jobs as (job, path) pairs, most recently published first."""
    done = [(j, p) for j, p in _all_jobs(videos_dir) if j.get("published")]
    done.sort(key=lambda jp: str(jp[0].get("published_at") or _order_key(*jp)), reverse=True)
    return done


def _oldest_done(videos_dir):
    """The oldest job with stage=='done' that has not been published yet."""
    ready = [(j, p) for j, p in _all_jobs(videos_dir)
             if j.get("stage") == "done" and not j.get("published")]
    if not ready:
        return None, None
    ready.sort(key=lambda jp: _order_key(*jp))
    return ready[0]


# --------------------------------------------------------------- network bits

def _video_id(job):
    return job.get("videoId") or job.get("video_id") or job.get("youtube_id")


def _go_public(video_id, job):
    if not video_id:
        print("[publish] %s has no video id -- cannot flip privacy" % job.get("id"))
        return
    try:
        from pipeline.upload import _service
        svc = _service("youtube")
        svc.videos().update(
            part="status",
            body={"id": video_id, "status": {"privacyStatus": "public"}},
        ).execute()
        print("[publish] %s -> public" % video_id)
    except Exception as e:
        print("[publish] going public failed for %s: %s" % (video_id, e))


def _post_extra_credit(video_id, job):
    text = str(job.get("extra_credit") or "").strip()
    if not (video_id and text):
        return
    try:
        from pipeline.upload import _service
        svc = _service("youtube")
        svc.commentThreads().insert(
            part="snippet",
            body={"snippet": {"videoId": video_id,
                              "topLevelComment": {"snippet": {"textOriginal": text}}}},
        ).execute()
        print("[publish] extra-credit comment posted on %s" % video_id)
    except Exception as e:
        print("[publish] extra-credit comment failed on %s: %s" % (video_id, e))


# --------------------------------------------------------------- the step

def publish_one(videos_dir="data/videos"):
    """Publish the oldest finished job. Returns the job dict, or None if none.

    Online: flip privacy to public and pin the extra-credit comment. DRY_RUN:
    do everything but the network calls. Either way the job is marked published
    and saved, so the queue drains once and only once.
    """
    job, path = _oldest_done(videos_dir)
    if job is None:
        print("[publish] nothing ready to publish")
        return None

    video_id = _video_id(job)
    if has_yt():
        _go_public(video_id, job)
        _post_extra_credit(video_id, job)
    else:
        print("[publish] no creds / DRY_RUN -- marking %s published without network"
              % job.get("id"))

    job["published"] = True
    job["published_at"] = datetime.now(timezone.utc).isoformat()
    write_job(path, job)
    print("[publish] %s published (video %s)" % (job.get("id"), video_id))
    return job


if __name__ == "__main__":
    publish_one()
