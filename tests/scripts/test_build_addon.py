"""Tests for what ``scripts/build_addon.py`` puts in — and keeps out of — the package.

The archive's CONTENTS are the whole reason this script exists, and one wrong entry is a
60x size regression that nobody notices until AnkiWeb rejects the upload or users complain
that every update is a 60 MB download. Building the real zip here would take seconds and pull
in ``vendor/``, so the exclusion rule itself is what gets pinned.
"""

from __future__ import annotations

import json
import sys
import zipfile
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


class TestTheBuildStampsItsVersion:
    """``human_version`` is what Anki shows beside the add-on, and the only way someone at
    another machine can answer "am I on the latest build?" without comparing file dates.

    The repo's own ``manifest.json`` deliberately carries no version — a number committed by
    hand goes stale the moment someone forgets — so the BUILD writes it in.
    """

    def test_the_stamp_lands_in_the_manifest(self):
        stamped = json.loads(build_addon._stamped_manifest("v26.09.7"))

        assert stamped["human_version"] == "v26.09.7"
        assert stamped["package"] == "omnia"  # everything else survives

    def test_the_file_on_disk_is_never_rewritten(self):
        # A build must not leave the working tree dirty, or the next commit carries a version
        # that belongs to one machine's last run.
        path = build_addon.ADDON_DIR / "manifest.json"
        before = path.read_bytes()

        build_addon._stamped_manifest("v26.09.7")

        assert path.read_bytes() == before

    def test_a_local_build_never_claims_to_be_a_release(self, monkeypatch, tmp_path):
        monkeypatch.delenv("OMNIA_BUILD_VERSION", raising=False)
        assert _built_version(monkeypatch, tmp_path) == "dev"

    def test_a_blank_version_falls_back_rather_than_shipping_empty(
        self, monkeypatch, tmp_path
    ):
        # Exactly how this happens: `${{ steps.name.outputs.tag }}` from a step that was skipped
        # expands to an empty string, and the env var arrives set-but-blank.
        monkeypatch.setenv("OMNIA_BUILD_VERSION", "   ")
        assert _built_version(monkeypatch, tmp_path) == "dev"

    def test_the_archive_holds_exactly_one_manifest(self, monkeypatch, tmp_path):
        # The hazard `_add_tree`'s docstring warns about: a zip CAN hold two members of one
        # name, and readers disagree about which wins. `ZipFile.read` is last-write-wins, so a
        # local check would look fine while an installer reading the central directory took the
        # unstamped copy — and the build that was supposed to announce its version ships
        # without one. Dropping the `skip=` argument is a silent change: it has a default.
        monkeypatch.setenv("OMNIA_BUILD_VERSION", "v26.09.7")
        output = _build_into(monkeypatch, tmp_path)

        with zipfile.ZipFile(output) as archive:
            assert archive.namelist().count("manifest.json") == 1


def _build_into(monkeypatch, tmp_path) -> Path:
    """Run the real build into ``tmp_path`` and return the archive."""
    output = tmp_path / "omnia.ankiaddon"
    monkeypatch.setattr(build_addon, "DIST_DIR", tmp_path)
    monkeypatch.setattr(build_addon, "OUTPUT", output)
    build_addon.build()
    return output


def _built_version(monkeypatch, tmp_path) -> str:
    """The ``human_version`` the build actually wrote into the archive."""
    with zipfile.ZipFile(_build_into(monkeypatch, tmp_path)) as archive:
        return json.loads(archive.read("manifest.json"))["human_version"]
