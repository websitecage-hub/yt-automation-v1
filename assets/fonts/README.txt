SCALED — display, mono and pixel faces, in two formats each
==========================================================

Six committed files, three typefaces, all Google Fonts under the SIL Open
Font License 1.1 (free commercially, fine to ship inside this repo):

  display.woff2 / display.ttf   Archivo Black    900   CSS family 'Display'
  mono.woff2    / mono.ttf      JetBrains Mono   400   CSS family 'Mono'
  pixel.woff2   / pixel.ttf     Press Start 2P   400   CSS family 'Pixel'

Why each face is committed twice
--------------------------------
The two rendering paths cannot read the same file.

  woff2  ->  the VIDEO. config/style.css @font-face's these, and compose
             rebases the url() to 'assets/fonts/*.woff2' for the project root.
  ttf    ->  the THUMBNAIL. pipeline/thumbnail.py paints every word with
             Pillow, and Pillow CANNOT render woff2: it parses the container
             happily and then fails to decompress the Brotli-packed glyph
             tables, so every letter comes out .notdef — a row of tofu boxes
             in a 1280x720 JPEG that nothing else in the pipeline notices.

So the same typeface must be present in both formats, and the two must be
the same typeface — otherwise the thumbnail's headline and the video's
captions are set in different fonts and only a human ever spots it.
/tmp-style scratch checks are not enough; the assertion that matters is that
`ttf family name` matches what the woff2 was downloaded for.

Never point style.css at a remote URL. Renders run in a headless Chrome with
no reliable network, and a missing webfont silently falls back to Arial Black
mid-render.

Re-downloading them
-------------------
  UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
  css () { curl -s -A "$UA" "https://fonts.googleapis.com/css2?family=$1&display=swap"; }

  # woff2: take the LATIN subset, not the first one in the file.
  latin () { css "$1" | awk '/^\/\* latin \*\//{f=1} f&&/woff2/{match($0,/https:[^)]+woff2/);print substr($0,RSTART,RLENGTH);exit}'; }
  curl -sL -o display.woff2 "$(latin 'Archivo+Black')"
  curl -sL -o mono.woff2    "$(latin 'JetBrains+Mono:wght@400')"
  curl -sL -o pixel.woff2   "$(latin 'Press+Start+2P')"

  # ttf: the v1 API still serves TrueType, which is exactly what Pillow wants.
  curl -sL -o display.ttf "$(curl -s "https://fonts.googleapis.com/css?family=Archivo+Black" | grep -oE 'https[^)]+\.ttf' | head -1)"

THE TRAP, because it already cost us once
-----------------------------------------
Google's css2 response lists one @font-face per subset, in this order:

    cyrillic-ext, cyrillic, greek, vietnamese, latin-ext, latin

`... | grep -oE 'https.*woff2' | head -1` therefore grabs CYRILLIC-EXT.
mono.woff2 was committed that way and sat in the repo as a valid 1160-byte
woff2 containing no latin letters at all — the lesson chip and the topic line
quietly fell back to system monospace in every render. Match on the
`/* latin */` comment. Verify afterwards: each file starts with the bytes
wOF2, and a correct latin subset is 12-22 KB, never one or two.

Swapping in a different display face is fine — keep the filenames, keep the
weights heavy (900 display / 400 mono / 400 pixel), and replace BOTH formats,
since every caption, stat number, lesson chip and thumbnail word is sized
against these metrics.

Press Start 2P has no arrow codepoints, which is why compose draws arrow
beats as CSS triangles instead of glyphs. Do not "fix" that back to a
character.
