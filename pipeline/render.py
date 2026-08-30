"""Turns a baked Hyperframes project into the finished mp4 (PART D / TASK 11).

compose.py has already done the hard part: index.html carries every media clip,
caption, the croc's idle motion, the stat cards and the verdict stamp on one
paused GSAP timeline, 1920x1080 at 30fps, with Whisper as the only clock. All
this module does is *play it out to a file*.

Two rendering paths, in order of fidelity:
  * the real renderer -- `hyperframes render` walks the paused timeline and bakes
    BOTH the video and the composition's own audio elements, so its mp4 is final;
  * a Ken Burns ffmpeg slideshow, so a missing/broken renderer still yields a
    watchable cut: one slow-zoom clip per scene image, concatenated, with the
    per-scene narration muxed back in on top.

One renderer fact is load-bearing and unconfirmed on this machine -- the exact
render output flag -- so it is probed once against `hyperframes render --help`
exactly the way htmlgen probes its lint subcommand (see the PART I register).

Everything the ffmpeg fallback needs is built by pure, side-effect-free argv
helpers (kenburns_frames / ffmpeg_still_cmd / concat_cmd / mux_cmd) so the command
shapes can be unit-tested without ffmpeg on the box. Failures raise RuntimeError
loudly and never leave a half-written mp4 behind.
"""

import os
import shutil
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HF = ["npx", "-y", "hyperframes@0.8.16"]
RENDER_TIMEOUT = 3600     # a full render is slow; this is the ceiling, not a target
FPS = 30                  # matches data-fps="30" baked by compose.build_project

# VERIFY-ON-FIRST-RUN #1 -- the render output flag is unconfirmed. `--out` is the
# coded default; _render_outflag() probes `render --help` once and, if one of the
# known spellings appears, remembers it here for the rest of the process.
RENDER_OUTFLAG = "--out"
_OUTFLAG_PROBED = False


# ---------------------------------------------------------------- logging

def _short(snippet, n=90):
    one = " ".join(str(snippet).split())
    return one if len(one) <= n else one[: n - 1] + "…"


# ------------------------------------------------ pure ffmpeg command builders

def kenburns_frames(dur, fps=FPS):
    """Frames in one scene's Ken Burns clip: round(dur*fps), never below 1.

    The clip length is the scene duration -- Whisper is the only clock -- so the
    frame count is derived straight from it, matching compose's 30fps timeline.
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
    """VERIFY-ON-FIRST-RUN #1: which of --out/--output/-o `hyperframes render` wants.

    Mirrors htmlgen._lint_subcommand(): probe `render --help` exactly once, cache
    the answer in a module global, log the choice. Defaults to `--out` when the
    help text can't be read or names none of the known spellings.
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
    for candidate in ("--out", "--output", "-o"):
        if candidate in blob:
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
            img = os.path.join(assets, "s%d.jpg" % i)
            if not os.path.exists(img):
                print("[render] scene %d has no image (%s) -- skipped in the slideshow" % (i, img))
                continue
            part = os.path.join(work_dir, "_kb_%d.mp4" % i)
            _run(ffmpeg_still_cmd(img, dur, part, FPS), "ken burns scene %d (%d frames)"
                 % (i, kenburns_frames(dur, FPS)))
            parts.append(part)
            temps.append(part)
            aud = os.path.join(assets, "a%d.mp3" % i)
            if os.path.exists(aud):
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
            print("[render] hyperframes rendered %s (%d bytes) -- final" % (mp4, os.path.getsize(mp4)))
            return mp4
        print("[render] `hyperframes render` exited clean but %s is missing/empty -- "
              "falling back to ffmpeg" % mp4)

    return _fallback_slideshow(job, proj, work_dir, mp4)
