"""Voice + image providers, driven entirely by config/apis.json.

Nothing about a specific vendor is hard-coded here. config/apis.json describes
the request (base, path, method, payload, headers) and the response shape
(`returns`), and this module executes it. Swapping providers is a config edit.

Two response shapes are supported, named by `returns`:

  "*-bytes"   the HTTP body IS the media          (mp3-bytes, jpeg-bytes, ...)
  "json-url"  the body is JSON holding a URL to the media, which is then
              fetched with GET; `url_field` is the path to it ("url",
              "urls.0", "data.0.b64" style dotted/indexed access). A relative
              URL is resolved against `base`.

Whatever comes back is sniffed against real file magic. The caller gets bytes
plus a format hint via last_format(); run.py normalises to mp3/jpg with ffmpeg,
so a provider that speaks WAV or WebP is fine.
"""
import json
import os
import time

import requests

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "apis.json")
TIMEOUT = 600
RETRIES = 5

# The Bible's default file content. Written to config/apis.json when that file
# is missing, and the shape any replacement must keep.
DEFAULT_CONFIG = {
    "voice": {"base": "", "path": "/tts", "payload": {"text": "{text}", "voice_id": "{voice_id}"},
              "returns": "mp3-bytes", "voice_id": ""},
    "image": {"base": "", "path": "/image", "payload": {"prompt": "{prompt}", "size": "{size}"},
              "returns": "jpeg-bytes"},
}

MAGIC = [
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF8", "gif"),
    (b"ID3", "mp3"),
    (b"\xff\xfb", "mp3"), (b"\xff\xf3", "mp3"), (b"\xff\xf2", "mp3"), (b"\xff\xe3", "mp3"),
    (b"OggS", "ogg"),
    (b"fLaC", "flac"),
]
AUDIO_FORMATS = {"mp3", "wav", "ogg", "flac", "m4a"}
IMAGE_FORMATS = {"jpg", "png", "gif", "webp"}

_LAST_FORMAT = {"kind": None}


def load_config():
    """config/apis.json, creating it from DEFAULT_CONFIG when absent."""
    if not os.path.exists(CONFIG_PATH):
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(DEFAULT_CONFIG, fh, indent=2)
        print("[adapters] wrote default config/apis.json — bases are empty, edit it")
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        return json.load(fh)


CONFIG = load_config()


def sniff(data):
    """File-format name from magic bytes, or None when unrecognised."""
    if not data or len(data) < 4:
        return None
    for magic, name in MAGIC:
        if data.startswith(magic):
            return name
    if data[:4] == b"RIFF" and len(data) >= 12:
        if data[8:12] == b"WAVE":
            return "wav"
        if data[8:12] == b"WEBP":
            return "webp"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "m4a"
    return None


def last_format(kind=None):
    """Format of the most recent successful fetch ('wav', 'webp', ...)."""
    return _LAST_FORMAT.get(kind or "kind")


def _substitute(value, subs):
    """Replace {key} placeholders inside a payload value, at any nesting depth."""
    if isinstance(value, str):
        out = value
        for k, v in subs.items():
            token = "{%s}" % k
            if out == token:          # whole value is the placeholder: keep type
                return v
            if token in out:
                out = out.replace(token, str(v))
        return out
    if isinstance(value, dict):
        return {k: _substitute(v, subs) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, subs) for v in value]
    return value


def _dig(data, path):
    """Walk a dotted/indexed path through nested JSON: 'urls.0' -> data['urls'][0]."""
    cur = data
    for part in str(path).replace("[", ".").replace("]", "").split("."):
        if part == "":
            continue
        if isinstance(cur, list):
            cur = cur[int(part)]
        else:
            cur = cur[part]
    return cur


def _headers(cfg):
    headers = {"Content-Type": "application/json"}
    headers.update(cfg.get("headers") or {})
    key = (os.getenv(cfg.get("key_env", "SERVICE_KEY")) or "").strip()
    if key:
        scheme = cfg.get("auth_scheme", "Bearer")
        headers["Authorization"] = "%s %s" % (scheme, key) if scheme else key
        if cfg.get("key_header"):
            headers[cfg["key_header"]] = key
    return headers


