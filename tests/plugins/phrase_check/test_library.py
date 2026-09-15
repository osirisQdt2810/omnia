"""Putting a saved correction into the collection.

Driven against a fake ``col`` rather than a real one: everything worth checking here is a
DECISION — which note type to write into when the name is taken, whether a second save makes a
second note type, what the user is told afterwards — and a real collection would make those
slower to assert without making them truer.

The one rule that needs no collection at all (:func:`available_note_type_name`) is tested on its
own, because it is the part somebody will want to argue with.
"""

from __future__ import annotations

import pytest

from omnia.plugins.phrase_check import card, library
from omnia.plugins.phrase_check.correction import WRITTEN, parse


class _Models:
    def __init__(self):
        self.models: dict = {}

    def by_name(self, name):
        return self.models.get(name)

    def new(self, name):
        return {"name": name, "flds": [], "tmpls": [], "css": ""}

    def new_field(self, name):
        return {"name": name}

    def add_field(self, model, field):
        model["flds"].append(field)

    def new_template(self, name):
        return {"name": name}

    def add_template(self, model, template):
        model["tmpls"].append(template)

    def add(self, model):
        self.models[model["name"]] = model


class _Decks:
    def __init__(self):
        self.by_id: dict = {}
        self._next = 1

    def id(self, name):
        for did, existing in self.by_id.items():
            if existing == name:
                return did
        self._next += 1
        self.by_id[self._next] = name
        return self._next

    def get(self, did):
        name = self.by_id.get(did)
        return {"name": name} if name else None


class _Note(dict):
    id = 0


class _Col:
    def __init__(self):
        self.models = _Models()
        self.decks = _Decks()
        self.added: list = []
        self.notes: dict = {}
        self.searches: list = []
        self._next_id = 4242

    def new_note(self, _model):
        return _Note()

    def add_note(self, note, deck_id):
        note.id = self._next_id
        self._next_id += 1
        self.notes[note.id] = note
        self.added.append((dict(note), deck_id))

    def find_notes(self, query):
        """Every note, whatever was asked for.

        Deliberately not a query engine. `find_existing` treats the search as a FILTER and
        confirms every hit field-by-field, because Anki's grammar treats `*` and `_` as
        wildcards even inside quotes and a phrase is arbitrary user text. A fake that
        over-matches is the honest stand-in for that: it exercises the comparison that is
        actually load-bearing, where a fake that re-implemented the escaping would only
        test my reading of it twice.
        """
        self.searches.append(query)
        return list(self.notes)

    def get_note(self, nid):
        return self.notes[nid]


def _correction(original="I have went to the shop.", rewritten="I went to the shop."):
    return parse(
        {
            "rewritten": rewritten,
            "fixes": [
                {
                    "before": "have went",
                    "after": "went",
                    "why": "Simple past.",
                    "kind": "grammar",
                }
            ],
        },
        original=original,
        mode=WRITTEN,
    )


def _foreign(name):
    """Somebody else's note type, squatting on the name Omnia wants."""
    return {"name": name, "flds": [{"name": "Front"}, {"name": "Back"}], "tmpls": []}


class TestPickingTheName:
    """Pure: the only questions it asks a collection are the two callables it is handed."""

    def test_a_free_name_is_used_as_is(self):
        assert library.available_note_type_name(
            "Wanted", exists=lambda n: False, ours=lambda n: False
        ) == ("Wanted", False)

    def test_one_of_ours_is_reused_even_though_it_exists(self):
        # A note type Omnia made and the user renamed is still the right home.
        assert library.available_note_type_name(
            "Wanted", exists=lambda n: True, ours=lambda n: True
        ) == ("Wanted", False)

    def test_somebody_elses_is_never_written_into(self):
        # Their note type has their fields; a note added to it lands with the phrase in their
        # "Front" and the rest nowhere.
        taken = {"Wanted"}

        name, renamed = library.available_note_type_name(
            "Wanted", exists=lambda n: n in taken, ours=lambda n: False
        )

        assert (name, renamed) == ("Wanted (copy)", True)

    def test_it_keeps_counting_past_the_first_copy(self):
        taken = {"Wanted", "Wanted (copy)", "Wanted (copy 2)"}

        name, _ = library.available_note_type_name(
            "Wanted", exists=lambda n: n in taken, ours=lambda n: False
        )

        assert name == "Wanted (copy 3)"

    def test_a_copy_of_ours_is_reused_rather_than_copied_again(self):
        # Otherwise every save makes one more note type, for ever.
        taken = {"Wanted", "Wanted (copy)"}

        name, renamed = library.available_note_type_name(
            "Wanted",
            exists=lambda n: n in taken,
            ours=lambda n: n == "Wanted (copy)",
        )

        assert (name, renamed) == ("Wanted (copy)", True)

    def test_it_gives_up_rather_than_looping(self):
        with pytest.raises(library.SaveError, match="taken"):
            library.available_note_type_name(
                "Wanted", exists=lambda n: True, ours=lambda n: False
            )


