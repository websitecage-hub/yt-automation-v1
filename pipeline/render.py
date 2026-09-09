"""Turns a baked Hyperframes project into the finished mp4 (PART D / TASK 11).

compose.py has already done the hard part: index.html carries every visual beat,
Croc's idle bob, the music bed, the SFX hits and the verdict stamp on one paused
GSAP timeline, 1920x1080 at 60fps, with Whisper as the only clock. All this
module does is *play it out to a file*.

Two rendering paths, in order of fidelity:
  * the real renderer -- `hyperframes render` walks the paused timeline and bakes
    BOTH the video and the composition's own audio elements, so its mp4 is final;
  * a Ken Burns ffmpeg slideshow, so a missing/broken renderer still yields a
    watchable cut: one slow-zoom clip per scene built from that scene's FIRST
    beat still, concatenated, with the per-scene narration muxed back on top.
    The fallback is deliberately dumber than the composition -- it keeps the
    words and the pictures, and loses the pacing.

One renderer fact is load-bearing and unconfirmed on this machine -- the exact
render output flag -- so it is probed once against `hyperframes render --help`
once and the answer is cached for the process (see the PART I register).

Everything the ffmpeg fallback needs is built by pure, side-effect-free argv
helpers (kenburns_frames / ffmpeg_still_cmd / concat_cmd / mux_cmd) so the command
shapes can be unit-tested without ffmpeg on the box. Failures raise RuntimeError
loudly and never leave a half-written mp4 behind.
"""

import os
import re
import shutil
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HF = ["npx", "-y", "hyperframes@0.8.16"]
RENDER_TIMEOUT = 3600     # a full render is slow; this is the ceiling, not a target
FPS = 60                  # matches data-fps="60" baked by compose.build_project (C1)

# The render output flag, VERIFIED against hyperframes 0.8.16 on 2026-09-03:
#
#     -o, --output=<output>    Output path (default: renders/<name>.mp4)
#
# `--out` is NOT accepted -- `hyperframes render --out x.mp4` answers "Unknown
# flag: --out" and exits non-zero, which would have sent every single render
# down the ffmpeg slideshow fallback. _render_outflag() still probes, because a
# future version may rename it, but it now matches whole tokens: `--out` is a
# substring of `--output`, so a naive `in` test picks the broken spelling out of
# a help text that only ever offered the working one.
RENDER_OUTFLAG = "--output"
_OUTFLAG_PROBED = False


# ---------------------------------------------------------------- logging

def _short(snippet, n=90):
    one = " ".join(str(snippet).split())
    return one if len(one) <= n else one[: n - 1] + "…"


# ------------------------------------------------ pure ffmpeg command builders

def kenburns_frames(dur, fps=FPS):
    """Frames in one scene's Ken Burns clip: round(dur*fps), never below 1.

    The clip length is the scene duration -- Whisper is the only clock -- so the
    frame count is derived straight from it, matching compose's 60fps timeline.
    """
    try:
        n = round(float(dur) * float(fps))
    except (TypeError, ValueError):
        n = 0
    return max(1, int(n))


def ffmpeg_still_cmd(img_path, dur, out_path, fps=FPS):
    """Argv for one Ken Burns clip: a still image, slow-zoomed, 1920x1080 yuv420p.

    Encodes EXACTLY kenburns_frames(dur) frames -- pinned twice, by the zoompan
    d= duration and by -frames:v, so the clip lands on the scene's frame budget
    regardless of how -loop feeds the encoder. Pure: builds argv, runs nothing.
    """
    n = kenburns_frames(dur, fps)
    span = max(n - 1, 1)          # linear zoom denominator; guards n == 1
    vf = (
        "scale=1920:1080:force_original_aspect_ratio=increase,"
        "crop=1920:1080,"
        "zoompan=z='1+0.12*on/%d':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        ":d=%d:s=1920x1080:fps=%d,"
        "format=yuv420p" % (span, n, fps)
    )
    return [
        "ffmpeg", "-y",
        "-loop", "1",
        "-i", str(img_path),
        "-frames:v", str(n),
        "-vf", vf,
        "-r", str(fps),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-an",
        str(out_path),
    ]


def concat_cmd(part_paths, out_path):
    """Argv to join per-scene clips into one silent video via the concat filter.

    The filter graph (not the demuxer) keeps this side-effect-free -- no list file
    to write -- so it is a pure argv builder like its siblings.
    """
    cmd = ["ffmpeg", "-y"]
    for p in part_paths:
        cmd += ["-i", str(p)]
    n = len(part_paths)
    streams = "".join("[%d:v]" % k for k in range(n))
    graph = "%sconcat=n=%d:v=1:a=0[v]" % (streams, n)
    cmd += [
        "-filter_complex", graph,
        "-map", "[v]",
        "-r", str(FPS),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        str(out_path),
    ]
    return cmd


def mux_cmd(video_path, audio_path, out_path):
    """Argv to lay one audio track over the finished video (video copied, not re-encoded)."""
    return [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-i", str(audio_path),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        str(out_path),
    ]