def _proxies(cfg):
    # Some hosts sit behind a workspace tunnel that a shell HTTP_PROXY breaks.
    return {"http": None, "https": None} if cfg.get("no_proxy") else None


def _fetch_media(cfg, subs, kind):
    """One attempt. Returns bytes on success, raises on anything else."""
    base = (cfg.get("base") or "").strip().rstrip("/")
    payload = _substitute(cfg.get("payload") or {}, subs)
    url = base + cfg.get("path", "")
    r = requests.post(url, headers=_headers(cfg), json=payload,
                      timeout=TIMEOUT, proxies=_proxies(cfg))
    if r.status_code != 200:
        raise RuntimeError("%s HTTP %s: %s" % (kind, r.status_code, r.text[:300]))

    returns = (cfg.get("returns") or "").lower()
    data = r.content

    if returns == "json-url":
        try:
            doc = r.json()
        except Exception:
            raise RuntimeError("%s expected JSON, got: %s" % (kind, r.text[:300]))
        if isinstance(doc, dict) and doc.get("error"):
            raise RuntimeError("%s provider error: %s" % (kind, str(doc["error"])[:300]))
        try:
            media_url = _dig(doc, cfg.get("url_field", "url"))
        except Exception:
            raise RuntimeError("%s: no media URL at %r in %s"
                               % (kind, cfg.get("url_field", "url"), json.dumps(doc)[:300]))
        if not media_url:
            raise RuntimeError("%s: empty media URL in %s" % (kind, json.dumps(doc)[:300]))
        if str(media_url).startswith("/"):
            media_url = base + str(media_url)
        g = requests.get(media_url, timeout=TIMEOUT, proxies=_proxies(cfg),
                         headers=cfg.get("download_headers") or None)
        if g.status_code != 200:
            raise RuntimeError("%s download HTTP %s: %s" % (kind, g.status_code, g.text[:300]))
        data = g.content

    fmt = sniff(data)
    allowed = AUDIO_FORMATS if kind == "voice" else IMAGE_FORMATS
    if fmt not in allowed:
        head = data[:300]
        try:
            head = head.decode("utf-8", "replace")
        except Exception:
            head = repr(head)
        raise RuntimeError("%s returned %s, not %s media. Body starts: %s"
                           % (kind, fmt or "unrecognised bytes", kind, head))
    _LAST_FORMAT["kind"] = fmt
    _LAST_FORMAT[kind] = fmt
    return data


def _call(cfg, subs, kind="voice"):
    """POST the configured request, retrying transient failures 5 times."""
    if not (cfg.get("base") or "").strip():
        raise RuntimeError("voice/image API base URL not configured — edit config/apis.json")
    last = None
    for i in range(RETRIES):
        try:
            data = _fetch_media(cfg, subs, kind)
            print("[adapters] %s ok: %d bytes (%s)" % (kind, len(data), _LAST_FORMAT[kind]))
            return data
        except Exception as e:
            last = e
            print("[adapters] %s attempt %d/%d failed: %s" % (kind, i + 1, RETRIES, e))
            if i < RETRIES - 1:
                nap = min(10 * 2 ** i, 90)
                print("[adapters] retrying in %ds" % nap)
                time.sleep(nap)
    raise RuntimeError("%s provider failed after %d attempts: %s" % (kind, RETRIES, last))


def tts(text):
    """Narration audio for one scene. Bytes; format via last_format('voice')."""
    cfg = CONFIG.get("voice") or {}
    return _call(cfg, {"text": text, "voice_id": cfg.get("voice_id", "")}, kind="voice")


def image(prompt, size="1920x1080"):
    """One still. Bytes; format via last_format('image')."""
    cfg = CONFIG.get("image") or {}
    w, h = (size.split("x") + ["", ""])[:2]
    return _call(cfg, {"prompt": prompt, "size": size, "width": w, "height": h}, kind="image")