class TestWhatIsOurs:
    def test_a_note_type_with_our_fields_is(self):
        assert library.is_ours({"flds": [{"name": f} for f in card.FIELDS]})

    def test_extra_fields_do_not_disqualify_it(self):
        # Somebody added a field of their own to their own note type. Still ours to write into.
        fields = [{"name": f} for f in card.FIELDS] + [{"name": "Notes"}]

        assert library.is_ours({"flds": fields})

    def test_a_missing_field_does(self):
        fields = [{"name": f} for f in card.FIELDS[:-1]]

        assert not library.is_ours({"flds": fields})

    def test_something_that_is_not_a_note_type_is_not(self):
        assert not library.is_ours(None)
        assert not library.is_ours({})
        assert not library.is_ours({"flds": "nonsense"})

    def test_the_name_has_nothing_to_do_with_it(self):
        # It is matched by its fields on purpose: the name is the user's to change.
        assert library.is_ours(
            {
                "name": "Something Else Entirely",
                "flds": [{"name": f} for f in card.FIELDS],
            }
        )


class TestSavingOne:
    def test_it_builds_the_note_type_on_the_first_save(self):
        col = _Col()

        library.save_correction(col, _correction())

        model = col.models.models[card.NOTE_TYPE_NAME]
        assert [f["name"] for f in model["flds"]] == list(card.FIELDS)
        assert [t["name"] for t in model["tmpls"]] == [card.CARD_NAME]
        assert model["css"] == card.card_css()

    def test_the_template_is_the_one_from_the_card_module(self):
        col = _Col()

        library.save_correction(col, _correction())

        template = col.models.models[card.NOTE_TYPE_NAME]["tmpls"][0]
        assert template["qfmt"] == card.front_template()
        assert template["afmt"] == card.back_template()

    def test_the_note_carries_every_field(self):
        col = _Col()

        library.save_correction(col, _correction())

        fields, _deck = col.added[0]
        assert set(fields) == set(card.FIELDS)
        assert fields[card.FIELD_PHRASE] == "I have went to the shop."

    def test_a_second_save_reuses_the_note_type(self):
        # Two DIFFERENT phrases: saving the same one twice is deduplicated now, which would
        # make this pass for the wrong reason (one note, one note type, nothing reused).
        col = _Col()

        library.save_correction(col, _correction())
        library.save_correction(
            col, _correction(original="I has a cat.", rewritten="I have a cat.")
        )

        assert list(col.models.models) == [card.NOTE_TYPE_NAME]
        assert len(col.added) == 2

    def test_the_deck_is_created_and_named_back(self):
        col = _Col()

        saved = library.save_correction(col, _correction(), deck="Study::Corrections")

        assert saved.deck == "Study::Corrections"
        assert col.added[0][1] == col.decks.id("Study::Corrections")

    def test_an_empty_deck_name_falls_back_rather_than_failing(self):
        col = _Col()

        saved = library.save_correction(col, _correction(), deck="   ")

        assert saved.deck == library.DEFAULT_DECK

    def test_a_foreign_note_type_is_left_alone(self):
        # The one that would be a disaster: writing our fields into somebody's own note type.
        col = _Col()
        col.models.models[card.NOTE_TYPE_NAME] = _foreign(card.NOTE_TYPE_NAME)

        saved = library.save_correction(col, _correction())

        assert saved.note_type == f"{card.NOTE_TYPE_NAME} (copy)"
        assert saved.renamed is True
        assert [f["name"] for f in col.models.models[card.NOTE_TYPE_NAME]["flds"]] == [
            "Front",
            "Back",
        ]

    def test_a_rename_is_said_out_loud(self):
        # Silently writing into a name the user did not choose is how a note ends up somewhere
        # they will never look for it.
        col = _Col()
        col.models.models[card.NOTE_TYPE_NAME] = _foreign(card.NOTE_TYPE_NAME)

        saved = library.save_correction(col, _correction())

        assert "was used" in saved.summary()
        assert card.NOTE_TYPE_NAME in saved.summary()

    def test_an_ordinary_save_says_only_where_it_went(self):
        saved = library.save_correction(_Col(), _correction(), deck="Decky")

        assert saved.summary() == "Saved to Decky."

    def test_nothing_to_save_is_refused_before_the_collection_is_touched(self):
        col = _Col()

        with pytest.raises(library.SaveError, match="nothing to save"):
            library.save_correction(col, _correction(original="   "))

        assert col.added == []
        assert col.models.models == {}

    def test_a_collection_that_refuses_the_note_says_so_usefully(self):
        class _Refuses(_Col):
            def add_note(self, note, deck_id):
                raise RuntimeError("note type is missing a field")

        with pytest.raises(library.SaveError, match="note type is missing a field"):
            library.save_correction(_Refuses(), _correction())

    def test_the_note_id_comes_back(self):
        # The clipper does nothing with it today, but "which note" is the first thing anyone
        # asks when a save goes somewhere unexpected.
        assert library.save_correction(_Col(), _correction()).note_id == 4242


