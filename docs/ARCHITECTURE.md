# SCALED / KRONVEX — Architecture Review & v4 Blueprint

_Status: written 2026-09-08 against the `beat-engine` branch (23 commits, 1 episode in flight)._

This document has three parts:

1. **Part I** — the architecture *as it actually is today* (not the spec, the built thing).
2. **Part II** — the audit: every problem found, ranked by how badly it hurts.
3. **Part III** — the v4 blueprint: a better architecture that keeps what works, adopts the
   agent-with-a-GitHub-brain model where it is right, and refuses it where it is wrong.

The goal stated by the operator: a fully autonomous content system that creates and uploads
videos on its own, **never crashes irrecoverably**, and — most importantly — **learns content
quality much faster than it does today**.

---

# Part I — Current architecture (as built)

## 1.1 The one-paragraph version

A YouTube channel ("Kronvex", hosted by Professor Croc) run entirely inside a git repo.
GitHub Actions is the only compute; the repo is the database; each video is one JSON job in
`data/videos/` carrying a `stage`. A cron workflow (`produce`, every 6h) advances one video
through a deterministic stage machine; other workflows publish, answer comments, and read
analytics. Two free hosted APIs supply the intelligence (LLM chain) and the media (images,
voice); Hyperframes + headless Chrome render the edit; YouTube Data API publishes.

## 1.2 The real pipeline

```
produce (cron 6h) ──▶ pipeline.run.main()
                        │
                        ├─ data/PAUSE? ── exit
                        ├─ resume in-progress job, or stage_idea()
                        └─ advance(job, deadline): run stages until done or budget
                             │
   ┌──────────┬─────────────┼──────────────┬─────────────┬──────────┐
   ▼          ▼             ▼              ▼             ▼          ▼
 idea      voice          srt           beats        visuals     render
 topic bank  TTS clone    Whisper      director LLM   art calls   hyperframes
 SCRIPT LLM  per scene    word clock   fills slots    4 workers   / KB fallback
 (16 scenes) (skip if     (fallback:   validate +     (skip if    then upload →
             file exists) ffprobe dur) one repair     file exists) thumbnail
                                       + autofix
```

Stage order is load-bearing: `voice → srt → beats → visuals` because durations come from
Whisper, beats snap to word onsets, and visuals only buy art for beats that exist.

## 1.3 The division of labour (the core design decision)

> **The model chooses content; Python owns time and motion.**

| Concern | Owner | Where |
|---|---|---|
| Topics, scripts, beat slots, comments, titles, strategy | LLM | `config/prompts.md` prompts |
| Every timestamp | Python | `beats.plan_times()` — RHYTHM grid snapped to Whisper onsets |
| Every tween | Python | `compose.py` — one verbatim GSAP template per beat kind |
| Beat validity | Python | `beats.validate()` → one LLM repair call → `beats.autofix()` |
| Word timing | Whisper | `srt.word_timeline()` (Groq, `whisper-large-v3-turbo`) |
| Art consistency | Python | style locks appended in `adapters.py` (specimen / doodle / meme) |
| Motion quality | locked templates | a bad LLM day can weaken *content*, never *pacing/motion* |

## 1.4 Providers

**LLM chain** (`llm.py`) — tried in order, per-call failover, 3 retries on transient errors:
Meta AI thinking (keyless, native `/api/chat`) → tabi opus-4-8 (×2 keys) → NIM nemotron
super/nano → Gemini 2.5 flash → Groq gpt-oss-120b. `json_out=True` asks for JSON and
`parse_json()` recovers fenced/embedded JSON.

**Media** (`adapters.py`) — config-driven from `config/apis.json`:
- voice: zero-shot clone, multipart form + reference clip, kept in its native container
- image: MCP `tools/call` envelope (beat stills), REST `/api/image` thinking pass (thumbnails)
- upload + `edit_image` MCP tool for consistency (same-croc memes, thumbnail-from-beat-1)

**YouTube** (`upload/publish/comments/learn.py`) — lazy imports, `has_yt()` gate, DRY_RUN
makes every network action a logged no-op.

## 1.5 Thumbnail tiers

REST thinking (headline baked by the host) → MCP baked → consistency edit of this episode's
beat-1 still → Pillow brand layer over beat-1/backdrop. Six composition archetypes chosen by
`sha1(job id)`; annotations are value-free (invented numbers burned us once).

## 1.6 What is genuinely good (keep at all costs)

