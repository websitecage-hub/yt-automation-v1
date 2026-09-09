"""Thumbnail v3 — the image host paints the whole frame, text included.

v2 was a hybrid: the art call was forbidden from drawing text (`no text, no
letters, no numbers`) and Pillow composited the headline on top. That was the
right call for a weak image host and the wrong one for this host. It produced a
sticker: flat cartoon art with a white slab of Archivo Black pasted over it, in
the same place, at the same weight, on every episode. It read as generated.

v3 hands the whole frame to the host, which renders short display copy cleanly
and correctly spelled -- verified live against media-gen-mcp on 2026-09-04. Type
that the model draws sits *in* the photograph: it takes the scene's light, it
overlaps the subject, and it is a different composition every episode. Pillow's
only remaining jobs are the small SCALED/LESSON wordmark (the corner mark every
reference thumbnail has) and the safety net -- if the art call fails outright it
still paints a backdrop and draws the headline itself, so a dead host degrades
to a v2-looking thumbnail instead of no thumbnail.

WHAT THE FRAME MUST DO, from reference/*.png and the operator's brief:
  * ONE continuous photograph bleeding off all four edges. The failed first
    attempt split the canvas into a text panel beside a picture panel -- half the
    frame was dead black. Every archetype below forbids panels by name.
  * A CLUE. "160 VS 3700" alone is cryptic; nobody knows it is about bite force.
    So every headline carries a small kicker line naming the subject, and every
    archetype carries an annotation device (a labelled hairline arrow, a circled
    part, a dimension line) that shows what is being measured.
  * VARIETY. Six archetypes, picked by a hash of the episode id: deterministic
    per episode -- a rebuild is byte-identical -- but the channel page reads as
    six different ideas rather than one template with the nouns swapped.

1280x720 throughout, which is what YouTube wants and what survives being shrunk
to a 320px sidebar tile.
"""
import hashlib
import io
import os

W, H = 1280, 720
QUALITY = 90

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONTS = os.path.join(REPO, "assets", "fonts")
# Pillow's FreeType parses a woff2 container header -- getname() even reports the
# right family -- but cannot decompress its Brotli glyph tables, so every glyph
# rasterises as .notdef and the thumbnail comes out full of tofu boxes. The
# browser render uses the woff2 files; Pillow gets the TTFs, which are committed
# beside them for exactly this reason. Never point these at a .woff2.
DISPLAY = os.path.join(FONTS, "display.ttf")       # Archivo Black
PIXEL = os.path.join(FONTS, "pixel.ttf")           # Press Start 2P
MONO = os.path.join(FONTS, "mono.ttf")             # JetBrains Mono
AVATAR_DIR = os.path.join(REPO, "assets", "avatar")
FALLBACK_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

INK = (255, 255, 255)
BLACK = (0, 0, 0)
DIM = (138, 143, 152)
BLUE = (0, 209, 255)
GREEN = (0, 255, 136)
YELLOW = (255, 214, 10)
RED = (255, 59, 48)

MAX_WORDS = 4          # a thumbnail line longer than this is unreadable at 320px
MARGIN = 48
STROKE = 8             # black outline, fallback path only
HEADLINE_BAND = 0.56   # fraction of the frame height the headline block may fill
AVATAR_W = 280
AVATAR_EVERY = 3       # Croc appears on lesson % AVATAR_EVERY == 0

# Words worth setting in yellow when one shows up in the headline: the turn, the
# verdict, the comparison. Everything else accents its longest word instead.
ACCENT_WORDS = ("VS", "NERFED", "BROKE", "BROKEN", "DEAD", "WRONG", "LOST",
                "SOLD", "FAKE", "FRAUD", "APEX", "THREAT", "SLEEPER", "NOT",
                "ZERO", "NEVER", "WORST", "BEST", "ONLY", "STOLE", "CHEATS")

