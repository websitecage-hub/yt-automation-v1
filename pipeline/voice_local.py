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

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_REF = os.path.join(REPO, "assets", "voice", "goku_hope_8sec.mp3")

PIP_HINT = "pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu chatterbox-tts"

_model = {"instance": None}


def _load():
    """The Chatterbox model, loaded once per process."""
    if _model["instance"] is not None:
        return _model["instance"]
    try:
        from chatterbox.tts import ChatterboxTTS
    except ImportError as e:
        raise RuntimeError("local voice needs its packages (%s): %s" % (PIP_HINT, e))
    try:
        model = ChatterboxTTS.from_pretrained(device="cpu")
    except Exception as e:
        raise RuntimeError("chatterbox model load failed: %s" % e)
    _model["instance"] = model
    return model


def clone(text, ref=DEFAULT_REF, exaggeration=0.0):
    """Narrate `text` in the reference voice. Returns (wav_bytes, sample_rate).

    Raises on empty text (the old API 400'd on this; failing here with words
    is cheaper than failing after a 3-minute generation).
    """
    text = str(text or "").strip()
    if not text:
        raise RuntimeError("local voice: refusing empty text")
    if not (ref and os.path.exists(ref)):
        raise RuntimeError("local voice: reference clip missing: %r" % (ref,))
    model = _load()
    try:
        wav = model.generate(text, audio_prompt_path=ref, exaggeration=exaggeration)
    except TypeError:
        # Older chatterbox-tts without the exaggeration kwarg.
        wav = model.generate(text, audio_prompt_path=ref)
    except Exception as e:
        raise RuntimeError("local voice generation failed: %s" % e)
    try:
        import io
        import numpy as np
        import soundfile
        data = wav.detach().cpu().numpy() if hasattr(wav, "detach") else np.asarray(wav)
        if data.ndim == 1:
            data = data[np.newaxis, :]
        buf = io.BytesIO()
        soundfile.write(buf, data.T, int(model.sr), format="WAV")
        return buf.getvalue(), int(model.sr)
    except Exception as e:
        raise RuntimeError("local voice: wav encode failed: %s" % e)