1. **The crash-only stage machine.** Stage saved before the next begins; per-scene/per-beat
   skip checks inside stages. This is the correct skeleton for CI-resident autonomy.
2. **Python owning time and motion.** The single best decision in the repo. Never undo it.
3. **Whisper as the only clock.** One source of temporal truth; clean fallback chain.
4. **Fail-soft degradation everywhere.** Missing art → punch-in; missing avatar → loud banner;
   dead image host → plainer thumbnail with words still on it; no YT creds → DRY_RUN.
5. **The beat engine's syncopated RHYTHM** measured off a real reference edit.
6. **Style locks in code, not in prompts** — the anti-"AI slop" mechanism.
7. **Tests that run with zero network.** 232 of them, all mocked.

---

# Part II — Audit: problems found

Ranked by severity. 🔴 = loses content or money, 🟠 = quality ceiling, 🟡 = robustness/debt.

## 2.1 🔴 P0 — correctness holes that lose finished content

**A1. Crash between upload success and `save_job` re-uploads the video.**
`stage_upload` uploads, then `advance()` saves. Kill the runner in between (push race, runner
reclaim — it happens) and the next run sees stage `upload` with no `video_id` and uploads a
**second copy of the same video to the channel**. YouTube has no idempotency key; nothing in
the code checks for a previous orphan upload.

**A2. `publish_one()` marks a video published even when going public failed.**
`_go_public()` logs the failure and returns; `publish_one()` then unconditionally sets
`published = True`. The video stays **private forever** while the system records it as live.
Silent, permanent content loss.

**A3. `work/` is ephemeral but is never restored — paid art is re-bought.**
`produce.yml` *uploads* artifacts (mp4/jpg/mp3) but no step ever *downloads* them back. A run
killed during `render` restarts on a fresh runner with an empty `work/`; `stage_visuals`
re-buys every beat still (dozens of image calls), and `stage_srt` re-pays Whisper for every
scene because it has no done-file check either.

**A4. Four workflows race on the same repo state.**
`produce`, `publish`, `comments`, `learn` each have their own concurrency group but can all
commit to `data/` and `STATUS.md` simultaneously. `git push || echo "nothing to push"` swallows
non-fast-forward rejections — the commit is dropped, and the runner's stage advance is
**silently lost**, re-triggering A1 on the next run.

**A5. No QC gate anywhere before publish.**
A rescue-failed silent render, a 3-second mp4, a thumbnail that is the painted fallback
backdrop, a scene whose whisper failed and whose beats all sit on an even grid — all ship
without anyone or anything looking at them. The failure modes are caught as *exceptions*, not
as *bad output*.

## 2.2 🟠 P1 — why the content quality is bad (the operator's actual complaint)

**B1. There is no research step.** The SCRIPT model writes biology from its head. The media
host ships `deep_research`, `web_search`, `social_search` MCP tools (`api-usage.md` §MCP) —
the pipeline **never calls any of them**. Every "shocking true number" is an unverified
memory of a model. On a channel whose pitch is *real biology*, a fabricated number is a
credibility bug one viral video from a community-note.

**B2. There is no critique step.** Script → voice in one shot. Nobody — model or code — asks
"does the hook land, does every step carry a number, is the staircase actually chained?"
The BEATS stage is the only place with a validate-repair loop; the SCRIPT stage, which
determines 80% of quality, has none.

**B3. The learning loop is nearly inert.** What exists: analytics rows → one STRATEGIST call
→ `strategy.json` (which stage_idea does read) → `topics.reweight(±1)`. What doesn't:
- `{perf}` in the SCRIPT prompt is **hardcoded to "(no performance data yet)"** in
  `stage_idea` — even after learn.py has written a strategy file.
- No experiment register: hypotheses are never stated, so they can never be confirmed or killed.
- No factor logging: nothing records *which hook type, which thumbnail archetype, which title
  pattern, which beat mix* each published video used, so analytics can attribute outcomes to
  choices. The six thumbnail archetypes are chosen by hash and never evaluated against CTR.
- Analytics are read as **lifetime totals only** — no per-day snapshots, so retention curves
  and trajectory (is this video still moving after 7 days?) are invisible.
- Lessons are not written as memory files and are not injected into future SCRIPT prompts.

**B4. Packaging is an afterthought of the script call.** `thumb_words` / `thumb_kicker` /
title are emitted by the same 16-scene mega-call. The channel's CTR is decided in the first
200 tokens of a call whose attention budget is mostly spent on scenes. Packaging deserves its
own pass with its own critique.