# The look, held constant across all six archetypes. This is the half the
# operator signed off on: photoreal, black, one hard rim light, faint handwritten
# science in the dark air.
#
# Why the annotations are explicitly value-free: the first live pass produced
# gorgeous frames captioned "TEMPORALIS - 1200 N FORCE" and, worse, labelled the
# crocodile 160 lbf and the human 3700 lbf -- exactly backwards. The host has no
# idea which number belongs to which side and will happily invent both. On a
# channel whose whole pitch is real biology, a fabricated figure on the thumbnail
# is a credibility bug, so the annotation layer gets names, arrows, ticks and
# generic symbolic equations only. The headline is the sole place numbers appear,
# and those come quoted from the script model, which is grounded in the research.
LOOK = (
    "Photorealistic cinematic YouTube thumbnail, 16:9, shot on 85mm. ONE dominant "
    "subject in extreme close-up filling at least two thirds of the frame, cropped "
    "by the edges, not floating. Dark photographic atmosphere -- deep charcoal-blue "
    "gradient wash, not pure black: a dimly visible environment behind (laboratory "
    "bench surface, concrete wall, out-of-focus shelving), volumetric haze "
    "catching a single warm key light from the side plus a faint cool rim from "
    "behind, shallow depth of field, natural skin/bone/wet textures with film "
    "grain, dramatic chiaroscuro like a Real Science cover frame. A single small "
    "electric cyan glint may catch ONE edge only. ONE handwritten white annotation "
    "at most -- a short arrow or a circled detail with a one-word name -- drawn "
    "into the dark air, or none at all. "
    "The headline type is the other half of the frame: ENORMOUS bold condensed "
    "sans-serif capitals, half the frame tall, overlapping the subject, lit by "
    "the scene. "
)

# Appended to every archetype. The panel/seam clause is load-bearing: without it
# the host cheerfully returns a text box glued to a photo box. The numerals clause
# is the fix for the invented-measurements failure described above.
NEGATIVE = (
    " NEGATIVE: no split screen, no side-by-side panels, no dividing line, no "
    "inset frame, no border, no letterbox bars, no collage, no numbers or digits "
    "or measured quantities anywhere in the handwritten annotations, no other "
    "text anywhere, no gibberish letters, no misspelling, no watermark, no logo, "
    "no cartoon, no flat vector art, no pure black empty void background, no "
    "subject floating in nothing, no glowing neon x-ray innards, no translucent "
    "glowing organs, no laser glow, no floating lamp or random props, no smoke "
    "machine haze, no videogame asset look, no 3d render look."
)

# Six compositions. `scene` gets {subject}; `pos` is where the type goes. Each
# one is built around ONE dominant close-up subject and at most ONE annotation
# device -- the reference thumbnails (crow profile, wasp macro, lightkeeper
# face) are all a single face/object going edge to edge with huge two-tone
# type, never a labelled diagram. Versus is the only two-subject setup, staged
# as an absurd scale gag, not a face-off in a void.
ARCHETYPES = (
    ("versus",
     "Composition: the two things named here in the SAME frame at wildly "
     "different scales for an absurd size gag, like a snail versus a climber: "
     "{subject}. The small one sits on the bench surface in the foreground in "
     "razor macro focus, the huge one looms behind it out of focus in the dark. "
     "ONE short handwritten white arrow between them, no words on it.",
     "upper right"),
    ("callout",
     "Composition: extreme full-bleed macro of {subject} filling the entire "
     "frame edge to edge, one face or object cropped by the frame. A single "
     "crisp thin white circle is drawn around the one part that matters, with "
     "one short handwritten white arrow and a one-word white label naming that "
     "part and nothing more.",
     "lower left"),
    ("scale",
     "Composition: {subject}, the small thing perched on the laboratory bench "
     "surface in the immediate foreground in huge macro detail while the large "
     "thing towers behind it in the dark haze, both under the same warm side "
     "light, so the size difference reads instantly. No arrows, no labels.",
     "upper left"),
    ("autopsy",
     "Composition: {subject}, one real museum cutaway specimen photographed "
     "like a war-surgeon textbook plate: matte bone and tissue under warm "
     "practical light, the cut face a flat pale surface with visible grain, "
     "nothing glowing, nothing translucent. ONE hairline white leader line from "
     "the cut face out into the dark ends in the plain handwritten name of "
     "that structure.",
     "lower right"),
    ("specimen",
     "Composition: {subject} in severe side profile, cropped by the frame "
     "edges, filling the frame like a Real Science cover -- a museum "
     "type-specimen photograph under one warm side light with concrete wall "
     "barely visible behind. No arrows, no ticks, no labels at all.",
     "lower left"),
    ("witness",
     "Composition: one enormous living reptilian eye with a vertical slit pupil "
     "fills the left third of the frame in razor focus, staring directly down the "
     "lens, wet and reflective, and behind it out of focus in the dark sits "
     "{subject} on the bench surface. No arrows, no labels.",
     "right third"),
)



