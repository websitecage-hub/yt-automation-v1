"""Build the recurring cast ONCE (Memeic plan step 1).

Generates guy.png (dumb everyman), world.png (the recurring off-white room) and
cast_sheet.png (both characters side by side), uploads each to the media host,
and caches the public URLs beside the files. croc.png already exists and is
uploaded on first use by adapters.avatar_ref_url().

    python3 tools/make_cast.py [--skip-art]   # --skip-art: upload+cache only

Costs 3 image calls + 3 uploads, once, ever. Idempotent: existing files and
cached URLs are never regenerated.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import adapters

CAST_DIR = os.path.join(adapters.REPO, "assets", "cast")

HOUSE = ("flat cartoon, thick black outlines, solid flat colors, same simple "
         "style as a children's educational cartoon, plain background, no shading, "
         "no photorealism, no text, no watermark.")

CAST = {
    "guy": ("a dumb everyman character, dead tired eyes, plain grey t-shirt, "
            "holding a tiny coffee mug, full body, " + HOUSE),
    "world": ("an empty cartoon room: plain off-white background, one wooden desk, "
              "one wall graph with a rising arrow, one small potted plant, "
              "nothing else, " + HOUSE),
    "cast_sheet": ("character lineup sheet: a green cartoon crocodile professor "
                   "in a graduation cap beside a dumb everyman with dead eyes "
                   "holding a tiny coffee mug, full bodies side by side, " + HOUSE),
}


def _cached_url(name):
    path = os.path.join(CAST_DIR, name + ".url")
    try:
        with open(path, encoding="utf-8") as fh:
            url = fh.read().strip()
        if url.startswith("http"):
            return url
    except OSError:
        pass
    return None


def main():
    skip_art = "--skip-art" in sys.argv
    os.makedirs(CAST_DIR, exist_ok=True)
    for name, prompt in CAST.items():
        img_path = os.path.join(CAST_DIR, name + ".png")
        if not skip_art and not os.path.exists(img_path):
            data = adapters.image(prompt, "1024x1024")
            with open(img_path, "wb") as fh:
                fh.write(data)
            print("[cast] drew %s (%d bytes)" % (img_path, len(data)))
        elif os.path.exists(img_path):
            print("[cast] %s exists -- kept" % img_path)
        if _cached_url(name):
            print("[cast] %s url cached" % name)
            continue
        if not os.path.exists(img_path):
            print("[cast] no art for %s -- run without --skip-art first" % name)
            continue
        url = adapters.media_upload(img_path)
        with open(os.path.join(CAST_DIR, name + ".url"), "w", encoding="utf-8") as fh:
            fh.write(url.strip() + "\n")
        print("[cast] uploaded %s" % name)


if __name__ == "__main__":
    main()
