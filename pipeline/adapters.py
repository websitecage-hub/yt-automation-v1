"""Voice + image providers, driven entirely by config/apis.json.

Nothing about a specific vendor is hard-coded here. config/apis.json describes
the request (base, path, method, payload, headers) and the response shape
(`returns`), and this module executes it. Swapping providers is a config edit.

Three response shapes are supported, named by `returns`:

  "*-bytes"   the HTTP body IS the media          (mp3-bytes, jpeg-bytes, ...)
  "json-url"  the body is JSON holding a URL to the media, which is then
              fetched with GET; `url_field` is the path to it ("url",
              "urls.0", "data.0.b64" style dotted/indexed access). A relative
              URL is resolved against `base`.
  "mcp-tool"  the host is an MCP server: the request is wrapped in a JSON-RPC
              `tools/call` envelope naming `tool`, and the media comes back as
              a base64 content block (or, failing that, as a URL in the text
              block, which is then fetched).

Two request shapes, chosen by which key the config uses:

  "payload"   a JSON body (the default)
  "form"      multipart/form-data, with `files` naming repo-relative uploads --
              which is what a zero-shot voice cloner needs, since the reference
              recording has to ride along with every request.

Whatever comes back is sniffed against real file magic. The caller gets bytes
plus a format hint via last_format(); run.py normalises to mp3/jpg with ffmpeg,
so a provider that speaks WAV or WebP is fine.
"""
import base64
import hashlib
import json
import os
import re
import time

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(REPO, "config", "apis.json")
MEME_DIR = os.path.join(REPO, "assets", "memes")
TIMEOUT = 600
RETRIES = 5

# Direct REST image endpoint on the same media host (docs: POST /api/image
# {prompt, mode, project_id, timeout} -> {urls[], error}). `mode` "thinking" buys
# the host's slow high-quality pass; "instant" is the fast default. Used for
# thumbnails (quality matters, one call per episode) while beat stills stay on
# the MCP path (proven 24/24, 4 workers). Keyless, same as /mcp.
IMAGE_REST_PATH = "/api/image"
UPLOAD_PATH = "/api/upload"

# PART C4 v3 -- the SPECIMEN look. Appended by Python to every art call: the
# script LLM supplies only a SUBJECT, so consecutive episodes cannot drift into
# different-looking art. That drift is what reads as "AI slop"; one locked style
# is the whole fix.
#
# v3 replaced v2's flat-vector lock outright. v2 asked for "flat 2D technical
# illustration, thick clean outlines, limited palette of electric blue / neon
# green / yellow" and the host obliged perfectly -- which was the problem. It
# returns neon outline clip-art: a cyan crocodile skull on black, no texture, no
# light, no depth. Cheap, generated, and instantly recognisable as such.
#
# The reference thumbnails (reference/image copy 3.png, image copy 5.png) are the
# opposite and are what the show is being aimed at: a photoreal subject isolated
# on black, one hard rim light, near-monochrome with a SINGLE accent, and faint
# scientific linework in the empty space. "Minimal composition, complex image" --
# the frame is nearly empty, the subject is dense with real detail. So the lock
# now demands the detail and forbids, by name, every flat-art escape hatch the
# host reached for last time.
STYLE_BLOCK = (
    "STYLE: hyper-detailed cinematic macro photograph on a pure black #000000 "
    "background, museum specimen lighting, ONE subject isolated with wide empty "
    "black negative space around it, hard single-source rim light raking across "
    "the subject from behind, deep crushed shadows, near-monochrome desaturated "
    "charcoal and bone greys with a SINGLE electric cyan #00D1FF accent glinting "
    "on the edges, extreme micro-detail and surface texture, shallow depth of "
    "field, faint volumetric dust in the light beam, subtle low-opacity white "
    "scientific schematic linework and measurement ticks drawn into the black "
    "space around the subject, shot on 85mm at f/2.8, high dynamic range, "
    "photographic, minimal composition."
)
STYLE_NEGATIVE = (
    "NEGATIVE: no text, no letters, no numbers, no labels, no UI elements, no "
    "watermarks, no flat vector art, no cartoon, no anime, no line drawing, no "
    "clip art, no neon outlines, no coloring book outlines, no illustration, no "
    "white background, no grey background, no busy background, no collage, no "
    "split frames, no borders, no vignette frames, no rainbow palette."
)
# v4 "one world" lock. The video body is ONE flat cartoon universe -- the same
# off-white world, the same faces, every frame. This replaced the v2/v3 split
# brain (photoreal specimen base + cartoon memes + HTML text stamps fighting on
# screen), which read as three styles at war. Words live INSIDE the pictures:
# the host spells short display copy correctly (proven on thumbnails), so no
# Python-side text overlay exists anymore for the body of the video.
WORLD_LOCK = (
    "Flat cartoon, thick black outlines, solid flat colors, plain off-white "
    "background, simple crooked figures with dot eyes, hand-lettered labels, "
    "children's educational cartoon meets meme page, deliberately simple, "
    "no shading, no gradients, no photorealism, no 3d, no watermark."
)
WORLD_NEGATIVE = (
    "NEGATIVE: no photorealism, no 3d render, no shading, no gradients, "
    "no photograph, no dark background, no cinematic lighting, no gore, "
    "no watermark, no logo."
)