def pick_archetype(job):
    """Which of the six compositions this episode gets — stable per episode.

    Hashed from the episode id (not random, not the date) so a re-run of a failed
    upload rebuilds the identical thumbnail, while consecutive episodes almost
    never land on the same composition. SCALED_THUMB_ARCH forces one by name for
    art direction and for the style tests in work/styletest.
    """
    want = str(os.getenv("SCALED_THUMB_ARCH", "")).strip().lower()
    for arch in ARCHETYPES:
        if arch[0] == want:
            return arch
    seed = str((job or {}).get("id") or (job or {}).get("title") or "scaled")
    return ARCHETYPES[hashlib.sha1(seed.encode("utf-8")).digest()[0] % len(ARCHETYPES)]


def thumb_kicker(job):
    """The small line above the headline that says what is being measured.

    This is the fix for the operator's "there is no clue about watching teh
    thumbnail what is teh vdeo about". "160 VS 3700" is a riddle on its own;
    "BITE FORCE, POUNDS" over it is a video. The script model supplies
    `thumb_kicker`; when it doesn't, the topic's own words stand in, which is
    always at least literally true.
    """
    job = job or {}
    raw = job.get("thumb_kicker") or ""
    words = [w for w in str(raw).upper().split() if w]
    if not words:
        stat = str(job.get("stat_label") or "").upper().split()
        words = stat or str(job.get("topic") or "").upper().split()[:4]
    if not words:
        words = ["GRADED", "ON", "STREAM"]
    return " ".join(words[:5])[:34]


def accent_word(words):
    """The one word set in yellow. Reference thumbnails are two-tone, never one.

    Prefers a turn word (VS, NERFED, BROKE...) because that is where the claim
    actually is; otherwise the longest word, which is the one the eye lands on
    anyway. Pure numbers never take the accent -- white numerals on black are
    already the loudest thing in the frame.
    """
    clean = [w.strip(".,!?:'\"") for w in (words or [])]
    for w in clean:
        if w in ACCENT_WORDS:
            return w
    text = [w for w in clean if not w.replace(",", "").replace(".", "").isdigit()]
    return max(text or clean or [""], key=len)


def text_clause(job, pos):
    """The instruction that makes the host draw the headline into the photograph.

    Short, all-caps, quoted one string at a time: that is the shape this host
    spells correctly and kerns cleanly. Long sentences, lowercase and paragraphs
    are where image models start inventing letters, so the copy is capped hard
    upstream (thumb_kicker <=5 words, thumb_words <=MAX_WORDS).
    """
    words = thumb_words(job)
    big = " ".join(words)
    accent = accent_word(words)
    # Sizes are spelled out as fractions because the host otherwise promotes
    # the FIRST text it reads (the kicker) to the biggest: the kicker is a
    # small eyebrow line, the headline is the ONLY enormous element, and the
    # two never share a size.
    clause = (
        ' Overlaid ON TOP of the photograph in the %s, razor-sharp crisp clean '
        'correctly-spelled lettering and NOTHING else: the headline "%s" in '
        'ENORMOUS bold condensed sans-serif capitals, each letter about one '
        'quarter of the frame height, in pure white'
        % (pos, big))
    if accent and accent in big and accent != big:
        clause += ', with ONLY the word "%s" in bright yellow' % accent
    clause += (
        '. Above the headline, a TINY eyebrow line of small bold white '
        'sans-serif capitals reading "%s", letters no taller than one '
        'twelfth of the frame height, one single line, never cropped. '
        'The eyebrow stays small; the headline stays huge. '
        'The type overlaps the subject and takes the scene\'s light.'
        % thumb_kicker(job))
    return clause


