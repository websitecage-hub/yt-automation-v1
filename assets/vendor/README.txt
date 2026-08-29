SCALED — vendored GSAP
======================

gsap.min.js (GSAP 3.13.0, standard free/"no charge" license) is already
committed here. pipeline/compose.py copies it into every render project as
proj_<id>/assets/gsap.min.js, and index.html loads it with a single relative
<script src="assets/gsap.min.js">.

It is vendored on purpose. The composition must contain zero remote references:
htmlgen.py's static ban-check rejects any https:// URL in LLM-written scene
fragments, and a CDN script that fails to load inside headless Chrome would
produce a silent, animation-free 8-minute video instead of a loud error.

If you ever need to re-download it
----------------------------------
  curl -sL -o assets/vendor/gsap.min.js \
    https://cdn.jsdelivr.net/npm/gsap@3.13.0/dist/gsap.min.js

Verify the first line mentions "GSAP 3" and the file is roughly 70-75 KB.

Only the core gsap build is needed. The pipeline animates opacity, x, y, scale,
scaleX/scaleY, rotation, width, color, and svgOrigin/transformOrigin — all core
CSSPlugin properties, no paid or bonus plugins, no ScrollTrigger.

If this file is missing, compose.py writes a stub in its place that throws a
clear error at render time and logs loudly, so the failure is obvious rather
than silent. The render then falls back to the ffmpeg Ken Burns path.
