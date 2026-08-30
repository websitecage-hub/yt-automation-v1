"""Per-scene overlay HTML + GSAP fragments, written by the code LLM behind a hard gate.

The LLM only ever writes decoration: extra overlay elements and tl.* lines inside
its own scene window. Media, captions, the croc lip-sync, stat cards and the
verdict stamp are all baked in Python by compose.py -- see PART C6. Anything the
model returns has to survive a static ban-check, a time-window check, an id-scope
check and a real `hyperframes lint` run before it is allowed near a render.

generate_scene() never raises: after MAX_REPAIRS failed repair rounds it falls
back to SAFE_FALLBACK, which is plain hand-written motion that always passes.
"""

import json
import os
import re
import shutil
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR_GSAP = os.path.join(REPO, "assets", "vendor", "gsap.min.js")

HF = ["npx", "-y", "hyperframes@0.8.16"]
LINT_TIMEOUT = 600
MAX_REPAIRS = 3          # 1 first attempt + 3 repair rounds, then SAFE_FALLBACK
WINDOW_SLACK = 0.2       # a tween may sit 0.2s either side of the scene window

# VERIFY-ON-FIRST-RUN #3 -- both subcommands are coded; the first one that runs
# is remembered here for the rest of the process.
LINT_SUBCOMMAND = None

BANNED = re.compile(
    r"Math\.random|Date\.now|setInterval|setTimeout|requestAnimationFrame"
    r"|fetch\(|localStorage|https?://|@import|<link|<script src"
)
# Framework-owned selectors the model must never touch (PART C6). #croc is now a
# flat framework-animated image (no rig), so the whole #croc* family is off-limits.
FRAMEWORK = re.compile(r"#croc\b|\.croc-eye|#verdict|#lesson|#c\d+-\d+|#statbar|#stat-")
CSS_MOTION = re.compile(r"(?:^|[\s;\"'{])(?:transition|animation)\s*:", re.I)
FENCE = re.compile(r"```[ \t]*([A-Za-z0-9+#]*)[ \t]*\r?\n(.*?)(?:```|\Z)", re.S)
CALL = re.compile(r"tl\s*\.\s*(to|fromTo)\s*\(")
HTML_ID = re.compile(r"""\bid\s*=\s*["']([^"']+)["']""")
JS_SEL = re.compile(r"""["'`]([^"'`]*#[A-Za-z][-\w]*[^"'`]*)["'`]""")
HASH = re.compile(r"#([A-Za-z][-\w]*)")

# Globals the scene prompt allows the model to animate. The host (#croc) is NOT
# here: it is a framework-owned flat image with a baked entrance and idle motion.
ALLOWED_GLOBAL_IDS = {"stage"}


# ---------------------------------------------------------------- brief

def scene_t0(job, i):
    """Absolute start of scene i = the sum of every earlier scene's duration."""
    total = 0.0
    for s in (job.get("scenes") or [])[:i]:
        try:
            total += float(s.get("dur") or 0.0)
        except (TypeError, ValueError):
            pass
    return round(total, 3)


def scene_words(job, i, work_dir):
    """Word list for scene i: whatever srt.py wrote, else whatever is on the job."""
    scene = (job.get("scenes") or [])[i]
    words = scene.get("words")
    if not words and work_dir:
        p = os.path.join(work_dir, "w%s_%d.json" % (job.get("id", ""), i))
        try:
            with open(p, encoding="utf-8") as fh:
                words = json.load(fh)
        except (OSError, json.JSONDecodeError):
            words = []
    return [w for w in (words or []) if isinstance(w, dict)]


def word_clock(words, t0):
    """`word(104.900-105.300)` per word, absolute seconds, space separated."""
    parts = []
    for w in words:
        try:
            s = float(w.get("s", 0)) + t0
            e = float(w.get("e", 0)) + t0
        except (TypeError, ValueError):
            continue
        text = str(w.get("w", "")).strip()
        if not text:
            continue
        parts.append("%s(%.3f-%.3f)" % (text, s, e))
    return " ".join(parts)


def build_brief(job, i, work_dir):
    """Everything the SCENE prompt needs, with the clock already absolute."""
    scene = (job.get("scenes") or [])[i]
    t0 = scene_t0(job, i)
    try:
        dur = float(scene.get("dur") or 0.0)
    except (TypeError, ValueError):
        dur = 0.0
    words = scene_words(job, i, work_dir)
    clock = word_clock(words, t0)
    if not clock:
        clock = "(no word timings for this scene -- treat the narration as evenly paced)"
    return {
        "i": i,
        "title": str(job.get("title", "")),
        "act": str(scene.get("act", "")),
        "heading": str(scene.get("heading", "")),
        "text": str(scene.get("text", "")),
        "image_prompt": str(scene.get("image_prompt", "")),
        "clock": clock,
        "t0": round(t0, 3),
        "t1": round(t0 + dur, 3),
    }