def art_prompt(job):
    """The full image-API prompt: look + composition + baked headline.

    v2 asked for a textless scene and let Pillow paste the words on. v3 asks for
    the finished thumbnail. Assembled here rather than in config/prompts.md
    because the composition is chosen by code (pick_archetype) and the copy is
    quoted into the prompt verbatim -- prompts.md documents the contract and the
    fields the SCRIPT model has to supply, but this function is the source of
    truth for the string that goes over the wire.
    """
    job = job or {}
    subject = " ".join(str(job.get("thumbnail_prompt") or job.get("topic") or "").split())
    if not subject:
        subject = "a powerful animal skull lit from behind on a steel table"
    key, scene, pos = pick_archetype(job)
    print("[thumbnail] archetype=%s" % key)
    return (LOOK + scene.replace("{subject}", subject[:400])
            + text_clause(job, pos) + NEGATIVE)



def thumb_words(job):
    """The headline: at most MAX_WORDS punchy uppercase words.

    Prefers the script model's `thumb_words` (the payoff phrase it was asked to
    design first), then falls back to the title with connectives stripped --
    short filler words waste the pixels that make a thumbnail readable.
    """
    job = job or {}
    raw = job.get("thumb_words") or ""
    if isinstance(raw, (list, tuple)):
        raw = " ".join(str(x) for x in raw)
    words = [w for w in str(raw).upper().split() if w]
    if not words:
        skip = {"THE", "A", "AN", "OF", "IS", "IT", "TO", "AND", "YOUR", "THIS",
                "HOW", "WHY", "THAT", "WITH", "FOR"}
        title = str(job.get("title") or "").upper()
        keep = [w for w in title.split() if w.strip(".,!?:'\"") not in skip]
        words = keep or title.split()
    return [w[:14] for w in words[:MAX_WORDS]]


# --- pixel work -----------------------------------------------------------

