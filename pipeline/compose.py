"""Beat Engine v4 — bakes a job into a Hyperframes project: index.html + a paused
GSAP timeline.

The division of labour: the LLM picks WHAT each beat shows (four slots,
`pipeline/beats.py`), Python decides WHEN every beat fires (the Whisper word
clock, also `beats.py`), and this module owns HOW it appears. There are two
templates: a full-frame hard cut, and a snap zoom. No model ever emits
animation code, so a bad LLM day can only make the *content* weaker -- never
the pacing or the render.

Layering, by DOM order:
  base   full-bleed cartoon frames, one hard-cutting into the next (track 0)
  top    the avatar PNG, the lesson chip, the verdict stamp

Two facts about the renderer are load-bearing and were confirmed on this machine
with `hyperframes lint` (see the PART I register):
  * the root element needs a duration source, so `data-duration` is emitted;
  * every timed media element needs an `id` or its audio renders SILENT.

Everything degrades instead of failing: a missing art file drops its beat, a
missing avatar prints a loud banner, an empty `assets/audio/lofi/` just means
no music. The same job always bakes a byte-identical index.html -- there is no
randomness anywhere in this module.
"""
import hashlib
import html as html_mod
import math
import os
import shutil
import subprocess

from pipeline import beats as beat_engine

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STYLE = os.path.join(REPO, "config", "style.css")
VENDOR_GSAP = os.path.join(REPO, "assets", "vendor", "gsap.min.js")
FONT_DIR = os.path.join(REPO, "assets", "fonts")
AVATAR_DIR = os.path.join(REPO, "assets", "avatar")
LOFI_DIR = os.path.join(REPO, "assets", "audio", "lofi")
SFX_DIR = os.path.join(REPO, "assets", "audio", "sfx")

W, H, FPS = 1920, 1080, 60

# VERIFY-ON-FIRST-RUN #2 -- `<img class="clip">` lints and renders clean here.

# Lanes. Audio kinds need their own index each: narration, music and SFX all play
# at once, and clips that overlap inside one lane are what makes a track drop out.
TRACK_ART, TRACK_OVERLAY = 0, 1
TRACK_VOICE, TRACK_MUSIC, TRACK_SFX = 2, 3, 4
TRACK_BRAND = 5

MUSIC_VOL = "0.12"          # C3: the bed sits under a fast voice, never with it
SFX_VOL = "0.22"            # seasoning, not slaps; almost every beat is silent
SFX_DUR = 0.4
DEFAULT_TRACK_LEN = 150.0   # used only when ffprobe is unavailable

FADE = 0.3
BOB_PERIOD = 1.2            # C3 avatar idle bob
BOB_RISE = -5               # The croc breathes, it does not bounce.
VERDICT_TAIL = 20.0         # the stamp owns the last 20 seconds

FONT_FILES = ("display.woff2", "mono.woff2", "pixel.woff2")
SFX_NAMES = tuple(n for n in beat_engine.SFX if n != "none")

GSAP_STUB = (
    "throw new Error('SCALED: assets/vendor/gsap.min.js was missing at compose time -- \\n"
    "re-add it with the command in assets/vendor/README.txt. No timeline exists, so this \\n"
    "composition cannot render.');\n"
)

AVATAR_BANNER = """
[compose] !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
[compose] !! NO AVATAR ART. Professor Croc will be absent from this video.
[compose] !! Drop ONE transparent PNG at assets/avatar/croc.png (512-1024px).
[compose] !! Set SCALED_REQUIRE_AVATAR=1 to make this a hard failure instead.
[compose] !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
"""

# Everything config/style.css (PART E1) does not cover. E1 is verbatim and owns
# the look of each beat class; this owns the plumbing those classes need.
COMPOSITION_CSS = """
html,body{margin:0;padding:0;background:#FFF8EE}
#stage{background:#FFF8EE}
.beat-wrap{position:absolute;inset:0;overflow:hidden}
div.beat-img{background-size:cover;background-position:center}
#avatar{pointer-events:none}
"""


# ---------------------------------------------------------------- pure helpers

def _num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _esc(text):
    return html_mod.escape(str(text or ""), quote=True)


