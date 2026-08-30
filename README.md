# SCALED — an autonomous YouTube factory

SCALED is a YouTube show hosted by **Professor Croc**, a deadpan crocodile who
grades real biology like anime power stats. This repo *is* the studio: GitHub
Actions is the only compute, and the git repo is the database. Each video is one
JSON job in `data/videos/` with a `stage`; every stage saves before the next, so
a killed run resumes exactly where it stopped.

```
idea -> voice -> srt -> visuals -> html -> render -> upload -> thumbnail -> done
```

Python bakes everything that has to be reliable (media clips, word-synced
captions, the host image, stat cards, the verdict stamp). The LLM only writes
per-scene overlay motion, behind a four-part gate (ban-check, time-window,
id-scope, real `hyperframes lint`) with a hand-written `SAFE_FALLBACK`.

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

3. **Fonts + GSAP** — drop `assets/fonts/display.woff2` and `mono.woff2` in place,
   and `assets/vendor/gsap.min.js` (restore command in `assets/vendor/README.txt`).
   Missing files degrade loudly, they never crash the render.

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

## Local development

```bash
pip install requests google-api-python-client google-auth
python -m pytest -q        # unit tests; no network, no ffmpeg, no npm needed
```

Real rendering needs `ffmpeg` and `npx -y hyperframes@0.8.16`, which the GitHub
runner provides; locally the command-building is unit-tested instead.
