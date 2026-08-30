"""Bakes a job into a Hyperframes project: index.html + a paused GSAP timeline.

This is the core. Everything the show needs to be *reliable* is generated here in
Python -- media clips, word-synced captions, the croc's jaw and blinks, stat
cards, the verdict stamp, the lesson slate -- so that a bad LLM day can only
degrade decoration, never break the render (PART C6).

Two facts about the renderer are load-bearing and were confirmed on this machine
with `hyperframes lint` (see the PART I register):
  * the root element needs a duration source, so `data-duration` is emitted;
  * every timed media element needs an `id` or its audio renders SILENT.
"""

import html as html_mod
import os
import random
import re
import shutil

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STYLE = os.path.join(REPO, "config", "style.css")
VENDOR_GSAP = os.path.join(REPO, "assets", "vendor", "gsap.min.js")
FONT_DIR = os.path.join(REPO, "assets", "fonts")
CROC_PNG = os.path.join(REPO, "assets", "rig", "croc.png")

# VERIFY-ON-FIRST-RUN #2 -- `<img class="clip">` lints and renders clean here, so
# "img" is the default; "bgdiv" is the coded alternative, switchable by env.
MEDIA_MODE = (os.getenv("HF_MEDIA_MODE") or "img").strip().lower() or "img"

# The host is one flat PNG (no rig): the framework slides him in and gives him a
# gentle idle zoom / rotate / drift -- that is all a flat image can do.
CROC_ORIGIN = "center bottom"      # transform-origin used by croc_motion
LIME = "#A6FF3D"
DIM = "#7E8C84"

CAPTION_TAIL = 0.25        # a caption lingers this long past its last word
GRADE_BASE = {"A": 92, "B": 75, "C": 55, "D": 35, "F": 15}
VERDICT_TAIL = 20.0        # verdict stamp owns the last 20 seconds
SENTENCE_END = (".", "!", "?")

GSAP_STUB = (
    "throw new Error('SCALED: assets/vendor/gsap.min.js was missing at compose time -- \\n"
    "re-add it with the command in assets/vendor/README.txt. No timeline exists, so this \\n"
    "composition cannot render.');\n"
)


# ---------------------------------------------------------------- pure helpers

def group_lines(words, max_chars=34, max_words=5):
    """Split a scene's words into caption lines.

    A line flushes when adding the next word would pass max_chars or max_words,
    and after any word that ends a sentence. A single word longer than max_chars
    gets its own line and is allowed to overflow -- never dropped, never a crash.
    """
    lines, line, chars = [], [], 0
    for w in words or []:
        if not isinstance(w, dict):
            continue
        text = str(w.get("w", "")).strip()
        if not text:
            continue
        extra = len(text) + (1 if line else 0)
        if line and (chars + extra > max_chars or len(line) + 1 > max_words):
            lines.append(line)
            line, chars = [], 0
            extra = len(text)
        line.append(w)
        chars += extra
        if text.endswith(SENTENCE_END):
            lines.append(line)
            line, chars = [], 0
    if line:
        lines.append(line)
    return lines


def grade_pct(grade):
    """Letter grade -> bar width percent. A+ is a flat 100; +/- move a letter 5."""
    text = str(grade or "").strip().upper()
    if not text:
        return 50
    if text.startswith("A+"):
        return 100
    if text.startswith("S"):            # anime-stat scripts sometimes reach for S
        return 100
    letter = text[0]
    if letter not in GRADE_BASE:
        print("[compose] unknown grade %r -- bar set to 50%%" % grade)
        return 50
    pct = GRADE_BASE[letter]
    if "+" in text[1:3]:
        pct += 5
    elif "-" in text[1:3]:
        pct -= 5
    return max(0, min(100, pct))


def _num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _esc(text):
    return html_mod.escape(str(text or ""), quote=True)


def scene_durations(job):
    return [_num((s or {}).get("dur")) for s in (job.get("scenes") or [])]


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