def scene_durations(job):
    return [_num((s or {}).get("dur")) for s in ((job or {}).get("scenes") or [])]


def total_duration(job):
    return round(sum(scene_durations(job)), 3)


def load_style():
    """config/style.css, with its font URLs rebased for a project-root index.html."""
    try:
        with open(STYLE, encoding="utf-8") as fh:
            css = fh.read()
    except OSError as e:
        print("[compose] WARNING: %s unreadable (%s) -- composition CSS only" % (STYLE, e))
        return ""
    return css.replace("../assets/fonts/", "assets/fonts/")


def _clip(el, cls, start, dur, track, inner="", style=""):
    """One timed element. The id is mandatory: without it the renderer cannot
    discover the clip, and for audio that means it renders SILENT."""
    return ('<div id="%s" class="%s clip" data-start="%.3f" data-duration="%.3f" '
            'data-track-index="%d"%s>%s</div>'
            % (el, cls, start, dur, track, (' style="%s"' % style) if style else "", inner))


# ------------------------------------------------------- the v4 templates
# Two templates. That is the whole edit: a full-frame hard cut, and a snap
# zoom the director asks for by name. v3 had eight presenters with overshoots,
# drifts, slides and wiggles, and every cut arriving the same way is exactly
# what made the video feel systematic. A Casually cut does nothing -- one frame
# this, next frame that -- so the templates do nothing: a 0.02s blink against
# black-flash, then a dead-static hold. The joke is the picture, the timing is
# the voice, the motion is one idle bob and the occasional yanked zoom.

def t_frame(el, wrap, src, t, vis):
    """Every visual beat (scene/photo/meme): full-bleed, instant, static."""
    inner = ('<img id="%s" class="beat-img clip" data-start="%.3f" data-duration="%.3f" '
             'data-track-index="%d" src="%s" alt="">'
             % (el, t, vis, TRACK_ART, _esc(src)))
    tag = '<div id="%s" class="beat-wrap">%s</div>' % (wrap, inner)
    return [tag], [
        "tl.fromTo('#%s',{opacity:0},{opacity:1,duration:0.020,ease:'none'},%.3f);" % (el, t),
    ]

def t_zoom(sel, amount, t):
    """`zoom`: a snap zoom -- 0.08s in on expo.out, then an elastic settle.

    v2 took 0.18s on power2.in, which is an ease *into* the movement: the punch
    arrived late and softly. expo.out front-loads it so the scale is essentially
    there within five frames, and elastic.out gives the small overshoot-and-wobble
    that reads as a camera being yanked. beats.ZOOM_MAX rose to 1.45 to match --
    a 1.18 punch-in at 1080p is a nudge nobody consciously sees.

    Acts on art that is already on screen, so it emits no element of its own --
    which is also why it is the safe landing spot for any unrenderable beat.
    """
    return [
        "tl.to('%s',{scale:%.3f,duration:0.080,ease:'expo.out'},%.3f);" % (sel, amount, t),
        "tl.to('%s',{scale:1,duration:0.620,ease:'elastic.out(1,0.55)'},%.3f);"
        % (sel, round(t + 0.08, 3)),
    ]


AVATAR_NAMES = ("croc.png", "neutral.png")


def avatar_file():
    """The avatar PNG's filename, or None when none is installed.

    Prefers croc.png, then neutral.png, then whatever single PNG is in there --
    so dropping in one file under any name is enough to get the host on screen.
    """
    try:
        names = sorted(n for n in os.listdir(AVATAR_DIR) if n.lower().endswith(".png"))
    except OSError:
        return None
    for want in AVATAR_NAMES:
        if want in names:
            return want
    return names[0] if names else None


def avatar_tag(name):
    """The one <img>. Not a clip -- Croc is on screen for the entire video, so he
    needs no window and must not be discovered as a timed element."""
    return '<img id="avatar" src="assets/avatar/%s" alt="">' % _esc(name) if name else ""


