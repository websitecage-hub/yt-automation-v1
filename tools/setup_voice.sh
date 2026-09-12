#!/bin/bash
# Reinstall the local-voice stack after a box reset. Idempotent; re-runnable.
# Installs to /usr/local (overlay disk, survives nothing -- rerun as needed).
# Recipe notes:
#  - torch CPU wheels first (default index would pull 2.5GB of CUDA libs).
#  - chatterbox-tts --no-deps (its pins demand CUDA torch) + hand-picked deps.
#  - antlr4 4.9.x (omegaconf 2.3.x grammar breaks on 4.13).
set -u
export PIP_NO_CACHE_DIR=1 TMPDIR=/tmp
CPU_INDEX="https://download.pytorch.org/whl/cpu"
# typing_extensions ships with the runner image as a debian package with no
# RECORD file, so any upgrade attempt aborts pip. Shadow it first; everything
# after then resolves normally.
pip install --user --ignore-installed typing_extensions
pip install --user torch torchaudio --index-url "$CPU_INDEX"
pip install --user --ignore-installed --no-deps chatterbox-tts
# --ignore-installed throughout: more debian packages without RECORD files
# (rich, ...) lurk in the runner image; shadowing beats uninstalling.
# torch is pinned to the CPU index on BOTH lines: without the pin, pip
# re-resolves torch from PyPI (CUDA build) as a transitive dep and the
# CPU-only runner ends up with a torch that cannot run.
pip install --user --ignore-installed "torch==2.14.0+cpu" "torchaudio==2.11.0+cpu" \
  librosa==0.11.0 soundfile transformers==5.2.0 tokenizers \
  conformer==0.3.2 s3tokenizer resemble-perth diffusers==0.29.0 safetensors==0.5.3 \
  pykakasi==2.3.0 spacy-pkuseg pyloudnorm omegaconf lazy_loader einops onnx pooch \
  numba "antlr4-python3-runtime==4.9.3" \
  --index-url "$CPU_INDEX" --extra-index-url https://pypi.org/simple
python3 -c "import torch; assert 'cpu' in torch.__version__, torch.__version__"
python3 -c "from chatterbox.tts import ChatterboxTTS; print('VOICE_STACK_OK')"