COMPOSITION_CSS = """
html,body{margin:0;padding:0;background:var(--bg)}
img.clip,div.mediaclip{position:absolute;left:0;top:0;width:1920px;height:1080px}
img.clip{object-fit:cover}
div.mediaclip{background-size:cover;background-position:center}
.cap-line{position:absolute;left:210px;right:210px;bottom:130px;text-align:center}
#lesson{position:absolute;left:64px;top:52px;font:400 34px 'ScaledMono',monospace;
  color:var(--dim);letter-spacing:.14em;opacity:0}
#croc{position:absolute;left:56px;bottom:0;width:520px;height:auto;z-index:60;opacity:0;
  pointer-events:none;transform-origin:center bottom}
img#croc{object-fit:contain}
.statcard{position:absolute;right:84px;top:286px;width:520px;padding:26px 30px;z-index:50}
.statcard .stat-label{font-size:26px;color:var(--dim);letter-spacing:.12em}
.statcard .stat-grade{font-family:'ScaledDisplay','Arial Black',sans-serif;font-size:132px;
  line-height:1;color:var(--amber);margin:6px 0 10px}
.statcard .stat-note{font-size:24px;line-height:1.35;color:var(--ink)}
.statcard .stat-bar{margin-top:20px;height:18px;background:rgba(242,237,226,.14);border-radius:9px;
  overflow:hidden}
.statcard .stat-fill{height:18px;width:0;background:var(--lime);border-radius:9px}
.verdict-stamp{position:absolute;left:0;right:0;top:392px;margin:0 auto;width:900px;
  text-align:center;padding:14px 0;z-index:70;opacity:0}
"""


# ---------------------------------------------------------------- baked pieces

def media_tags(i, t0, dur, have_img, have_audio):
    """Image + audio clips for one scene. Both carry ids -- without one the
    renderer cannot discover the media and the audio renders SILENT."""
    out = []
    if have_img:
        if MEDIA_MODE == "bgdiv":
            out.append('<div id="img%d" class="mediaclip clip" data-start="%.2f" data-duration="%.2f" '
                        'data-track-index="0" style="background-image:url(assets/s%d.jpg)"></div>'
                        % (i, t0, dur, i))
        else:
            out.append('<img id="img%d" class="clip" data-start="%.2f" data-duration="%.2f" '
                       'data-track-index="0" src="assets/s%d.jpg">' % (i, t0, dur, i))
    if have_audio:
        out.append('<audio id="aud%d" data-start="%.2f" data-duration="%.2f" data-track-index="2" '
                   'src="assets/a%d.mp3"></audio>' % (i, t0, dur, i))
    return out


def caption_block(i, t0, words):
    """Caption lines plus the per-word colour tweens that ride the word clock."""
    tags, tweens = [], []
    j = 0
    for n, line in enumerate(group_lines(words)):
        spans, first, last = [], line[0], line[-1]
        for w in line:
            spans.append('<span id="c%d-%d">%s</span>' % (i, j, _esc(str(w.get("w", "")).strip())))
            tweens.append("tl.to('#c%d-%d',{color:'%s',duration:0.05},%.3f);"
                          % (i, j, LIME, t0 + _num(w.get("s"))))
            tweens.append("tl.to('#c%d-%d',{color:'%s',duration:0.05},%.3f);"
                          % (i, j, DIM, t0 + _num(w.get("e"))))
            j += 1
        start = t0 + _num(first.get("s"))
        dur = max(0.3, _num(last.get("e")) - _num(first.get("s")) + CAPTION_TAIL)
        tags.append('<div id="cap%d-%d" class="cap-line clip" data-start="%.2f" data-duration="%.2f" '
                    'data-track-index="1">%s</div>' % (i, n, start, dur, " ".join(spans)))
    return tags, tweens


def croc_motion(i, t0, dur):
    """Idle life for the flat host image: a gentle zoom in/out, a little rotation and
    a small vertical drift. Seeded per scene so a job always bakes the same
    performance, and every beat resolves back to the base transform so consecutive
    scenes chain without a visible jump. No rig -- this is a single PNG (PART C6,
    amended: the host is not rigged)."""
    rnd = random.Random(i * 13 + 5)
    tweens = []
    t = t0 + (1.4 if i == 0 else 0.15)      # let the scene-0 entrance slide land first
    end = t0 + dur
    while t < end - 0.9:
        beat = round(rnd.uniform(1.6, 2.6), 2)
        half = round(beat / 2.0, 3)
        sc = round(1.0 + rnd.uniform(0.02, 0.06), 3)
        rot = round(rnd.uniform(-2.5, 2.5), 2)
        dy = round(rnd.uniform(-16.0, 4.0), 1)
        tweens.append("tl.to('#croc',{scale:%.3f,rotation:%.2f,y:%.1f,transformOrigin:'%s',"
                      "duration:%.2f,ease:'sine.inOut'},%.3f);"
                      % (sc, rot, dy, CROC_ORIGIN, half, t))
        tweens.append("tl.to('#croc',{scale:1.000,rotation:0.00,y:0.0,transformOrigin:'%s',"
                      "duration:%.2f,ease:'sine.inOut'},%.3f);"
                      % (CROC_ORIGIN, half, round(t + half, 3)))
        t = round(t + beat, 3)
    return tweens


