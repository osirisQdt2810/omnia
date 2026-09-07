"""Tests for the per-clipper lookup settings and their upgrade path.

The interesting case is not the shape of the model, it is the UPGRADE: every release before
per-client profiles wrote the seven content settings as flat top-level keys, and a user who has
tuned them (searchable note types, the per-note-type field lists) must not find them reset — nor
silently deleted from the config a second device is still reading (ADR-010).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omnia.core import anki_compat
from omnia.plugins.word_lookup import WordLookupPlugin
from omnia.plugins.word_lookup.config import (
    CLIENT_KEYS,
    DESKTOP_CLIPPER,
    WEB_CLIPPER,
    LookupProfile,
    WordLookupSettings,
)

# A real ``[word_lookup]`` section as written by the versions before per-client profiles.
_PRE_UPGRADE = {
    "note_types": ["Vocabulary"],
    "max_results": 3,
    "max_fields": 12,
    "search_fields": {"Vocabulary": ["Word"]},
    "display_fields": {"Vocabulary": ["Definition", "Meaning (vi)"]},
    "match_word_forms": False,
    "hidden_fields": ["Note ID"],
    "port": 8899,
}


class TestUpgradingFromFlatSettings:
    def test_both_clippers_inherit_the_settings_the_user_had(self):
        settings = WordLookupSettings.parse_obj(_PRE_UPGRADE)

        for client in CLIENT_KEYS:
            profile = settings.profile_for(client)
            assert profile.note_types == ["Vocabulary"], client
            assert profile.max_results == 3, client
            assert profile.max_fields == 12, client
            assert profile.search_fields == {"Vocabulary": ["Word"]}, client
            assert profile.display_fields == {
                "Vocabulary": ["Definition", "Meaning (vi)"]
            }, client
            assert profile.match_word_forms is False, client
            assert profile.hidden_fields == ["Note ID"], client

    def test_the_port_stays_a_single_top_level_setting(self):
        assert WordLookupSettings.parse_obj(_PRE_UPGRADE).port == 8899

    def test_the_legacy_keys_are_written_back_untouched(self):
        """``extra = "allow"`` is what makes this an upgrade rather than a data loss.

        The same config is read and rewritten by every synced device, so a build that dropped
        the keys it no longer declares would delete an older device's settings on the next save.
        """
        written = WordLookupSettings.parse_obj(_PRE_UPGRADE).dict()

        for key, value in _PRE_UPGRADE.items():
            assert written[key] == value, key

    def test_a_key_only_a_newer_version_knows_also_survives(self):
        stored = dict(_PRE_UPGRADE, something_from_the_future={"a": 1})

        assert WordLookupSettings.parse_obj(stored).dict()[
            "something_from_the_future"
        ] == {"a": 1}

    def test_an_unusable_legacy_value_falls_back_to_the_defaults(self):
        """A hand-edited out-of-range value must not take the whole lookup down."""
        stored = dict(_PRE_UPGRADE, max_results=999)

        profile = WordLookupSettings.parse_obj(stored).profile_for(WEB_CLIPPER)

        assert profile == LookupProfile()


class TestProfileResolution:
    def test_a_client_with_a_profile_gets_it(self):
        settings = WordLookupSettings.parse_obj(
            {"clients": {WEB_CLIPPER: {"max_results": 2, "note_types": ["Web"]}}}
        )

        profile = settings.profile_for(WEB_CLIPPER)

        assert (profile.max_results, profile.note_types) == (2, ["Web"])

    def test_keys_the_profile_omits_keep_their_defaults(self):
        settings = WordLookupSettings.parse_obj(
            {"clients": {WEB_CLIPPER: {"max_results": 2}}}
        )

        assert (
            settings.profile_for(WEB_CLIPPER).max_fields == LookupProfile().max_fields
        )

    def test_the_other_client_still_falls_back_to_the_legacy_values(self):
        settings = WordLookupSettings.parse_obj(
            dict(_PRE_UPGRADE, clients={WEB_CLIPPER: {"max_results": 2}})
        )

        assert settings.profile_for(WEB_CLIPPER).max_results == 2
        assert settings.profile_for(DESKTOP_CLIPPER).max_results == 3

    def test_an_unknown_client_resolves_instead_of_raising(self):
        settings = WordLookupSettings.parse_obj(_PRE_UPGRADE)

        assert settings.profile_for("some_future_clipper").max_results == 3

    def test_no_client_at_all_resolves_too(self):
        """Older clipper builds send no ``client=``; they must be served exactly as before."""
        settings = WordLookupSettings.parse_obj(_PRE_UPGRADE)

        assert settings.profile_for("") == settings.profile_for(DESKTOP_CLIPPER)

    def test_a_fresh_install_lands_on_the_defaults(self):
        settings = WordLookupSettings()

        assert settings.profile_for(WEB_CLIPPER) == LookupProfile()
        assert settings.profile_for("") == LookupProfile()

    def test_whitespace_around_a_client_name_is_ignored(self):
        settings = WordLookupSettings.parse_obj(
            {"clients": {WEB_CLIPPER: {"max_results": 2}}}
        )

        assert settings.profile_for(f"  {WEB_CLIPPER} ").max_results == 2


class TestTheClipperToken:
    def test_it_starts_empty_so_the_write_path_starts_shut(self):
        assert WordLookupSettings().token == ""

    def test_it_round_trips(self):
        stored = WordLookupSettings.parse_obj({"token": "abc"}).dict()

        assert stored["token"] == "abc"


class _FakeNote:
    """Only what a lookup reads: the fields in note-type order, and the type's name."""

    tags: list[str] = []

    def items(self) -> list[tuple[str, str]]:
        return [("Word", "plunge"), ("Definition", "to dive"), ("Example", "he dove")]

    def note_type(self) -> dict[str, str]:
        return {"name": "Vocabulary"}

    def cards(self) -> list:
        return []


class TestALookupIsServedItsClientsProfile:
    """The end of the chain: a request's ``client`` decides what comes back."""

    @pytest.fixture
    def plugin(self, monkeypatch):
        monkeypatch.setattr(anki_compat, "find_note_ids", lambda query: [1])
        monkeypatch.setattr(anki_compat, "get_note", lambda nid: _FakeNote())
        instance = WordLookupPlugin()
        instance._ctx = SimpleNamespace(
            settings=WordLookupSettings.parse_obj(
                {
                    "clients": {
                        WEB_CLIPPER: {
                            "display_fields": {"Vocabulary": ["Definition"]},
                        },
                        DESKTOP_CLIPPER: {"hidden_fields": ["Example"]},
                    }
                }
            )
        )
        return instance

    def test_each_client_sees_its_own_fields(self, plugin):
        web = plugin.lookup("plunge", WEB_CLIPPER)["cards"][0]["fields"]
        desktop = plugin.lookup("plunge", DESKTOP_CLIPPER)["cards"][0]["fields"]

        assert [f["name"] for f in web] == ["Definition"]
        assert [f["name"] for f in desktop] == ["Definition"]  # Example is hidden

    def test_an_unconfigured_client_gets_the_fallback(self, plugin):
        fields = plugin.lookup("plunge", "some_future_clipper")["cards"][0]["fields"]

        assert [f["name"] for f in fields] == ["Definition", "Example"]
