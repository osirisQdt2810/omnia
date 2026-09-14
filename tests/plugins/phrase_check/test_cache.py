"""Tests for remembering a correction.

The panel is transient by design — click outside and it is gone — so without this, coming back to
the same phrase costs another request, another wait, and another few cents. Worse, it can come
back DIFFERENT, because models are not deterministic; a second look that disagrees with the first
reads as the tool being unreliable rather than as sampling.

So the property under test is that a stored answer is returned for **exactly** the question that
produced it, and for no other.
"""

from __future__ import annotations

import pytest

from omnia.plugins.phrase_check.cache import (
    CacheKey,
    CorrectionCache,
    section_store,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def cache(clock):
    store: dict = {}

    def read() -> dict:
        return store

    def write(new: dict) -> None:
        store.clear()
        store.update(new)

    return CorrectionCache(read, write, clock=clock, max_entries=3, max_age=100.0)


def _key(text="I have went.", mode="written", language="en") -> CacheKey:
    return CacheKey(text=text, mode=mode, language=language)


class TestRememberingAnAnswer:
    def test_what_went_in_comes_back(self, cache):
        cache.put(_key(), {"rewritten": "I have gone."})

        assert cache.get(_key()) == {"rewritten": "I have gone."}

    def test_a_question_never_asked_is_absent(self, cache):
        assert cache.get(_key("something else")) is None

    def test_forgetting_one_leaves_the_others(self, cache):
        cache.put(_key("a"), {"rewritten": "A"})
        cache.put(_key("b"), {"rewritten": "B"})

        cache.forget(_key("a"))

        assert cache.get(_key("a")) is None
        assert cache.get(_key("b")) == {"rewritten": "B"}

    def test_clearing_drops_everything(self, cache):
        cache.put(_key("a"), {"rewritten": "A"})
        cache.put(_key("b"), {"rewritten": "B"})

        cache.clear()

        assert len(cache) == 0


class TestWhatCountsAsTheSameQuestion:
    def test_the_mode_is_part_of_it(self):
        # The same sentence corrected for speech is not the one corrected for writing.
        assert _key(mode="spoken").digest() != _key(mode="written").digest()

    def test_the_language_is_part_of_it(self):
        # An explanation in English is not one in Vietnamese.
        assert _key(language="en").digest() != _key(language="vi").digest()

    def test_whitespace_is_not(self, cache):
        # Selecting a phrase from a web page picks up ragged spacing and newlines; asking twice
        # because of them is paying twice for one answer.
        cache.put(_key("I  have\n went."), {"rewritten": "ok"})

        assert cache.get(_key("I have went.")) == {"rewritten": "ok"}

    def test_case_IS_part_of_it(self, cache):
        # Capitalisation is one of the things being corrected, so "i went" and "I went" are
        # genuinely different questions with different right answers.
        cache.put(_key("i went."), {"rewritten": "I went."})

        assert cache.get(_key("I went.")) is None

    def test_punctuation_is_part_of_it_too(self, cache):
        cache.put(_key("I went"), {"rewritten": "x"})

        assert cache.get(_key("I went.")) is None

    def test_the_digest_is_short_enough_to_read(self):
        # It is a key in a stored map; a five-hundred-character one makes that unreadable.
        assert len(_key("a" * 5000).digest()) == 32


class TestItDoesNotGrowForEver:
    def test_the_oldest_go_first(self, cache, clock):
        for name in ("a", "b", "c"):
            cache.put(_key(name), {"rewritten": name})
            clock.tick(1)

        cache.put(_key("d"), {"rewritten": "d"})

        assert cache.get(_key("a")) is None
        assert cache.get(_key("d")) == {"rewritten": "d"}
        assert len(cache) == 3

    def test_an_old_answer_is_re_asked_rather_than_shown(self, cache, clock):
        # Advice a month old came from a model and a prompt that may both have changed. A stale
        # correction that still renders is one the user acts on.
        cache.put(_key(), {"rewritten": "old"})

        clock.tick(101)

        assert cache.get(_key()) is None

    def test_an_expired_answer_is_actually_removed_not_just_hidden(self, cache, clock):
        cache.put(_key(), {"rewritten": "old"})
        clock.tick(101)

        cache.get(_key())

        assert len(cache) == 0

    def test_an_entry_with_an_unreadable_timestamp_is_not_trusted(self, clock):
        store = {"whatever": {"at": "the day before", "payload": {"rewritten": "x"}}}
        cache = CorrectionCache(
            lambda: store, lambda new: store.clear() or store.update(new), clock=clock
        )

        assert cache.get(CacheKey("x", "written", "en")) is None


class TestWhenTheStoreMisbehaves:
    def test_a_cache_that_cannot_be_read_does_not_stop_a_correction(self, clock):
        def read():
            raise RuntimeError("the config is a directory")

        cache = CorrectionCache(read, lambda _: None, clock=clock)

        assert cache.get(_key()) is None  # not an exception

    def test_a_cache_that_cannot_be_written_does_not_either(self, clock):
        def write(_store):
            raise RuntimeError("read-only filesystem")

        cache = CorrectionCache(dict, write, clock=clock)

        cache.put(_key(), {"rewritten": "x"})  # must not raise

    def test_a_store_holding_something_that_is_not_a_map_is_ignored(self, clock):
        cache = CorrectionCache(
            lambda: ["not", "a", "map"], lambda _: None, clock=clock
        )

        assert cache.get(_key()) is None

    def test_an_entry_that_is_not_a_correction_is_ignored(self, clock):
        store = {_key().digest(): "a string, somehow"}
        cache = CorrectionCache(lambda: store, lambda _: None, clock=clock)

        assert cache.get(_key()) is None


class TestTheConfigBackedStore:
    class _Repo:
        def __init__(self) -> None:
            self.sections: dict = {}

        def raw_section(self, name: str) -> dict:
            return dict(self.sections.get(name, {}))

        def update_section(self, name: str, values: dict) -> None:
            self.sections.setdefault(name, {}).update(values)

    def test_it_round_trips_through_the_config(self, clock):
        repo = self._Repo()
        read, write = section_store(repo, "phrase_check")
        cache = CorrectionCache(read, write, clock=clock)

        cache.put(_key(), {"rewritten": "I have gone."})

        assert CorrectionCache(read, write, clock=clock).get(_key()) == {
            "rewritten": "I have gone."
        }

    def test_it_is_stored_as_one_string_not_five_hundred_sections(self, clock):
        repo = self._Repo()
        read, write = section_store(repo, "phrase_check")
        CorrectionCache(read, write, clock=clock).put(_key(), {"rewritten": "x"})

        assert isinstance(repo.sections["phrase_check"]["corrections"], str)

    def test_a_corrupt_stored_string_reads_as_empty_rather_than_raising(self, clock):
        repo = self._Repo()
        repo.sections["phrase_check"] = {"corrections": "{not json"}
        read, _write = section_store(repo, "phrase_check")

        assert read() == {}