def render_prompt(brief):
    """Fill the SCENE user template. str.replace only -- never str.format."""
    from pipeline.llm import prompt
    user = prompt("SCENE", "user")
    for key, value in (
        ("{i+1}", str(brief["i"] + 1)),
        ("{title}", brief["title"]),
        ("{act}", brief["act"]),
        ("{heading}", brief["heading"]),
        ("{text}", brief["text"]),
        ("{clock}", brief["clock"]),
        ("{t0}", "%.3f" % brief["t0"]),
        ("{t1}", "%.3f" % brief["t1"]),
        ("{image_prompt}", brief["image_prompt"]),
    ):
        user = user.replace(key, value)
    return prompt("SCENE", "system"), user


# ---------------------------------------------------------------- parsing

def parse_fences(text):
    """Pull the html fence then the js fence out of an LLM reply.

    Tolerates messy whitespace, extra prose, upper-case language tags, `javascript`
    instead of `js`, and a final fence the model forgot to close.
    """
    blocks = [(lang.strip().lower(), body) for lang, body in FENCE.findall(text or "")]
    if len(blocks) < 2:
        raise ValueError("expected two fenced blocks (```html then ```js), found %d" % len(blocks))

    html = js = None
    for lang, body in blocks:
        if html is None and lang in ("html", "htm"):
            html = body
        elif js is None and lang in ("js", "javascript", "jsx", "gsap"):
            js = body
    if html is None or js is None:
        # Unlabelled or oddly labelled fences: fall back to positional order.
        html, js = blocks[0][1], blocks[1][1]
    return html.strip("\n"), js.strip("\n")


def _split_args(body):
    """Split a call's argument source on top-level commas."""
    args, depth, quote, esc, buf = [], 0, None, False, []
    for ch in body:
        if quote:
            buf.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                quote = None
            continue
        if ch in "\"'`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            args.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    args.append("".join(buf))
    return [a.strip() for a in args]


def iter_tweens(js):
    """Every tl.to()/tl.fromTo() call: (kind, args, snippet)."""
    out = []
    src = js or ""
    for m in CALL.finditer(src):
        k, depth, quote, esc = m.end(), 1, None, False
        while k < len(src) and depth:
            ch = src[k]
            if quote:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == quote:
                    quote = None
            elif ch in "\"'`":
                quote = ch
            elif ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            k += 1
        body = src[m.end():k - 1] if not depth else src[m.end():k]
        out.append((m.group(1), _split_args(body), src[m.start():k].strip()))
    return out


def tween_times(js):
    """(time_or_None, snippet) per tween -- the last argument, which must be seconds."""
    times = []
    for _kind, args, snippet in iter_tweens(js):
        raw = args[-1] if args else ""
        try:
            times.append((float(raw), snippet))
        except (TypeError, ValueError):
            times.append((None, snippet))
    return times


# ---------------------------------------------------------------- static gate

def _short(snippet, n=90):
    one = " ".join(str(snippet).split())
    return one if len(one) <= n else one[: n - 1] + "…"


def static_violations(html, js, i, t0, t1):
    """Everything checkable without launching a browser. Returns precise strings."""
    v = []
    prefix = "s%d-" % i
    lo, hi = t0 - WINDOW_SLACK, t1 + WINDOW_SLACK

    for blob, where in ((html, "html"), (js, "js")):
        for m in BANNED.finditer(blob or ""):
            v.append("banned token %r in the %s block: %s"
                     % (m.group(0), where, _short(blob[max(0, m.start() - 30):m.start() + 40])))

    if not CALL.search(js or ""):
        v.append("the js block contains no tl.to( or tl.fromTo( call -- nothing would animate")

    for value, snippet in tween_times(js):
        if value is None:
            v.append("tween has no absolute-second time as its last argument "
                     "(position parameter is required): %s" % _short(snippet))
        elif not (lo <= value <= hi):
            v.append("tween time %.3f is outside this scene's window [%.3f, %.3f] "
                     "(+/-%.1fs allowed): %s" % (value, t0, t1, WINDOW_SLACK, _short(snippet)))

    for found in HTML_ID.findall(html or ""):
        if not found.startswith(prefix):
            v.append("element id %r must start with %r -- every id you create is scene-scoped"
                     % (found, prefix))

    own_media = {"img%d" % i, "aud%d" % i}
    for sel in JS_SEL.findall(js or ""):
        for name in HASH.findall(sel):
            if name.startswith(prefix) or name in ALLOWED_GLOBAL_IDS or name in own_media:
                continue
            v.append("selector '#%s' is not yours: animate only #%s* ids, this scene's "
                     "own #img%d, and %s"
                     % (name, prefix, i,
                        ", ".join("#" + g for g in sorted(ALLOWED_GLOBAL_IDS))))

    for blob, where in ((html, "html"), (js, "js")):
        for m in FRAMEWORK.finditer(blob or ""):
            v.append("%r in the %s block is framework-owned (croc lip-sync, captions, "
                     "stat card and verdict stamp are baked in Python) -- do not animate it"
                     % (m.group(0), where))

    for m in CSS_MOTION.finditer(html or ""):
        v.append("CSS %s is banned -- the renderer seeks a paused timeline, so only "
                 "GSAP tweens are captured: %s"
                 % (m.group(0).strip(" ;\"'{"), _short(html[max(0, m.start() - 20):m.start() + 50])))

    # de-duplicate but keep the order the checks fired in
    seen, ordered = set(), []
    for item in v:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


