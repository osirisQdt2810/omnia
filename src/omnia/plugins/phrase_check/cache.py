"""Remembering a correction, so clicking away and back does not spend another LLM call.

The panel is transient by design — click outside and it is gone — and without this, coming back
to the same phrase costs another request, another wait, and another few cents. Worse, it can come
back DIFFERENT: models are not deterministic, so a second look at an identical sentence can
disagree with the first, which reads as the tool being unreliable rather than as sampling.

So a correction is stored against exactly what produced it: the phrase, the mode, and the
language the explanations were asked in. Change any of those and it is a different question with a
different right answer — the same sentence corrected for speech is not the one corrected for
writing, and an explanation in English is not one in Vietnamese.

Kept on the Anki side rather than in each clipper (the owner's decision): a phrase corrected in
the web clipper is already there when the desktop one asks, and there is one thing to clear rather
than three.

Stored in a plain JSON file under ``user_files/``, NOT in the collection config, for three
reasons that all point the same way. A check runs on the HTTP worker thread, and ``col.db`` is
not safe to write from one (the DB config backend buffers and flushes on the Qt main thread for
exactly this reason). The config write is read-blob → mutate → write-blob, so a check finishing
while the settings dialog is being saved would put the old table back over the user's settings.
And the feature config domain is the SYNCED one, so five hundred cached corrections would ride
to AnkiWeb and every other device, and every check would mark the collection modified. A cache
is per-machine scratch; ``user_files/`` is where per-machine scratch goes, and Anki preserves it
across add-on updates.

Pure apart from the store it is handed — no HTTP, no ``aqt`` — so the eviction rules are testable
without a collection.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from omnia.core.logging import get_logger

logger = get_logger("phrase_check")

#: How many corrections to keep. One is a few kilobytes of JSON, so this stays well under a
#: megabyte — small enough to sit in the config unnoticed, large enough that a study session never
#: evicts something the user is still going back and forth over.
MAX_ENTRIES = 500

#: Serialises the whole read-modify-write, not merely the write.
#:
#: Module-level, because a per-instance lock would protect nothing: a checker (and its cache)
#: is built PER REQUEST, so two concurrent checks hold two different objects over one file.
#: Checks land on HTTP worker threads and the server threads every connection, so two finishing
#: at once is ordinary rather than exotic -- and read-then-write without this means the later
#: write is built from a snapshot taken before the earlier one, which silently drops it.
#:
#: Within this process only. Two Anki profiles sharing a file would still race, but the write
#: is atomic (rename-over), so the worst case there is one lost entry rather than a torn file.
_MUTATE_LOCK = threading.RLock()

#: How long one stays useful. Not a cost measure: a correction is ADVICE about a sentence, and
#: advice a month old came from a model and a prompt that may both have changed since. Re-asking
#: occasionally is what stops the cache becoming a museum.
MAX_AGE_SECONDS = 30 * 24 * 60 * 60


@dataclass(frozen=True)
class CacheKey:
    """Exactly what a stored correction was the answer to."""

    text: str
    mode: str
    language: str
    #: The pinned model, when the user set one. Part of the question because a stronger model
    #: is asked for precisely when the current answers are not good enough: without this,
    #: switching to one keeps handing back the cheap model's work for up to a month, with
    #: nothing on screen to say why and no way past it but the per-phrase refresh.
    model: str = ""

    def digest(self) -> str:
        """A stable, short id for this question.

        Hashed rather than stored verbatim: a phrase can be a paragraph, and a five-hundred
        character key makes the stored map unreadable. Normalised for WHITESPACE only — case and
        punctuation are part of what is being corrected, so "i went" and "I went" are genuinely
        different questions with different right answers.
        """
        normalised = " ".join(self.text.split())
        raw = f"{self.mode} {self.language} {self.model} {normalised}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class CorrectionCache:
    """Corrections already paid for, by question.

    Args:
        read: Returns the whole stored map. Called on every lookup, so it must be cheap.
        write: Persists the whole map.
        clock: Seconds since some epoch, injected so the expiry rules are testable.
        max_entries: How many to keep.
        max_age: How old one may get before it is re-asked.
    """

    def __init__(
        self,
        read: Callable[[], dict[str, Any]],
        write: Callable[[dict[str, Any]], None],
        *,
        clock: Callable[[], float] = time.time,
        max_entries: int = MAX_ENTRIES,
        max_age: float = MAX_AGE_SECONDS,
    ) -> None:
        self._read = read
        self._write = write
        self._clock = clock
        self._max_entries = max_entries
        self._max_age = max_age

    def get(self, key: CacheKey) -> Optional[dict[str, Any]]:
        """The stored answer to this exact question, or None.

        An entry past its age is treated as absent AND removed, rather than returned with a
        warning: a stale correction that still renders is one the user acts on.

        Under the lock too, not only :meth:`put` — the expiry sweep WRITES, so without it a
        sweep on one HTTP worker can save a snapshot taken before a concurrent put on another
        and drop the entry it just paid for. A read that only read would not need this.
        """
        with _MUTATE_LOCK:
            store = self._load()
            entry = store.get(key.digest())
            if not isinstance(entry, dict):
                return None
            if self._expired(entry):
                store.pop(key.digest(), None)
                self._save(store)
                return None
            payload = entry.get("payload")
            return payload if isinstance(payload, dict) else None

    def put(self, key: CacheKey, payload: dict[str, Any]) -> None:
        """Remember an answer, evicting the oldest if there are now too many."""
        with _MUTATE_LOCK:
            store = self._load()
            store[key.digest()] = {"at": self._clock(), "payload": payload}
            self._save(self._evict(store))

    def __len__(self) -> int:
        # Under the lock like the rest: a read taken mid-write would be a count of a store that
        # never existed. Cheap, and consistency here costs nothing.
        with _MUTATE_LOCK:
            return len(self._load())

    # --- housekeeping ---------------------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        try:
            store = self._read()
        except Exception:
            # A cache that cannot be read must never stop a correction happening; the worst case
            # is paying for one more request.
            return {}
        return dict(store) if isinstance(store, dict) else {}

    def _save(self, store: dict[str, Any]) -> None:
        # The same reasoning in the other direction: failing to remember is not a failure. The
        # user gets their correction; the next identical request pays for one more.
        with contextlib.suppress(Exception):
            self._write(store)

    def _expired(self, entry: dict[str, Any]) -> bool:
        try:
            return (self._clock() - float(entry.get("at", 0))) > self._max_age
        except (TypeError, ValueError):
            return True  # an unreadable timestamp is not one to trust

    def _evict(self, store: dict[str, Any]) -> dict[str, Any]:
        """Keep the newest ``max_entries``, and drop anything already too old."""
        alive = {
            digest: entry
            for digest, entry in store.items()
            if isinstance(entry, dict) and not self._expired(entry)
        }
        if len(alive) <= self._max_entries:
            return alive
        newest = sorted(alive.items(), key=lambda item: _at(item[1]), reverse=True)
        return dict(newest[: self._max_entries])


def _at(entry: Any) -> float:
    try:
        return float(entry.get("at", 0))
    except (AttributeError, TypeError, ValueError):
        return 0.0


#: The file's name under ``user_files/``. Dotted so it sorts away from anything a user might go
#: looking for: it is scratch, and deleting it costs nothing but the next few LLM calls.
STORE_FILENAME = ".phrase_check_cache.json"


def file_store(
    path: Path,
) -> tuple[Callable[[], dict[str, Any]], Callable[[dict[str, Any]], None]]:
    """Read/write callables backed by one JSON file.

    Returned as a pair rather than as a class, because :class:`CorrectionCache` needs exactly two
    functions, and binding it to a path would put a filesystem dependency in a module whose whole
    point is that it has none.

    A file rather than the collection config. The config would be a ``col.set_config`` from
    whatever thread the HTTP request landed on, against a blob the settings dialog also rewrites,
    in the domain that syncs to AnkiWeb -- see this module's own docstring. None of that is what
    a cache is for.

    Every failure is swallowed to an empty store. A cache that cannot be read is a cache miss,
    which costs one LLM call; a cache that raises would fail the correction itself, which is the
    thing the user actually asked for.
    """

    def read() -> dict[str, Any]:
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}  # missing, unreadable, or half-written: a miss, not a failure
        return loaded if isinstance(loaded, dict) else {}

    def write(store: dict[str, Any]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Written beside the target and renamed over it: a crash (or a second Anki)
            # mid-write would otherwise leave a truncated file, and the next read would
            # throw the whole cache away rather than lose one entry.
            handle, temporary = tempfile.mkstemp(
                dir=str(path.parent), prefix=path.name, suffix=".tmp"
            )
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(store, stream, separators=(",", ":"))
                os.replace(temporary, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(temporary)
                raise
        except OSError:
            logger.debug("phrase_check: could not write the cache", exc_info=True)

    return read, write