def _font(path, size):
    """A Pillow font, falling back through the system face to the default.

    `path` must be a TTF/OTF -- see the note on DISPLAY about woff2 and tofu.
    """
    from PIL import ImageFont
    for candidate in (path, FALLBACK_FONT):
        try:
            return ImageFont.truetype(candidate, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _bbox(draw, text, font, stroke=0):
    """Ink box of `text` drawn at the origin. box[0]/box[1] are NOT zero.

    Pillow anchors text at the ascender line, so the ink starts box[1] pixels
    below the y you pass and box[0] to the right of the x. Layout code has to
    subtract those or a big display face silently walks off the bottom edge.
    """
    return draw.textbbox((0, 0), text, font=font, stroke_width=stroke)


def _size(draw, text, font, stroke=0):
    box = _bbox(draw, text, font, stroke)
    return box[2] - box[0], box[3] - box[1]


def _fit(draw, text, path, max_w, max_h=None, hi=260, lo=42, stroke=0):
    """Largest size in [lo, hi] whose stroked ink fits (max_w, max_h). Binary search."""
    best = _font(path, lo)
    while lo <= hi:
        mid = (lo + hi) // 2
        font = _font(path, mid)
        w, h = _size(draw, text, font, stroke)
        if w <= max_w and (max_h is None or h <= max_h):
            best, lo = font, mid + 1
        else:
            hi = mid - 1
    return best


def _cover(img):
    """Scale-and-crop `img` to exactly WxH without distorting it."""
    from PIL import Image
    src_w, src_h = img.size
    if not (src_w and src_h):
        raise ValueError("empty image")
    scale = max(W / src_w, H / src_h)
    grown = img.resize((max(1, int(src_w * scale + 0.5)), max(1, int(src_h * scale + 0.5))),
                       Image.LANCZOS)
    left = (grown.size[0] - W) // 2
    top = (grown.size[1] - H) // 2
    return grown.crop((left, top, left + W, top + H))


def _backdrop(seed):
    """Deterministic fallback art: a dark radial wash with a neon horizon.

    Used when the art call failed or returned bytes Pillow cannot read. Seeded
    from the episode text so each lesson still looks distinct, and dark enough
    that the headline stays readable.
    """
    from PIL import Image, ImageDraw, ImageFilter
    digest = hashlib.sha1(str(seed).encode("utf-8")).digest()
    accent = [BLUE, GREEN, YELLOW, RED][digest[0] % 4]
    cx = int(W * (0.56 + (digest[1] / 255.0) * 0.24))
    cy = int(H * (0.32 + (digest[2] / 255.0) * 0.26))

    img = Image.new("RGB", (W, H), BLACK)
    draw = ImageDraw.Draw(img)
    for step in range(26, 0, -1):
        r = int(step / 26.0 * H * 0.95)
        k = (1.0 - step / 26.0) ** 2
        draw.ellipse((cx - r, cy - r, cx + r, cy + r),
                     fill=tuple(int(c * k * 0.55) for c in accent))
    draw.line((0, int(H * 0.78), W, int(H * 0.72)), fill=accent, width=3)
    return img.filter(ImageFilter.GaussianBlur(18))


# --- brand layer ----------------------------------------------------------

def band(img, override=None):
    """Which half the headline goes in: 'bottom' (default) or 'top'.

    Auto mode puts the words wherever the art is quietest -- it compares mean
    luminance of the two bands and picks the darker one, so a top-heavy subject
    pushes the type down and a low-slung one pushes it up. SCALED_THUMB_POS
    forces the choice when a particular episode needs it.
    """
    want = str(override if override is not None
               else os.getenv("SCALED_THUMB_POS", "auto")).strip().lower()
    if want in ("top", "bottom"):
        return want
    try:
        grey = img.convert("L")
        top = grey.crop((0, 0, W, int(H * 0.42))).resize((16, 8))
        bottom = grey.crop((0, int(H * 0.58), W, H)).resize((16, 8))
        mean = lambda tile: sum(tile.tobytes()) / float(len(tile.tobytes()) or 1)
        return "top" if mean(top) < mean(bottom) - 12 else "bottom"
    except Exception:
        return "bottom"


def _scrim(img, where):
    """A soft black wash over the headline's band so white type always reads."""
    from PIL import Image, ImageDraw
    veil = Image.new("L", (W, H), 0)
    draw = ImageDraw.Draw(veil)
    for y in range(H):
        t = (1.0 - y / float(H)) if where == "top" else (y / float(H))
        t = max(0.0, (t - 0.42) / 0.58)
        draw.line((0, y, W, y), fill=int(min(1.0, t ** 1.4) * 170))
    return Image.composite(Image.new("RGB", (W, H), BLACK), img, veil)


def _draw_headline(draw, words, where, reserve=0):
    """The <=4-word line: Archivo Black, white, thick black stroke (prompt §7.5).

    Sized to actually fill its band -- the type is the thumbnail, so each line
    grows until it hits the frame width or its share of HEADLINE_BAND. `reserve`
    keeps it clear of the avatar when both share the bottom-right.
    """
    if not words:
        return
    # One word per line up to three reads bigger than a wrapped paragraph; four
    # words pair up so the block stays wide rather than tall.
    lines = words if len(words) <= 3 else [" ".join(words[:2]), " ".join(words[2:])]
    max_w = W - 2 * MARGIN - max(0, reserve)
    gap = 10
    budget = int(H * HEADLINE_BAND) - gap * (len(lines) - 1)
    per_line = max(42, budget // len(lines))
    fonts = [_fit(draw, line, DISPLAY, max_w, per_line, stroke=STROKE) for line in lines]
    boxes = [_bbox(draw, line, font, STROKE) for line, font in zip(lines, fonts)]
    heights = [box[3] - box[1] for box in boxes]
    total = sum(heights) + gap * (len(lines) - 1)
    y = MARGIN if where == "top" else H - MARGIN - total
    for line, font, box, th in zip(lines, fonts, boxes, heights):
        draw.text((MARGIN - box[0], y - box[1]), line, font=font, fill=INK,
                  stroke_width=STROKE, stroke_fill=BLACK)
        y += th + gap


def _draw_chip(draw, lesson, where):
    """LESSON #NNN in the pixel face, top-left (or bottom-left if type is on top)."""
    label = "LESSON #%03d" % int(lesson or 0)
    font = _font(PIXEL, 28)
    tw, th = _size(draw, label, font)
    x = MARGIN
    y = H - MARGIN - th if where == "top" else MARGIN
    draw.text((x, y), label, font=font, fill=DIM, stroke_width=4, stroke_fill=BLACK)
    draw.rectangle((x, y + th + 10, x + tw, y + th + 14), fill=GREEN)


def _draw_focus_ring(draw, where):
    """A red ellipse pointing at the focal third, opposite the headline band."""
    cx, cy = int(W * 0.70), int(H * 0.62 if where == "top" else 0.34)
    rx, ry = 152, 112
    draw.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), outline=RED, width=9)