def _audio_concat_cmd(audio_paths, out_path):
    """Argv to join per-scene narration mp3s, in order, into one track (concat filter)."""
    cmd = ["ffmpeg", "-y"]
    for p in audio_paths:
        cmd += ["-i", str(p)]
    n = len(audio_paths)
    streams = "".join("[%d:a]" % k for k in range(n))
    graph = "%sconcat=n=%d:v=0:a=1[a]" % (streams, n)
    cmd += [
        "-filter_complex", graph,
        "-map", "[a]",
        "-c:a", "libmp3lame",
        str(out_path),
    ]
    return cmd


# ---------------------------------------------------------------- real renderer

def _render_outflag():
    """Which of --output/-o/--out this `hyperframes render` wants.

    Probe `render --help` exactly once, cache the answer in a module global and
    log the choice. Matching is on whole tokens, longest spelling first: `--out`
    is a substring of `--output`, and a plain `in` test against 0.8.16's help
    therefore selects a flag the CLI rejects outright. Falls back to the
    verified default when the help text cannot be read or names none of them.
    """
    global RENDER_OUTFLAG, _OUTFLAG_PROBED
    if _OUTFLAG_PROBED:
        return RENDER_OUTFLAG
    _OUTFLAG_PROBED = True
    try:
        out = subprocess.run(HF + ["render", "--help"], capture_output=True,
                             text=True, timeout=RENDER_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        print("[render] `hyperframes render --help` could not run: %s -- defaulting to %s"
              % (e, RENDER_OUTFLAG))
        return RENDER_OUTFLAG
    blob = (out.stdout or "") + (out.stderr or "")
    for candidate in ("--output", "--out", "-o"):
        if re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(candidate), blob):
            RENDER_OUTFLAG = candidate
            print("[render] render output flag in use: %s" % candidate)
            return candidate
    print("[render] no output flag found in `render --help` -- defaulting to %s" % RENDER_OUTFLAG)
    return RENDER_OUTFLAG


def _try_hyperframes_render(proj, mp4):
    """Run `hyperframes render <proj> <flag> <mp4>`. True only on a clean exit."""
    cmd = HF + ["render", str(proj), _render_outflag(), str(mp4)]
    print("[render] hyperframes render: %s" % _short(" ".join(cmd), 120))
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=RENDER_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        print("[render] `hyperframes render` could not run (%s) -- using the ffmpeg fallback" % e)
        return False
    if out.returncode != 0:
        print("[render] `hyperframes render` failed rc=%s: %s"
              % (out.returncode, _short((out.stderr or out.stdout), 200)))
        return False
    return True


# ---------------------------------------------------------------- ffmpeg fallback

def _run(cmd, label):
    """Run a built ffmpeg argv, or raise RuntimeError with the tail of its output."""
    print("[render] %s: %s" % (label, _short(" ".join(cmd), 120)))
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=RENDER_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError("[render] %s could not run (%s)" % (label, e))
    if out.returncode != 0:
        raise RuntimeError("[render] %s failed rc=%s: %s"
                           % (label, out.returncode, _short((out.stderr or out.stdout), 300)))


def _scene_still(assets, i):
    """Scene i's first beat still inside the built project, or None.

    Beats are named b{scene}_{beat}.ext, so the sort key is the numeric beat index
    -- lexically, b2 would sort before b10 and the slideshow would open a scene on
    its middle picture.
    """
    prefix = "b%d_" % i
    found = []
    for name in os.listdir(assets) if os.path.isdir(assets) else []:
        if not name.startswith(prefix):
            continue
        stem, ext = os.path.splitext(name)
        if ext.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            continue
        try:
            found.append((int(stem[len(prefix):]), name))
        except ValueError:
            continue
    if not found:
        return None
    return os.path.join(assets, sorted(found)[0][1])


def _scene_audio(assets, i):
    """Scene i's narration inside the built project, whatever container it is in."""
    for ext in ("mp3", "wav", "m4a", "ogg", "flac"):
        path = os.path.join(assets, "a%d.%s" % (i, ext))
        if os.path.exists(path):
            return path
    return None


