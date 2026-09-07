assets/audio/lofi — the music bed, dropped in by hand
=====================================================

Any number of audio files with any names. compose.py sorts them by filename,
measures each one, and lays them end to end from t=0 until the video is
covered — so track order is filename order, and the last one is cut mid-bar
wherever the video happens to end. One long file is the simplest thing that
works; several is fine if you want the bed to change through a lecture.

Volume is fixed at 0.12 in the composition (data-volume on the clip). That is
deliberately far under the narration: the bed should be felt, not heard. Do
not "pre-quiet" the files as well or it will vanish entirely.

Duration measurement uses ffprobe when it exists and falls back to a
bitrate estimate when it does not, so a wildly non-standard encode can be
placed a beat early or late. Plain CBR mp3 or wav avoids the guesswork.

Sources that are safe to commit: pixabay.com/music, freemusicarchive.org
(CC0/CC-BY only), or your own. Nothing copyrighted — a Content-ID claim on
the bed silences the whole upload.

Note the top-level .gitignore blanket-ignores *.mp3, with an explicit
negation for this directory. A .wav needs its own negation adding, or git
will skip the file and the runner will render with no music at all.
