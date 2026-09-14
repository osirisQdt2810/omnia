"""Tests for what leaves the source machine.

This module had none, and all three of the defects a review found lived here or in what it
assumed. The worst of them: a request naming only settings reached ``find_cards("")``, which Anki
reads as the WHOLE COLLECTION — so pressing Copy with no decks chosen would have packed every card
and every media file the source owns and imported them on the other machine.

So the property under test is not "the right cards come out". It is **that nothing widens**: a
search is never built without a deck in it, and what is exported is only ever what was named.

``col`` is faked, because the question is which searches are constructed and what is handed to the
exporter — not whether Anki can find cards.
"""

from __future__ import annotations

import os
import sys
import types
from dataclasses import dataclass, field

import pytest
from aqt_stubs import install_gui_stubs

install_gui_stubs()


def _install_export_stubs() -> None:
    """The three ``anki.collection`` names the packager imports.

    Stubbed here rather than in the shared GUI stubs because only this module needs them, and
    because they are the things under test: a real ``SearchNode`` would escape a deck name and
    hide whether one was built at all, which is the property this file exists to check.
    """
    collection = sys.modules.get("anki.collection") or types.ModuleType(
        "anki.collection"
    )

    @dataclass
    class SearchNode:
        deck: str = ""
        note: str = ""

        def __str__(self) -> str:
            return f"deck={self.deck}" if self.deck else f"note={self.note}"

    @dataclass
    class CardIdsLimit:
        card_ids: list = field(default_factory=list)

    @dataclass
    class ExportAnkiPackageOptions:
        with_scheduling: bool = True
        with_deck_configs: bool = True
        with_media: bool = True
        legacy: bool = False

    for name, value in (
        ("SearchNode", SearchNode),
        ("CardIdsLimit", CardIdsLimit),
        ("ExportAnkiPackageOptions", ExportAnkiPackageOptions),
    ):
        setattr(collection, name, value)
    sys.modules["anki.collection"] = collection
    anki = sys.modules.get("anki")
    if anki is not None:
        anki.collection = collection


_install_export_stubs()

from omnia.core.sync.package import PackageError, PackageRequest  # noqa: E402
from omnia.gui.sync import export as export_module  # noqa: E402


class _Col:
    """Just enough collection to record what was asked of it."""

    def __init__(self, *, cards=(1, 2, 3), models=None) -> None:
        self.cards = list(cards)
        self.models = _Models(models or {})
        self.searches: list[str] = []
        self.exported: list[dict] = []

    # -- search building: the real Anki escapes here; the fake records the shape ------------
    def group_searches(self, *nodes, joiner="AND"):
        return f"({f' {joiner} '.join(str(n) for n in nodes)})"

    def join_searches(self, left, right, joiner):
        return f"{left} {joiner} {right}"

    def find_cards(self, search):
        self.searches.append(search)
        return list(self.cards)

    def export_anki_package(self, *, out_path, options, limit):
        self.exported.append({"path": out_path, "options": options, "limit": limit})
        with open(out_path, "wb") as handle:
            handle.write(b"apkg-bytes")
        return len(self.cards)


class _Models:
    def __init__(self, models: dict) -> None:
        self._models = models

    def by_name(self, name):
        return self._models.get(name)


class _Repo:
    def __init__(self, sections: dict | None = None) -> None:
        self.sections = sections or {}

    def raw_section(self, name: str) -> dict:
        return dict(self.sections.get(name, {}))


@pytest.fixture
def col(monkeypatch):
    collection = _Col()
    monkeypatch.setattr(export_module, "_collection", lambda: collection)
    return collection


def _built(col) -> tuple[str, object]:
    return export_module.build_package(
        PackageRequest(decks=("Japanese",), note_types=("Basic",)), _Repo()
    )


class TestNothingWidens:
    def test_a_settings_only_request_never_searches_for_cards(self, col):
        # The defect this file exists for. An empty search is not a narrow search — Anki reads it
        # as the whole collection — so the guard is "never build one", not "build one carefully".
        path, offer = export_module.build_package(
            PackageRequest(config=("smart_notes",)),
            _Repo({"smart_notes": {"note_types": []}}),
        )

        assert col.searches == [], f"a card search was built: {col.searches}"
        assert col.exported == [], "the whole collection was exported"
        assert path == "", "a file was written for a request that named no cards"
        assert offer.bytes == 0

    def test_a_note_type_only_request_never_searches_for_cards_either(self, col):
        col.models = _Models({"Brand New": {"name": "Brand New", "id": 99}})

        path, _ = export_module.build_package(
            PackageRequest(note_types=("Brand New",)), _Repo()
        )

        assert col.searches == []
        assert col.exported == []
        assert path == ""

    def test_the_search_always_names_the_decks_and_the_note_types(self, col):
        _built(col)

        assert len(col.searches) == 1
        assert "deck=Japanese" in col.searches[0].replace("'", "")
        assert "note=Basic" in col.searches[0].replace("'", "")

    def test_the_card_search_refuses_an_empty_deck_list_on_its_own(self, col):
        # Tested directly, because build_package returns early for a deckless request and that
        # early return MASKS this guard: removing it left every other test green. Two layers
        # protect the same thing, and each needs a test that can see it fail.
        request = PackageRequest(note_types=("Basic",))

        assert export_module._card_ids(col, request) == []
        assert col.searches == [], f"a search was built: {col.searches}"

    def test_the_card_search_refuses_an_empty_note_type_list_too(self, col):
        # Unreachable through PackageRequest, which refuses decks with no note types — guarded
        # anyway, because the cost of being wrong is every card on the machine.
        request = PackageRequest.__new__(PackageRequest)
        object.__setattr__(request, "decks", ("Japanese",))
        object.__setattr__(request, "note_types", ())

        assert export_module._card_ids(col, request) == []
        assert col.searches == []