def has_audio(path):
    """True when `path` really carries an audio stream.

    The renderer can exit 0 having silently dropped every <audio> -- that is what
    a missing element id does -- and a silent 8-minute lecture is worse than a
    slideshow. ffprobe is the only honest check; when it cannot run we assume the
    audio is there rather than throwing away a good render.
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        print("[render] ffprobe unavailable (%s) -- assuming %s has audio"
              % (e, os.path.basename(str(path))))
        return True
    return "audio" in (out.stdout or "")


def _fallback_slideshow(job, proj, work_dir, mp4):
    """Ken Burns slideshow: one slow-zoom clip per scene image, concatenated, with
    the per-scene narration muxed on top. Raises if there is nothing to render, and
    never leaves a partial mp4 or intermediate file behind."""
    from pipeline import compose

    assets = os.path.join(proj, "assets")
    durs = compose.scene_durations(job)
    temps = []
    try:
        parts, audio_paths = [], []
        for i, dur in enumerate(durs):
            img = _scene_still(assets, i)
            if not img:
                print("[render] scene %d has no beat still in %s -- skipped in the slideshow"
                      % (i, assets))
                continue
            part = os.path.join(work_dir, "_kb_%d.mp4" % i)
            _run(ffmpeg_still_cmd(img, dur, part, FPS), "ken burns scene %d (%d frames)"
                 % (i, kenburns_frames(dur, FPS)))
            parts.append(part)
            temps.append(part)
            aud = _scene_audio(assets, i)
            if aud:
                audio_paths.append(aud)

        if not parts:
            raise RuntimeError("[render] no scene images under %s and `hyperframes render` "
                               "was unavailable -- nothing to render" % assets)

        # Silent video: a single clip needs no concat pass.
        if len(parts) == 1:
            video = parts[0]
        else:
            video = os.path.join(work_dir, "_kb_all.mp4")
            _run(concat_cmd(parts, video), "concat %d scene clips" % len(parts))
            temps.append(video)

        # Narration, in scene order, muxed over the video.
        if audio_paths:
            if len(audio_paths) == 1:
                audio = audio_paths[0]
            else:
                audio = os.path.join(work_dir, "_kb_audio.mp3")
                _run(_audio_concat_cmd(audio_paths, audio),
                     "concat %d narration tracks" % len(audio_paths))
                temps.append(audio)
            _run(mux_cmd(video, audio, mp4), "mux video + narration")
        else:
            print("[render] no scene narration found -- writing a silent slideshow")
            shutil.copyfile(video, mp4)

        if not (os.path.exists(mp4) and os.path.getsize(mp4) > 0):
            raise RuntimeError("[render] ffmpeg fallback produced no output at %s" % mp4)
        print("[render] ffmpeg fallback wrote %s (%d bytes)" % (mp4, os.path.getsize(mp4)))
        return mp4
    except Exception:
        if os.path.exists(mp4):                      # never leave a half-written mp4
            try:
                os.remove(mp4)
            except OSError:
                pass
        raise
    finally:
        for t in temps:                              # drop intermediates, keep the mp4
            if t != mp4:
                try:
                    os.remove(t)
                except OSError:
                    pass


def _rescue_silent(proj, work_dir, mp4):
    """Mux the project's narration onto a rendered-but-silent mp4, in place.

    Cheaper and truer than re-rendering: the visuals are already correct, only
    the audio graph was dropped. Writes to a temp file and only moves it over the
    original once ffmpeg succeeded, so a failure leaves the silent video intact.
    """
    assets = os.path.join(proj, "assets")
    tracks = []
    i = 0
    while True:
        found = _scene_audio(assets, i)
        if not found:
            break
        tracks.append(found)
        i += 1
    if not tracks:
        print("[render] no narration in %s to rescue with" % assets)
        return False

    tmp_audio = os.path.join(work_dir, "_rescue_audio.mp3")
    tmp_mp4 = os.path.join(work_dir, "_rescue.mp4")
    try:
        if len(tracks) == 1:
            audio = tracks[0]
        else:
            _run(_audio_concat_cmd(tracks, tmp_audio), "concat %d narration tracks" % len(tracks))
            audio = tmp_audio
        _run(mux_cmd(mp4, audio, tmp_mp4), "mux narration onto the silent render")
        shutil.move(tmp_mp4, mp4)
        print("[render] rescued %s (%d bytes, %d narration tracks)"
              % (mp4, os.path.getsize(mp4), len(tracks)))
        return True
    except Exception as e:
        print("[render] rescue failed: %s" % e)
        return False
    finally:
        for t in (tmp_audio, tmp_mp4):
            if os.path.exists(t):
                try:
                    os.remove(t)
                except OSError:
                    pass


# ---------------------------------------------------------------- entry point

def render_video(job, work_dir):
    """Render `job` to work_dir/<job_id>.mp4 and return that path.

    Builds the composition, tries the real renderer first (its mp4 already carries
    the composition's audio, so a non-empty file is final), and drops to the Ken
    Burns ffmpeg slideshow only when the renderer is missing or fails. Raises
    RuntimeError if neither path yields a usable mp4.
    """
    from pipeline import compose

    work_dir = work_dir or "."
    job_id = str(job.get("id") or "job")
    mp4 = os.path.join(work_dir, "%s.mp4" % job_id)

    proj, total = compose.build_project(job, work_dir)
    print("[render] project %s built (%.2fs) -> %s" % (_short(proj), total, mp4))

    if _try_hyperframes_render(proj, mp4):
        if os.path.exists(mp4) and os.path.getsize(mp4) > 0:
            if has_audio(mp4):
                print("[render] hyperframes rendered %s (%d bytes) -- final"
                      % (mp4, os.path.getsize(mp4)))
                return mp4
            print("[render] hyperframes rendered %s but it has NO audio stream -- "
                  "muxing the narration back on" % mp4)
            if _rescue_silent(proj, work_dir, mp4):
                return mp4
            print("[render] could not rescue the silent render -- falling back to ffmpeg")
        else:
            print("[render] `hyperframes render` exited clean but %s is missing/empty -- "
                  "falling back to ffmpeg" % mp4)

    return _fallback_slideshow(job, proj, work_dir, mp4)