# Baked text is short or it isn't baked: the host holds ~4 words and one
# number per frame reliably (thumbnail track record). Anything longer belongs
# in the voiceover, not on screen.
MAX_BAKED_WORDS = 4
# PART C5 v3. Reaction images are GENERATED, never downloaded -- a real meme frame
# is someone else's copyright and would put the channel at risk.
#
# The reference edit (Casually Explained) gets its laugh from JUXTAPOSITION: a
# deliberately mundane real photograph dropped into an otherwise stylised video.
# So the meme beat is deliberately NOT the show's look -- a flat, badly-lit,
# straight-faced snapshot cutting into the black cinematic art is the joke. v2's
# "rough meme comic, flat colors" just produced more of the same clip-art.
MEME_STYLE = (
    "shot as a mundane real photograph, deadpan absurd situation played "
    "completely straight, plain everyday background, flat boring on-camera flash "
    "lighting, slightly cheap amateur snapshot quality, subject caught mid-action "
    "with a ridiculous straight face, photographic, no caption text"
)

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
MEDIA_SUFFIX = tuple("." + e for e in sorted(AUDIO_FORMATS | IMAGE_FORMATS | {"jpeg"}))

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
    # No Content-Type for multipart: requests must set it itself, because only it
    # knows the boundary string it generated.
    headers = {} if cfg.get("form") else {"Content-Type": "application/json"}
    headers.update(cfg.get("headers") or {})
    key = (os.getenv(cfg.get("key_env", "SERVICE_KEY")) or "").strip()
    if key:
        scheme = cfg.get("auth_scheme", "Bearer")
        headers["Authorization"] = "%s %s" % (scheme, key) if scheme else key
        if cfg.get("key_header"):
            headers[cfg["key_header"]] = key
    return headers


def _resolve(path):
    """A config file reference -> an absolute path inside the repo."""
    path = str(path or "")
    return path if os.path.isabs(path) else os.path.join(REPO, path)