def avatar_bob(t0, dur):
    """C3 idle bob: y 0 -> -10, dur 1.2, yoyo, sine.inOut, repeated to fill the
    scene. GSAP counts `repeat` as *extra* plays, hence the -1."""
    reps = max(0, int(math.ceil(_num(dur) / BOB_PERIOD)) - 1)
    return ["tl.fromTo('#avatar',{y:0},{y:%d,duration:%.3f,yoyo:true,repeat:%d,"
            "ease:'sine.inOut'},%.3f);" % (BOB_RISE, BOB_PERIOD, reps, round(t0, 3))]


# ------------------------------------------------------------- the scene walker

def scene_layer(i, t0, dur, beats, media, state):
    """Every tag, tween and SFX request for one scene.

    `media` is {"art": {beat_index: path}} of the files that actually exist --
    a beat whose art never rendered simply loses its visual, which is how a
    partial art failure still produces a watchable episode.
    """
    tags, tweens, sfx = [], [], []
    end = round(t0 + _num(dur), 3)
    art = (media or {}).get("art") or {}
    beats = [b for b in (beats or []) if isinstance(b, dict)]
    times = [round(t0 + _num(b.get("t")), 3) for b in beats]
    # Where the base layer changes next: a frame holds the screen until another
    # frame replaces it, never until merely the next beat of any kind.
    swaps = [n for n, b in enumerate(beats)
             if str(b.get("kind") or "").strip().lower() in ("scene", "photo", "meme")
             and art.get(n)]
    live = state.get("live")

    for n, b in enumerate(beats):
        j = n              # beat index, and the key copy_scene_media filed art under
        t = times[n]
        kind = str(b.get("kind") or "").strip().lower()

        if kind in ("scene", "photo", "meme") and art.get(j):
            after = next((times[k] for k in swaps if k > n), end)
            el, wrap = "b%d_%d" % (i, j), "w%d_%d" % (i, j)
            tg, tw = t_frame(el, wrap, art[j], t, round(max(0.3, after - t), 3))
            tags += tg
            tweens += tw
            live = "#" + wrap
        elif kind == "zoom" and live:
            tweens += t_zoom(live, _num(b.get("amount"), 1.15), t)

        name = str(b.get("sfx") or "none").strip().lower()
        if name in SFX_NAMES:
            sfx.append((name, t))

    state["live"] = live
    return tags, tweens, sfx


# ------------------------------------------------------------------ the audio
# Three independent lanes. Narration is untouched at full volume, the lo-fi bed
# sits at 0.12 under it, and SFX punctuate at 0.5. Anything sharing a lane index
# and overlapping in time is what makes a track drop out, hence one lane each.

AUDIO_EXT = ("mp3", "m4a", "wav", "ogg")


def find_audio(dirpath, stem):
    """First `stem.<ext>` in dirpath for any container we accept, else None."""
    for ext in AUDIO_EXT:
        path = os.path.join(dirpath, "%s.%s" % (stem, ext))
        if os.path.exists(path):
            return path
    return None


def list_tracks(dirpath=None):
    """Sorted music files in assets/audio/lofi/. Sorted, so the bed a given job
    gets never changes between builds."""
    dirpath = dirpath or LOFI_DIR
    try:
        names = sorted(n for n in os.listdir(dirpath)
                       if n.lower().rsplit(".", 1)[-1] in AUDIO_EXT)
    except OSError:
        return []
    return [os.path.join(dirpath, n) for n in names]


def probe_seconds(path):
    """Duration of an audio file via ffprobe, or None when it cannot be read.

    ffprobe ships with ffmpeg, which the render step already requires, so this
    is free in CI -- but the whole music path still works without it, using
    DEFAULT_TRACK_LEN and letting the last clip run long under the fade.
    """
    if not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        secs = float((out.stdout or "").strip())
    except ValueError:
        return None
    return secs if secs > 0.5 else None


def music_plan(total, tracks):
    """C3 segmentation: k = ceil(total / track_len) clips laid back to back.

    Returns [(src_path, start, duration)]. The final clip is trimmed to the exact
    end of the video so the bed can never outlast the picture.
    """
    total = round(_num(total), 3)
    if total <= 0 or not tracks:
        return []
    lengths = [probe_seconds(p) or DEFAULT_TRACK_LEN for p in tracks]
    plan, t, n = [], 0.0, 0
    while t < total - 0.05 and n < 200:
        src = tracks[n % len(tracks)]
        length = lengths[n % len(lengths)]
        dur = round(min(length, total - t), 3)
        plan.append((src, round(t, 3), dur))
        t = round(t + dur, 3)
        n += 1
    return plan