**B5. No cross-episode memory for the director.** The BEATS prompt says "vary patterns between
scenes" but the model never sees what patterns episodes 1..n-1 used, so the channel converges
on the same beat mix every episode — the repetitive feel the operator described.

**B6. The topic bank is static and blind.** Topics are scored 1–10 once at generation, refilled
only when <15 unused, never re-searched for freshness, never crossed with what analytics say
the audience actually watches. `used` topics are excluded forever instead of being allowed to
earn a remaster.

**B7. Audio quality is unmanaged.** No loudness normalization between scene clips (hosts and
providers vary), no music bed (`assets/audio/lofi/` is still empty), SFX at 0.22 with almost
all defaults "none". Voice inconsistency between scenes reads as cheap.

## 2.3 🟡 P2 — robustness and debt

**C1. One LLM call can blow the entire run budget.** `_http_post` retries 3× with a 300s
timeout inside `_run_chain` across 7 providers — a hanging host can cost 60+ minutes of a
300-minute budget on a *single* call, and `advance()` checks the deadline only **between**
stages. Free Render dynos sleep; cold starts + hangs are the *normal* case, not the edge case.

**C2. No host health preflight.** The chains discover a dead host by waiting out its timeout.
A 2-second `GET /health` probe (`api-usage.md` documents both hosts' `/health`) would
re-order or skip a sleeping host before the first real call.

**C3. `stage_srt` is not resumable** (overlaps A3): it re-transcribes every scene on every
re-entry; the `w{id}_i.json` it writes is never checked before transcribing.

**C4. `adapters.CONFIG` is frozen at import time** — fine for CI (one run per process), a trap
for local long-lived use; more importantly `_media_base()` silently breaks if `image.base`
is empty, surfacing only as five failed uploads at thumbnail time instead of a preflight error.

**C5. Single-video production.** `in_progress()` returns the first non-done job; only one
video can ever be in flight. With the shelf at 5 and publish at 1/day this is *workable* but
makes the 6h produce cadence mostly idle waiting and gives no pipeline parallelism (a video
in voice while another waits for analytics).

**C6. `github-pat.txt` sits in the working tree.** It is gitignored (verified) but a PAT on
disk in a repo directory that gets zipped/shared by tooling is one `git add -f` away from
leakage. Move it out of the repo; rotate it if it has ever been committed in history.

**C7. Tests are red.** 9 failures: 8 assert the old Meta `/v1/chat/completions` dialect
(uncommitted change moved to native `/api/chat`), 1 asserts a phrase the rewritten BEATS
system prompt no longer contains. The suite no longer guards the changes that were just made.

**C8. Voice clip deletion between runs orphans `srt` fallbacks** — minor, covered by A3's
artifact restore.

**C9. No structured logging/telemetry.** Everything is `print`. Run-to-run comparison, cost
tracking per episode (API calls per stage), and failure post-mortems all rely on grepping CI
logs that retention will eventually delete.

---

# Part III — v4 blueprint

## 3.0 The verdict on the proposed agent architecture

The ChatGPT proposal (agent loop + GitHub brain + checkpointed media jobs + layered context +
self-improvement) is **directionally right and adopted here** — with one large and one small
refusal:

✅ **Adopted:** GitHub as permanent memory (`/brain`), layered context assembly, a research
step before writing, a critique step after it, checkpointed async media jobs, an experiment
ledger driving strategy, `/health` preflight, strict state machine with QC gates.

❌ **Refused (big):** "ASK LLM: WHAT SHOULD I DO?" as the **top-level control loop**. An LLM
deciding the next step at runtime is nondeterministic control flow, and nondeterministic
control flow is incompatible with crash-resume: after a kill, you cannot replay "what the
agent was thinking", you can only replay *state*. The current stage machine is the correct
skeleton. The synthesis is:

> **A deterministic spine carries the video; an agent brain is consulted *at the decision
> points* of that spine.** The LLM proposes content and judgements inside stages; Python
> alone decides what stage comes next. The "agent loop" exists *within* research, critique,
> and learning stages — bounded, resumable, and logged.

❌ **Refused (small):** `conversation_id` working context. Every LLM call is already
state-assembled (identity + task + relevant memory); a server-side conversation thread would
make replies depend on hidden server state that a fresh runner cannot inspect or replay. It
would also break the mocked test suite's provider-failover model.

## 3.1 Target layout (delta on the current repo)

```
/brain                              # NEW — the permanent mind (markdown = human-editable)
    channel/
        identity.md                 # who Kronvex is (moves from config/niche.md)
        personality.md              # Croc's voice, catchphrase budget, forbidden moves
        visual_style.md             # the style-lock canon, by reference to adapters.py
        rules.md                    # hard invariants (numbered, testable)
    knowledge/                      # deep_research output, one file per topic, cite-backed
        2026-09/bite-force.md
    memory/
        lessons/                    # LEARNED heuristics, one file per lesson w/ evidence link
            2026-09-14_hooks-with-numbers-beat-questions.md
        patterns/                   # what recurring structures won/lost (auto-updated)
    strategy/
        current.md                  # the playbook the SCRIPT prompt obeys (from learn)
        hypotheses.json             # H1..Hn: claim, experiment id, status, evidence
        experiments.json            # E1..En: factor, variants, assigned episodes, readout rule

/data                               # EXISTS — machine state (JSON = machine-only)
    videos/*.json                   # jobs, now with research, critique, factors, qc
    topics.json                     # as today + freshness/last_eval
    analytics/                      # NEW — per-video snapshot series (append-only)
        20260830-*.json             # one file per video: [{day, views, impressions, ctr,...}]

/state                              # NEW — cross-workflow coordination
    production.json                 # which jobs are in flight, generation job IDs
    locks/                          # advisory lock files (workflow-level, best-effort)
```

Markdown for anything a human (or the LLM) should read and edit; JSON for anything the code
diffs and trusts. Both are in git = both survive every crash by construction.

## 3.2 The v4 state machine

```
IDEA → RESEARCH → SCRIPT → CRITIQUE ─┐ (loop ≤2, then best-of)
   ▲                                 ▼
   │                            PACKAGING → VOICE → SRT → BEATS → BEAT_QC
   │                                                              │
   └────────────── LEARNING ◀── ANALYSIS_PENDING ◀── PUBLISHED ◀─┐
                                                                  │
        THUMBNAIL ◀── QC ◀── RENDER ◀── VISUALS(job-checkpointed)─┘
```

New stages, in execution order:

| Stage | What happens | New code |
|---|---|---|
| `RESEARCH` | For the chosen topic: 1× `deep_research` (media MCP) + `web_search`; extract **claims with sources + numbers** into `brain/knowledge/<topic>.md`. Cached: if a knowledge file for this topic exists and is <30 days old, skip. | `pipeline/research.py` (new) |
| `CRITIQUE` | A second LLM call (different provider in the chain — force provider #2 for independence) scores the script on a rubric: hook ≤2s curiosity gap, every staircase step has mechanism+number, no invented stats not present in the knowledge file, staircase actually chained, payoff zoom-out. Returns JSON `{scores, problems, fixes}`. Score < threshold → **one** repair call with the problems appended → keep the better (re-critiqued) script, max 2 rounds, then best-of. | `pipeline/critique.py` (new) |
| `PACKAGING` | Title / thumb_words / thumb_kicker / description get their **own** call, input = approved script + winning title patterns from `brain/strategy/current.md`. 5 candidate titles generated, ranked by the critic, top 1 stored with all 5 logged as factors for later A/B. | `pipeline/packaging.py` (new) |
| `BEAT_QC` | Deterministic checks *plus* a cheap LLM pass: "do these beats betray the narration or illustrate it literally?" — returns per-beat keep/replace votes; replaces are `autofix`-style, from a within-scene variant, so cost is zero extra art. | `beats.py` + one prompt |
| `QC` (pre-upload) | Numeric gates, no LLM: duration ∈ [45s, 15min], every scene has audible narration (`ffprobe`), loudness ≈ −14 LUFS after normalize, ≥70% of beats have their art file present, thumbnail exists and is ≥30KB, word-coverage: no scene where Whisper produced 0 words. **Failure quarantines** the job (`stage: "qc_failed"`, reasons list) — it does not upload. | `pipeline/qc.py` (new) |
| `ANALYSIS_PENDING → LEARNING` | §3.5. | `learn.py` v2 |

Everything else (VOICE, SRT, BEATS, VISUALS, RENDER, THUMBNAIL, UPLOAD) keeps its current
form with the reliability fixes below.

## 3.3 The brain / context system

`pipeline/brain.py` (new) — pure functions, fully testable:

```python
def core_context()      -> str   # identity + personality + rules (cache-busted per run)
def task_context(job)   -> str   # stage, video, what already happened this episode
def relevant_memory(topic, lane) -> str   # top-k lessons + active hypotheses + knowledge
def assemble(section, job) -> str # the layered prompt header every call gets
```

- **Core memory** is always supplied (~600 tokens max): identity, audience, rules, current
  playbook summary. The playbook lives in `brain/strategy/current.md`, *not* raw
  `strategy.json` — learn.py writes both (JSON for diffing, MD for prompting).
- **Relevant memory** is retrieved, not dumped: lessons whose tags match the task's
  (lane, act, beat-kind) — a lessons file carries front-matter tags, retrieval is tag overlap
  scoring, no embeddings needed at this scale (<200 files).
- Every stage prompt becomes: `assemble(...) + stage-specific instructions`. Prompt bodies
  stay in `config/prompts.md` (one home, already enforced).

## 3.4 Reliability changes (the "never crashes irrecoverably" list)

| # | Change | Where |
|---|---|---|
| R1 | **Upload idempotency:** `stage_upload` first checks the channel for an existing private video tagged with the job id (deterministic string in the description). Before calling upload, save `upload_started_at` to the job **and commit**. Resume path: search first, upload only if absent. | `run.py`, `upload.py` |
| R2 | **Publish verification:** `_go_public` re-fetches the video's `privacyStatus` after update; `published=True` is written **only** on verified public. Otherwise job stays `stage: upload` with `publish_attempts` counter. | `publish.py` (fixes A2) |
| R3 | **Restore work/ artifacts:** in every workflow, `actions/cache/restore` keyed on job id + stage before produce; save after. Fall back to the existing regenerate-everything path silently. Also make `stage_srt` skip scenes whose `w{id}_i.json` already exists. | workflows, `run.py` (fixes A3/C3) |
| R4 | **Push loop:** replace `git push \|\| echo` with `git pull --rebase` + retry ×3, and a final `git status` assertion that fails the workflow loudly if state didn't land. Shared `concurrency: scaled-state` group across all four workflows (serialize the writers; they're short). | workflows (fixes A4) |
| R5 | **Global deadline inside the LLM chain:** `_run_chain` takes `deadline`; a provider is skipped if `now + TIMEOUT > deadline`; connect-timeout 10s / read-timeout 90s for *thinking* calls; RETRY_TRIES drops to 2 when past 50% budget. | `llm.py` (fixes C1) |
| R6 | **Health preflight:** at chain start, `GET /health` on meta + media hosts (2s timeout). A sleeping host is deprioritized (not skipped — cold start ≠ dead) and logged. Cache the result for the process. | `llm.py`, `adapters.py` (fixes C2) |
| R7 | **QC gate before upload** (see 3.2) — bad output quarantines instead of shipping. | `qc.py` (fixes A5) |
| R8 | **Loudness pass:** `ffmpeg loudnorm` (I=-14, TP=-1.5) per scene clip at voice stage; music bed at −28 LUFS under voice once `lofi/` is populated. | `run.py` stage_voice (fixes B7) |
| R9 | **Advisory file locks** in `state/locks/` for produce-vs-learn touching the same job; stale-lock takeover after 2h. | new, tiny |
| R10 | **Structured run log:** `data/runlog/<ts>.json` — stages, durations, API call counts and failures per provider, cost estimate. One JSON per run, committed. Makes C9 disappear and feeds learning. | `run.py` |
| R11 | Move `github-pat.txt` out of the repo directory entirely; verify it never appears in `git log -p`. | operator (fixes C6) |
| R12 | Fix the 9 red tests: update Meta-dialect mocks to `/api/chat`, reword the BEATS-prompt assertion, and add tests for every R1–R7 path (they're all pure/mockable). | `tests/` (fixes C7) |

## 3.5 The learning system v2 (the operator's priority)

The current loop reads lifetime totals once and nudges lane scores by ±1. The v2 loop is a
proper closed control loop:

```
publish → log factors → snapshot analytics daily → at T+3d and T+7d:
  read out experiments → confirm/kill hypotheses → write lessons → rewrite playbook
```

**1. Factor logging (the missing foundation).** `stage_thumbnail`/`PACKAGING`/`stage_beats`
append to the job:

```json
"factors": {
  "hook_type": "question|number|betrayal",        // critic labels it
  "title_pattern": "number-pain",                  // from PACKAGING candidates
  "thumb_archetype": "versus",                     // pick_archetype already knows
  "thumb_words_len": 3,
  "beat_mix": {"img": 4, "photo": 2, "meme": 1, ...},
  "median_cut": 1.6,                               // actual, from planned times
  "topic_lane": "VERSUS",
  "script_score": 8.2,                             // CRITIQUE's score
  "had_research": true
}
```

**2. Daily analytics snapshots.** `learn.py` appends to `data/analytics/<id>.json`
(`[{day, views, impressions, ctr, avg_view_pct}]`) instead of reading lifetime totals.
Retention curve, trajectory, and "did the thumbnail get clicked in week 2" all become
computable. A/B retitling (exists today) reads snapshots, not totals.

**3. Experiment ledger.** `brain/strategy/experiments.json`:

```json
{"id": "E3", "factor": "hook_type", "variants": ["question", "number"],
 "assignment": {"20260901-x": "question", "20260903-y": "number"},
 "readout_at": "T+7d", "metric": "ctr", "status": "running"}
```

PACKAGING assigns variants round-robin from running experiments (stratified by lane).
This turns the hash-chosen thumbnail archetype into a real six-arm experiment for free.

**4. Hypothesis ledger + readout.** At each video's T+3d/T+7d snapshot, every experiment with
enough episodes runs its readout rule → hypothesis moves `running → confirmed|killed|uncertain`
with the effect size and evidence link. Confirmed → a lesson file
(`brain/memory/lessons/…`) + a rule in `brain/strategy/current.md`. Killed → the lesson states
what was tried and why it failed (so the system never re-learns it).

**5. Inject, don't just accumulate.** `SCRIPT` and `PACKAGING` prompts get:
top-5 lessons by relevance + active playbook rules + `{perf}` **actually filled** with the
last 5 episodes' real numbers (this is currently hardcoded to a placeholder — B3).

**6. Topic bank v2.** `topics.refill` gains a research pass (top-of-bank candidates get one
`web_search` each for freshness/novelty), scores decay +1/month unused, and lanes get
re-weighted from *experiment readouts* rather than one strategist LLM's vibe.

## 3.6 Migration plan (incremental, repo never broken)

| Phase | Contents | Risk |
|---|---|---|
| **1 — stop the bleeding** (≈1 day) | R1 R2 R3 R4 R12 + fix `{perf}` + QC gate (R7) with quarantine | trivial; all test-covered |
| **2 — learn for real** (≈2–3 days) | factor logging, analytics snapshots, experiment/hypothesis ledgers, playbook MD, `{perf}` from real data, lessons injection | low; additive files |
| **3 — content brain** (≈3–4 days) | RESEARCH (deep_research + knowledge files), CRITIQUE loop, PACKAGING stage, BEAT_QC, brain.py context assembly | medium; new stages behind a `SCALED_V4=1` flag until one full green run |
| **4 — polish** | loudnorm + music bed, health preflight + chain deadline, runlog, topic bank v2, advisory locks | low |

Each phase keeps `pytest -q` green and DRY_RUN able to produce a full episode.

## 3.7 What v4 deliberately does *not* do

- **No LLM-controlled stage order.** The spine stays code. (§3.0)
- **No vector DB / embeddings.** Tag-overlap retrieval over <200 lesson files is enough; a
  vector store would be a second database to crash.
- **No video generation in v1 of v4.** `/api/video` jobs are checkpointed in the design
  (state/generation_jobs.json) but b-roll stays out until images + edit quality are proven —
  the bottleneck today is *writing*, not footage.
- **No moving off GitHub Actions.** The runner-as-employee model is correct for zero-infra;
  the fixes above are what make it survivable, not a platform change.

---

## Appendix — problem → fix cross-reference

| Problem | Fix | Phase |
|---|---|---|
| A1 duplicate uploads | R1 | 1 |
| A2 publish lies | R2 | 1 |
| A3 work/ re-bought | R3 | 1 |
| A4 push races | R4 | 1 |
| A5 no QC | R7 | 1 |
| B1 no research | RESEARCH stage | 3 |
| B2 no critique | CRITIQUE stage | 3 |
| B3 inert learning | §3.5 all | 2 |
| B4 packaging dilution | PACKAGING stage | 3 |
| B5 director amnesia | lessons + beat-mix injection | 2 |
| B6 static topics | topic bank v2 | 4 |
| B7 audio | R8 | 4 |
| C1 budget blowout | R5 | 4 |
| C2 dead hosts | R6 | 4 |
| C3 srt redo | R3 | 1 |
| C4 config freeze | preflight assert | 4 |
| C5 single-video | second job slot (post-v4) | — |
| C6 PAT on disk | R11 | 1 |
| C7 red tests | R12 | 1 |
| C9 print logging | R10 | 4 |
