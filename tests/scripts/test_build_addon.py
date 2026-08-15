"""Tests for what ``scripts/build_addon.py`` puts in — and keeps out of — the package.

The archive's CONTENTS are the whole reason this script exists, and one wrong entry is a
60x size regression that nobody notices until AnkiWeb rejects the upload or users complain
that every update is a 60 MB download. Building the real zip here would take seconds and pull
in ``vendor/``, so the exclusion rule itself is what gets pinned.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import build_addon  # noqa: E402


class TestVoiceWeightsAreNotPackaged:
    def test_an_onnx_voice_never_ships(self):
        """~60 MB of weights per voice, downloaded on install AND on every single update."""
        assert build_addon._should_skip(Path("models/piper/vi_VN-vais1000-medium.onnx"))

    def test_the_voice_config_and_readme_still_ship(self):
        """Piper loads ``<model>.json`` by guessing the path, so the ~5 KB config stays.

        Keeping it is what lets a user on a blocked network drop a hand-downloaded ``.onnx``
        next to it and end up with a complete voice; the README is where that is explained.
        """
        assert not build_addon._should_skip(
            Path("models/piper/vi_VN-vais1000-medium.onnx.json")
        )
        assert not build_addon._should_skip(Path("models/piper/README.md"))

    def test_downloaded_voices_in_user_files_are_not_swept_into_a_build(self):
        """A dev who ran the add-on has a fetched voice under ``user_files``."""
        assert build_addon._should_skip(
            Path("user_files/models/piper/vi_VN-vais1000-medium.onnx")
        )
