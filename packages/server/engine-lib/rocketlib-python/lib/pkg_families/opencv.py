# =============================================================================
# MIT License — Copyright (c) 2026 Aparavi Software AG
# (full text in depends.py)
# =============================================================================

"""The ``cv2`` family: four distributions, one import directory.

**The declared order is a lattice flattened deliberately, and the obvious order is wrong.**
``opencv-python`` adds the GUI functions to ``opencv-python-headless``;
``opencv-contrib-python-headless`` adds the contrib modules to it; those two are
**incomparable** — neither contains the other. Only ``opencv-contrib-python`` is above
everything.

So the order below is headless -> GUI -> contrib-headless -> contrib, and two things
follow. Writing ``opencv-python`` first — the order the distribution names suggest — would
let headless overwrite the GUI build in any environment resolving both without contrib,
which is exactly the doctr + easyocr combination. And where the middle two meet with
nothing above them *something* is lost whichever way we choose; the tiebreak is contrib
over GUI, on the measured ground that nothing in this tree calls an OpenCV GUI function
while ``ximgproc`` has a real consumer.

All four apply on every platform, so the owner is never actually used here — something is
always resolved, or the family would not have been detected. Its position marks the widest
variant and nothing more.

No ``namespace_version``: this family **derives** its version from what the consumers
resolved, which is the whole point of the item. Nor is there a base-environment override —
the base aligns exactly as an overlay does.
"""

from __future__ import annotations

from . import Family, Member

CV2 = Family(
    name='cv2',
    import_name='cv2',
    members=(
        Member(dist='opencv-python-headless'),
        Member(dist='opencv-python'),
        Member(dist='opencv-contrib-python-headless'),
        Member(dist='opencv-contrib-python'),
    ),
    probe=None,  # lands with the probes
    namespace_version=None,
    notes=(
        'Lockstep-released: 4.10.0.84, 4.11.0.86, 4.12.0.88, 4.13.0.90, 4.13.0.92 and '
        '5.0.0.93 all exist for every one of the four members, so a derived minimum is '
        'always a version they can all be held at.',
        'opencv-python and opencv-contrib-python are the non-headless builds and want '
        'libGL on Linux. Drawing the install set from the resolution confines that to '
        'environments whose consumers actually name one.',
    ),
)