def voice_clips(job, have):
    """One narration <audio> per scene, in narration's own lane."""
    out, t = [], 0.0
    for i, dur in enumerate(scene_durations(job)):
        if have.get(i):
            out.append('<audio id="v%d" class="clip" data-start="%.3f" data-duration="%.3f" '
                       'data-track-index="%d" src="%s"></audio>'
                       % (i, t, dur, TRACK_VOICE, _esc(have[i])))
        t = round(t + dur, 3)
    return out


def music_clips(plan):
    """The lo-fi bed as back-to-back clips at C3's volume 0.12."""
    return ['<audio id="mus%d" class="clip" data-start="%.3f" data-duration="%.3f" '
            'data-track-index="%d" data-volume="%s" src="assets/audio/%s"></audio>'
            % (n, start, dur, TRACK_MUSIC, MUSIC_VOL, _esc(os.path.basename(src)))
            for n, (src, start, dur) in enumerate(plan)]


def sfx_clips(requests, have):
    """One 0.4s <audio> per beat that asked for a sound and whose file exists."""
    out = []
    for n, (name, t) in enumerate(requests):
        if name not in have:
            continue
        out.append('<audio id="fx%d" class="clip" data-start="%.3f" data-duration="%.3f" '
                   'data-track-index="%d" data-volume="%s" src="assets/audio/%s"></audio>'
                   % (n, round(t, 3), SFX_DUR, TRACK_SFX, SFX_VOL, _esc(have[name])))
    return out


# ------------------------------------------------------------ the brand layer

def lesson_chip(lesson, total):
    """LESSON #NNN, top-left, present for the whole video (C3 / E1 #lesson-chip)."""
    if not lesson:
        return [], []
    el = "lesson-chip"
    tag = ('<div id="%s" class="clip" data-start="0.000" data-duration="%.3f" '
           'data-track-index="%d">LESSON #%03d</div>'
           % (el, max(0.3, total), TRACK_BRAND, int(_num(lesson))))
    return [tag], ["tl.fromTo('#%s',{opacity:0},{opacity:1,duration:0.500,ease:'none'},0.300);"
                   % el]


def verdict_stamp(verdict, total):
    """The APEX/THREAT/SLEEPER/FRAUD stamp, slamming in over the last 20 seconds.

    E1 gives it opacity 0 and rotate(-8deg), so the slam only has to animate
    scale and opacity -- and GSAP keeps the CSS rotation while doing it.
    """
    verdict = str(verdict or "").strip().upper()
    if not verdict:
        return [], []
    at = round(max(0.0, _num(total) - VERDICT_TAIL), 3)
    vis = round(max(0.5, _num(total) - at), 3)
    el = "verdict"
    tag = ('<div id="%s" class="verdict-stamp clip" data-start="%.3f" data-duration="%.3f" '
           'data-track-index="%d">%s</div>' % (el, at, vis, TRACK_BRAND, _esc(verdict)))
    return [tag], [
        "tl.fromTo('#%s',{scale:2.4,opacity:0},{scale:1,opacity:1,duration:0.400,"
        "ease:'back.out(1.4)'},%.3f);" % (el, at),
        "tl.fromTo('#%s',{rotation:-8},{rotation:-5,duration:0.120,yoyo:true,repeat:3,"
        "ease:'none'},%.3f);" % (el, round(at + 0.4, 3)),
    ]


# ------------------------------------------------------- copying assets across
# The project directory has to be self-contained: hyperframes loads it as a page,
# so every src must resolve inside it. Nothing here can raise -- a missing file
# means one fewer element, which the templates above already tolerate.

def _copy(src, dst, label):
    try:
        shutil.copyfile(src, dst)
        return True
    except OSError as e:
        print("[compose] could not copy %s (%s) -- %s omitted" % (src, e, label))
        return False