class TestWhatComesOut:
    def test_the_cards_are_exported_with_their_media(self, col, tmp_path):
        # A card whose audio and images stayed behind is not the card the user picked.
        path, offer = _built(col)
        try:
            assert col.exported[0]["options"].with_media is True
            assert offer.cards == 3
            assert offer.bytes > 0
            assert offer.has_package
        finally:
            os.path.exists(path) and os.unlink(path)

    def test_the_export_is_limited_to_the_cards_that_were_found(self, col, tmp_path):
        path, _ = _built(col)
        try:
            assert list(col.exported[0]["limit"].card_ids) == [1, 2, 3]
        finally:
            os.path.exists(path) and os.unlink(path)

    def test_a_selection_that_matches_nothing_is_refused_rather_than_exported(
        self, col
    ):
        # A zero-card package arriving after a long wait looks exactly like a bug; the real
        # problem is a choice that no longer matches this collection.
        col.cards = []

        with pytest.raises(PackageError, match="renamed or emptied"):
            _built(col)

        assert col.exported == []


class TestWhatCannotRideInsideAPackage:
    def test_a_note_type_chosen_on_its_own_travels_as_a_definition(self, col):
        # Anki gathers note types from the notes it exports, so one with no cards contributes
        # nothing to an .apkg at all. It has to travel as data or it does not travel.
        col.models = _Models({"Brand New": {"name": "Brand New", "id": 99}})

        _path, offer = export_module.build_package(
            PackageRequest(note_types=("Brand New",)), _Repo()
        )

        assert [entry["name"] for entry in offer.note_types] == ["Brand New"]

    def test_a_note_type_chosen_outright_travels_even_when_decks_were_also_picked(
        self, col, tmp_path
    ):
        # The defect: with decks named, no definition was sent at all, and the card search
        # (deck AND note-type) yields nothing for a note type no card uses — so a chip the user
        # had lit as chosen arrived as nothing, while the tally counted it and the pull reported
        # success.
        col.models = _Models({"Unused": {"name": "Unused", "id": 7}})

        path, offer = export_module.build_package(
            PackageRequest(
                decks=("Japanese",),
                note_types=("Basic", "Unused"),
                definitions=("Unused",),
            ),
            _Repo(),
        )
        try:
            assert [entry["name"] for entry in offer.note_types] == ["Unused"]
        finally:
            os.path.exists(path) and os.unlink(path)

    def test_note_types_are_not_duplicated_when_decks_carry_them(self, col, tmp_path):
        # With decks named, the package already holds whatever its notes use.
        col.models = _Models({"Basic": {"name": "Basic"}})

        path, offer = _built(col)
        try:
            assert offer.note_types == ()
        finally:
            os.path.exists(path) and os.unlink(path)

    def test_settings_travel_in_the_answer(self, col):
        values = {"note_types": [{"note_type": "Basic"}]}

        _path, offer = export_module.build_package(
            PackageRequest(config=("smart_notes",)), _Repo({"smart_notes": values})
        )

        assert offer.config == {"smart_notes": values}

    @pytest.mark.parametrize("section", ["llm", "tts", "sync", "plugins"])
    def test_credentials_and_identity_never_leave_however_they_are_asked_for(
        self, col, section
    ):
        # Withheld at the SOURCE, not only refused on arrival: a peer that asks for the llm
        # section must not be sent one to refuse.
        with pytest.raises(PackageError, match="nothing on this machine matches"):
            export_module.build_package(
                PackageRequest(config=(section,)),
                _Repo({section: {"api_key": "sk-secret"}}),
            )

    def test_a_section_that_is_empty_here_is_not_sent_as_empty(self, col):
        # Sending {} would overwrite a section the other machine has filled in.
        with pytest.raises(PackageError, match="nothing on this machine matches"):
            export_module.build_package(
                PackageRequest(config=("smart_notes",)), _Repo({})
            )
