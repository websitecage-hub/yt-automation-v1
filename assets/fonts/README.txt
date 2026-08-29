SCALED — display + mono fonts
=============================

Two font files belong in this directory. Both are already committed, downloaded
from Google Fonts (SIL Open Font License 1.1 — free for commercial use, and it
is fine to ship them inside this repo):

  display.woff2  = Archivo Black   (weight 900) -> CSS family 'ScaledDisplay'
  mono.woff2     = IBM Plex Mono   (weight 400) -> CSS family 'ScaledMono'

config/style.css references them with a RELATIVE url() only
(../assets/fonts/display.woff2). Never point it at a remote URL: renders run in
an offline-ish headless Chrome and a network font would silently fall back to
Arial Black mid-render.

If you ever need to replace or re-download them
-----------------------------------------------
  UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"

  # Archivo Black -> display.woff2
  curl -s -A "$UA" "https://fonts.googleapis.com/css2?family=Archivo+Black&display=swap" \
    | grep -oE "https://fonts.gstatic.com/[^)]+\.woff2" | head -1 \
    | xargs curl -sL -o assets/fonts/display.woff2

  # IBM Plex Mono -> mono.woff2
  curl -s -A "$UA" "https://fonts.googleapis.com/css2?family=IBM+Plex+Mono&display=swap" \
    | grep -oE "https://fonts.gstatic.com/[^)]+\.woff2" | head -1 \
    | xargs curl -sL -o assets/fonts/mono.woff2

The modern browser User-Agent matters: without it Google serves legacy TTF URLs
instead of woff2. Verify afterwards that each file starts with the bytes wOF2.

Swapping in a different display face is fine — keep the filenames and keep the
weights heavy (900 display / 400 mono), since every caption and stat card is
sized against these metrics.