def _avatar_file():
    """The avatar PNG, or None if it isn't installed.

    Croc is one static image everywhere in this factory -- there is no expression
    set to choose from. Resolution matches compose.avatar_file() so the thumbnail
    and the video can never disagree about who the host is.
    """
    try:
        names = sorted(n for n in os.listdir(AVATAR_DIR) if n.lower().endswith(".png"))
    except OSError:
        return None
    for want in ("croc.png", "neutral.png"):
        if want in names:
            return os.path.join(AVATAR_DIR, want)
    return os.path.join(AVATAR_DIR, names[0]) if names else None


def _avatar_on(lesson):
    """Whether Croc shows on this lesson. Asked before the headline is sized so the
    two never fight over the bottom-right -- and so a missing PNG gives the type
    the whole width back instead of reserving a hole for nothing."""
    try:
        return int(lesson or 0) % AVATAR_EVERY == 0 and _avatar_file() is not None
    except (TypeError, ValueError):
        return False


def _paste_avatar(img, lesson):
    """Croc, bottom-right, on every AVATAR_EVERY-th lesson. Optional."""
    from PIL import Image
    path = _avatar_file() if _avatar_on(lesson) else None
    if not path:
        return img
    try:
        croc = Image.open(path).convert("RGBA")
    except Exception as e:
        print("[thumbnail] avatar %s unreadable (%s) -- omitted" % (path, e))
        return img
    scale = AVATAR_W / float(croc.size[0] or 1)
    croc = croc.resize((AVATAR_W, max(1, int(croc.size[1] * scale + 0.5))), Image.LANCZOS)
    img.paste(croc, (W - AVATAR_W - 24, H - croc.size[1] - 16), croc)
    return img


def _draw_wordmark(draw):
    """KRONVEX, small, top-left — the corner mark every reference thumbnail has.

    Deliberately tiny. On the baked path the host has already drawn the headline;
    this is the channel signature, not a caption, and anything bigger competes
    with the type it is supposed to be standing next to.
    """
    font = _font(PIXEL, 22)
    tw, th = _size(draw, "KRONVEX", font)
    draw.text((MARGIN, MARGIN), "KRONVEX", font=font, fill=INK,
              stroke_width=4, stroke_fill=BLACK)
    draw.rectangle((MARGIN, MARGIN + th + 9, MARGIN + tw, MARGIN + th + 13), fill=GREEN)


# --- assembly -------------------------------------------------------------

def compose(art_bytes, job, ring=False, position=None, baked=True):
    """Art bytes + job -> the finished 1280x720 RGB image.

    `baked` says whether the art already contains the headline. On the baked path
    -- a successful v3 art call -- Python adds only the corner wordmark and Croc,
    because scrimming and re-typesetting over type the host already lit correctly
    is what made v2 look like a sticker. On the unbaked path -- a beat-1 still or
    a painted backdrop, neither of which has any words in it -- the full v2 brand
    layer comes back: scrim, headline, chip. Either way this function returns an
    image, so no caller has to branch on failure.
    """
    from PIL import Image
    job = job or {}
    art = None
    if art_bytes:
        source = io.BytesIO(art_bytes) if isinstance(art_bytes, (bytes, bytearray)) else art_bytes
        try:
            art = _cover(Image.open(source).convert("RGB"))
        except Exception as e:
            print("[thumbnail] art unreadable (%s) -- painting a backdrop" % e)
    if art is None:
        art = _backdrop("%s|%s" % (job.get("id", ""), job.get("title", "")))
        baked = False

    from PIL import ImageDraw
    lesson = job.get("lesson")
    if baked:
        canvas = art
        draw = ImageDraw.Draw(canvas)
        _draw_wordmark(draw)
        return _paste_avatar(canvas, lesson)

    where = band(art, position)
    canvas = _scrim(art, where)
    draw = ImageDraw.Draw(canvas)
    if ring:
        _draw_focus_ring(draw, where)
    reserve = (AVATAR_W + 48) if (where == "bottom" and _avatar_on(lesson)) else 0
    _draw_headline(draw, thumb_words(job), where, reserve)
    _draw_chip(draw, lesson, where)
    return _paste_avatar(canvas, lesson)