def stat_card(i, t0, dur, stat):
    """Right-hand stat panel: label, grade, note, and a bar that fills to grade_pct."""
    if not isinstance(stat, dict):
        return [], []
    pct = grade_pct(stat.get("grade"))
    tag = ('<div id="stat%d" class="statcard clip" data-start="%.2f" data-duration="%.2f" '
           'data-track-index="4">'
           '<div class="stat-label">%s</div>'
           '<div class="stat-grade">%s</div>'
           '<div class="stat-note">%s</div>'
           '<div class="stat-bar"><div id="statbar%d" class="stat-fill"></div></div>'
           '</div>' % (i, t0, dur, _esc(stat.get("label")), _esc(stat.get("grade")),
                       _esc(stat.get("note")), i))
    tween = ("tl.to('#statbar%d',{width:'%d%%',duration:0.9,ease:'power3.out'},%.3f);"
             % (i, pct, t0 + 0.6))
    return [tag], [tween]


def verdict_stamp(job, total):
    """The APEX/THREAT/SLEEPER/FRAUD stamp: slams in, then shakes. Python only."""
    text = str(job.get("verdict") or "").strip().upper()
    if not text:
        return [], []
    start = max(0.0, total - VERDICT_TAIL)
    hit = max(0.0, total - (VERDICT_TAIL - 1.0))
    tag = ('<div id="verdict" class="verdict-stamp clip" data-start="%.2f" data-duration="%.2f" '
           'data-track-index="3">%s</div>' % (start, max(0.5, total - start), _esc(text)))
    tweens = [
        "tl.fromTo('#verdict',{scale:3,opacity:0},{scale:1,opacity:1,duration:0.5,"
        "ease:'back.in(1.2)'},%.3f);" % hit,
        "tl.to('#verdict',{x:12,y:-8,duration:0.15},%.3f);" % (hit + 0.5),
        "tl.to('#verdict',{x:0,y:0,duration:0.15},%.3f);" % (hit + 0.65),
    ]
    return [tag], tweens


def lesson_slate(job, scene0_dur):
    """LESSON #NNN, top-left, fading in at 0.2s over the first scene."""
    try:
        n = int(job.get("lesson") or job.get("n") or 1)
    except (TypeError, ValueError):
        n = 1
    dur = max(2.0, scene0_dur)
    tag = ('<div id="lesson" class="clip" data-start="0.20" data-duration="%.2f" '
           'data-track-index="5">LESSON #%03d</div>' % (dur, n))
    tween = "tl.fromTo('#lesson',{opacity:0},{opacity:1,duration:0.5,ease:'power2.out'},0.200);"
    return [tag], [tween]


def croc_entrance(t):
    """A guaranteed entrance, so the host is on screen even if every LLM scene
    fragment fell back to something quiet."""
    return [
        "tl.to('#croc',{opacity:1,duration:0.4},%.3f);" % t,
        "tl.fromTo('#croc',{x:1980},{x:1450,duration:1.0,ease:'power2.out'},%.3f);" % t,
    ]


def croc_markup():
    """The host as a single flat image (assets/croc.png), copied in by _copy_croc.
    No rig: the framework slides him in (croc_entrance) and gives him a gentle idle
    zoom/rotate/drift (croc_motion) -- that is all a flat PNG can do. If the image is
    missing we still emit an element carrying #croc so the baked tweens are no-ops."""
    if not os.path.exists(CROC_PNG):
        print("[compose] WARNING: %s missing -- the host image will be absent" % CROC_PNG)
        return '<div id="croc" style="background:var(--croc);border-radius:24px"></div>'
    return '<img id="croc" src="assets/croc.png" alt="Professor Croc">'


# ---------------------------------------------------------------- assets

def _copy_gsap(assets):
    dst = os.path.join(assets, "gsap.min.js")
    if os.path.exists(VENDOR_GSAP):
        shutil.copyfile(VENDOR_GSAP, dst)
        return True
    print("[compose] !!!! %s IS MISSING -- writing a stub that fails loudly at render "
          "time. Restore it from assets/vendor/README.txt." % VENDOR_GSAP)
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write(GSAP_STUB)
    return False


def _copy_croc(assets):
    """assets/rig/croc.png -> the project's assets/croc.png (the one flat host image
    used across every scene). Missing is a loud warning, never a crash."""
    dst = os.path.join(assets, "croc.png")
    if os.path.exists(CROC_PNG):
        shutil.copyfile(CROC_PNG, dst)
        return True
    print("[compose] WARNING: %s missing -- the host image will be absent" % CROC_PNG)
    return False


