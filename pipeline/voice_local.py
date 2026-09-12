"""Local voice: zero-shot cloning on the runner's CPU, no API key.

Uses Chatterbox TTS (same model family as the old clone API, so the voice
stays continuous) via its pip package. All heavy imports are lazy so this
module imports cleanly on boxes without torch installed; the single error at
generate time tells the operator exactly what to pip-install.

Quality levers (operator-locked): exaggeration 0.0 (flat deadpan delivery, no
editorializing), default cfg weight. One model load per process (cached
globally); weights come from HuggingFace on first use and should be cached
between CI runs via actions/cache on ~/.cache/huggingface.
"""
import os

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_REF = os.path.join(REPO, "assets", "voice", "monopoly_first_8sec.wav")

PIP_HINT = "pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu chatterbox-tts"

# Operator-locked delivery pace: 1.3x tempo (pitch preserved), measured against
# the reference edit's ~225 wpm. Applied to every clone after trimming.
PACE_RATE = 1.3

_model = {"instance": None}
_ref_attrs = {}


def ref_attributes(ref=DEFAULT_REF):
    """Sample rate, channels and RMS of the reference clip (cached).

    The output of every clone is conformed to these: same container shape and
    same loudness family as the voice it was cloned from. Read with soundfile,
    then librosa, then an ffmpeg decode -- the chain never guesses: if nothing
    can read the file it raises instead of conforming to a dummy signal (which
    once shipped a quietly wrong loudness with no trace).
    """
    ref = ref or DEFAULT_REF
    if ref in _ref_attrs:
        return _ref_attrs[ref]
    data, sr = None, None
    try:
        import soundfile
        data, sr = soundfile.read(ref, always_2d=True)
    except Exception as e1:
        try:
            import librosa
            loaded, sr = librosa.load(ref, sr=None, mono=False)
            loaded = np.asarray(loaded)
            data = loaded.T if loaded.ndim > 1 else loaded[:, np.newaxis]
        except Exception as e2:
            try:
                import io as _io
                import subprocess
                import soundfile as _sf
                raw = subprocess.run(
                    ["ffmpeg", "-nostats", "-v", "error", "-i", ref,
                     "-acodec", "pcm_f32le", "-ac", "2", "-ar", "48000",
                     "-f", "wav", "-"],
                    capture_output=True, timeout=120, check=True).stdout
                data, sr = _sf.read(_io.BytesIO(raw), always_2d=True)
            except Exception as e3:
                raise RuntimeError(
                    "local voice: cannot read reference %r (%s / %s / %s)"
                    % (ref, e1, e2, e3))
    rms = float(np.sqrt(np.mean(np.asarray(data, dtype=np.float64) ** 2)) or 1e-9)
    attrs = {"sr": int(sr), "channels": int(data.shape[1]), "rms": rms}
    _ref_attrs[ref] = attrs
    return attrs


