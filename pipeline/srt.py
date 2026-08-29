"""Word-level timing from Groq whisper-large-v3-turbo.

Whisper is the ONLY clock in this factory (C4): scene durations, croc lip-sync,
caption highlighting and stat-card timing all derive from the word list this
module returns. Nothing else is allowed to invent a timestamp.

VERIFY-ON-FIRST-RUN #4 — the verbose_json word payload comes back in one of two
shapes depending on API version:

    {"segments": [{"words": [{"word": "Your", "start": .., "end": ..}, ...]}]}
    {"words":    [{"word": "Your", "start": .., "end": ..}, ...]}

normalize_words() accepts both, prefers the flat top-level list, and logs which
shape it saw so the first real run tells us the truth instead of us guessing.
"""
import os

import requests

ENDPOINT = "https://api.groq.com/openai/v1/audio/transcriptions"
MODEL = "whisper-large-v3-turbo"
TIMEOUT = 600

# Set by normalize_words() on first use; printed for the VERIFY register.
WORD_SHAPE = None


def _one(item):
    """Map a raw whisper word dict to our {"w","s","e"} shape, or None."""
    if not isinstance(item, dict):
        return None
    text = item.get("word", item.get("text", ""))
    text = (text or "").strip()
    if not text:
        return None
    try:
        s = round(float(item.get("start", 0.0)), 3)
        e = round(float(item.get("end", item.get("start", 0.0))), 3)
    except (TypeError, ValueError):
        return None
    return {"w": text, "s": s, "e": e}


def normalize_words(data):
    """Pull a flat word list out of either verbose_json shape."""
    global WORD_SHAPE
    words = []

    flat = data.get("words") if isinstance(data, dict) else None
    if isinstance(flat, list) and flat:
        for item in flat:
            w = _one(item)
            if w:
                words.append(w)
        if words:
            WORD_SHAPE = "flat words[]"
            print("[srt] VERIFY-ON-FIRST-RUN #4: whisper returned flat words[] (%d words)" % len(words))
            return words

    segs = data.get("segments") if isinstance(data, dict) else None
    if isinstance(segs, list):
        for seg in segs:
            if not isinstance(seg, dict):
                continue
            for item in seg.get("words") or []:
                w = _one(item)
                if w:
                    words.append(w)
        if words:
            WORD_SHAPE = "segments[].words[]"
            print("[srt] VERIFY-ON-FIRST-RUN #4: whisper returned segments[].words[] (%d words)" % len(words))
            return words

    WORD_SHAPE = "unrecognised"
    print("[srt] VERIFY-ON-FIRST-RUN #4: no words in either shape; keys=%s"
          % (sorted(data.keys()) if isinstance(data, dict) else type(data).__name__))
    return []


def word_timeline(mp3_path):
    """Transcribe one narration file into [{"w","s","e"}, ...] absolute-in-clip."""
    key = (os.getenv("GROQ_KEY") or "").strip()
    if not key:
        raise RuntimeError("GROQ_KEY missing")

    with open(mp3_path, "rb") as fh:
        r = requests.post(
            ENDPOINT,
            headers={"Authorization": "Bearer " + key},
            files={"file": (os.path.basename(mp3_path), fh, "application/octet-stream")},
            data={
                "model": MODEL,
                "response_format": "verbose_json",
                "timestamp_granularities[]": "word",
            },
            timeout=TIMEOUT,
        )
    if r.status_code != 200:
        raise RuntimeError("whisper HTTP %s: %s" % (r.status_code, r.text[:300]))

    words = normalize_words(r.json())
    if not words:
        raise RuntimeError("whisper returned no words for %s" % mp3_path)
    return words