# ---------------------------------------------------------------- lint gate

def _lint_subcommand():
    """VERIFY-ON-FIRST-RUN #3: prefer `lint`, fall back to `check`."""
    global LINT_SUBCOMMAND
    if LINT_SUBCOMMAND:
        return LINT_SUBCOMMAND
    for candidate in ("lint", "check"):
        try:
            out = subprocess.run(HF + [candidate, "--help"], capture_output=True,
                                 text=True, timeout=LINT_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as e:
            print("[htmlgen] `hyperframes %s --help` could not run: %s" % (candidate, e))
            continue
        blob = (out.stdout or "") + (out.stderr or "")
        if out.returncode == 0 and "unknown command" not in blob.lower():
            LINT_SUBCOMMAND = candidate
            print("[htmlgen] validation subcommand in use: %s" % candidate)
            return candidate
        print("[htmlgen] subcommand %r unavailable (rc=%s)" % (candidate, out.returncode))
    LINT_SUBCOMMAND = "lint"
    print("[htmlgen] neither lint nor check probed clean -- trying `lint` anyway")
    return LINT_SUBCOMMAND


def test_composition(html, js, i, t0, t1):
    """A minimal standalone composition holding just this scene's fragments."""
    return (
        "<!doctype html>\n<html>\n<head>\n<meta charset=\"utf-8\">\n"
        "<script src=\"assets/gsap.min.js\"></script>\n"
        "<style>:root{--ink:#F2EDE2;--dim:#7E8C84;--lime:#A6FF3D;--amber:#E8B04B;--red:#D9483B}"
        "html,body{margin:0;padding:0;background:#0A0F0C;color:var(--ink)}</style>\n"
        "</head>\n<body>\n"
        "<div id=\"stage\" data-composition-id=\"scaled\" data-start=\"0\" "
        "data-duration=\"%.2f\" data-width=\"1920\" data-height=\"1080\" data-fps=\"30\">\n"
        "%s\n</div>\n"
        "<script>\nconst tl = gsap.timeline({paused:true});\n%s\n"
        "window.__timelines = {scaled: tl};\n</script>\n</body>\n</html>\n"
        % (max(t1, t0 + 0.1), html, js)
    )


def lint_violations(html, js, i, t0, t1, work_dir):
    """Run the real linter on this fragment. Errors are violations; warnings are logged."""
    proj = os.path.join(work_dir or ".", "lintcheck_s%d" % i)
    assets = os.path.join(proj, "assets")
    try:
        os.makedirs(assets, exist_ok=True)
        if os.path.exists(VENDOR_GSAP):
            shutil.copyfile(VENDOR_GSAP, os.path.join(assets, "gsap.min.js"))
        else:
            print("[htmlgen] WARNING: %s missing -- lint composition has no GSAP" % VENDOR_GSAP)
        with open(os.path.join(proj, "index.html"), "w", encoding="utf-8") as fh:
            fh.write(test_composition(html, js, i, t0, t1))

        cmd = HF + [_lint_subcommand(), "--json", proj]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=LINT_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as e:
            print("[htmlgen] lint could not run (%s) -- skipping the lint gate" % e)
            return []

        blob = (out.stdout or "") + "\n" + (out.stderr or "")
        start = blob.find("{")
        if start < 0:
            print("[htmlgen] lint produced no JSON -- skipping the lint gate. rc=%s %s"
                  % (out.returncode, _short(blob, 200)))
            return []
        try:
            report = json.loads(blob[start: blob.rfind("}") + 1])
        except json.JSONDecodeError as e:
            print("[htmlgen] lint JSON unreadable (%s) -- skipping the lint gate" % e)
            return []

        findings = report.get("findings") or []
        errors = [f for f in findings if str(f.get("severity", "")).lower() == "error"]
        for f in findings:
            if f not in errors:
                print("[htmlgen] lint %s %s: %s"
                      % (f.get("severity"), f.get("code"), _short(f.get("message", ""), 120)))
        # Trust the report body, not the exit status: shell pipes can mask the code.
        if not errors and (report.get("ok") is False or report.get("errorCount")):
            return ["hyperframes lint reported errorCount=%s with no error findings"
                    % report.get("errorCount")]
        return ["hyperframes lint %s: %s | fix: %s"
                % (f.get("code"), _short(f.get("message", ""), 160), _short(f.get("fixHint", ""), 120))
                for f in errors]
    finally:
        shutil.rmtree(proj, ignore_errors=True)


# ---------------------------------------------------------------- fallback

def SAFE_FALLBACK(t0, t1, heading, i=0):
    """Hand-written motion that always passes the gate.

    Heading fades up over [t0, t0+0.8] and the scene image drifts 1.00 -> 1.07
    across the whole window. The host (#croc) is framework-owned and animated by
    compose.py, so the fallback never touches it.
    """
    t0 = float(t0)
    t1 = max(float(t1), t0 + 1.2)
    dur = t1 - t0
    text = re.sub(r"[<>&]", " ", str(heading or "")).strip() or "SCALED"
    html = (
        '<div id="s%d-fbhead" class="clip" data-start="%.2f" data-duration="%.2f" '
        'data-track-index="1" style="position:absolute;left:120px;top:120px;width:1100px;'
        'font:900 72px/1.1 \'Archivo Black\',\'Arial Black\',sans-serif;color:var(--ink);'
        'opacity:0">%s</div>' % (i, t0, dur, text)
    )
    js = "\n".join([
        "tl.fromTo('#s%d-fbhead',{opacity:0,y:40},{opacity:1,y:0,duration:0.8,ease:'power2.out'},%.3f);"
        % (i, t0),
        "tl.fromTo('#img%d',{scale:1.00},{scale:1.07,duration:%.3f,ease:'none'},%.3f);"
        % (i, dur, t0),
    ])
    return html, js


# ---------------------------------------------------------------- entry point

def generate_scene(job, i, work_dir):
    """Fill job["scenes"][i]["frag_html"] / ["frag_js"]. Never raises.

    Path taken is always logged: `llm attempt N`, or `SAFE_FALLBACK`.
    """
    from pipeline.llm import llm_code

    scenes = job.setdefault("scenes", [])
    while len(scenes) <= i:
        scenes.append({})
    scene = scenes[i]

    brief = build_brief(job, i, work_dir)
    t0, t1 = brief["t0"], brief["t1"]
    system, user = render_prompt(brief)

    attempt, feedback = 0, ""
    while attempt <= MAX_REPAIRS:
        attempt += 1
        try:
            reply = llm_code(system, user + feedback)
            html, js = parse_fences(reply)
            problems = static_violations(html, js, i, t0, t1)
            if not problems:
                problems = lint_violations(html, js, i, t0, t1, work_dir)
        except Exception as e:                      # noqa: BLE001 - never break the pipeline
            html = js = None
            problems = ["%s: %s" % (type(e).__name__, e)]

        if not problems:
            scene["frag_html"], scene["frag_js"] = html, js
            scene["frag_source"] = "llm attempt %d" % attempt
            print("[htmlgen] scene %d accepted on attempt %d/%d" % (i, attempt, MAX_REPAIRS + 1))
            return scene

        print("[htmlgen] scene %d attempt %d/%d rejected (%d problems):"
              % (i, attempt, MAX_REPAIRS + 1, len(problems)))
        for p in problems[:12]:
            print("[htmlgen]   - %s" % p)
        feedback = (
            "\n\nYOUR PREVIOUS ATTEMPT WAS REJECTED:\n"
            + "\n".join("- %s" % p for p in problems[:12])
            + "\nFix every point above and return the FULL corrected two fences "
              "(```html then ```js). Keep every tl time inside [%.3f, %.3f]." % (t0, t1)
        )

    scene["frag_html"], scene["frag_js"] = SAFE_FALLBACK(t0, t1, brief["heading"], i)
    scene["frag_source"] = "SAFE_FALLBACK"
    print("[htmlgen] scene %d fell back to SAFE_FALLBACK after %d attempts" % (i, attempt))
    return scene