def copy_gsap(assets):
    dst = os.path.join(assets, "gsap.min.js")
    if os.path.exists(VENDOR_GSAP) and _copy(VENDOR_GSAP, dst, "GSAP"):
        return True
    print("[compose] !!!! %s IS MISSING -- writing a stub that fails loudly at render "
          "time. Restore it with the command in assets/vendor/README.txt." % VENDOR_GSAP)
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write(GSAP_STUB)
    return False


def copy_fonts(assets):
    """The three E1 faces. woff2 here on purpose -- this is the browser path.
    (thumbnail.py uses the committed TTFs, because Pillow cannot decode woff2.)"""
    dst = os.path.join(assets, "fonts")
    os.makedirs(dst, exist_ok=True)
    for name in FONT_FILES:
        src = os.path.join(FONT_DIR, name)
        if os.path.exists(src):
            _copy(src, os.path.join(dst, name), "font " + name)
        else:
            print("[compose] WARNING: font %s missing -- Chrome falls back to a system face"
                  % src)


def copy_avatar(assets):
    """The avatar PNG -> the project. Returns its filename, or None.

    None is the degrade path: no tag, no bob, and a banner in the log.
    SCALED_REQUIRE_AVATAR=1 turns it into a crash for anyone who would rather not
    ship an episode without the host.
    """
    found = avatar_file()
    if not found:
        print(AVATAR_BANNER.strip())
        if (os.getenv("SCALED_REQUIRE_AVATAR") or "").strip() in ("1", "true", "yes"):
            raise RuntimeError("no avatar art in assets/avatar/ and SCALED_REQUIRE_AVATAR is set")
        return None
    dst = os.path.join(assets, "avatar")
    os.makedirs(dst, exist_ok=True)
    if not _copy(os.path.join(AVATAR_DIR, found), os.path.join(dst, found), "avatar"):
        return None
    print("[compose] avatar: %s" % found)
    return found


def copy_audio_beds(assets, plan, wanted):
    """The music files in `plan` plus the SFX named in `wanted`, into assets/audio/."""
    dst = os.path.join(assets, "audio")
    os.makedirs(dst, exist_ok=True)
    for src, _start, _dur in plan:
        _copy(src, os.path.join(dst, os.path.basename(src)), "music")
    have = {}
    for name in sorted(set(wanted)):
        src = find_audio(SFX_DIR, name)
        if not src:
            continue
        base = os.path.basename(src)
        if _copy(src, os.path.join(dst, base), "sfx " + name):
            have[name] = base
    missing = sorted(set(wanted) - set(have))
    if missing:
        print("[compose] no audio for sfx %s in %s -- those beats are silent"
              % (", ".join(missing), SFX_DIR))
    return have


def copy_scene_media(job, work_dir, assets, scenes):
    """Beat frames and narration out of work/ and into the project.

    Returns ({scene: {"art": {...}}}, {scene: voice_src}), holding
    project-relative paths for the files that actually made it across.
    """
    job_id = str(job.get("id") or "")
    work_dir = work_dir or "."
    media, voice = {}, {}
    for i, scene in enumerate(scenes):
        art = {}
        for j, beat in enumerate(((scene or {}).get("beats") or [])):
            if not isinstance(beat, dict):
                continue
            kind = str(beat.get("kind") or "").lower()
            if kind not in ("scene", "photo", "meme"):
                continue
            stem = "%s%s_%d_%d" % ({"scene": "s", "photo": "p",
                                    "meme": "m"}[kind], job_id, i, j)
            for ext in ("jpg", "png", "jpeg", "webp", "gif"):
                src = os.path.join(work_dir, "%s.%s" % (stem, ext))
                if not os.path.exists(src):
                    continue
                name = "b%d_%d.%s" % (i, j, ext)
                if _copy(src, os.path.join(assets, name), "%s beat %d.%d" % (kind, i, j)):
                    art[j] = "assets/" + name
                break
            else:
                print("[compose] scene %d beat %d (%s) has no art in %s -- beat degrades"
                      % (i, j, kind, work_dir))
        media[i] = {"art": art}
        # The container follows the provider (Chatterbox returns wav), so scan the
        # extensions instead of assuming mp3 -- Chrome and ffmpeg both take either,
        # and transcoding here would only add a step that can fail.
        src = find_audio(work_dir, "v%s_%d" % (job_id, i))
        if src:
            ext = src.rsplit(".", 1)[-1].lower()
            if _copy(src, os.path.join(assets, "a%d.%s" % (i, ext)), "narration %d" % i):
                voice[i] = "assets/a%d.%s" % (i, ext)
        else:
            print("[compose] scene %d has no narration in %s -- it plays silent"
                  % (i, work_dir))
    return media, voice


