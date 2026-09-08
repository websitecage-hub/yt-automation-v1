# PATCH REPORT

Every place the built factory deliberately differs from the original build spec,
with the reason. Anything not listed here follows the spec as written.

## 1. The LLM no longer writes motion at all

**Spec:** the model emits per-scene GSAP for the overlay layer, screened by a
four-part gate (ban-check, time-window, id-scope, real `hyperframes lint`) and
falling back to a hand-written `SAFE_FALLBACK` when the screen rejects it.

**Built:** the gate and the fallback are gone. `pipeline/compose.py` owns six
verbatim tween templates — one per beat kind — and the model only picks a kind and
fills its slots.

**Why:** the gate was passing code that was *legal* but not *good*: timing drifted,
two tweens fought over the same property, and every rejection cost a retry. Motion
is the one thing in the show that must be identical across 100 episodes, so it
belongs in Python. The model kept the job it is actually good at — choosing what is
on screen and what the card says.

## 2. `beats.py` is a separate module

**Spec:** the beat clock lives in `compose.py`.

**Built:** `pipeline/beats.py` holds the clock (`beat_count`, `plan_times`,
`RHYTHM`), the composition law (`validate`, `autofix`, `img_cap`) and the slot
schema (`normalise`); `compose.py` only turns finished beats into a timeline.

**Why:** the clock is the part with the interesting arithmetic and it is worth
testing on its own. `compose.py` was already the largest module in the repo.

## 3. The host is one static PNG

**Spec:** a rig with an expression stack.

**Built:** `assets/rig/croc.png`, one flat image, sliding in with an idle
zoom/rotate/drift.

**Why:** the operator's explicit instruction. No rig, no lip-sync, no expressions.

## 4. A missing host image is loud, not fatal

`compose.py` prints a banner and renders without the host when `croc.png` is
absent. `SCALED_REQUIRE_AVATAR=1` restores the crash. A missing decorative asset
should not cost a scheduled run its whole episode, but silently shipping videos
with no host would be worse than either, so it is impossible to miss in the log.

## 5. Idle bob repeats are `ceil(dur / BOB_PERIOD) - 1`

GSAP counts `repeat` as *extra* plays, so a `repeat` of n runs n+1 passes. Every
looping tween in the repo subtracts the one it gets for free. Related: with
`yoyo:true` an **odd** repeat count ends on the `from` values — the stat-card
camera shake uses `repeat:4` (five passes) specifically so the frame lands back at
zero rather than staying 9px off-centre for the rest of the scene.

## 6. Two new adapter dialects

`config/apis.json` gained `returns: "mcp-tool"` (the image host is an MCP server:
the call is wrapped in a JSON-RPC `tools/call` envelope over Streamable HTTP) and
`form` (the voice host takes `multipart/form-data` with the reference clip
uploaded as a file). Neither was in the spec's request table; both are what the
chosen providers actually speak.

## 7. `render._rescue_silent`

Hyperframes renders a timed media element **silent** unless it carries an `id`.
The renderer now detects a silent mp4 and remuxes the audio graph back over it
rather than shipping a mute video. `compose.py` also puts an id on every media
element, so the rescue should never fire; it exists because the failure is
invisible until someone watches the upload.

## 8. Three fixes the spec's commands could not survive on CI

- `hyperframes render` accepts `-o/--output` only — the spec's flag does not exist
  in 0.8.16.
- The voice reference clip was inside a gitignored path, so CI cloned a voice from
  a file that was not there.
- The shipped `mono.woff2` had no latin subset, which renders every stat card as
  tofu in headless Chrome.

## 9. Thumbnails: the image host bakes the headline

**Spec (and v2):** the art call is forbidden from drawing text and Pillow
composites every word.

**Built:** the art call draws the headline itself, lit by the scene and overlapping
the subject; Pillow adds only the corner wordmark. If the art call fails, Pillow
draws the full brand layer over a generated backdrop.

**Why:** the operator rejected the composited version — flat type pasted on a photo
reads as a sticker. The host spells short all-caps display copy correctly, so the
only thing lost is control, and the fallback keeps the guarantee that matters: a
thumbnail always has words on it.

Two rules inside that reversal were learned from live failures. Every archetype
negates split screen / dividing line / inset frame by name, because the first
baked attempt welded a text panel to a picture panel. And the handwritten
annotation layer is value-free — asked for figures, the host labelled the
crocodile 160 lbf and the human 3700 lbf, exactly backwards.

## 10. Beat pacing is syncopated, and the art budget scales

`beats.RHYTHM` places beats on an uneven grid instead of `i * dur / n`, and
`beats.img_cap(n)` allows one new image per three beats (floor 2, ceiling 5)
instead of a flat two per scene.

**Why:** measured off the reference edit the operator supplied — median cut 3.17s,
p25 1.77s, p75 5.20s. Its strength is variance, not speed, so a metronome at any
tempo is the wrong shape. And a flat art cap meant a long scene stamped eighteen
text cards over two static pictures: the words changed every 1.6s while the
picture sat there.

