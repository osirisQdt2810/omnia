"""Audio utilities shared by features: stdlib-only PCM handling, no binary dependency.

The add-on can only vendor pure-Python code, so anything that touches samples has to be built
on :mod:`wave`/:mod:`struct`/:mod:`math`. This package is that toolbox. Like every core seam
it must stay free of ``aqt``/``anki`` imports (and of ``omnia.plugins``) so it unit-tests
headless.
"""

from __future__ import annotations

from omnia.core.audio.wav import SAMPLE_WIDTH, WavClip, WavFormatError

__all__ = [
    "SAMPLE_WIDTH",
    "WavClip",
    "WavFormatError",
]
