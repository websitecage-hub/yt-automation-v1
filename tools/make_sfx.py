"""Synthesise the four SCALED sound effects with ffmpeg. Run once; commit the wavs.

The edit brief calls for "whoosh, pop, or ding sound effects timed precisely to
cuts and text appearances", and compose.py already places one at every beat that
asks for it -- but assets/audio/sfx/ shipped empty, so every one of those beats
rendered silent and the whole percussive layer of the edit was missing.

Rather than block the factory on sourcing a licensed pack, these are generated
from first principles with ffmpeg's `aevalsrc`, which evaluates a sample-level
expression of `t`. No samples, no licence, no attribution, byte-identical on
every machine -- and they are trivially tweakable, which a purchased pack is not.
Each is a classic synthesis recipe:

  whoosh  filtered white noise under a fast attack / exponential decay -- the
          sound of air moving, used on every hard cut to art
  pop     a descending sine blip with a very fast decay -- a mouth pop, used on
          text and stat cards
  zap     a rising sine chirp -- a riser, used on snap zooms
  confetti  three ascending sine pings, a major arpeggio, for the verdict stamp

24 kHz mono to match the Chatterbox voice, so ffmpeg never has to resample when
it mixes the audio graph.

    python3 tools/make_sfx.py [--force]
"""
import os
import subprocess
import sys

SR = 24000
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DST = os.path.join(HERE, "assets", "audio", "sfx")

# ffmpeg's eval has no `pi` constant in aevalsrc expressions -- PI is spelled out.
# `between(t,a,b)` gates a term to a time window, which is how the arpeggio stacks
# three pings into one expression without three separate sources to mix.
SOUNDS = {
    # Noise burst, band-limited by the filters below, 6 ms attack then decay.
    "whoosh": ("(random(0)*2-1)*min(t/0.006,1)*exp(-t*11)", 0.30,
               "highpass=f=420,lowpass=f=5200,volume=0.9"),
    # 780 Hz falling to ~240 Hz, gone in a tenth of a second.
    "pop": ("sin(2*PI*(780-540*t/0.10)*t)*min(t/0.002,1)*exp(-t*34)", 0.12,
            "highpass=f=120,volume=0.85"),
    # Linear chirp 260 -> 3400 Hz. The `t*t` term is what makes it sweep: the
    # instantaneous frequency of sin(2*PI*f(t)*t) rises as f(t) does.
    "zap": ("sin(2*PI*(260+3140*t/0.20)*t)*min(t/0.003,1)*exp(-t*7)", 0.22,
            "highpass=f=180,volume=0.8"),
    # C6-E6-G6 at 80 ms apart, each with its own decay envelope.
    "confetti": ("sin(2*PI*1046*t)*between(t,0,0.18)*exp(-t*16)"
                 "+sin(2*PI*1318*t)*between(t,0.08,0.28)*exp(-(t-0.08)*16)"
                 "+sin(2*PI*1568*t)*between(t,0.16,0.44)*exp(-(t-0.16)*13)", 0.46,
                 "highpass=f=300,volume=0.55"),
}


def build(name, expr, dur, chain, force=False):
    dst = os.path.join(DST, "%s.wav" % name)
    if os.path.exists(dst) and not force:
        print("  %-9s exists, skipping" % name)
        return dst
    # A short fade at each end keeps the WAV from starting or ending on a
    # discontinuity, which is audible as a click at the head of every beat.
    chain = "%s,afade=t=in:st=0:d=0.004,afade=t=out:st=%.3f:d=0.010" % (chain, max(0.0, dur - 0.010))
    # A comma separates FILTERS inside a filtergraph, so every comma belonging to
    # a function call in the expression -- min(a,b), between(t,a,b) -- has to be
    # escaped or ffmpeg reads the tail of the expression as a second filter and
    # exits 234 with no output.
    graph = "aevalsrc=%s:d=%.3f:s=%d:c=mono" % (expr.replace(",", "\\,"), dur, SR)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", graph,
           "-af", chain, "-c:a", "pcm_s16le", "-ar", str(SR), "-ac", "1", dst]
    subprocess.run(cmd, check=True)
    print("  %-9s %.2fs  %d bytes" % (name, dur, os.path.getsize(dst)))
    return dst


def main():
    force = "--force" in sys.argv
    os.makedirs(DST, exist_ok=True)
    print("[sfx] writing to %s" % DST)
    for name, (expr, dur, chain) in sorted(SOUNDS.items()):
        build(name, expr, dur, chain, force)


if __name__ == "__main__":
    main()
