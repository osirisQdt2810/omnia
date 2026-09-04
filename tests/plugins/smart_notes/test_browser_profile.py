"""Picking the Chrome profile to open ``chrome://extensions`` in, and finding the one that
already has the clipper.

Chrome numbers profile DIRECTORIES in creation order and shows an unrelated display name, so
"Profile 16" can be the everyday one and "Default" long abandoned. Opening the wrong one is
not a crash — it is the user loading the extension into a profile they never browse in and
concluding the extension does not work.

The mirror image of that, and a reported bug: a clipper loaded and running in "Profile 3"
was reported as not loaded because Chrome's last-used profile was "Default". Installing still
targets the last-used profile; LOOKING SOMETHING UP now searches them all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnia.plugins.smart_notes.integration import browser
from omnia.plugins.smart_notes.integration.browser import (
    ChromeProfile,
    chrome_user_data_dir,
    find_extension_id,
    locate_extension,
    pick_profile,
    profile_search_order,
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


def _entry_without_manifest(path):
    """A real Chrome entry for an unpacked load: a path, and no cached manifest.

    Copied from what a live profile actually holds. `_entry` above is the convenient shape,
    not the common one, and testing only against it is how the name fallback stayed broken
    while its test stayed green.
    """
    return {
        "path": path,
        "location": 4,
        "active_permissions": {"api": ["storage"]},
        "has_started_service_worker": True,
        "was_installed_by_default": False,
    }


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

    def test_the_fallback_works_when_chrome_cached_no_manifest(self, tmp_path):
        """The case the old fallback could not reach, and the one users actually hit.

        Chrome routinely records an unpacked extension with a path and NO manifest. The name
        then has to come off disk, from the directory Chrome did record, or a running
        extension is reported as not loaded.
        """
        loaded = tmp_path / "omnia-web-clipper"
        loaded.mkdir()
        (loaded / "manifest.json").write_text(
            json.dumps({"name": "Omnia Web Clipper", "version": "1.0"}),
            encoding="utf-8",
        )
        prefs = _prefs({"zzz": _entry_without_manifest(str(loaded))})

        found = find_extension_id(
            prefs,
            name="Omnia Web Clipper",
            source_dir=tmp_path / "somewhere" / "we" / "installed",
        )

        assert found == "zzz"

    def test_a_manifest_less_entry_for_someone_elses_extension_is_ignored(
        self, tmp_path
    ):
        """Reading names off disk must not turn into matching anything with a manifest."""
        other = tmp_path / "unrelated"
        other.mkdir()
        (other / "manifest.json").write_text(
            json.dumps({"name": "Some Other Extension"}), encoding="utf-8"
        )
        prefs = _prefs({"aaa": _entry_without_manifest(str(other))})

        assert find_extension_id(prefs, name="Omnia Web Clipper") is None

    def test_a_store_extensions_relative_path_is_never_read_from_disk(
        self, tmp_path, monkeypatch
    ):
        """Chrome writes "<id>/<version>" for store extensions; resolving that against the
        process CWD would match a stranger's manifest.

        The directory is CREATED here on purpose: without it a relative path merely raises
        FileNotFoundError, which the reader swallows into "" anyway, and the guard could be
        deleted without this test noticing. It was.
        """
        monkeypatch.chdir(tmp_path)
        (tmp_path / "abcdef" / "1.2.3_0").mkdir(parents=True)
        (tmp_path / "abcdef" / "1.2.3_0" / "manifest.json").write_text(
            json.dumps({"name": "Omnia Web Clipper"}), encoding="utf-8"
        )
        prefs = _prefs({"aaa": _entry_without_manifest("abcdef/1.2.3_0")})

        assert find_extension_id(prefs, name="Omnia Web Clipper") is None

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


def _local_state(last_used, **profiles):
    """Chrome's ``Local State`` shape: ``profile.last_used`` + ``profile.info_cache``.

    ``profiles`` maps directory -> (display name, active_time), the two fields the code reads.
    """
    return {
        "profile": {
            "last_used": last_used,
            "info_cache": {
                directory: {"name": name, "active_time": active}
                for directory, (name, active) in profiles.items()
            },
        }
    }


class TestTheSearchOrder:
    """Which profiles a lookup walks, and in what order.

    Installing still targets the one Chrome last used. Looking something UP has to try them all,
    or an extension running in "Profile 3" is reported missing because Chrome happens to be
    sitting in "Default" — the reported bug.
    """

    def test_the_preferred_profile_leads(self):
        """A lookup that succeeded before must still resolve to the same profile."""
        state = _local_state(
            "Default",
            **{"Default": ("moreh", 10), "Profile 3": ("phuc", 999)},
        )
        order = profile_search_order(state)
        assert [p.directory for p in order] == ["Default", "Profile 3"]

    def test_the_rest_follow_most_recently_active_first(self):
        """Between two profiles that both hold it, the one the user browses in is the one meant."""
        state = _local_state(
            "Default",
            **{
                "Default": ("moreh", 10),
                "Profile 3": ("phuc", 500),
                "Profile 8": ("phuc", 900),
                "Profile 5": ("moreh.io", 100),
            },
        )
        order = profile_search_order(state)
        assert [p.directory for p in order] == [
            "Default",
            "Profile 8",
            "Profile 3",
            "Profile 5",
        ]

    def test_ties_break_on_directory_so_the_order_is_stable(self):
        """Equal (often zero) active_time must not reorder between clicks."""
        state = _local_state(
            "Default",
            **{"Default": ("a", 0), "Profile 9": ("x", 0), "Profile 2": ("y", 0)},
        )
        first = [p.directory for p in profile_search_order(state)]
        second = [p.directory for p in profile_search_order(state)]
        assert first == second == ["Default", "Profile 2", "Profile 9"]

    def test_every_profile_appears_exactly_once(self):
        """The preferred one is pulled to the front, not duplicated."""
        state = _local_state(
            "Profile 3",
            **{"Default": ("moreh", 10), "Profile 3": ("phuc", 999)},
        )
        dirs = [p.directory for p in profile_search_order(state)]
        assert sorted(dirs) == ["Default", "Profile 3"]
        assert dirs[0] == "Profile 3"

    def test_no_profiles_at_all_is_an_empty_list(self):
        assert profile_search_order({}) == []
        assert profile_search_order({"profile": {}}) == []


class TestLocatingTheExtension:
    """`locate_extension` walks the given profiles and returns the FIRST that has it."""

    @staticmethod
    def _preferences_by_dir(monkeypatch, per_dir):
        """Route `installed_extension_id`'s preference read to an in-memory map by directory."""
        monkeypatch.setattr(
            browser,
            "read_profile_preferences",
            lambda _udd, directory: per_dir.get(directory, {}),
        )
        monkeypatch.setattr(
            browser, "chrome_user_data_dir", lambda _p="": Path("/nowhere")
        )

    def test_finds_it_in_a_profile_that_is_not_the_preferred_one(self, monkeypatch):
        """The reported bug: loaded in Profile 3, Chrome last used Default."""
        self._preferences_by_dir(
            monkeypatch,
            {
                "Default": _prefs({}),
                "Profile 3": _prefs(
                    {
                        "jmdh": _entry(
                            "Omnia Web Clipper", "/home/u/dev/omnia-web-clipper"
                        )
                    }
                ),
            },
        )
        profiles = [
            ChromeProfile("Default", "moreh", 10.0),
            ChromeProfile("Profile 3", "phuc", 999.0),
        ]
        found = locate_extension(profiles, name="Omnia Web Clipper")
        assert found is not None
        assert found.profile.directory == "Profile 3"
        assert found.extension_id == "jmdh"

    def test_the_first_profile_in_order_wins_when_several_have_it(self, monkeypatch):
        """Two copies loaded in two profiles: act on the one the caller ranked first."""
        self._preferences_by_dir(
            monkeypatch,
            {
                "Default": _prefs({"aaaa": _entry("Omnia Web Clipper", "/one")}),
                "Profile 3": _prefs({"bbbb": _entry("Omnia Web Clipper", "/two")}),
            },
        )
        profiles = [
            ChromeProfile("Default", "moreh", 10.0),
            ChromeProfile("Profile 3", "phuc", 999.0),
        ]
        found = locate_extension(profiles, name="Omnia Web Clipper")
        assert found is not None
        assert (found.profile.directory, found.extension_id) == ("Default", "aaaa")

    def test_our_clone_in_a_later_profile_beats_another_build_in_the_preferred_one(
        self, monkeypatch
    ):
        """Match strength outranks profile order.

        The user keeps a dev checkout of the clipper loaded in their everyday profile and the
        add-on's own clone in another. After Upgrade, Reload must act on the CLONE — the copy
        that was just upgraded — not on whichever build happens to sit in the profile Chrome
        opened last. A single first-hit pass over profiles got this backwards.
        """
        self._preferences_by_dir(
            monkeypatch,
            {
                "Default": _prefs(
                    {
                        "devdev": _entry(
                            "Omnia Web Clipper", "/home/u/dev/omnia-web-clipper"
                        )
                    }
                ),
                "Profile 3": _prefs(
                    {
                        "oursid": _entry(
                            "Omnia Web Clipper", "/home/u/clippers/web_clipper"
                        )
                    }
                ),
            },
        )
        profiles = [
            ChromeProfile("Default", "moreh", 10.0),
            ChromeProfile("Profile 3", "phuc", 999.0),
        ]
        found = locate_extension(
            profiles,
            name="Omnia Web Clipper",
            source_dir=Path("/home/u/clippers/web_clipper"),
        )
        assert found is not None
        assert (found.profile.directory, found.extension_id) == ("Profile 3", "oursid")

    def test_nowhere_is_none_not_a_wrong_profile(self, monkeypatch):
        self._preferences_by_dir(
            monkeypatch, {"Default": _prefs({}), "Profile 3": _prefs({})}
        )
        profiles = [
            ChromeProfile("Default", "moreh", 10.0),
            ChromeProfile("Profile 3", "phuc", 999.0),
        ]
        assert locate_extension(profiles, name="Omnia Web Clipper") is None

    def test_no_profiles_to_search_is_none(self):
        assert locate_extension([], name="Omnia Web Clipper") is None