def _fallback_art(job, work_dir):
    """Beat-1 art from the rendered scenes, reused when the thumbnail call fails."""
    prefix = "i%s_0_" % job.get("id", "")
    try:
        names = sorted(n for n in os.listdir(work_dir)
                       if n.startswith(prefix) and n.endswith((".jpg", ".png")))
    except OSError:
        return None
    if not names:
        return None
    try:
        with open(os.path.join(work_dir, names[0]), "rb") as fh:
            return fh.read()
    except OSError:
        return None


def make_thumbnail(job, work_dir):
    """Render work/{id}_thumb.jpg for one episode. Returns the path, or None.

    Four tiers, each cheaper than the last: a fresh art call with the headline
    baked in (REST "thinking" pass first for maximum quality, MCP baked second
    -- both carry the kicker + headline IN the photograph), then a consistency
    edit built FROM this episode's own beat-1 still (upload it, edit the
    headline into it -- the thumbnail is then literally the video's world),
    then this episode's beat-1 still with the Pillow brand layer, then a painted
    backdrop. Only the first three tiers carry baked type, so the Pillow
    fallbacks hand the words back to Pillow -- a dead image host degrades to a
    v2-style thumbnail rather than shipping a picture with no headline on it at
    all. Only a Pillow failure or an unwritable work_dir returns None.
    """
    job = job or {}
    os.makedirs(work_dir, exist_ok=True)
    dst = os.path.join(work_dir, "%s_thumb.jpg" % job.get("id", "episode"))

    art, baked = None, True
    prompt = art_prompt(job)
    try:
        from pipeline import adapters
        art = adapters.image_rest(prompt, mode="thinking")
        print("[thumbnail] art call ok (%d bytes, rest/thinking)" % len(art or b""))
    except Exception as e:
        print("[thumbnail] rest/thinking failed (%s) -- trying MCP baked" % e)
    if art is None:
        try:
            from pipeline import adapters
            art = adapters.image(prompt, "1280x720")
            print("[thumbnail] art call ok (%d bytes, mcp)" % len(art or b""))
        except Exception as e:
            print("[thumbnail] art call failed (%s) -- trying consistency edit" % e)
    if art is None:
        # Tier 3: the thumbnail grows OUT OF the episode. Upload beat-1, ask the
        # host to re-stage it as the thumbnail composition with the headline
        # baked in. Same subject, same light, same specimen -- no stranger art.
        try:
            from pipeline import adapters
            ref = _fallback_art(job, work_dir)
            if ref:
                import tempfile
                with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as fh:
                    fh.write(ref)
                    tmp = fh.name
                try:
                    url = adapters.media_upload(tmp)
                    art = adapters.edit_image(
                        url, prompt + " Restage this exact subject as the finished "
                        "YouTube thumbnail described here, keeping the same specimen, "
                        "same lighting and same black background.")
                    print("[thumbnail] consistency edit ok (%d bytes)" % len(art or b""))
                finally:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
        except Exception as e:
            print("[thumbnail] consistency edit failed (%s) -- Pillow fallback" % e)
    if art is None:
        art, baked = _fallback_art(job, work_dir), False

    try:
        compose(art, job, baked=baked).save(dst, "JPEG", quality=QUALITY)
    except Exception as e:
        print("[thumbnail] compose failed: %s" % e)
        return None
    print("[thumbnail] wrote %s" % dst)
    return dst