# ------------------------------------------------------------------ the build

PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>SCALED %(id)s</title>
<script src="assets/gsap.min.js"></script>
<style>
%(style)s
%(css)s</style>
</head>
<body>
<div id="stage" data-composition-id="scaled" data-start="0" data-duration="%(total).3f" \
data-width="%(w)d" data-height="%(h)d" data-fps="%(fps)d">
%(body)s
</div>
<script>
const tl = gsap.timeline({paused:true});
%(tl)s
window.__timelines = {scaled: tl};
</script>
</body>
</html>
"""


def build_project(job, work_dir):
    """Write work/proj_{id}/ and return (project_dir, total_duration).

    Deterministic by construction: every timestamp comes from the job dict, the
    file lists are sorted, and nothing consults a clock or an RNG -- so the same
    job always bakes a byte-identical index.html. The sha1 in the closing log
    line is there to make that checkable at a glance across runs.
    """
    job = job or {}
    job_id = str(job.get("id") or "job")
    proj = os.path.join(work_dir or ".", "proj_%s" % job_id)
    assets = os.path.join(proj, "assets")
    os.makedirs(assets, exist_ok=True)

    scenes = [s if isinstance(s, dict) else {} for s in (job.get("scenes") or [])]
    total = round(sum(_num(s.get("dur")) for s in scenes), 3)

    copy_gsap(assets)
    copy_fonts(assets)
    avatar = copy_avatar(assets)
    media, voice = copy_scene_media(job, work_dir, assets, scenes)

    body, tl, sfx = [], [], []
    state = {"live": None}
    t0 = 0.0
    for i, scene in enumerate(scenes):
        dur = _num(scene.get("dur"))
        beats = scene.get("beats") or []
        if not beats:
            print("[compose] scene %d has no beats -- it holds the previous picture" % i)
        tags, tweens, asks = scene_layer(i, round(t0, 3), dur, beats, media.get(i), state)
        body.extend(tags)
        tl.extend(tweens)
        sfx.extend(asks)
        if avatar:
            tl.extend(avatar_bob(round(t0, 3), dur))
        t0 = round(t0 + dur, 3)

    # Audio last: the music plan needs the final total, and the SFX list needs
    # every scene's requests. Both are pure functions of what came before.
    plan = music_plan(total, list_tracks())
    have_sfx = copy_audio_beds(assets, plan, [name for name, _t in sfx])
    body.extend(voice_clips(job, voice))
    body.extend(music_clips(plan))
    body.extend(sfx_clips(sfx, have_sfx))

    chip_tags, chip_tweens = lesson_chip(job.get("lesson"), total)
    stamp_tags, stamp_tweens = verdict_stamp(job.get("verdict"), total)
    body.extend(chip_tags + stamp_tags)
    tl.extend(chip_tweens + stamp_tweens)

    # DOM order is the layering (C2): art, then overlays, then Croc, and the meme
    # class carries a z-index above him for the seconds it is on screen.
    page = PAGE % {
        "id": _esc(job_id), "style": load_style(), "css": COMPOSITION_CSS,
        "total": max(total, 0.1), "w": W, "h": H, "fps": FPS,
        "body": "\n".join(body + ([avatar_tag(avatar)] if avatar else [])),
        "tl": "\n".join(tl),
    }
    with open(os.path.join(proj, "index.html"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(page)

    print("[compose] %s: %d scenes, %.2fs, %d beats, %d tweens, %d music, %d sfx, "
          "avatar %s, sha1 %s"
          % (proj, len(scenes), total, sum(len((s.get("beats") or [])) for s in scenes),
             len(tl), len(plan), len(sfx_clips(sfx, have_sfx)), avatar or "ABSENT",
             hashlib.sha1(page.encode("utf-8")).hexdigest()[:12]))
    return proj, total
