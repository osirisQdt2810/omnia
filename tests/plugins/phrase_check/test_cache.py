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
    file_store,
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


class TestWhatCountsAsTheSameQuestion:
    def test_the_mode_is_part_of_it(self):
        # The same sentence corrected for speech is not the one corrected for writing.
        assert _key(mode="spoken").digest() != _key(mode="written").digest()

    def test_the_language_is_part_of_it(self):
        # An explanation in English is not one in Vietnamese.
        assert _key(language="en").digest() != _key(language="vi").digest()

    def test_the_pinned_model_is_part_of_it(self):
        # A stronger model is pinned precisely when the current answers are not good enough.
        # Leaving it out of the key means every phrase already asked keeps returning the cheap
        # model's answer for up to a month, with nothing on screen to say why.
        assert CacheKey(text="x", mode="written", language="en", model="").digest() != (
            CacheKey(text="x", mode="written", language="en", model="gpt-5").digest()
        )

    def test_two_pinned_models_are_two_questions(self):
        a = CacheKey(text="x", mode="written", language="en", model="haiku")
        b = CacheKey(text="x", mode="written", language="en", model="opus")

        assert a.digest() != b.digest()

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


class TestTheFileBackedStore:
    """A file under ``user_files/`` — deliberately NOT the collection config.

    A check runs on the HTTP worker thread, where ``col.db`` must not be written; the config
    blob is also rewritten by the settings dialog, so a check landing mid-save would put the old
    table back over it; and the feature domain SYNCS, so five hundred cached corrections would
    ride to AnkiWeb and mark the collection modified on every check. A cache is per-machine
    scratch and belongs in a per-machine file.
    """

    def test_it_round_trips_through_the_file(self, clock, tmp_path):
        read, write = file_store(tmp_path / "cache.json")
        CorrectionCache(read, write, clock=clock).put(
            _key(), {"rewritten": "I have gone."}
        )

        assert CorrectionCache(read, write, clock=clock).get(_key()) == {
            "rewritten": "I have gone."
        }

    def test_a_store_that_was_never_written_reads_as_empty(self, clock, tmp_path):
        read, _write = file_store(tmp_path / "nothing-here.json")

        assert read() == {}

    def test_it_makes_the_directory_it_was_pointed_at(self, clock, tmp_path):
        # user_files/ exists, but nothing guarantees a subdirectory does, and a cache that
        # refused to write until someone made a folder would silently never cache anything.
        path = tmp_path / "deeper" / "still" / "cache.json"
        read, write = file_store(path)
        CorrectionCache(read, write, clock=clock).put(_key(), {"rewritten": "x"})

        assert path.is_file()

    def test_a_corrupt_file_reads_as_empty_rather_than_raising(self, tmp_path):
        # A cache that cannot be read is a cache MISS, which costs one LLM call. A cache that
        # raised would fail the correction itself — the thing the user actually asked for.
        path = tmp_path / "cache.json"
        path.write_text("{not json", encoding="utf-8")
        read, _write = file_store(path)

        assert read() == {}

    def test_a_file_holding_something_that_is_not_a_map_reads_as_empty(self, tmp_path):
        path = tmp_path / "cache.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        read, _write = file_store(path)

        assert read() == {}

    def test_a_write_that_cannot_happen_does_not_take_the_correction_down(
        self, clock, tmp_path
    ):
        # A read-only disk, a full one, a path that is somehow a directory. The correction has
        # already been produced; losing the chance to remember it is not a reason to lose it.
        path = tmp_path / "cache.json"
        path.mkdir()
        read, write = file_store(path)

        CorrectionCache(read, write, clock=clock).put(_key(), {"rewritten": "x"})

    def test_it_leaves_no_temporary_file_behind(self, clock, tmp_path):
        # Written beside the target and renamed over it, so a crash mid-write cannot leave a
        # truncated file that the next read would throw the whole cache away over.
        read, write = file_store(tmp_path / "cache.json")
        CorrectionCache(read, write, clock=clock).put(_key(), {"rewritten": "x"})

        assert sorted(p.name for p in tmp_path.iterdir()) == ["cache.json"]

    def test_concurrent_writers_do_not_lose_each_other(self, tmp_path):
        # Checks land on HTTP worker threads and the server threads every connection, so two
        # finishing at once is ordinary. Read-modify-write without a lock loses one of them.
        import threading

        path = tmp_path / "cache.json"
        ready = threading.Barrier(8)

        def contribute(index: int) -> None:
            read, write = file_store(path)
            cache = CorrectionCache(read, write, max_entries=100, max_age=10_000.0)
            ready.wait(5)
            cache.put(_key(text=f"phrase {index}"), {"rewritten": str(index)})

        workers = [threading.Thread(target=contribute, args=(i,)) for i in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)

        read, _write = file_store(path)
        assert len(read()) == 8, "a concurrent write was lost"