## 11. The sound effects are synthesised, not licensed

`tools/make_sfx.py` builds whoosh, pop, zap and confetti from ffmpeg `aevalsrc`
expressions. `assets/audio/sfx/` had shipped empty, so every beat that asked for a
sound rendered silent and the whole percussive layer of the edit was missing.
Synthesis unblocked that without a licence, an attribution, or a binary blob
nobody can tweak.

## 12. Thumbnails buy the "thinking" pass, with a consistency-edit tier

`adapters.image_rest(prompt, mode="thinking")` calls the media host's REST
`/api/image` endpoint directly (bypassing MCP) for the thumbnail only — one
call per episode, so the slow high-quality pass is affordable there while beat
stills stay on the fast MCP path (24/24 proven). `make_thumbnail` is now four
tiers: REST thinking baked → MCP baked → consistency edit (upload this
episode's own beat-1 still, `edit_image` the headline into it, so the thumbnail
is literally the video's world) → Pillow brand layer over beat-1/backdrop.
The kicker (`BITE FORCE, POUNDS`) bakes into the photograph on every art tier,
which is the operator's "no clue what the video is about" fix.

## 13. Upload-first consistency helpers

`adapters.media_upload(path)` (POST `/api/upload` → public URL) and
`adapters.edit_image(image_url, instruction)` (MCP `edit_image` → bytes) expose
the media API's upload features for anything that needs a stable subject. Both
retry like every other call in the module and raise on exhaustion so callers
fall through to the next tier. No key required — same keyless host.

## 14. v4 Phase 1 — reliability (from docs/ARCHITECTURE.md)

- **R1 upload idempotency:** every upload description carries `[kronvex:<job-id>]`;
  `stage_upload` adopts orphans via `find_upload()` and saves state before
  uploading, so a kill between upload and save can never duplicate a video.
- **R2 publish verification:** `_go_public` re-reads `privacyStatus`; the job is
  marked `published` only on verified public (or DRY_RUN). Failures stay queued
  with a `publish_attempts` counter.
- **R3 artifact restore:** produce restores/saves `work/` via `actions/cache`
  (miss = regenerate as before); `stage_srt` reuses `w*.json` word files, so a
  kill during render no longer re-pays Whisper or re-buys art.
- **R4 one writer at a time:** all four workflows share `concurrency: scaled-state`;
  the push step is pull---rebase + retry x3 and fails loudly if state didn't land.
- **R7 QC gate:** new `pipeline/qc.py` (duration bounds, narration present, art
  coverage >=70%, thumbnail >=30KB; Whisper gaps fail only when GROQ_KEY exists,
  loudness is warn-only and read-only -- the voice is never touched per the
  operator's rule). Failures quarantine to `qc_failed`, invisible to resume and
  publish. New `qc` stage sits between `thumbnail` and `done`.
- **`{perf}` un-hardcoded:** `stage_idea` now fills it from `strategy.json` notes
  plus the last finished episodes; the placeholder survives only with no history.
- **R11:** `github-pat.txt` moved out of the repo (`~/.config/kronvex/`); verified
  it never appears in git history.

## 15. v3 hard-cut edit + Memeic fast path (from docs/MEMEIC_QUALITY_PLAN.md)

- New beat kinds `photo` (ironic real photo, zero motion) and `doodle`
  (flat-cartoon gag); meme presenters `split|full|stamp`; caps photo/doodle<=2,
  meme<=2; MIN_PUNCH 3->2; every scene must contain >=1 joke beat (autofix
  converts a spare zoom to a captioned photo rather than shipping jokeless).
- Base layer is hard cuts only: no drift, no overshoot (dead constants removed);
  photo has zero motion at all; SFX defaults almost all `none`, volume 0.5->0.22.
- Punchline hold: the last beat's onset pulls earlier so its window runs ~2x the
  median (the laugh gets air).
- "One Cast, One World": `tools/make_cast.py` built guy/world/sheet once
  (committed under `assets/cast/` with `.url` sidecars); `cast_edit()` re-stages
  them per gag; doodle/meme beats route through the cast first, text fallback
  after. Photo beats stay raw irony.
- Voice: pure clone (steps 10, exaggeration 0.0); render path verified
  filter-free.
- BEATS prompt rewritten as an output contract (JSON-only, exact keys, few-shot
  examples, cast + setup->betrayal rules) after proving the thinking model
  roleplays prose on loose prompts; `llm.py` meta dialect moved to native
  `/api/chat` (the OpenAI-compatible path truncates long instructions);
  `beats.normalise` tolerates subject/desc/factor aliases.

## Still outstanding

`assets/audio/lofi/` is empty, so episodes render with no music bed. It needs a
licensed loop from the operator; synthesising one would sound like a synthesised
one.
