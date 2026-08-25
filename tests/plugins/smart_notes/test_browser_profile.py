"""Picking the Chrome profile to open ``chrome://extensions`` in.

Chrome numbers profile DIRECTORIES in creation order and shows an unrelated display name, so
"Profile 16" can be the everyday one and "Default" long abandoned. Opening the wrong one is
not a crash — it is the user loading the extension into a profile they never browse in and
concluding the extension does not work.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnia.plugins.smart_notes.integration.browser import (
    ChromeProfile,
    chrome_user_data_dir,
    find_extension_id,
    pick_profile,
    read_local_state,
)


class TestPickingTheProfile:
    def test_chromes_own_last_used_wins(self):
        """Chrome records which profile it last opened; that is a better answer than a clock."""
        state = {
            "profile": {
                "last_used": "Profile 1",
                "info_cache": {
                    "Default": {"name": "old", "active_time": 9_999_999_999},
                    "Profile 1": {"name": "phuc", "active_time": 1},
                },
            }
        }

        assert pick_profile(state) == ChromeProfile("Profile 1", "phuc", 1.0)

    def test_falls_back_to_the_most_recently_active(self):
        state = {
            "profile": {
                "info_cache": {
                    "Default": {"name": "old", "active_time": 100},
                    "Profile 9": {"name": "new", "active_time": 900},
                }
            }
        }

        assert pick_profile(state).directory == "Profile 9"

    def test_a_directory_with_no_display_name_reports_its_directory(self):
        state = {"profile": {"last_used": "Profile 3", "info_cache": {}}}

        assert pick_profile(state) == ChromeProfile("Profile 3", "Profile 3", 0.0)

    @pytest.mark.parametrize("state", [{}, {"profile": {}}, {"profile": "nonsense"}])
    def test_nothing_to_go_on_is_none_not_a_guess(self, state):
        """No preference is a fine answer — the caller then opens Chrome the ordinary way."""
        assert pick_profile(state) is None


class TestReadingLocalState:
    def test_reads_the_file(self, tmp_path):
        (tmp_path / "Local State").write_text(
            json.dumps({"profile": {"last_used": "Profile 2"}}), encoding="utf-8"
        )

        assert read_local_state(tmp_path)["profile"]["last_used"] == "Profile 2"

    def test_a_missing_file_is_empty_not_an_error(self, tmp_path):
        assert read_local_state(tmp_path) == {}

    def test_corrupt_json_is_empty_not_an_error(self, tmp_path):
        """A Chrome that is running may have written a partial file; installing must not fail."""
        (tmp_path / "Local State").write_text("{not json", encoding="utf-8")

        assert read_local_state(tmp_path) == {}

    def test_no_user_data_dir_is_empty(self):
        assert read_local_state(None) == {}


class TestTheUserDataDirectory:
    def test_macos(self, tmp_path):
        assert chrome_user_data_dir("darwin", tmp_path) == (
            tmp_path / "Library" / "Application Support" / "Google" / "Chrome"
        )

    def test_windows_uses_localappdata(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))

        assert chrome_user_data_dir("win32", tmp_path) == (
            tmp_path / "Local" / "Google" / "Chrome" / "User Data"
        )

    def test_linux(self, tmp_path):
        assert chrome_user_data_dir("linux", tmp_path) == (
            tmp_path / ".config" / "google-chrome"
        )

    def test_an_unknown_platform_is_none(self, tmp_path):
        assert chrome_user_data_dir("plan9", tmp_path) is None


def _prefs(entries):
    """Chrome's shape: ``extensions.settings`` keyed by extension id."""
    return {"extensions": {"settings": entries}}


def _entry(name, path):
    return {"manifest": {"name": name}, "path": path}


class TestFindingOurExtension:
    """An unpacked extension with no manifest ``key`` gets a PATH-DERIVED id.

    It therefore differs per machine and cannot be hard-coded, which is why Reload has to look
    it up in the profile Chrome recorded it in.
    """

    def test_it_matches_the_clone_we_installed(self):
        prefs = _prefs(
            {
                "aaa": _entry("Something Else", "/other"),
                "bbb": _entry("Omnia Web Clipper", "/home/u/clippers/web_clipper"),
            }
        )
        found = find_extension_id(
            prefs, source_dir=Path("/home/u/clippers/web_clipper")
        )
        assert found == "bbb"

    def test_the_path_wins_over_a_same_named_copy(self):
        """A user may also have their own build loaded; Reload must act on OURS."""
        prefs = _prefs(
            {
                "theirs": _entry("Omnia Web Clipper", "/home/u/dev/omnia-web-clipper"),
                "ours": _entry("Omnia Web Clipper", "/home/u/clippers/web_clipper"),
            }
        )
        found = find_extension_id(
            prefs,
            name="Omnia Web Clipper",
            source_dir=Path("/home/u/clippers/web_clipper"),
        )
        assert found == "ours"

    def test_the_name_is_the_fallback_when_the_path_does_not_match(self):
        """Someone who loaded it from elsewhere still gets a working Reload."""
        prefs = _prefs({"zzz": _entry("Omnia Web Clipper", "/somewhere/else")})
        found = find_extension_id(
            prefs,
            name="Omnia Web Clipper",
            source_dir=Path("/home/u/clippers/web_clipper"),
        )
        assert found == "zzz"

    def test_matching_ignores_case_and_a_trailing_separator(self):
        """Chrome writes the path as the OS gave it; ours comes from pathlib."""
        prefs = _prefs({"bbb": _entry("X", "C:/Users/U/Clippers/Web_Clipper/")})
        found = find_extension_id(
            prefs, source_dir=Path("C:/users/u/clippers/web_clipper")
        )
        assert found == "bbb"

    def test_not_installed_is_none_not_a_wrong_guess(self):
        """Returning some other extension's id would reload a stranger's extension."""
        prefs = _prefs({"aaa": _entry("Something Else", "/other")})
        assert find_extension_id(prefs, name="Omnia Web Clipper") is None

    def test_a_profile_with_no_extensions_at_all_is_none(self):
        assert find_extension_id({}, name="Omnia Web Clipper") is None
        assert find_extension_id(_prefs({}), name="Omnia Web Clipper") is None

    def test_a_malformed_entry_does_not_crash_the_search(self):
        """Preferences is a large file written by another program; it must be read defensively."""
        prefs = _prefs(
            {
                "bad": "not-a-mapping",
                "good": _entry("Omnia Web Clipper", "/home/u/clippers/web_clipper"),
            }
        )
        assert find_extension_id(prefs, name="Omnia Web Clipper") == "good"
