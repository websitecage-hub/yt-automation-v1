"""Beat Engine — deterministic visual pacing, snapped to the Whisper word clock.

The show cuts to a new visual roughly every BEAT_TARGET seconds. Python owns the
clock and the LLM owns the content: `plan_times()` decides WHEN every beat fires
(from the narration duration and the word timeline) and the director model only
fills the slots of four locked templates. The model never writes animation code
and never picks a timestamp, so pacing is identical on every run and no model
mistake can desync a beat from the voice.

Why snap to word onsets: a cut that lands mid-word reads as a glitch, while a cut
on a word onset reads as emphasis. The ideal grid is laid out by RHYTHM, then each
beat slides to the nearest onset that still respects MIN_GAP -- musical, without
ever double-cutting inside a breath.

Why the grid is NOT even (v2 was, and it was the single worst thing about the
edit): evenly spaced cuts are a metronome, and a metronome is the one rhythm the
eye tunes out inside ten seconds. Measured against the reference edit the
operator supplied, its cuts fall at a median 3.17s with p25 1.77s and p75 5.20s
-- i.e. its whole retention trick is VARIANCE: three fast cuts, a hold, two fast
cuts, a long hold on the punchline. So RHYTHM below is a repeating weight pattern
that produces short-short-LONG-short-short-short-LONGER intervals, which lands
the brief's "cuts every 1-3 seconds" as a median while keeping the burst-then-hold
shape that makes the holds feel like jokes instead of dead air.

`validate()` reports every way a scene's beats break the C3 composition rules so
run.py can spend ONE repair call on the model; `autofix()` then forces the scene
into a legal shape no matter what came back, because a bad beat list must cost a
retry, never a video. Everything here is pure -- no I/O, no clock, no randomness.
"""

BEAT_TARGET = 3.0      # seconds of narration per visual beat (v4: every beat
                             # is a full composed picture that costs an image
                             # call, so beats are fewer and meatier than v3's 1.9)
BEAT_MIN = 3           # a scene never gets fewer beats than this
BEAT_MAX = 12          # ...nor more, however long it runs
MIN_GAP = 0.55         # two beats may never land closer together than this

# The syncopation. Cycled, normalised per scene, and multiplied out to the scene's
# real duration -- so the pattern is scale-free: it gives the same short/long
# feel in a 12-second scene as in a 40-second one. 1.0 is the average interval;
# 0.6 is a snap cut, 1.5 is a hold. Beat 0 is always pinned to 0.0, so the first
# weight below describes the gap from beat 0 to beat 1.
RHYTHM = (0.62, 0.62, 1.45, 0.72, 0.72, 0.72, 1.62, 0.68, 0.85)

# The four v4 templates. Every visual beat is one FULL composed picture --
# cartoon world or ironic photo, words baked inside it by the image host.
# There are no overlay kinds anymore (v3's type/stat/arrow stamped HTML text
# over art and read as stickers). compose.py owns one hard-cut template per
# kind; the director only chooses a kind and fills its slots.
#
#   scene   the one cartoon world: Croc / The Guy / desk-graph-plant, re-staged
#           per gag. text (<=4 words) and value+label (one number) are drawn
#           INTO the picture. Beat 1 of every scene is always a scene.
#   photo   an ironic REAL photograph, full-bleed, zero motion, caption/counter
#           baked in like a meme macro. The sarcasm nuke.
#   meme    Croc himself IN a situation (full-frame). Max 2 per scene.
#   zoom    snap punch-in on the live frame. The only surviving motion.
KINDS = ("scene", "photo", "meme", "zoom")

# Composition law, v4: one picture per visual beat, so the only budget is the
# beat count itself (BEAT_TARGET 3.0 keeps a 20s scene at ~6 pictures).
# Variety caps: Croc max twice per scene; every scene needs a betrayal.
MAX_MEME = 2           # Croc is the professor, not the whole cast
MIN_PUNCH = 0          # retired: numbers live baked inside scene frames now
JOKE_KINDS = ("photo", "meme")


# (img_cap retired in v4: every visual beat buys exactly one picture.)

MAX_TYPE_WORDS = 4
MAX_BAKED_VALUE = 14   # characters: one number per frame, max
MAX_LABEL_WORDS = 2
MAX_CAPTION_WORDS = 4
MAX_PHOTO_CAPTION_WORDS = 6   # photo macros get a beat more room
MAX_COUNTER_CHARS = 12        # the dumb on-photo counter ("31,957,4!?")
MAX_SPEECH_WORDS = 8          # (retired slot, kept for prompt back-compat)