def _copy_fonts(assets):
    dst = os.path.join(assets, "fonts")
    os.makedirs(dst, exist_ok=True)
    for name in ("display.woff2", "mono.woff2"):
        src = os.path.join(FONT_DIR, name)
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(dst, name))
        else:
            print("[compose] WARNING: font %s missing -- Chrome will fall back to Arial Black" % src)


def _copy_scene_media(job, i, work_dir, assets):
    """work/i{id}_{i}.jpg -> assets/s{i}.jpg, work/v{id}_{i}.mp3 -> assets/a{i}.mp3."""
    job_id = job.get("id", "")
    have = {}
    for kind, src_name, dst_name in (("img", "i%s_%d.jpg" % (job_id, i), "s%d.jpg" % i),
                                     ("audio", "v%s_%d.mp3" % (job_id, i), "a%d.mp3" % i)):
        src = os.path.join(work_dir or ".", src_name)
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(assets, dst_name))
            have[kind] = True
        else:
            have[kind] = False
            print("[compose] scene %d has no %s (%s) -- element omitted from the composition"
                  % (i, kind, src))
    return have["img"], have["audio"]


# ---------------------------------------------------------------- the build

def build_project(job, work_dir):
    """Write work/proj_{id}/ and return (project_dir, total_duration).

    Deterministic: the same job dict always produces a byte-identical index.html.
    """
    from pipeline.htmlgen import scene_words

    job_id = str(job.get("id") or "job")
    proj = os.path.join(work_dir or ".", "proj_%s" % job_id)
    assets = os.path.join(proj, "assets")
    os.makedirs(assets, exist_ok=True)
    _copy_gsap(assets)
    _copy_fonts(assets)
    _copy_croc(assets)

    scenes = job.get("scenes") or []
    body, tl = [], []
    t0 = 0.0

    for i, scene in enumerate(scenes):
        scene = scene or {}
        dur = _num(scene.get("dur"))
        words = scene_words(job, i, work_dir)
        have_img, have_audio = _copy_scene_media(job, i, work_dir, assets)

        body.extend(media_tags(i, t0, dur, have_img, have_audio))

        cap_tags, cap_tweens = caption_block(i, t0, words)
        body.extend(cap_tags)
        tl.extend(cap_tweens)

        tl.extend(croc_motion(i, t0, dur))

        card_tags, card_tweens = stat_card(i, t0, dur, scene.get("stat"))
        body.extend(card_tags)
        tl.extend(card_tweens)

        if i == 0:
            slate_tags, slate_tweens = lesson_slate(job, dur)
            body.extend(slate_tags)
            tl.extend(slate_tweens)
            # Only bake an entrance if the scene fragment does not already do one,
            # otherwise the two tweens overlap on the same properties.
            if "#croc'" in str(scene.get("frag_js") or "") or '#croc"' in str(scene.get("frag_js") or ""):
                print("[compose] scene 0 brings the croc in itself -- skipping the baked entrance")
            else:
                tl.extend(croc_entrance(0.3))

        frag_html = str(scene.get("frag_html") or "").strip()
        if frag_html:
            body.append(frag_html)
        for line in str(scene.get("frag_js") or "").splitlines():
            line = line.strip()
            if line:
                tl.append(line)

        t0 = round(t0 + dur, 3)

    total = round(t0, 3)
    stamp_tags, stamp_tweens = verdict_stamp(job, total)
    body.extend(stamp_tags)
    tl.extend(stamp_tweens)

    page = (
        "<!doctype html>\n<html>\n<head>\n<meta charset=\"utf-8\">\n"
        "<title>SCALED %s</title>\n"
        "<script src=\"assets/gsap.min.js\"></script>\n<style>\n%s\n%s</style>\n</head>\n<body>\n"
        "<div id=\"stage\" data-composition-id=\"scaled\" data-start=\"0\" data-duration=\"%.2f\" "
        "data-width=\"1920\" data-height=\"1080\" data-fps=\"30\">\n"
        "%s\n%s\n</div>\n"
        "<script>\nconst tl = gsap.timeline({paused:true});\n%s\n"
        "window.__timelines = {scaled: tl};\n</script>\n</body>\n</html>\n"
        % (_esc(job_id), load_style(), COMPOSITION_CSS, max(total, 0.1),
           croc_markup(), "\n".join(body), "\n".join(tl))
    )

    with open(os.path.join(proj, "index.html"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(page)

    print("[compose] %s: %d scenes, %.2fs, %d timeline lines, media mode %s"
          % (proj, len(scenes), total, len(tl), MEDIA_MODE))
    return proj, total
