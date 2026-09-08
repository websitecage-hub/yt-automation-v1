# The Memeic-Educational Quality Plan
### How Kronvex gets to reference-video quality — fast, with the APIs we already have

_Reference: Casually Explained. Deconstruct it and the quality is **not** animation.
It is three cheap things stacked on top of each other:_

> **1. A recurring cast you recognize in 2 frames.**
> **2. Visuals that BETRAY the narration (juxtaposition), never illustrate it.**
> **3. Comic timing — fast cuts, one hold on the punchline.**

AI video generation gives you none of these (slow, inconsistent characters, uncanny motion).
We already own all three mechanisms. The plan is to point them at one idea:

---

## THE BIG IDEA: "One Cast, One World" — the show is a **moving comic**, not generated video

**Every beat in the show is the SAME recurring cast, re-staged by `edit_image`.**

1. **Build the cast ONCE** (one-time, ~4 image calls, committed to `assets/cast/`):
   - `croc.png` — Professor Croc (exists ✅)
   - `guy.png` — "The Guy": the dumb everyman victim of every joke (flat cartoon, dead eyes, tiny coffee mug)
   - `world.png` — the recurring world: plain off-white background, one desk, one graph, one plant. **Same background every episode.**
   - `cast_sheet.png` — both characters side by side, turnaround poses.
   Upload each once → cache public URLs in git (the `croc.url` pattern already exists ✅).

2. **Every visual beat = `edit_image(cast_url, situation)`** — the mechanism already proven by
   `meme_croc()` (24/24 success on the MCP path). The director's `doodle`/`photo`/`meme` beats
   all become **cast edits**:
   - `"Same croc, same guy, same world. The guy is now doing taxes in a hard hat. Flat cartoon, plain background, no text."`
   → Same faces, same world, every episode = **it reads as a SHOW, not AI slop.** This is the
   single highest-leverage change. Consistency *is* the production value.

3. **No AI video in the body of the edit.** Motion comes from the beat engine we already have
   (zooms, pops, slides, syncopated cuts). The stills + locked tweens + voice = the "moving
   comic" feel of the reference. AI video is uncanny and slow; a 1.6s cut hides everything.

4. **AI video has exactly ONE job: the punchline.** Budget **1–2 `animate_image` calls per
   episode**, only on the biggest gag (the meme beat marked `template:"full"`), 2–4s, 480p.
   The joke moves; the lesson doesn't. Async job IDs checkpoint into the job JSON (resume-safe).

---

## The director prompt upgrade (one paragraph change, big quality jump)

Add the **Setup → Betrayal** rule and the cast to the BEATS prompt:

```
THE CAST (edit-only): Croc (deadpan professor), The Guy (dumb everyman), the World
(desk, graph, plant, off-white). NEVER invent new characters — re-stage THESE.
Every beat = SETUP (narration says X) → BETRAYAL (visual shows the humiliating truth of X).
  narration: "humans have weaker jaws"  →  visual: The Guy losing arm-wrestle to a house cat
One gag per beat. If a beat isn't a joke, a number, or a cut — it's a mistake.
The punchline beat of each scene gets template "full" (the 1 animation budget).
```

## The timing recipe we already have (keep, measure, don't touch)

- `beats.RHYTHM` short-short-LONG grid = the reference's variance ✅
- 1.6s median cut, hold lands on the punchline ✅
- SFX: pop on numbers, silence on cuts ✅
- **New micro-rule:** the punchline beat holds 2× longer than the median (the laugh needs air).

## The quality gate (QC, 30 seconds, no LLM)

A beat ships only if it has: **a face from the cast + a betrayal of the narration + (numbers)
or (a caption ≤4 words)**. A scene ships only if ≥1 beat is `photo`-style irony and the scene's
last beat is its biggest gag. Thumbnail: cast face + 2-tone type (already built ✅).

## Implementation order (fastest path, ~1 day)

| # | Change | Where | Cost |
|---|---|---|---|
| 1 | Generate the 4 cast assets once, commit + cache URLs | `tools/make_cast.py` (new, ~40 lines) | 4 API calls |
| 2 | `adapters.cast_edit(situation, who="guy")` = `edit_image(cast_url, …)` | 10 lines | — |
| 3 | Route `doodle` + `meme` beats to `cast_edit` (photo beats stay raw irony) | `run._render_beat_art` | — |
| 4 | BEATS prompt: cast rule + setup→betrayal rule | `config/prompts.md` | free |
| 5 | Punchline animation: `animate_image` on `template:"full"` beats, job-ID checkpointed | `run.py` + 1 job field | 1–2 calls/ep |
| 6 | Punchline hold 2× + QC gate | `beats.py`, `qc` check | free |

**Net effect:** every episode looks like the same cartoonist drew it, every joke lands through
juxtaposition the viewer already understands, and the only new API spend is 1–2 short
animations per episode. That is the reference quality formula — cast, betrayal, timing —
using the endpoints that are already proven in this repo.