def _request_kwargs(cfg, subs, opened):
    """The requests.post keyword arguments this config describes.

    `opened` collects file handles the caller must close. Three shapes: an MCP
    JSON-RPC envelope, a multipart form, or a plain JSON body. The JSON-RPC id is
    a constant -- these calls are one-shot, and a counter would make otherwise
    identical requests differ between runs for no reason.
    """
    if (cfg.get("returns") or "").lower() == "mcp-tool":
        args = _substitute(cfg.get("payload") or {}, subs)
        return {"json": {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": cfg.get("tool") or "", "arguments": args}}}
    if cfg.get("form"):
        data = {k: str(v) for k, v in _substitute(cfg.get("form"), subs).items()}
        files = {}
        for field, ref in (cfg.get("files") or {}).items():
            path = _resolve(_substitute(ref, subs))
            if not os.path.exists(path):
                raise RuntimeError("upload %r for field %r is missing" % (path, field))
            fh = open(path, "rb")
            opened.append(fh)
            files[field] = (os.path.basename(path), fh, "application/octet-stream")
        return {"data": data, "files": files or None}
    return {"json": _substitute(cfg.get("payload") or {}, subs)}


def _proxies(cfg):
    # Some hosts sit behind a workspace tunnel that a shell HTTP_PROXY breaks.
    return {"http": None, "https": None} if cfg.get("no_proxy") else None


def _mcp_media(doc, kind):
    """(bytes, url) out of an MCP tools/call result: inline base64 wins.

    The inline block is preferred over the CDN link in the text -- it is already
    in hand, cannot expire, and needs no second round trip.
    """
    if not isinstance(doc, dict):
        raise RuntimeError("%s: MCP returned %s, not an object" % (kind, type(doc).__name__))
    if doc.get("error"):
        raise RuntimeError("%s: MCP error %s" % (kind, str(doc["error"])[:300]))
    result = doc.get("result") if isinstance(doc.get("result"), dict) else doc
    blocks = result.get("content") or []
    if result.get("isError"):
        texts = " ".join(str(b.get("text") or "") for b in blocks if isinstance(b, dict))
        raise RuntimeError("%s: MCP tool failed: %s" % (kind, texts[:300] or "no detail"))

    want = "audio" if kind == "voice" else "image"
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != want:
            continue
        try:
            return base64.b64decode(block.get("data") or "", validate=False), None
        except (ValueError, TypeError) as e:
            raise RuntimeError("%s: MCP %s block was not base64 (%s)" % (kind, want, e))

    # No inline block: fall back to the first plausible media URL in the text.
    text = " ".join(str(b.get("text") or "") for b in blocks if isinstance(b, dict))
    for match in re.finditer(r"https?://[^\s)\]<>\"']+", text):
        url = match.group(0).rstrip(".,")
        if url.lower().rsplit("?", 1)[0].endswith(MEDIA_SUFFIX):
            return None, url
    raise RuntimeError("%s: MCP result had no %s block and no media URL. Text: %s"
                       % (kind, want, text[:300] or "(empty)"))


def _download(cfg, base, media_url, kind):
    """GET a media URL the provider handed back. Relative URLs resolve on `base`."""
    if str(media_url).startswith("/"):
        media_url = base + str(media_url)
    g = requests.get(media_url, timeout=TIMEOUT, proxies=_proxies(cfg),
                     headers=cfg.get("download_headers") or None)
    if g.status_code != 200:
        raise RuntimeError("%s download HTTP %s: %s" % (kind, g.status_code, g.text[:300]))
    return g.content


def _fetch_media(cfg, subs, kind):
    """One attempt. Returns bytes on success, raises on anything else."""
    base = (cfg.get("base") or "").strip().rstrip("/")
    url = base + cfg.get("path", "")
    opened = []
    try:
        kwargs = _request_kwargs(cfg, subs, opened)
        r = requests.post(url, headers=_headers(cfg), timeout=TIMEOUT,
                          proxies=_proxies(cfg), **kwargs)
    finally:
        for fh in opened:
            fh.close()
    if r.status_code != 200:
        raise RuntimeError("%s HTTP %s: %s" % (kind, r.status_code, r.text[:300]))

    returns = (cfg.get("returns") or "").lower()
    data = r.content

    if returns == "mcp-tool":
        try:
            doc = r.json()
        except Exception:
            raise RuntimeError("%s expected JSON-RPC, got: %s" % (kind, r.text[:300]))
        data, media_url = _mcp_media(doc, kind)
        if data is None:
            data = _download(cfg, base, media_url, kind)

    elif returns == "json-url":
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
        data = _download(cfg, base, media_url, kind)

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


def _voice_engine():
    """Which TTS backend: "local" (default, runner CPU, no key) or "api".

    Set in config/apis.json (`voice.engine`) or via SCALED_VOICE. The API path
    is kept as a fallback for boxes that cannot run torch.
    """
    cfg = CONFIG.get("voice") or {}
    return str(cfg.get("engine") or os.getenv("SCALED_VOICE") or "local").strip().lower()


def tts(text):
    """Narration audio for one scene. Bytes; format via last_format('voice')."""
    if _voice_engine() == "local":
        from pipeline import voice_local
        data, _sr = voice_local.clone(text)
        _LAST_FORMAT["kind"] = "wav"
        _LAST_FORMAT["voice"] = "wav"
        print("[adapters] voice ok: %d bytes (wav, local clone)" % len(data))
        return data
    cfg = CONFIG.get("voice") or {}
    return _call(cfg, {"text": text, "voice_id": cfg.get("voice_id", "")}, kind="voice")


def image(prompt, size="1920x1080"):
    """One still. Bytes; format via last_format('image')."""
    cfg = CONFIG.get("image") or {}
    w, h = (size.split("x") + ["", ""])[:2]
    return _call(cfg, {"prompt": prompt, "size": size, "width": w, "height": h}, kind="image")


# ------------------------------------------------------------ the style lock

def styled_prompt(subject, split_negative=False):
    """Subject + the C4 style lock -> (prompt, negative).

    `negative` is empty unless the API takes its own negative field, in which
    case the NEGATIVE half moves there. Appending is idempotent: a subject that
    already carries the lock (a retry, a prompt round-tripped through state)
    gets it stripped first, because a doubled style block doubles its weight.
    """
    subject = " ".join(str(subject or "").split())
    for chunk in (STYLE_BLOCK, STYLE_NEGATIVE):
        subject = subject.replace(chunk, " ")
    subject = " ".join(subject.split()).strip().rstrip(".")
    if not subject:
        subject = "an abstract diagram of a biological system"
    subject = subject[:400].rstrip().rstrip(".")
    if split_negative:
        return "%s. %s" % (subject, STYLE_BLOCK), STYLE_NEGATIVE
    return "%s. %s %s" % (subject, STYLE_BLOCK, STYLE_NEGATIVE), ""


def image_styled(prompt, size="1920x1080"):
    """A beat's art, with the C4 lock applied. Bytes.

    VERIFY-ON-FIRST-RUN: config/apis.json may declare `"negative_prompt": true`
    for a provider with a real negative field (name it with `negative_field`);
    otherwise the NEGATIVE half rides in the prompt. Both paths are implemented
    and the one taken is logged.
    """
    cfg = dict(CONFIG.get("image") or {})
    split = bool(cfg.get("negative_prompt"))
    text, negative = styled_prompt(prompt, split)
    if split:
        field = str(cfg.get("negative_field") or "negative_prompt")
        payload = dict(cfg.get("payload") or {})
        payload[field] = "{negative}"
        cfg["payload"] = payload
        print("[adapters] image API declares a negative field (%s) -- NEGATIVE sent separately"
              % field)
    else:
        print("[adapters] image API declares no negative field -- NEGATIVE kept in-prompt")
    w, h = (str(size).split("x") + ["", ""])[:2]
    return _call(cfg, {"prompt": text, "negative": negative, "size": size,
                       "width": w, "height": h}, kind="image")


def meme_prompt(subject):
    """Subject + the C5 meme style block, appended exactly once."""
    subject = " ".join(str(subject or "").split()).replace(MEME_STYLE, " ")
    subject = " ".join(subject.split()).strip().rstrip(",").rstrip(".")
    if not subject:
        subject = "a crocodile in a lab coat looking deeply unimpressed"
    return "%s, %s" % (subject[:200], MEME_STYLE)


def meme_key(subject):
    """Cache filename stem: sha1 of the fully styled meme prompt."""
    return hashlib.sha1(meme_prompt(subject).encode("utf-8")).hexdigest()


def _cached_meme(key):
    """Bytes of an existing assets/memes/{key}.* file, or None. User files land
    here too, which is exactly how they take precedence over generation (C5)."""
    for ext in ("png", "jpg", "jpeg", "webp", "gif"):
        path = os.path.join(MEME_DIR, "%s.%s" % (key, ext))
        if not os.path.exists(path):
            continue
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as e:
            print("[adapters] meme %s unreadable (%s) -- regenerating" % (path, e))
            continue
        if data:
            print("[adapters] meme cache hit %s (%d bytes)" % (os.path.basename(path), len(data)))
            return data
    return None


def meme_img(subject):
    """A reaction image, generated once and then cached in git forever (C5).

    The cache is keyed by the prompt, so the same joke never costs a second API
    call, and an existing file is returned untouched -- never regenerated, never
    overwritten, which is what lets a user drop their own art in assets/memes/.
    """
    key = meme_key(subject)
    hit = _cached_meme(key)
    if hit is not None:
        return hit

    data = _call(dict(CONFIG.get("image") or {}),
                 {"prompt": meme_prompt(subject), "negative": "", "size": "1024x1024",
                  "width": "1024", "height": "1024"}, kind="image")

    dst = os.path.join(MEME_DIR, "%s.%s" % (key, last_format("image") or "png"))
    try:
        os.makedirs(MEME_DIR, exist_ok=True)
        if os.path.exists(dst):
            print("[adapters] %s appeared while generating -- keeping the file on disk" % dst)
            return data
        with open(dst, "wb") as fh:
            fh.write(data)
        print("[adapters] meme cached -> %s" % dst)
    except OSError as e:
        print("[adapters] could not cache the meme (%s) -- using it for this run only" % e)
    return data


# ------------------------------------------------- v3 Casually-native art calls

def world_prompt(situation, text="", value="", label=""):
    """A situation in the one world, with optional baked-in words.

    `text` (<=4 words caption), `value` (one number) + `label` (<=2 words) are
    drawn INTO the picture by the host as hand-lettered cartoon text. Empty
    means no text at all -- most frames should have none; the voice carries it.
    """
    situation = " ".join(str(situation or "").split()).strip().rstrip(".")
    if not situation:
        situation = "the guy staring at a wall graph that goes up"
    out = "%s. %s %s" % (situation[:300], WORLD_LOCK, WORLD_NEGATIVE)
    words = " ".join(str(text or "").split())[:40]
    number = " ".join(str(value or "").split())[:14]
    lab = " ".join(str(label or "").split()[:2])
    if number or words:
        show = ("a big hand-lettered cartoon number \"%s\" with tiny label \"%s\""
                % (number, lab) if number else
                "a short hand-lettered cartoon caption \"%s\"" % words)
        out += (" Drawn into the picture %s, correctly spelled, nothing else "
                "written anywhere." % show)
    return out


def image_world(situation, text="", value="", label="", size="1920x1080"):
    """One full video frame from the one world. Bytes."""
    w, h = (str(size).split("x") + ["", ""])[:2]
    return _call(dict(CONFIG.get("image") or {}),
                 {"prompt": world_prompt(situation, text, value, label),
                  "negative": "", "size": size, "width": w, "height": h},
                 kind="image")


def doodle_prompt(subject, speech=""):
    """Kept for back-compat; new code uses world_prompt via image_world."""
    return world_prompt(subject, text=speech)


def image_plain(prompt, size="1920x1080", caption="", counter=""):
    """A photo punch-in with NO style lock, words baked into the photo itself.

    `caption` (<=6 words) and `counter` (<=12 chars) are drawn onto the photo
    like a meme macro -- the host does this reliably at short lengths.
    """
    subject = " ".join(str(prompt or "").split())[:400] or "a crowd of people cheering"
    text = ("%s. Real candid photograph, natural light, photojournalistic, "
            "no illustration, no cartoon, no watermark." % subject)
    cap = " ".join(str(caption or "").split()[:6])
    cnt = " ".join(str(counter or "").split())[:12]
    if cap or cnt:
        show = " with bold white meme-macro text with black outline reading \"%s\"" % cap if cap else ""
        if cnt:
            show += (" and" if show else " with") + " a small overlay counter reading \"%s\"" % cnt
        text += " Drawn onto the photo%s, correctly spelled." % show
    w, h = (str(size).split("x") + ["", ""])[:2]
    return _call(dict(CONFIG.get("image") or {}),
                 {"prompt": text, "negative": "", "size": size,
                  "width": w, "height": h}, kind="image")


def image_doodle(prompt, speech="", size="1920x1080"):
    """Kept for back-compat; routes into the one world with baked speech."""
    return image_world(prompt, text=speech, size=size)


# --------------------------------- same-croc memes (upload consistency in action)
#
# Random crocs every meme is exactly the slurry feel the operator hates. The fix
# is api-usage.md's upload feature: assets/avatar/croc.png (the channel mascot,
# same file the video composites) is uploaded ONCE, the public URL is cached in
# assets/avatar/croc.url, and every croc meme is an edit_image OF that mascot --
# same character, new situation, every time. Falls back to text generation when
# the upload or the edit fails, so consistency never costs a meme.

AVATAR_REF = os.path.join(REPO, "assets", "avatar", "croc.png")
AVATAR_URL_CACHE = os.path.join(REPO, "assets", "avatar", "croc.url")


def avatar_ref_url():
    """Public URL of the mascot PNG, uploaded once and cached in git."""
    try:
        with open(AVATAR_URL_CACHE, encoding="utf-8") as fh:
            url = fh.read().strip()
        if url.startswith("http"):
            return url
    except OSError:
        pass
    url = media_upload(AVATAR_REF)
    try:
        with open(AVATAR_URL_CACHE, "w", encoding="utf-8") as fh:
            fh.write(url.strip() + "\n")
    except OSError as e:
        print("[adapters] could not cache avatar url (%s) -- re-upload next run" % e)
    return url


def meme_croc(situation):
    """The mascot IN a situation, via edit_image on the uploaded avatar.

    Raises on failure so the caller falls back to meme_img (text generation).
    """
    situation = " ".join(str(situation or "").split())[:200]
    if not situation:
        raise RuntimeError("meme_croc needs a situation")
    url = avatar_ref_url()
    return edit_image(
        url, "Keep this exact same green cartoon crocodile character -- same design, "
        "same face, same proportions. Place him into this situation, played "
        "completely straight like a deadpan snapshot: %s. Flat cartoon style, "
        "plain simple background, no caption text." % situation)


# ------------------------------------------------- the recurring cast (Memeic)
#
# "One Cast, One World": every joke visual is a re-staging of the same faces in
# the same world via edit_image, so the show reads as drawn by one cartoonist
# instead of generated fresh every beat. Cast files live in assets/cast/ with
# .url sidecars (same pattern as the avatar cache); tools/make_cast.py builds
# them once. Raises on any failure so callers fall back to text generation.

CAST_DIR = os.path.join(REPO, "assets", "cast")
CAST_WHO = ("croc", "guy", "world", "sheet")


def cast_url(who="guy"):
    """Public URL of a cast asset, from its committed .url sidecar."""
    who = str(who or "guy").strip().lower()
    if who not in CAST_WHO:
        who = "guy"
    if who == "croc":
        return avatar_ref_url()
    path = os.path.join(CAST_DIR, {"guy": "guy", "world": "world",
                                   "sheet": "cast_sheet"}.get(who, "guy") + ".url")
    try:
        with open(path, encoding="utf-8") as fh:
            url = fh.read().strip()
        if url.startswith("http"):
            return url
    except OSError:
        pass
    raise RuntimeError("cast %r has no cached url -- run tools/make_cast.py" % who)


def cast_edit(situation, who="guy"):
    """Re-stage the recurring cast: edit_image(cast_url, situation) -> bytes.

    The director never invents characters; every doodle/meme is THESE faces in
    a new humiliating situation. Raises so the caller falls back to text art.
    """
    situation = " ".join(str(situation or "").split())[:200]
    if not situation:
        raise RuntimeError("cast_edit needs a situation")
    return edit_image(
        cast_url(who), "Same characters, same flat cartoon style, same plain "
        "off-white world -- change ONLY the situation, played completely "
        "straight: %s. Thick black outlines, solid flat colors, no shading, "
        "no photorealism, no caption text." % situation)


# ------------------------------------------------- upload + consistency (media API v2)
#
# The media host grew an upload endpoint and an edit tool. Uploading an episode's
# own frame and editing FROM it (instead of generating every picture from text
# alone) is what keeps a recurring subject looking like itself across beats and
# what ties the thumbnail to the video it advertises. All keyless, same host.

def _media_base():
    """The media host base URL, from the image config (both live on one box)."""
    return ((CONFIG.get("image") or {}).get("base") or "").strip().rstrip("/")


def media_upload(path):
    """Upload a local file to the media host. Returns the public URL.

    Used for content consistency: upload a reference frame once, then hand its
    URL to edit_image / generate_video as the thing to vary rather than invent.
    Retried like everything else in this module; raises when exhausted.
    """
    base = _media_base()
    if not base:
        raise RuntimeError("media host base URL not configured -- edit config/apis.json")
    if not (path and os.path.exists(path)):
        raise RuntimeError("upload source missing: %r" % (path,))
    last = None
    for i in range(RETRIES):
        try:
            with open(path, "rb") as fh:
                r = requests.post(base + UPLOAD_PATH,
                                  files={"file": (os.path.basename(path), fh,
                                                  "application/octet-stream")},
                                  timeout=TIMEOUT,
                                  proxies=_proxies(CONFIG.get("image") or {}))
            if r.status_code != 200:
                raise RuntimeError("upload HTTP %s: %s" % (r.status_code, r.text[:200]))
            doc = r.json()
            url = doc.get("url") if isinstance(doc, dict) else None
            if not url:
                raise RuntimeError("upload gave no url: %s" % json.dumps(doc)[:200])
            print("[adapters] upload ok: %s -> %s" % (os.path.basename(path), url[:80]))
            return url
        except Exception as e:
            last = e
            print("[adapters] upload attempt %d/%d failed: %s" % (i + 1, RETRIES, e))
            if i < RETRIES - 1:
                time.sleep(min(10 * 2 ** i, 60))
    raise RuntimeError("upload failed after %d attempts: %s" % (RETRIES, last))


def edit_image(image_url, instruction):
    """Vary a hosted image: MCP edit_image(image_url, instruction) -> bytes.

    `image_url` is a public URL (e.g. from media_upload) or a media_id the host
    knows. Returns image bytes, format via last_format('image'). Raises when the
    tool errors so the caller can fall back to a from-text generation.
    """
    base = _media_base()
    if not base:
        raise RuntimeError("media host base URL not configured -- edit config/apis.json")
    envelope = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "edit_image",
                           "arguments": {"image_url": image_url,
                                         "instruction": instruction}}}
    last = None
    for i in range(RETRIES):
        try:
            r = requests.post(base + "/mcp", json=envelope, timeout=TIMEOUT,
                              headers={"Content-Type": "application/json",
                                       "Accept": "application/json, text/event-stream"},
                              proxies=_proxies(CONFIG.get("image") or {}))
            if r.status_code != 200:
                raise RuntimeError("edit HTTP %s: %s" % (r.status_code, r.text[:200]))
            try:
                doc = r.json()
            except Exception:
                raise RuntimeError("edit expected JSON-RPC, got: %s" % r.text[:200])
            data, media_url = _mcp_media(doc, "image")
            if data is None:
                data = _download(CONFIG.get("image") or {}, base, media_url, "image")
            fmt = sniff(data)
            if fmt not in IMAGE_FORMATS:
                raise RuntimeError("edit returned %s, not image media" % (fmt or "junk"))
            _LAST_FORMAT["kind"] = fmt
            _LAST_FORMAT["image"] = fmt
            print("[adapters] edit_image ok: %d bytes (%s)" % (len(data), fmt))
            return data
        except Exception as e:
            last = e
            print("[adapters] edit attempt %d/%d failed: %s" % (i + 1, RETRIES, e))
            if i < RETRIES - 1:
                time.sleep(min(10 * 2 ** i, 60))
    raise RuntimeError("edit_image failed after %d attempts: %s" % (RETRIES, last))


