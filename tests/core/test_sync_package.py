"""Tests for what one machine asks another to pack, and what that request is allowed to mean.

One property carries the file: **an empty request is refused, never widened.** No decks means
nothing to send; no note types means every one was dropped. Either could be read as "then send
everything", and that reading is how a mis-registered checkbox turns into somebody's whole
collection arriving on the other machine.
"""

from __future__ import annotations

import pytest

from omnia.core.sync.package import PackageError, PackageOffer, PackageRequest


class TestWhatARequestRefusesToMean:
    def test_a_request_for_nothing_is_refused(self):
        with pytest.raises(PackageError, match="nothing was chosen"):
            PackageRequest()

    def test_decks_with_every_note_type_dropped_is_refused(self):
        # Not silently widened to "all note types": the decks would arrive full of notes the user
        # explicitly said they did not want.
        with pytest.raises(PackageError, match="left behind"):
            PackageRequest(decks=("Japanese",), note_types=())

    def test_settings_alone_are_a_legitimate_request(self):
        # Copying only the configuration, with no decks at all, is a thing somebody wants.
        request = PackageRequest(config=("smart_notes",))

        assert request.decks == ()
        assert request.config == ("smart_notes",)

    def test_note_types_alone_are_a_legitimate_request(self):
        # Bringing a kind of card before there is anything to put in it. This used to be refused
        # here, one layer under the picker that had just lit the chip up as chosen — so the
        # headline feature was unreachable from the UI.
        request = PackageRequest(note_types=("Brand New",))

        assert request.note_types == ("Brand New",)
        assert request.wants_cards is False

    def test_a_request_with_decks_wants_cards(self):
        # What the source checks before it builds a search: a card search with no deck in it is
        # not a narrow search, it is the whole collection.
        assert PackageRequest(decks=("A",), note_types=("Basic",)).wants_cards is True
        assert PackageRequest(config=("smart_notes",)).wants_cards is False


class TestWhatTravels:
    def test_media_travels_by_default(self):
        # A card whose audio and images stayed behind is not the card the user picked.
        assert PackageRequest(decks=("A",), note_types=("Basic",)).with_media is True

    def test_review_history_travels_by_default(self):
        assert (
            PackageRequest(decks=("A",), note_types=("Basic",)).with_scheduling is True
        )


class TestTheWire:
    def _request(self) -> PackageRequest:
        return PackageRequest(
            decks=("Japanese", "Japanese::Kanji"),
            note_types=("Basic", "Cloze"),
            config=("smart_notes",),
        )

    def test_a_request_round_trips(self):
        request = self._request()

        assert PackageRequest.from_json(request.to_json()) == request

    def test_a_body_that_is_not_a_request_says_so(self):
        for body in ("<html>", "[]", "null", "12"):
            with pytest.raises(PackageError, match="not a request"):
                PackageRequest.from_json(body)

    def test_duplicates_and_blanks_are_dropped_without_changing_the_order(self):
        # The order is the order the user's decks were listed in, and it survives to the log.
        request = PackageRequest.from_json(
            '{"decks": ["B", "A", "B", ""], "note_types": ["Basic"]}'
        )

        assert request.decks == ("B", "A")

    def test_a_request_that_arrives_empty_is_refused_on_arrival_too(self):
        # The refusal is a property of the request, so it holds at BOTH ends — the source cannot
        # be talked into a wider export by a peer that skipped the check.
        with pytest.raises(PackageError):
            PackageRequest.from_json('{"decks": [], "note_types": []}')


class TestTheOffer:
    def test_it_round_trips(self):
        offer = PackageOffer(id="abc", bytes=742_000_000, cards=1200, notes=900)

        assert PackageOffer.from_json(offer.to_json()) == offer

    def test_a_size_that_arrives_as_nonsense_becomes_zero_rather_than_breaking(self):
        # 0 means "unknown" downstream, which shows a moving bar with no number — better than a
        # transfer that fails because a count was a string.
        offer = PackageOffer.from_json('{"id": "abc", "bytes": "big", "cards": null}')

        assert offer.bytes == 0
        assert offer.cards == 0

    def test_an_answer_with_no_id_is_not_an_offer(self):
        with pytest.raises(PackageError, match="did not answer with a package"):
            PackageOffer.from_json('{"bytes": 10}')

    def test_a_body_that_is_not_json_says_so(self):
        with pytest.raises(PackageError, match="did not answer with a package"):
            PackageOffer.from_json("<html>a login page</html>")
