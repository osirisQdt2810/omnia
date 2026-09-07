"""Where a clipper's lookup profile is read and written.

The dialog is Qt and is not built here, but the rule it saves by is not: ``store_profile``
lives in the pure config module and the dialog calls exactly this function, so these tests
fail when the dialog would have written the wrong thing.
"""

from __future__ import annotations

from omnia.plugins.word_lookup.config import (
    DESKTOP_CLIPPER,
    WEB_CLIPPER,
    WordLookupSettings,
    store_profile,
)


def _save(raw, client, profile, port=8766):
    """The dialog's save path, minus the widget: it calls exactly this function."""
    return store_profile(raw, client, profile, port=port)


class TestSavingOneClipper:
    def test_it_does_not_delete_the_other_clipper(self):
        # `update_section` merges SHALLOWLY, so writing `clients` replaces the whole map. A
        # save that started from an empty dict would silently wipe the other clipper's profile.
        raw = {"clients": {DESKTOP_CLIPPER: {"max_results": 9}}}

        stored = _save(raw, WEB_CLIPPER, {"max_results": 3})["clients"]
        assert stored[DESKTOP_CLIPPER] == {"max_results": 9}
        assert stored[WEB_CLIPPER] == {"max_results": 3}

    def test_a_client_key_a_newer_build_added_survives(self):
        # ADR-010, one layer above the models: a key this build cannot name is still someone's.
        raw = {"clients": {"phone_clipper": {"max_results": 2}}}

        section = _save(raw, WEB_CLIPPER, {"max_results": 3})

        assert "phone_clipper" in section["clients"]

    def test_the_port_is_written_top_level_not_into_the_profile(self):
        # There is ONE server and both clippers talk to it; a per-client port would be a
        # setting that cannot mean anything.
        section = _save({}, WEB_CLIPPER, {"max_results": 3}, port=9999)

        assert section["port"] == 9999
        assert "port" not in section["clients"][WEB_CLIPPER]

    def test_no_client_edits_the_flat_fallback(self):
        # The profile older clipper builds — the ones that send no `client` — are served from.
        section = _save({}, "", {"max_results": 3})

        assert section["max_results"] == 3
        assert "clients" not in section


class TestReadingBack:
    def test_each_clipper_reads_its_own_profile(self):
        settings = WordLookupSettings.parse_obj(
            {
                "clients": {
                    WEB_CLIPPER: {"max_results": 3},
                    DESKTOP_CLIPPER: {"max_results": 9},
                }
            }
        )
        assert settings.profile_for(WEB_CLIPPER).max_results == 3
        assert settings.profile_for(DESKTOP_CLIPPER).max_results == 9

    def test_a_clipper_with_no_profile_falls_back_rather_than_raising(self):
        settings = WordLookupSettings.parse_obj({"max_results": 7})
        assert settings.profile_for(WEB_CLIPPER).max_results == 7
        assert settings.profile_for("").max_results == 7