def image_rest(prompt, mode="thinking", timeout=300):
    """One image via the REST endpoint (POST /api/image), bypassing MCP.

    `mode` "thinking" is the host's slow high-quality pass -- thumbnails only,
    one call per episode. Returns bytes; raises so the caller falls back to the
    MCP path (adapters.image), which stays the default for beat stills.
    """
    base = _media_base()
    if not base:
        raise RuntimeError("media host base URL not configured -- edit config/apis.json")
    r = requests.post(base + IMAGE_REST_PATH,
                      json={"prompt": prompt, "mode": mode, "timeout": timeout},
                      headers={"Content-Type": "application/json"},
                      timeout=TIMEOUT + timeout,
                      proxies=_proxies(CONFIG.get("image") or {}))
    if r.status_code != 200:
        raise RuntimeError("rest image HTTP %s: %s" % (r.status_code, r.text[:200]))
    try:
        doc = r.json()
    except Exception:
        raise RuntimeError("rest image expected JSON, got: %s" % r.text[:200])
    if isinstance(doc, dict) and doc.get("error"):
        raise RuntimeError("rest image provider error: %s" % str(doc["error"])[:200])
    urls = (doc.get("urls") or []) if isinstance(doc, dict) else []
    if not urls:
        raise RuntimeError("rest image gave no urls: %s" % json.dumps(doc)[:200])
    data = _download(CONFIG.get("image") or {}, base, urls[0], "image")
    fmt = sniff(data)
    if fmt not in IMAGE_FORMATS:
        raise RuntimeError("rest image returned %s, not image media" % (fmt or "junk"))
    _LAST_FORMAT["kind"] = fmt
    _LAST_FORMAT["image"] = fmt
    print("[adapters] rest image ok (%s): %d bytes (%s)" % (mode, len(data), fmt))
    return data