class TestSavingTheSamePhraseTwice:
    """One press can reach `save_correction` twice, so it has to be idempotent.

    The HTTP hop gives up after five seconds and answers "nothing was saved" while the queued
    write still runs — so the retry that message invites is a second identical note. The ordinary
    "pressed Save twice" case does the same thing more simply.
    """

    def test_the_second_save_adds_nothing(self):
        col = _Col()
        library.save_correction(col, _correction())

        library.save_correction(col, _correction())

        assert len(col.added) == 1

    def test_it_hands_back_the_note_that_is_already_there(self):
        col = _Col()
        first = library.save_correction(col, _correction())

        second = library.save_correction(col, _correction())

        assert second.note_id == first.note_id

    def test_it_says_so_rather_than_claiming_a_fresh_save(self):
        # "Saved" over a press that added nothing sends the user hunting for a new card.
        col = _Col()
        library.save_correction(col, _correction())

        second = library.save_correction(col, _correction())

        assert second.already_there is True
        assert "already" in second.summary().lower()

    def test_the_other_register_is_a_different_card(self):
        """The same sentence judged as spoken and as written is two answers worth keeping.

        The panel treats switching register as a new question for exactly this reason;
        deduplicating on the phrase alone would refuse the second and hand back the first, which
        is the wrong card.
        """
        from omnia.plugins.phrase_check.correction import SPOKEN

        col = _Col()
        library.save_correction(col, _correction())

        spoken = parse(
            {
                "rewritten": "I went to the shop.",
                "fixes": [{"before": "have went", "after": "went"}],
            },
            original="I have went to the shop.",
            mode=SPOKEN,
        )
        library.save_correction(col, spoken)

        assert len(col.added) == 2

    def test_a_different_phrase_is_still_saved(self):
        col = _Col()
        library.save_correction(col, _correction())

        library.save_correction(
            col, _correction(original="I has a cat.", rewritten="I have a cat.")
        )

        assert len(col.added) == 2

    def test_a_search_the_collection_refuses_does_not_block_the_save(self):
        # Falling through adds the note, which is the behaviour without the check at all. A
        # query this cannot express must never cost the user their card.
        class _Refuses(_Col):
            def find_notes(self, query):
                raise RuntimeError("unparseable search")

        col = _Refuses()

        saved = library.save_correction(col, _correction())

        assert len(col.added) == 1 and saved.note_id

    def test_a_note_deleted_between_the_search_and_the_read_is_skipped(self):
        class _Vanishes(_Col):
            def get_note(self, nid):
                raise KeyError(nid)

        col = _Vanishes()
        library.save_correction(col, _correction())

        library.save_correction(col, _correction())

        assert (
            len(col.added) == 2
        ), "a missing note stopped the save instead of being skipped"

    def test_the_search_is_scoped_to_our_note_type(self):
        # Otherwise a phrase saved by something else's note type would be mistaken for ours.
        col = _Col()

        library.save_correction(col, _correction())

        assert card.NOTE_TYPE_NAME in col.searches[0]
        assert card.FIELD_PHRASE in col.searches[0]


class TestEscapingAPhraseForTheSearch:
    """Anki treats `*` and `_` as wildcards inside quotes, and a phrase is arbitrary text."""

    def test_a_quote_cannot_break_out_of_the_term(self):
        assert '\\"' in library._search_value('he said "hi"')

    def test_wildcards_are_escaped(self):
        escaped = library._search_value("a * and an _")

        assert "\\*" in escaped and "\\_" in escaped

    def test_a_phrase_containing_a_wildcard_still_deduplicates(self):
        # The whole point of escaping it: over-matching is harmless (every hit is confirmed),
        # under-matching costs a duplicate.
        col = _Col()
        library.save_correction(
            col, _correction(original="2 * 3 is 6", rewritten="2 × 3 is 6")
        )

        library.save_correction(
            col, _correction(original="2 * 3 is 6", rewritten="2 × 3 is 6")
        )

        assert len(col.added) == 1
