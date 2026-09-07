"""Where a clipper's lookup profile is read and written.

The dialog is Qt and is not built here, but the rule it saves by is not: ``store_profile``
lives in the pure config module and the dialog calls exactly this function, so these tests
fail when the dialog would have written the wrong thing.
"""

from __future__ import annotations

from omnia.plugins.word_lookup.config import (
    DESKTOP_CLIPPER,
    WEB_CLIPPER,
    LookupProfile,
    WordLookupSettings,
    store_profile,
)


def _save(raw, client, profile, port=8766):
    """The dialog's save path, minus the widget: it calls exactly this function.

    ``settings`` is parsed from the same stored section the dialog reads it from
    (``repo.feature_settings``), so these tests exercise the pair the dialog really passes.
    """
    return store_profile(
        raw,
        client,
        profile,
        port=port,
        settings=WordLookupSettings.parse_obj(raw),
    )


class TestSavingOneClipper:
    def test_it_does_not_delete_the_other_clipper(self):
        # `update_section` merges SHALLOWLY, so writing `clients` replaces the whole map. A
        # save that started from an empty dict would silently wipe the other clipper's profile.
        raw = {"clients": {DESKTOP_CLIPPER: {"max_results": 9}}}

        stored = _save(raw, WEB_CLIPPER, {"max_results": 3})["clients"]
        assert stored[DESKTOP_CLIPPER] == {"max_results": 9}
        assert stored[WEB_CLIPPER]["max_results"] == 3

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

    def test_the_flat_pre_upgrade_keys_are_never_written_again(self):
        # They are still READ — that is what an older clipper sending no `client` is served
        # from — but every profile belongs to somebody now, so a save only touches `clients`.
        section = _save({"max_results": 9}, WEB_CLIPPER, {"max_results": 3})

        assert set(section) == {"clients", "port"}
        assert section["clients"][WEB_CLIPPER]["max_results"] == 3


class TestSaveKeepsWhatTheDialogCannotRender:
    """ADR-010 one level down: inside a client's entry, not just across the ``clients`` map.

    The dialog draws six of :class:`LookupProfile`'s seven settings, so an entry rebuilt from
    what it posts deletes ``hidden_fields`` on every save — and deletes any per-profile setting
    a newer Omnia added, which the other synced device is still reading. The entry is seeded
    with what this client is served TODAY (its stored entry, else the flat pre-profile keys) and
    the posted values are written over that, so a save can only ever change what was rendered.
    """

    def test_hidden_fields_survive_a_save_that_never_showed_them(self):
        raw = {
            "clients": {WEB_CLIPPER: {"hidden_fields": ["Note ID"], "max_fields": 4}}
        }

        stored = _save(raw, WEB_CLIPPER, {"max_fields": 9})["clients"][WEB_CLIPPER]

        assert stored["hidden_fields"] == ["Note ID"]
        assert stored["max_fields"] == 9

    def test_a_profile_key_only_a_newer_omnia_knows_survives(self):
        raw = {"clients": {WEB_CLIPPER: {"a_future_setting": {"a": 1}}}}

        stored = _save(raw, WEB_CLIPPER, {"max_results": 3})["clients"][WEB_CLIPPER]

        assert stored["a_future_setting"] == {"a": 1}

    def test_the_flat_hidden_fields_this_client_is_served_reach_its_first_entry(self):
        # Before its first save the client resolves to the pre-profile flat keys, so it IS being
        # served this list; writing an entry without it would silently change what it shows.
        raw = {"hidden_fields": ["Note ID"], "max_fields": 4}

        stored = _save(raw, WEB_CLIPPER, {"max_fields": 9})["clients"][WEB_CLIPPER]

        assert stored["hidden_fields"] == ["Note ID"]

    def test_the_saved_entry_round_trips_back_to_what_it_was_served(self):
        # The whole point, end to end: parse what the save wrote and ask for the profile again.
        raw = {
            "clients": {
                WEB_CLIPPER: {"hidden_fields": ["Note ID"], "match_word_forms": False}
            }
        }

        section = _save(raw, WEB_CLIPPER, {"max_results": 3})
        profile = WordLookupSettings.parse_obj(section).profile_for(WEB_CLIPPER)

        assert profile.hidden_fields == ["Note ID"]
        assert profile.match_word_forms is False
        assert profile.max_results == 3

    def test_it_writes_no_setting_the_profile_model_does_not_carry(self):
        # The seed is the RESOLVED profile, so a fresh client's entry is exactly the seven
        # content settings — never the port or the token, which are not per-client.
        stored = _save({"port": 9999, "token": "s3cret"}, WEB_CLIPPER, {})["clients"]

        assert set(stored[WEB_CLIPPER]) == set(LookupProfile().dict())


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
