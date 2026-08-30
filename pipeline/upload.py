"""YouTube upload for SCALED — the one door to the Data API, and it fails soft.

Nothing here crashes a run. The pipeline uploads, thumbnails and publishes on a
best-effort basis: when the three OAuth env vars are not all present, or when
DRY_RUN is set, every network action becomes a logged no-op that returns None
(or a harmless False), and the caller advances its state exactly as it would
have on success. has_yt() is the single gate the rest of PART A reads, so a run
missing a credential degrades in one predictable place instead of ten.

The google-api-python-client / google-auth packages are imported LAZILY inside
the functions that touch the network, so this module imports cleanly on a box
where they were never pip-installed and in every DRY_RUN.
"""
import os
import re

from pipeline import adapters, llm

# The OAuth env vars that must ALL be present (and non-empty) before we touch
# the network. Secrets live only as these names; a literal token never appears.
_YT_VARS = ("YT_REFRESH_TOKEN", "YT_CLIENT_ID", "YT_CLIENT_SECRET")
_TRUTHY = {"1", "true", "yes"}

TOKEN_URI = "https://oauth2.googleapis.com/token"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
# 27 = "Education" in the YouTube category list; the natural home for a lecture.
DEFAULT_CATEGORY = "27"


def _dry_run():
    return (os.getenv("DRY_RUN") or "").strip().lower() in _TRUTHY


def has_yt():
    """True iff a live YouTube call is both possible and permitted.

    All three OAuth env vars present and non-empty AND DRY_RUN not truthy. This
    is the gate every network path in PART A checks first; when it is False the
    action is a logged no-op and the caller still advances.
    """
    if _dry_run():
        return False
    return all((os.getenv(v) or "").strip() for v in _YT_VARS)


# --------------------------------------------------------------- api client

def _service(kind="youtube"):
    """A Google API client built from the refresh token. Lazy imports.

    kind "youtube"   -> ("youtube", "v3")
    kind "analytics" -> ("youtubeAnalytics", "v2")
    """
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        token_uri=TOKEN_URI,
        scopes=SCOPES,
    )
    name, version = {
        "youtube": ("youtube", "v3"),
        "analytics": ("youtubeAnalytics", "v2"),
    }[kind]
    return build(name, version, credentials=creds, cache_discovery=False)


# --------------------------------------------------------------- helpers

def _lesson_num(job):
    """Zero-pad source: job['lesson'] or job['n'], defaulting to 1."""
    for key in ("lesson", "n"):
        try:
            return int(job.get(key))
        except (TypeError, ValueError):
            continue
    return 1


def _shock_word(job):
    """A short, shocking word or number for the thumbnail's one giant token.

    An explicit job field wins; otherwise the first number in the packaging,
    otherwise the first substantial word, otherwise the channel name.
    """
    for key in ("shock_word", "word", "hook_word"):
        val = str(job.get(key) or "").strip()
        if val:
            return val
    text = " ".join(str(job.get(k) or "") for k in ("title", "topic"))
    num = re.search(r"\d[\d,\.]*%?", text)
    if num:
        return num.group(0)
    words = [w for w in re.findall(r"[A-Za-z]+", text) if len(w) >= 4]
    return words[0].upper() if words else "SCALED"


# --------------------------------------------------------------- actions

def upload_video(job, mp4_path, privacy="private"):
    """Resumable upload of mp4_path. Returns the new videoId, or None.

    None means DRY_RUN / missing credentials / a soft failure — the caller
    treats every one the same way and moves on.
    """
    if not has_yt():
        print("[upload] no creds / DRY_RUN -- not uploading %s" % mp4_path)
        return None
    from googleapiclient.http import MediaFileUpload

    body = {
        "snippet": {
            "title": (str(job.get("title") or "SCALED"))[:100],
            "description": str(job.get("description") or ""),
            "tags": list(job.get("tags") or []),
            "categoryId": str(job.get("categoryId") or DEFAULT_CATEGORY),
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    try:
        svc = _service("youtube")
        media = MediaFileUpload(mp4_path, mimetype="video/*", resumable=True)
        request = svc.videos().insert(part="snippet,status", body=body, media_body=media)
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                print("[upload] %d%% uploaded" % int(status.progress() * 100))
        vid = response.get("id")
        print("[upload] %s live as %s (%s)" % (job.get("id"), vid, privacy))
        return vid
    except Exception as e:
        print("[upload] upload failed: %s -- returning None" % e)
        return None


def make_thumb(job, work_dir):
    """Render the 1280x720 thumbnail via the image API. Returns its path or None.

    Offline-safe on purpose: this is only an image call, so it runs even in
    DRY_RUN. The adapters call is guarded, so a down image provider degrades to
    None (a video with no custom thumbnail) instead of a crash.
    """
    subject = job.get("title") or job.get("topic") or "the specimen"
    prompt = (
        llm.prompt("THUMBNAIL", "user")
        .replace("{subject}", str(subject))
        .replace("{word}", str(_shock_word(job)))
        .replace("{NNN}", "%03d" % _lesson_num(job))
    )
    try:
        data = adapters.image(prompt, "1280x720")
    except Exception as e:
        print("[upload] thumbnail generation failed: %s -- returning None" % e)
        return None
    if not data:
        print("[upload] thumbnail came back empty -- returning None")
        return None
    os.makedirs(work_dir or ".", exist_ok=True)
    out = os.path.join(work_dir or ".", "thumb_%s.jpg" % (job.get("id") or "job"))
    with open(out, "wb") as fh:
        fh.write(data)
    print("[upload] thumbnail -> %s" % out)
    return out


def set_thumbnail(video_id, thumb_path):
    """Attach a rendered thumbnail to a video. False (no-op) in DRY_RUN."""
    if not has_yt():
        print("[upload] no creds / DRY_RUN -- not setting thumbnail on %s" % video_id)
        return False
    from googleapiclient.http import MediaFileUpload

    if not (video_id and thumb_path and os.path.exists(thumb_path)):
        print("[upload] set_thumbnail: missing video id or file -- skipped")
        return False
    try:
        svc = _service("youtube")
        svc.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(thumb_path)).execute()
        print("[upload] thumbnail set on %s" % video_id)
        return True
    except Exception as e:
        print("[upload] set_thumbnail failed: %s" % e)
        return False
