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

Pure apart from the store it is handed — no HTTP, no ``aqt`` — so the eviction rules are testable
without a collection.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional

#: How many corrections to keep. One is a few kilobytes of JSON, so this stays well under a
#: megabyte — small enough to sit in the config unnoticed, large enough that a study session never
#: evicts something the user is still going back and forth over.
MAX_ENTRIES = 500

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

    def digest(self) -> str:
        """A stable, short id for this question.

        Hashed rather than stored verbatim: a phrase can be a paragraph, and a five-hundred
        character key makes the stored map unreadable. Normalised for WHITESPACE only — case and
        punctuation are part of what is being corrected, so "i went" and "I went" are genuinely
        different questions with different right answers.
        """
        normalised = " ".join(self.text.split())
        raw = f"{self.mode} {self.language} {normalised}"
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
        """
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
        store = self._load()
        store[key.digest()] = {"at": self._clock(), "payload": payload}
        self._save(self._evict(store))

    def forget(self, key: CacheKey) -> None:
        """Drop one answer — what a "correct it again" button does."""
        store = self._load()
        if store.pop(key.digest(), None) is not None:
            self._save(store)

    def clear(self) -> None:
        """Drop everything."""
        self._save({})

    def __len__(self) -> int:
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


def section_store(
    repo: Any, section: str, key: str = "corrections"
) -> tuple[Callable[[], dict[str, Any]], Callable[[dict[str, Any]], None]]:
    """Read/write callables backed by one key of a config section.

    Returned as a pair rather than as a class, because :class:`CorrectionCache` needs exactly two
    functions, and binding it to the repository would put an Anki-shaped dependency in a module
    whose whole point is that it has none.

    Stored as a JSON STRING rather than a nested table: TOML has no good shape for a map of
    opaque digests to blobs, and the config file is more readable with one long line than with
    five hundred sections nobody can scan.
    """

    def read() -> dict[str, Any]:
        stored = repo.raw_section(section).get(key)
        if isinstance(stored, str):
            try:
                loaded = json.loads(stored)
            except ValueError:
                return {}
            return loaded if isinstance(loaded, dict) else {}
        return stored if isinstance(stored, dict) else {}

    def write(store: dict[str, Any]) -> None:
        repo.update_section(section, {key: json.dumps(store, separators=(",", ":"))})

    return read, write