def trim_silence(pcm, sr, thresh_db=-40.0, min_silence_s=0.30,
                 keep_silence_s=0.20, end_pad_s=0.06):
    """Cut silence hard, keep speech untouched (never time-stretched).

    - Leading/trailing silence trimmed to a short natural pad (end_pad_s).
    - Interior silent runs longer than min_silence_s are compressed to
      keep_silence_s. Shorter pauses (prosody, breaths) pass through intact.
    - Threshold is relative to the clip's own peak, so it tracks loud or
      quiet generations alike. Pure-numpy, no ffmpeg needed.
    pcm: (n_samples, n_channels) float array. Returns trimmed array.
    """
    pcm = np.asarray(pcm, dtype=np.float64)
    if pcm.size == 0:
        return pcm
    mono = pcm.mean(axis=1) if pcm.ndim > 1 else pcm.reshape(-1)
    frame = max(1, int(sr * 0.020))
    nframes = max(1, len(mono) // frame)
    trimmed = mono[:nframes * frame].reshape(nframes, frame)
    energy = np.sqrt((trimmed ** 2).mean(axis=1))
    peak = float(energy.max()) or 1e-9
    voiced = energy > peak * 10 ** (thresh_db / 20.0)

    first = int(np.argmax(voiced)) if voiced.any() else 0
    last = int(len(voiced) - 1 - np.argmax(voiced[::-1])) if voiced.any() else 0
    pad = int(sr * end_pad_s)
    start = max(0, first * frame - pad)

    max_sil = int(min_silence_s * sr)
    keep = int(keep_silence_s * sr)
    speech = mono[start:(last + 1) * frame + pad]
    if len(speech) == 0:
        return mono[start:start + frame]
    silent = np.abs(speech) < peak * 10 ** (thresh_db / 20.0)
    # Find silent runs and compress the long ones.
    idx = np.nonzero(silent)[0]
    if len(idx) == 0:
        return speech.reshape(-1, 1) if pcm.ndim == 1 else np.tile(
            speech.reshape(-1, 1), (1, pcm.shape[1]))
    cuts, run_start, prev = [], idx[0], idx[0]
    for k in idx[1:]:
        if k == prev + 1:
            prev = k
            continue
        cuts.append((run_start, prev))
        run_start, prev = k, k
    cuts.append((run_start, prev))
    keep_mask = np.ones(len(speech), dtype=bool)
    for a, b in cuts:
        if b - a + 1 > max_sil:
            drop_from = a + keep
            if drop_from <= b:
                keep_mask[drop_from:b + 1] = False
    out = speech[keep_mask]
    if pcm.ndim == 1:
        return out.reshape(-1, 1)
    # Rebuild channels from the kept mono mask (channels stay identical).
    ch = []
    base = mono[start:(last + 1) * frame + pad]
    for c in range(pcm.shape[1]):
        col = pcm[start:(last + 1) * frame + pad, c] if start < len(pcm) else base
        if len(col) != len(base):
            col = base
        ch.append(col[keep_mask[:len(col)]])
    n = min(len(c) for c in ch)
    return np.stack([c[:n] for c in ch], axis=1)


def conform_to_ref(data, model_sr, ref=DEFAULT_REF):
    """Resample/mix/gain raw mono model audio to the reference's attributes.

    Pure numpy (+librosa for resampling): no ffmpeg needed at runtime. Gain is
    capped at +12 dB and the peak is limited to -1 dBFS so a quiet generation
    gets loud without ever clipping.
    """
    attrs = ref_attributes(ref)
    mono = np.asarray(data, dtype=np.float64).reshape(-1)
    if int(model_sr) != attrs["sr"]:
        import librosa
        mono = librosa.resample(mono, orig_sr=int(model_sr), target_sr=attrs["sr"])
    gain = attrs["rms"] / (float(np.sqrt(np.mean(mono ** 2))) or 1e-9)
    gain = min(gain, 10 ** (12.0 / 20.0))
    out = mono * gain
    peak = float(np.max(np.abs(out))) or 1e-9
    if peak > 10 ** (-1.0 / 20.0):
        out = out * (10 ** (-1.0 / 20.0) / peak)
    if attrs["channels"] > 1:
        out = np.tile(out[:, np.newaxis], (1, attrs["channels"]))
    else:
        out = out[:, np.newaxis]
    return out, attrs["sr"]


def _load():
    """The Chatterbox model, loaded once per process."""
    if _model["instance"] is not None:
        return _model["instance"]
    try:
        from chatterbox.tts import ChatterboxTTS
    except ImportError as e:
        raise RuntimeError("local voice needs its packages (%s): %s" % (PIP_HINT, e))
    try:
        import torch
        torch.set_num_threads(max(2, int(os.cpu_count() or 2)))
    except Exception:
        pass
    try:
        model = ChatterboxTTS.from_pretrained(device="cpu")
    except Exception as e:
        raise RuntimeError("chatterbox model load failed: %s" % e)
    _model["instance"] = model
    return model


def _split_chunks(text, max_words=None):
    """Split long narration so no single diffusion run can OOM a small box.

    Splits at word boundaries into ~equal halves recursively. Each chunk is
    generated separately and concatenated -- prosody resets per chunk, which
    is inaudible inside a deadpan read and far cheaper than a dead process.
    Chunk size via SCALED_VOICE_CHUNK (default 12 words; raise on big boxes --
    fewer chunks is proportionally faster, one 1000-step run each).
    """
    if max_words is None:
        try:
            max_words = max(4, int(os.getenv("SCALED_VOICE_CHUNK", "12")))
        except ValueError:
            max_words = 12
    words = str(text).split()
    if len(words) <= max_words:
        return [str(text).strip()]
    mid = len(words) // 2
    return _split_chunks(" ".join(words[:mid]), max_words) + _split_chunks(
        " ".join(words[mid:]), max_words)


def _generate_one(model, text, ref, exaggeration):
    try:
        wav = model.generate(text, audio_prompt_path=ref, exaggeration=exaggeration)
    except TypeError:
        # Older chatterbox-tts without the exaggeration kwarg.
        wav = model.generate(text, audio_prompt_path=ref)
    except Exception as e:
        raise RuntimeError("local voice generation failed: %s" % e)
    data = wav.detach().cpu().numpy() if hasattr(wav, "detach") else np.asarray(wav)
    return np.asarray(data, dtype=np.float64).reshape(-1)


def clone(text, ref=DEFAULT_REF, exaggeration=0.3, pace=None):
    """Narrate `text` in the reference voice. Returns (wav_bytes, sample_rate).

    exaggeration 0.3 carries the reference's character; 0.0 flattens it into
    a generic read (proven dull in listening tests). pace overrides PACE_RATE
    (1.0 = natural speed). Raises on empty text.
    """
    text = str(text or "").strip()
    if not text:
        raise RuntimeError("local voice: refusing empty text")
    if not (ref and os.path.exists(ref)):
        raise RuntimeError("local voice: reference clip missing: %r" % (ref,))
    print("[voice] ref=%s attrs=%s" % (ref, ref_attributes(ref)), flush=True)
    model = _load()
    try:
        parts = [_generate_one(model, chunk, ref, exaggeration)
                 for chunk in _split_chunks(text)]
        gap = np.zeros(max(1, int(model.sr * 0.08)))
        data = np.concatenate([np.concatenate([p, gap]) for p in parts])[:-len(gap)]
    except Exception as e:
        raise RuntimeError("local voice generation failed: %s" % e)
    try:
        import io
        import soundfile
        pcm, sr = conform_to_ref(data, int(model.sr), ref)
        pcm = trim_silence(pcm, sr)
        pace = PACE_RATE if pace is None else float(pace)
        if abs(pace - 1.0) > 0.01:
            import librosa
            pcm = librosa.effects.time_stretch(pcm.T, rate=pace).T
            pcm = trim_silence(np.ascontiguousarray(pcm), sr)
        buf = io.BytesIO()
        soundfile.write(buf, pcm, sr, format="WAV")
        return buf.getvalue(), sr
    except Exception as e:
        raise RuntimeError("local voice: wav encode failed: %s" % e)
