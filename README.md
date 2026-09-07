# SCALED — an autonomous YouTube factory

SCALED is a YouTube show hosted by **Professor Croc**, a deadpan crocodile who
grades real biology like anime power stats. This repo *is* the studio: GitHub
Actions is the only compute, and the git repo is the database. Each video is one
JSON job in `data/videos/` with a `stage`; every stage saves before the next, so
a killed run resumes exactly where it stopped.

```
idea -> voice -> srt -> beats -> visuals -> render -> upload -> thumbnail -> done
```

Two rules keep every episode consistent:

- **The model chooses content; Python owns time and motion.** The SCRIPT model
  writes the episode; the BEATS model fills per-scene slots — *show a house cat*,
  *stamp A CAT OUTBITES YOU* — and that is the whole of its job. Every timestamp
  comes from `pipeline/beats.py` and every tween is one of six verbatim templates
  in `pipeline/compose.py`. No model ever emits CSS, JavaScript or a time.
- **Whisper is the only clock.** `pipeline/srt.py` word-aligns each scene's audio
  (Groq `whisper-large-v3-turbo`) so a card lands on the word being said. Without
  `GROQ_KEY` it degrades to the ffprobe duration and an even grid.

## The edit

Six beat kinds — `img`, `type`, `stat`, `meme`, `zoom`, `arrow` — one locked GSAP
template each. A scene gets `dur / 1.9` beats, clamped to 4–20, placed on a
syncopated grid (`beats.RHYTHM`) rather than a metronome: runs of ~0.6s cuts
around 1.5s holds, median ~1.6s. The grid was measured off the reference edit in
`reference/` (median cut 3.17s, p25 1.77s, p75 5.20s) — its strength is variance,
so variance is what the grid reproduces.

The art budget scales with the scene: `beats.img_cap(n)` buys one new picture per
three beats, floor 2, ceiling 5. A flat cap stamps eighteen text cards over two
static pictures, which is a slideshow, not an edit.

Any beat may name a sound: `whoosh`, `pop`, `zap`, `confetti`. `tools/make_sfx.py`
synthesises all four from ffmpeg `aevalsrc` expressions at 24 kHz mono to match
the voice — no samples, no licence, byte-identical on every machine.

## Thumbnails

`pipeline/thumbnail.py` assembles the image prompt in code from three parts: one
constant photoreal `LOOK`, one of six composition `ARCHETYPES` chosen by
`sha1(job id)` — stable per episode so a rebuild is identical, varied across the
channel page — and **the headline baked in by the image host**, which spells short
display copy correctly and lights it with the scene. Pillow adds only the corner
wordmark. If the art call fails, Pillow draws the full brand layer over a
generated backdrop instead, so a dead host degrades to a plainer thumbnail and
never to one with no words on it. Force an archetype with `SCALED_THUMB_ARCH=versus`.

The handwritten annotation layer is value-free by rule. Asked for figures, the
host invents them — it once labelled the crocodile 160 lbf and the human 3700 lbf,
exactly backwards. Numbers appear only in the headline, quoted from the script,
under a small literal kicker (`BITE FORCE, POUNDS`) that tells a stranger what the
video is about.

## Setup

1. **Secrets** — add these in *Settings → Secrets and variables → Actions*. Only
   the **names** ever appear in code; never commit a value.

   | Secret | Used for |
   | --- | --- |
   | `NIM_KEY` | creative + code LLM (NVIDIA NIM) |
   | `GROQ_KEY` | LLM fallback **and** Whisper word timing (required) |
   | `GEMINI_KEY` | LLM fallback |
   | `VOICE_KEY` / `IMAGE_KEY` | TTS + image providers (match `key_env` in `config/apis.json`) |
   | `YT_CLIENT_ID` / `YT_CLIENT_SECRET` / `YT_REFRESH_TOKEN` | YouTube upload/publish/comments/analytics |

2. **`config/apis.json`** — point `voice` and `image` at your providers (base URL,
   path, payload, `returns`, and `key_env` naming the secret to send).

3. **Fonts, GSAP, sound** — `assets/fonts/` holds the browser faces
   (`display.woff2`, `mono.woff2`) *and* TTF copies of the same families, because
   Pillow cannot read woff2 and renders tofu from one. `assets/vendor/gsap.min.js`
   has its restore command in `assets/vendor/README.txt`. Generate the sound
   effects once with `python3 tools/make_sfx.py` and commit the four wavs. Missing
   files degrade loudly; they never crash the render.

4. **Host image** — `assets/rig/croc.png` is the single flat host image used across
   every video (slides in, gentle idle zoom/rotate/drift). No rig, no lip-sync.

5. **Google OAuth** (only for real publishing) — create an OAuth *Desktop* client,
   grant the YouTube Data v3 + Analytics scopes, and generate a refresh token for
   the channel. Store the three values as the `YT_*` secrets above.

## DRY_RUN

If the `YT_*` secrets are absent (or `DRY_RUN=1`), `upload.has_yt()` is false and
every YouTube call becomes a logged no-op — the pipeline still writes a finished
`work/<id>.mp4`. This is the default until you wire real credentials, so you can
watch the whole factory run end-to-end without a channel.

## Workflows

| Workflow | Schedule | Does |
| --- | --- | --- |
| `produce` | every 6h | advance the next video one stage at a time |
| `publish` | daily | release one finished video off the shelf, post extra-credit |
| `comments` | every 4h | reply as Professor Croc to new comments (`SKIP` = ignore) |
| `learn` | weekly | analytics → strategy, reweight topics, A/B under-performing titles |

Each commits changed `data/` and `STATUS.md` back to the repo. A **shelf** of
`SHELF=5` finished-but-unpublished videos throttles production. `RUN_BUDGET_MIN`
(default 300) is the soft deadline a run stops at, to resume next time.

## Kill switch

Create an empty file `data/PAUSE` (commit it) and `produce` exits immediately on
its next run. Delete it to resume.

`PATCH_REPORT.md` lists every place the build deliberately differs from the
original spec, with the reason.

## Local development

```bash
pip install requests google-api-python-client google-auth pillow
python -m pytest -q          # unit tests; no network, no ffmpeg, no npm needed
python3 tools/make_sfx.py    # regenerate the four sound effects
python3 work/drive_sample.py # one episode end to end from work/sample_*.json
```

`drive_sample.py` runs every stage except the two LLM ones for real — live voice,
live art, the real beat engine, the real render — with the script and beat slots
supplied from `work/sample_*.json`, standing in for exactly what the models return
in CI. Use it to look at the show before pushing.

Real rendering needs `ffmpeg` and `npx -y hyperframes@0.8.16`, which the GitHub
runner provides; locally the command-building is unit-tested instead.
