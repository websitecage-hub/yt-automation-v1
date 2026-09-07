assets/avatar — Professor Croc, one static PNG
=============================================

    croc.png    the host. Bottom-right of every frame, all 1080p of it.

That is the whole system. There is no rig, no expression set, no mouth
shapes and no second image: compose.py takes the alphabetically-first .png
in this directory, drops it in once as a global element (not a timed clip),
and gives it a single 1.2s sine-eased vertical bob that yoyos for the
length of each scene. Nothing else ever touches it.

Requirements:
  * PNG with a real alpha channel — it is composited over the art, so a
    white box background will show as a white box.
  * Roughly portrait or square. compose scales to height and pins to the
    bottom-right corner, so a wide image eats the frame.
  * Transparent margins are cropped by nothing — trim them yourself or the
    bob will look like it has slack in it.

A missing PNG is loud but not fatal: compose logs the miss, emits no
#avatar element and no bob, and the video renders host-less. Set
SCALED_REQUIRE_AVATAR=1 to turn that into a crash instead, which is what
you want in CI once the file is committed — a silently host-less episode is
worse than a failed run.
