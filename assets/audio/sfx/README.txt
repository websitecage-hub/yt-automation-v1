assets/audio/sfx — four one-shot hits, dropped in by hand
=========================================================

compose.py looks for exactly these four names (any of .mp3 .wav .m4a .ogg
.flac; the extension is read off the file, not assumed):

    whoosh.mp3     every `img` and `meme` beat  — the picture-change swish
    pop.mp3        every `type` and `stat` beat — the word/number punch-in
    zap.mp3        every `zoom` beat            — the punch-in accent
    confetti.mp3   reserved for the verdict stamp

A name with no file is skipped with one line in the log
("[compose] no audio for sfx ...") and the video renders without that hit.
Nothing here is fatal — SFX are polish, not structure.

Keep them SHORT. compose emits each one as a 0.4s clip at volume 0.5, so
anything longer is cut off mid-tail and anything front-loaded with silence
lands late. 100-300ms of actual sound is the target.

Sources that are safe to commit: freesound.org (CC0 filter), or
pixabay.com/sound-effects (Pixabay licence). Do not commit anything
copyrighted — the channel is monetised and this repo is public.

Note the top-level .gitignore blanket-ignores *.mp3 to keep generated
narration out of the repo, with an explicit negation for this directory.
If you add a .wav instead, add the matching negation or git will silently
skip your file and the runner will render silent.
