"""Comment replies for SCALED — Professor Croc works the comments in character.

For the most recently published videos, walk the top-level comment threads and
reply, once, to any comment the channel has not already answered. The reply is
drafted by the COMMENT prompt (PART F), which knows the episode and answers in
Croc's deadpan voice — and is told to return exactly "SKIP" for hate, trolls
and spam, which we honour by posting nothing.

Fully guarded and fail-soft: when has_yt() is False (no creds, or DRY_RUN) this
is a no-op that returns 0, and any single API or LLM hiccup skips one comment
rather than sinking the run. The google client is imported lazily via
upload._service, so this module imports fine without google installed.
"""
from pipeline import llm
from pipeline.publish import published_jobs
from pipeline.upload import has_yt

# One page of threads is plenty for a young video; we do not paginate the
# firehose of an old viral one — the freshest comments are what earn a reply.
_MAX_THREADS = 50


def _scene_text(job):
    """The episode's spoken content, one scene per line, for the COMMENT prompt."""
    parts = []
    for scene in job.get("scenes") or []:
        if isinstance(scene, dict):
            text = str(scene.get("text") or "").strip()
            if text:
                parts.append(text)
    return "\n".join(parts)


def _video_id(job):
    return job.get("videoId") or job.get("video_id") or job.get("youtube_id")


def _our_channel(svc):
    """This channel's id, so we can tell our own replies from a viewer's."""
    try:
        resp = svc.channels().list(part="id", mine=True).execute()
        items = resp.get("items") or []
        if items:
            return items[0]["id"]
    except Exception as e:
        print("[comments] could not resolve own channel: %s" % e)
    return None


def _has_author_reply(thread, me):
    """True when the channel has already replied in this thread."""
    if not me:
        return False
    for reply in (thread.get("replies") or {}).get("comments", []):
        author = (reply.get("snippet") or {}).get("authorChannelId") or {}
        if author.get("value") == me:
            return True
    return False


def _draft_reply(title, scenes, author, comment):
    """Render the COMMENT prompt and ask the creative chain. None on LLM failure."""
    user = (
        llm.prompt("COMMENT", "user")
        .replace("{title}", str(title))
        .replace("{scenes}", scenes)
        .replace("{author}", str(author))
        .replace("{comment}", str(comment))
    )
    try:
        return llm.llm(llm.prompt("COMMENT", "system"), user)
    except Exception as e:
        print("[comments] draft failed: %s" % e)
        return None


def _reply_to_video(svc, job, video_id, me):
    """Reply to the unanswered comments on one video. Returns replies posted."""
    posted = 0
    title = job.get("title") or ""
    scenes = _scene_text(job)
    try:
        resp = svc.commentThreads().list(
            part="snippet,replies", videoId=video_id, maxResults=_MAX_THREADS,
            order="time", textFormat="plainText",
        ).execute()
    except Exception as e:
        print("[comments] fetching threads for %s failed: %s" % (video_id, e))
        return 0

    for thread in resp.get("items", []):
        try:
            top = thread["snippet"]["topLevelComment"]
            snippet = top["snippet"]
        except (KeyError, TypeError):
            continue
        if _has_author_reply(thread, me):
            continue
        author = snippet.get("authorDisplayName") or "a viewer"
        comment = snippet.get("textOriginal") or snippet.get("textDisplay") or ""
        reply = _draft_reply(title, scenes, author, comment)
        if reply is None:
            continue
        if reply.strip() == "SKIP":
            print("[comments] SKIP (troll/spam/praise) from %s" % author)
            continue
        try:
            svc.comments().insert(
                part="snippet",
                body={"snippet": {"parentId": top["id"], "textOriginal": reply.strip()}},
            ).execute()
            posted += 1
            print("[comments] replied to %s on %s" % (author, video_id))
        except Exception as e:
            print("[comments] posting reply failed: %s" % e)
    return posted


def run_comments(videos_dir="data/videos", max_videos=5):
    """Reply to new comments on the most recent videos. Returns replies posted.

    A full no-op returning 0 when has_yt() is False.
    """
    if not has_yt():
        print("[comments] no creds / DRY_RUN -- skipping comment replies")
        return 0

    try:
        from pipeline.upload import _service
        svc = _service("youtube")
    except Exception as e:
        print("[comments] could not build client: %s -- 0 replies" % e)
        return 0

    me = _our_channel(svc)
    if not me:
        print("[comments] own channel unknown -- proceeding, may re-reply")

    jobs = published_jobs(videos_dir)[: max(0, int(max_videos))]
    posted = 0
    for job, _path in jobs:
        video_id = _video_id(job)
        if not video_id:
            print("[comments] %s has no video id -- skipped" % job.get("id"))
            continue
        posted += _reply_to_video(svc, job, video_id, me)

    print("[comments] posted %d replies across %d videos" % (posted, len(jobs)))
    return posted


if __name__ == "__main__":
    run_comments()