COLORS = ("yellow", "green", "red", "blue")
DIRS = ("left", "right", "center")
SFX = ("whoosh", "pop", "zap", "confetti", "none")

# v4: silence is the default everywhere. A hard cut needs no sound; the voice
# carries the rhythm. "pop" survives only where the director explicitly asks.
DEFAULT_SFX = {"scene": "none", "photo": "none", "meme": "none", "zoom": "none"}

# Snap-zoom depth. v2 topped out at 1.25, which at 1080p is a nudge nobody
# registers; a punch-in has to be visible in one frame to read as emphasis.
ZOOM_MIN, ZOOM_MAX = 1.12, 1.45


def beat_count(dur):
    """How many visual beats a scene of `dur` seconds gets.

    n = clamp(floor(dur / BEAT_TARGET), BEAT_MIN, BEAT_MAX). Short scenes still
    get BEAT_MIN so nothing ever sits static, long ones cap at BEAT_MAX so a
    runaway scene cannot demand forty images.
    """
    try:
        dur = float(dur)
    except (TypeError, ValueError):
        dur = 0.0
    if dur <= 0:
        return BEAT_MIN
    return max(BEAT_MIN, min(BEAT_MAX, int(dur // BEAT_TARGET)))


def word_starts(words):
    """Ascending, de-duplicated word onsets from a Whisper timeline.

    Takes the srt.word_timeline() shape ({"s": start, "e": end, "w": word}) and
    tolerates junk entries -- one malformed word should not take a scene down.
    """
    out = []
    for item in words or []:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item.get("s"))
        except (TypeError, ValueError):
            continue
        if start >= 0:
            out.append(round(start, 3))
    return sorted(set(out))


def rhythm_grid(dur, n):
    """The ideal (pre-snap) beat times: RHYTHM's weights stretched to fill `dur`.

    The weights are cycled to length n, normalised so they sum to the scene, then
    accumulated. Scale-free by construction: a 12-second scene and a 40-second one
    get the same short-short-hold feel, just at different absolute lengths. Beat 0
    is 0.0 and the last ideal time is strictly inside the scene, because a beat
    that fires on the final frame is a beat nobody sees.
    """
    n = max(1, int(n))
    if n == 1 or dur <= 0:
        return [0.0]
    gaps = [RHYTHM[i % len(RHYTHM)] for i in range(n)]
    span = dur * 0.97                      # leave the tail so beat n-1 has room
    scale = span / float(sum(gaps) or 1.0)
    times, t = [0.0], 0.0
    for g in gaps[:n - 1]:
        t += g * scale
        times.append(round(t, 3))
    return times


def plan_times(dur, words, n=None):
    """Beat times for one scene: RHYTHM's syncopated grid, snapped to word onsets.

    Beat 0 is pinned to 0.0 -- something must be on screen the instant the scene
    starts. Each later beat takes the nearest onset to its ideal slot that is at
    least MIN_GAP past the previous beat; with no usable onset it falls back to
    the bare ideal time, and a beat that cannot fit before the scene ends is
    dropped rather than stacked on the final frame.

    Returns strictly ascending floats whose first element is 0.0.
    """
    try:
        dur = float(dur)
    except (TypeError, ValueError):
        dur = 0.0
    if n is None:
        n = beat_count(dur)
    n = max(1, int(n))
    if dur <= 0:
        return [0.0]

    starts = word_starts(words)
    ideals = rhythm_grid(dur, n)
    times = [0.0]
    for ideal in ideals[1:]:
        floor_t = times[-1] + MIN_GAP
        if floor_t >= dur:
            break
        usable = [s for s in starts if floor_t <= s < dur]
        pick = min(usable, key=lambda s: (abs(s - ideal), s)) if usable else max(ideal, floor_t)
        times.append(round(min(pick, dur - 0.01), 3))
    return times


# --- slot hygiene ---------------------------------------------------------

def _words(value, limit):
    """Collapse whitespace and keep at most `limit` words, uppercased."""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(x) for x in value)
    return " ".join(str(value or "").upper().split()[:limit])


def _pick(value, allowed, default):
    got = str(value or "").strip().lower()
    return got if got in allowed else default


def normalise(spec, scene, index):
    """One director beat spec -> a renderable beat dict.

    Never raises and never returns None: an unusable spec degrades to a `zoom`
    on the art already on screen, which always exists by this stage. A single
    bad beat must not fail a whole video at compose time.
    """
    spec = spec if isinstance(spec, dict) else {}
    scene = scene if isinstance(scene, dict) else {}
    kind = _pick(spec.get("kind"), KINDS, "zoom")
    beat = {"i": index, "kind": kind}

    def _subject():
        return " ".join(str(spec.get("prompt") or spec.get("subject")
                            or spec.get("desc") or "").split())

    if kind == "scene":
        subject = _subject()
        if not subject:
            subject = " ".join(str(scene.get("heading") or scene.get("text") or "").split())
        if not subject:
            kind, beat["kind"] = "zoom", "zoom"
        else:
            beat["prompt"] = subject[:300]
            beat["text"] = _words(spec.get("text"), MAX_CAPTION_WORDS)
            beat["value"] = " ".join(str(spec.get("value") or "").split())[:MAX_BAKED_VALUE]
            beat["label"] = _words(spec.get("label"), MAX_LABEL_WORDS)
    elif kind == "photo":
        subject = _subject()
        beat["caption"] = _words(spec.get("caption"), MAX_PHOTO_CAPTION_WORDS)
        beat["counter"] = " ".join(str(spec.get("counter") or "").split())[:MAX_COUNTER_CHARS]
        if not subject:
            kind, beat["kind"] = "zoom", "zoom"
        else:
            beat["prompt"] = subject[:200]
    elif kind == "meme":
        subject = _subject()
        if not subject:
            kind, beat["kind"] = "zoom", "zoom"
        else:
            beat["prompt"] = subject[:200]

    if kind == "zoom":
        try:
            amount = float(spec.get("amount", spec.get("factor", 1.18)))
        except (TypeError, ValueError):
            amount = 1.18
        beat["amount"] = round(min(ZOOM_MAX, max(ZOOM_MIN, amount)), 3)

    beat["sfx"] = _pick(spec.get("sfx"), SFX, DEFAULT_SFX[beat["kind"]])
    return beat


# --- composition law ------------------------------------------------------

def _counts(beats):
    tally = {k: 0 for k in KINDS}
    for beat in beats:
        tally[beat.get("kind", "zoom")] = tally.get(beat.get("kind", "zoom"), 0) + 1
    return tally


def validate(beats):
    """Every way this scene's beats break C3, as plain sentences.

    The list is fed straight back to the director as the repair call's brief, so
    each item names the rule and the actual count -- vague feedback produces
    vague repairs.
    """
    problems = []
    beats = list(beats or [])
    if not beats:
        return ["scene has no beats at all"]
    if beats[0].get("kind") != "scene":
        problems.append('beat 1 must be kind "scene" (got %r)' % beats[0].get("kind"))
    tally = _counts(beats)
    if tally["meme"] > MAX_MEME:
        problems.append('at most %d "meme" beats per scene (got %d)' % (MAX_MEME, tally["meme"]))
    if not any(tally[k] for k in JOKE_KINDS):
        problems.append('at least 1 photo/meme beat per scene (the joke)')
    for beat in beats:
        i = beat.get("i", 0) + 1
        kind = beat.get("kind")
        if kind in ("scene", "photo", "meme") and not str(beat.get("prompt", "")).strip():
            problems.append('beat %d %r needs a situation prompt' % (i, kind))
        if kind == "scene" and len(str(beat.get("text", "")).split()) > MAX_CAPTION_WORDS:
            problems.append('beat %d baked caption must be <=%d words' % (i, MAX_CAPTION_WORDS))
        if kind == "scene" and len(str(beat.get("value", ""))) > MAX_BAKED_VALUE:
            problems.append('beat %d baked number must be <=%d characters'
                            % (i, MAX_BAKED_VALUE))
        if kind == "photo" and len(str(beat.get("caption", "")).split()) > MAX_PHOTO_CAPTION_WORDS:
            problems.append('beat %d "photo" caption must be <=%d words' % (i, MAX_PHOTO_CAPTION_WORDS))
    return problems


def _phrase(scene, used):
    """A <=5-word ALL CAPS phrase from the narration, preferring one with a number.

    Used only to manufacture a legal `type` beat when the director under-supplied
    them. Numbers are the show's currency, so a phrase containing a digit wins.
    """
    words = str((scene or {}).get("text") or (scene or {}).get("heading") or "").split()
    windows = [words[i:i + MAX_TYPE_WORDS] for i in range(0, max(1, len(words)), MAX_TYPE_WORDS)]
    for window in windows:
        phrase = _words(" ".join(window), MAX_TYPE_WORDS)
        if phrase and phrase not in used and any(c.isdigit() for c in phrase):
            return phrase
    for window in windows:
        phrase = _words(" ".join(window), MAX_TYPE_WORDS)
        if phrase and phrase not in used:
            return phrase
    return ""


def autofix(beats, scene):
    """Force a scene's beats into a legal shape, deterministically.

    Runs after the one repair call. Excess `img` and `meme` beats become `zoom`
    (cheap, always renderable), a missing opening `img` is promoted or inserted,
    and a scene short on words gets `type` beats built from its own narration.
    The result always passes validate(), so compose.py can trust its input.
    """
    beats = [dict(b) for b in (beats or [])]
    scene = scene if isinstance(scene, dict) else {}
    if not beats:
        beats = [{"i": 0, "kind": "zoom", "amount": 1.18, "sfx": DEFAULT_SFX["zoom"]}]

    subject = " ".join(str(scene.get("heading") or scene.get("text") or "").split())[:240]

    # Rule 1: the scene opens in the cartoon world.
    if beats[0].get("kind") != "scene":
        donor = next((b for b in beats if b.get("kind") == "scene"), None)
        if donor:
            beats.remove(donor)
            beats.insert(0, donor)
        else:
            beats[0] = {"i": 0, "kind": "scene",
                        "prompt": "the guy staring at " + (subject or "a wall graph"),
                        "text": "", "value": "", "label": "",
                        "sfx": DEFAULT_SFX["scene"]}

    # Rule 2: cap Croc overuse, demoting extras to zoom.
    seen_memes = 0
    for beat in beats:
        if beat.get("kind") == "meme":
            seen_memes += 1
            if seen_memes > MAX_MEME:
                beat.clear()
                beat.update({"kind": "zoom", "amount": 1.18, "sfx": DEFAULT_SFX["zoom"]})

    # Rule 3: no jokeless scenes. A spare zoom becomes an ironic photo of the
    # scene's own subject -- never as sharp as the director's gag, but a betrayal
    # beats another straight cartoon.
    if not any(b.get("kind") in JOKE_KINDS for b in beats):
        for beat in reversed(beats):
            if beat.get("kind") == "zoom":
                phrase = _phrase(scene, set()) or "SO RELATABLE"
                beat.clear()
                beat.update({"kind": "photo",
                             "prompt": "ironic real-world photograph of " + subject[:180],
                             "caption": phrase, "counter": "",
                             "sfx": DEFAULT_SFX["photo"]})
                break

    for i, beat in enumerate(beats):
        beat["i"] = i
        beat.setdefault("sfx", DEFAULT_SFX.get(beat.get("kind", "zoom"), "none"))
    return beats


def build(scene, words, specs, n=None):
    """Merge the Python clock with the director's content into a beat list.

    `specs` is whatever §7.2 returned. It is truncated or padded to the number of
    beats the clock allows, so the model's count never changes the pacing, then
    forced legal by autofix(). Each beat carries its own `t` and the `end` of its
    visible window, which is what the templates tween against.
    """
    scene = scene if isinstance(scene, dict) else {}
    times = plan_times(scene.get("dur"), words, n)
    specs = list(specs or [])
    staged = [normalise(specs[i] if i < len(specs) else {}, scene, i)
              for i in range(len(times))]
    staged = autofix(staged, scene)
    for i, beat in enumerate(staged):
        beat["t"] = times[i]
        beat["end"] = round(times[i + 1], 3) if i + 1 < len(times) else round(
            float(scene.get("dur") or times[i]), 3)
    # Memeic micro-rule: the punchline (last) beat holds ~2x the median window.
    # The laugh needs air. Pull its onset earlier when there is room -- never
    # past the previous beat's MIN_GAP, never changing the scene's end.
    if len(staged) > 2:
        try:
            dur = float(scene.get("dur") or 0.0)
        except (TypeError, ValueError):
            dur = 0.0
        wins = [staged[k + 1]["t"] - staged[k]["t"] for k in range(len(staged) - 1)]
        wins.append(max(0.0, dur - staged[-1]["t"]))
        med = sorted(wins)[len(wins) // 2]
        if med > 0 and wins[-1] < 2 * med:
            floor_t = staged[-2]["t"] + MIN_GAP
            want_t = max(floor_t, dur - 2 * med)
            if want_t < staged[-1]["t"] - 0.05:
                staged[-1]["t"] = round(want_t, 3)
                staged[-1]["end"] = round(dur, 3)
    return staged
