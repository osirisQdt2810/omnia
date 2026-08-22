#!/usr/bin/env python3
"""Measure smart-notes generation against the REAL provider, to pick a default.

The sibling ``smart_notes_throughput.py`` answers "does each layer do what it claims" with a
fake whose latency we control. It cannot answer the question that actually decides the shipped
defaults, because the deciding factor is not in the model: **what the provider does to us under
concurrency.** A real endpoint has a rate limit, a variable time-to-first-token, and a habit of
being slower for a long answer than a short one. Those are exactly the terms in which batching
and concurrency trade against each other.

So this one runs the real thing:

* fields come from real notes in a COPY of the collection — never the live one. Anki holds the
  live file open with a WAL and a benchmark has no business contending with the app for it;
* nothing is written back, and that is ENFORCED rather than assumed. Every Anki seam the runner
  touches (``get_note``, ``update_note``, ``add_media_file``, the progress plumbing) is replaced
  for the duration of the run, so generated text lands in throwaway in-memory notes and
  generated audio bytes are dropped on the floor; on top of that :class:`WriteGuard` blocks the
  ``aqt``/``anki`` imports outright and installs raisers over every OTHER ``anki_compat`` seam,
  then asserts at the end that no mutating one was reached. Until that guard existed the safety
  was an accident of this venv not having ``aqt`` installed — run the same file on a machine
  that does, with a profile open, and an unpatched path would have written to the real
  collection with nothing to notice. The collection copy is opened read-only, once, to read
  fields and settings;
* the image field is skipped. It is a constant per note in every arm, so it cannot change the
  comparison, and generating a hundred pictures twelve times over is a waste of the user's
  money. Every table this prints says so.

It reports speed AND correctness, because "fastest" is not the answer to "best":

* wall clock and provider call count per arm;
* rate-limit errors and retries — the thing the limiter exists to prevent;
* **cross-contamination**: how often a note's generated text names ANOTHER note's headword.
  That is context bleed, the one risk batching adds that no parsing discipline can catch, and
  it is measurable rather than hypothetical.

Two deliberate departures from the obvious shape, both because the obvious shape would measure
the wrong thing:

* the entry point is ``BatchGenerator``, not ``GenerationService.generate_note``. Batching
  lives on the batch path — ``generate_note`` dispatches solo thunks and has no planner — so
  driving the arms through it would make ``4x10`` a byte-identical rerun of ``4x1`` and the
  whole K axis would silently vanish from the table. ``BatchGenerator`` is also what the
  Browser action a user waits on actually calls;
* every generatable field is BLANKED on the way in, on the copy, so each note generates its
  whole shape exactly as a freshly captured note would. Reading the collection's existing
  values instead would let the skip predicate cancel most of the workload, and the arms would
  be comparing how fast we can decline to work.

What it needs, and why it is not a test: real notes, real credentials and real provider spend.
Give it an Anki collection holding the note type (``--note-type``, default ``AnkiVocabulary``), a
``user_files/config`` with working provider credentials, and ``--collection`` pointed at your own
file — the default is one developer's path. CI never runs this; ``pytest`` never imports it.

Usage::

    python tests/benchmarks/smart_notes_live.py --notes 100 --repeats 2 \
        --out tests/benchmarks/data/live_100notes_<date>.json
    python tests/benchmarks/smart_notes_live.py --notes 6 --repeats 1 --arms 8x1 8x10

An arm is ``<workers>x<K>``; ``K=1`` means batching off. Append ``r<N>`` to bound the provider
requests below the pool width (``8x10r4`` = 8 workers, K=10, at most 4 requests in flight).

**Write the rows out, and commit them.** ``tests/benchmarks/data/`` holds the sessions the
shipped defaults cite, with a README saying what each one does and does not establish. A default
whose evidence lives in a scratch directory cannot be audited or re-derived — and the first round
of this study shipped a default and a UI claim from a single session whose central result did not
reproduce. Two samples of a network-bound arm is not a measurement; the aggregated table prints
the spread next to the mean so that is visible without arithmetic.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.error
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
for _path in (_REPO_ROOT / "src", _REPO_ROOT / "vendor" / "universal"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from common import enable_utf8_output  # noqa: E402

# Prints a results table with box characters; on a cp1252 console one of them would take the
# run down after all the measuring is done.
enable_utf8_output()

from omnia.core.network.http import UrllibHttpClient  # noqa: E402
from omnia.core.network.limiter import PROVIDER_LIMITER  # noqa: E402
from omnia.core.providers.usage import (  # noqa: E402
    RecordingLLMProvider,
    RecordingTTSProvider,
)
from omnia.plugins.smart_notes.integration import batch as batch_module  # noqa: E402

#: Field types skipped in every arm. Image is a constant per note, so dropping it cannot change
#: the comparison — only the absolute seconds, which are stated as such.
SKIPPED_TYPES = frozenset({"image"})

#: The ``anki_compat`` names the batch runner reaches for. Every one is replaced for the
#: duration of a run: this benchmark must not be able to touch the user's collection even by
#: accident. Every OTHER public seam is replaced too — by :class:`WriteGuard`, with a raiser —
#: so "a seam added later that is not in this list cannot write" is a checked property rather
#: than a hope.
_COMPAT_SEAMS = (
    "get_note",
    "note_deck_ids",
    "update_note",
    "add_media_file",
    "progress_start",
    "progress_update",
    "progress_finish",
    "progress_was_cancelled",
    "run_on_main",
    "run_in_background",
)

#: ``anki_compat`` seams that need no guard because they touch neither the collection nor the
#: GUI — they operate on whatever object they are handed. Everything public and not in here or
#: in ``_COMPAT_SEAMS`` is denied for the duration of a run.
_PURE_SEAMS = ("escape_search_term", "card_side_av_text")

#: ...of the denied seams, the ones that would MUTATE the user's data. Reaching one is a bug in
#: this benchmark rather than a degraded environment, so the run fails loudly at the end.
_MUTATING_SEAMS = frozenset(
    {
        "update_note",
        "update_notes",
        "add_media_file",
        "add_note_type_field",
    }
)

#: Headwords shorter than this are not scanned for as contamination. "act", "own" and "set"
#: occur in ordinary English prose constantly, and counting them would bury the signal the
#: column exists for under noise that is identical in every arm anyway.
_MIN_HEADWORD = 4


# ---------------------------------------------------------------------------------------
# Reading the collection (a COPY, read-only)
# ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class NoteSample:
    """One real note: its id, its headword, and its field map with the targets blanked."""

    nid: int
    headword: str
    fields: dict[str, str]


class CollectionSample:
    """The notes and the smart-notes settings, read once from a COPY of the collection.

    Everything this benchmark knows about the user comes from here, and it is all read through
    ``sqlite3`` against a copy — the add-on's own store is never asked for a collection, so
    there is no path from this file to the live database at all.
    """

    #: Where the smart-notes settings live. Not ``providers.toml``: the per-note-type rules
    #: sync with the collection (see ``SmartNotesStore.KEY``), and only the credentials are
    #: on disk.
    SETTINGS_KEY = "omnia:smart_notes"

    def __init__(self, collection: Path, note_type: str) -> None:
        self._copy = self._copy_aside(collection)
        self._note_type = note_type
        self._con = sqlite3.connect(f"file:{self._copy}?mode=ro", uri=True)
        # Anki's schema declares this collation on some indexes; sqlite3 refuses to read them
        # without it, even for a plain select.
        self._con.create_collation(
            "unicase", lambda a, b: (a.lower() > b.lower()) - (a.lower() < b.lower())
        )
        row = self._con.execute(
            "select id from notetypes where name = ?", (note_type,)
        ).fetchone()
        if row is None:
            raise SystemExit(f"note type {note_type!r} not found in {collection}")
        self._ntid = row[0]
        self._field_names = [
            name
            for (name,) in self._con.execute(
                "select name from fields where ntid = ? order by ord", (self._ntid,)
            )
        ]

    @staticmethod
    def _copy_aside(collection: Path) -> Path:
        """Copy the collection (and its WAL sidecars) somewhere this run owns."""
        scratch = Path(tempfile.gettempdir()) / "omnia-live-bench.anki2"
        shutil.copy(collection, scratch)
        for suffix in ("-wal", "-shm"):
            side = collection.with_name(collection.name + suffix)
            if side.exists():
                shutil.copy(side, scratch.with_name(scratch.name + suffix))
        return scratch

    def settings_blob(self) -> dict[str, Any]:
        """The raw ``omnia:smart_notes`` settings as this collection stores them."""
        row = self._con.execute(
            "select val from config where key = ?", (self.SETTINGS_KEY,)
        ).fetchone()
        if row is None:
            raise SystemExit(f"no {self.SETTINGS_KEY!r} in the collection config")
        return json.loads(row[0])

    def notes(
        self, limit: int, *, base_field: str, blank: Sequence[str]
    ) -> list[NoteSample]:
        """Return ``limit`` notes with a non-empty base field, their targets blanked.

        Ordered by note id so every arm and every repeat runs the SAME notes in the same order:
        the comparison is between configurations, and a different sample per arm would make it
        a comparison between vocabularies.
        """
        samples: list[NoteSample] = []
        for nid, flds in self._con.execute(
            "select id, flds from notes where mid = ? order by id", (self._ntid,)
        ):
            values = dict(zip(self._field_names, flds.split("\x1f"), strict=False))
            headword = _plain(values.get(base_field, ""))
            if not headword:
                continue
            for name in blank:
                if name in values:
                    values[name] = ""
            samples.append(NoteSample(nid, headword, values))
            if len(samples) >= limit:
                break
        return samples


def _plain(html: str) -> str:
    """Strip tags/entities/sound tags so a field can be compared as text."""
    text = re.sub(r"\[sound:[^\]]*\]", " ", html)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return " ".join(text.split())


# ---------------------------------------------------------------------------------------
# The Anki seams, all of them inert
# ---------------------------------------------------------------------------------------
class BenchNote:
    """A dict-like stand-in for an Anki note, holding one sample's fields in memory."""

    def __init__(self, sample: NoteSample, note_type: str) -> None:
        self.id = sample.nid
        self._fields = dict(sample.fields)
        self._note_type = note_type

    def keys(self) -> list[str]:
        return list(self._fields)

    def __contains__(self, key: str) -> bool:
        return key in self._fields

    def __getitem__(self, key: str) -> str:
        return self._fields[key]

    def __setitem__(self, key: str, value: str) -> None:
        self._fields[key] = value

    def note_type(self) -> dict[str, str]:
        return {"name": self._note_type}

    @property
    def values(self) -> dict[str, str]:
        """The field map as it stands after the run."""
        return dict(self._fields)


class InertCollection:
    """Every ``anki_compat`` seam the batch runner uses, with the writes removed.

    One class rather than a bag of lambdas because the seams share state — the notes it hands
    out are the notes it is asked to update — and because "nothing was written" is then a
    property of one object a reader can check in one place.
    """

    def __init__(self, notes: dict[int, BenchNote]) -> None:
        self._notes = notes
        self.updated: list[int] = []
        self.media: list[str] = []

    # --- reads ---------------------------------------------------------------------
    def get_note(self, nid: int, col: Any = None) -> BenchNote:
        return self._notes[nid]

    def note_deck_ids(self, note: Any, col: Any = None) -> list[int]:
        return [1]

    # --- writes, dropped -----------------------------------------------------------
    def update_note(self, note: Any, col: Any = None) -> None:
        self.updated.append(note.id)

    def add_media_file(self, filename: str, data: bytes, col: Any = None) -> str:
        self.media.append(filename)
        return filename

    # --- progress / threading ------------------------------------------------------
    def progress_start(self, label: str, maximum: int) -> None:
        pass

    def progress_update(self, label: str, value: int, maximum: int) -> None:
        pass

    def progress_finish(self) -> None:
        pass

    def progress_was_cancelled(self) -> bool:
        return False

    def run_on_main(self, callback: Callable[[], None]) -> None:
        callback()

    def run_in_background(
        self,
        op: Callable[[], Any],
        *,
        on_success: Callable[[Any], None],
        on_failure: Optional[Callable[[Exception], None]] = None,
        label: Optional[str] = None,
    ) -> None:
        try:
            on_success(op())
        except Exception as exc:  # a benchmark failure must be visible, not counted
            if on_failure:
                on_failure(exc)
            raise

    @contextmanager
    def installed(self) -> Iterator[InertCollection]:
        """Swap these seams into ``anki_compat`` for the duration of the block."""
        originals = {
            name: getattr(batch_module.anki_compat, name) for name in _COMPAT_SEAMS
        }
        for name in _COMPAT_SEAMS:
            setattr(batch_module.anki_compat, name, getattr(self, name))
        try:
            yield self
        finally:
            for name, original in originals.items():
                setattr(batch_module.anki_compat, name, original)


class WriteGuard:
    """Denies every route from this benchmark to the user's real Anki, and CHECKS it afterwards.

    :class:`InertCollection` swaps the ten seams the batch runner is known to use. That covered
    the run as it was written, and nothing more: the property "this file cannot write to the
    collection" held only because ``aqt`` is not installed in the dev venv, so
    ``anki_compat.main_window()`` raised ``ImportError`` on every other path and callers like
    ``engine.tools.base.resolve_media_dir`` quietly swallowed it into ``""``. Run the same file
    on a machine where ``aqt`` imports and a profile is open — a developer's Anki, a future CI
    image with the GUI installed — and an unpatched seam writes to the real collection with
    nothing to notice. Safe by accident is not safe.

    Two layers, both restored on exit:

    * ``aqt`` and ``anki`` are made UNIMPORTABLE for the duration (a ``sys.meta_path`` finder
      that raises ``ImportError``), so the benchmark runs identically on every machine and a
      direct ``from aqt import mw`` inside some helper cannot reach a live profile;
    * every public ``anki_compat`` callable except the pure ones is replaced by a raiser that
      RECORDS the name and then raises ``ImportError``. The exception type is deliberate: it is
      exactly what a machine without ``aqt`` produces, so the existing ``except Exception``
      fallbacks behave as they always have and the guard changes no measurement — it only makes
      the denial explicit and the reach visible. The ten seams in ``_COMPAT_SEAMS`` are guarded
      too, and :meth:`InertCollection.installed` simply layers its inert versions on top for the
      duration of an arm, restoring the raisers afterwards — so the gap BETWEEN arms (the warm-up
      call, the scoring, the reporting) is covered as well.

    :meth:`check` then turns the record into a result. A seam in ``_MUTATING_SEAMS`` is fatal:
    it means a path exists that WOULD have written. Anything else is reported as reached-and-
    denied, because that is information about the run, not a failure of it.
    """

    def __init__(self, module: Any) -> None:
        # A rename in anki_compat must not silently shrink the guard: an allowlist entry that
        # no longer exists means some seam is now unguarded under a new name.
        stale = [
            n
            for n in (*_COMPAT_SEAMS, *_PURE_SEAMS, *_MUTATING_SEAMS)
            if not hasattr(module, n)
        ]
        if stale:
            raise SystemExit(
                f"anki_compat no longer has {stale} — the benchmark's seam lists are stale and "
                "the write guard cannot be trusted until they are updated"
            )
        self._module = module
        self._denied = tuple(
            name
            for name in dir(module)
            if not name.startswith("_")
            and callable(getattr(module, name))
            and name not in _PURE_SEAMS
            # Defined HERE, not imported into the module's namespace — a re-export is somebody
            # else's function and guarding it would patch it for the whole process.
            and getattr(getattr(module, name), "__module__", "") == module.__name__
        )
        self.reached: dict[str, int] = {}
        self._lock = __import__("threading").Lock()

    def _raiser(self, name: str) -> Callable[..., Any]:
        def denied(*_args: Any, **_kwargs: Any) -> Any:
            with self._lock:
                self.reached[name] = self.reached.get(name, 0) + 1
            # ModuleNotFoundError, not a bespoke type: it is what a machine without ``aqt``
            # already raises here, and it satisfies both ``except ImportError`` and
            # ``except ModuleNotFoundError`` handlers unchanged.
            raise ModuleNotFoundError(
                f"anki_compat.{name} is denied inside the live benchmark"
            )

        return denied

    @contextmanager
    def installed(self) -> Iterator[WriteGuard]:
        """Deny the real seams (and the ``aqt``/``anki`` imports) for the duration of the block."""
        blocker = _ImportBlocker(("aqt", "anki"))
        originals = {name: getattr(self._module, name) for name in self._denied}
        for name in self._denied:
            setattr(self._module, name, self._raiser(name))
        sys.meta_path.insert(0, blocker)
        try:
            yield self
        finally:
            with contextlib.suppress(ValueError):
                sys.meta_path.remove(blocker)
            for name, original in originals.items():
                setattr(self._module, name, original)

    def check(self) -> str:
        """Raise if a mutating seam was reached; return a one-line report of the rest."""
        mutating = {n: c for n, c in self.reached.items() if n in _MUTATING_SEAMS}
        if mutating:
            raise SystemExit(
                "the benchmark reached a MUTATING Anki seam — it would have written to the "
                f"collection on a machine with aqt installed: {mutating}"
            )
        if not self.reached:
            return "write guard : no real Anki seam reached (0 denials)"
        detail = ", ".join(f"{n} x{c}" for n, c in sorted(self.reached.items()))
        return f"write guard : reached and DENIED (no writes possible): {detail}"


class _ImportBlocker:
    """A ``sys.meta_path`` finder that makes the named top-level packages unimportable."""

    def __init__(self, names: Sequence[str]) -> None:
        self._names = frozenset(names)

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> None:
        if fullname.split(".")[0] in self._names:
            raise ModuleNotFoundError(
                f"{fullname} is blocked: the live benchmark must not be able to reach a real "
                "Anki profile, on any machine"
            )
        return None


# ---------------------------------------------------------------------------------------
# Counting what the provider actually did
# ---------------------------------------------------------------------------------------
@dataclass
class ProviderCounts:
    """What one arm cost the provider, counted at two altitudes.

    LOGICAL calls (what the engine asked for) and HTTP ATTEMPTS (what went on the wire) are both
    here because the gap between them IS the retry story: a run whose attempts exceed its
    requests was throttled, and one whose 429 count is zero was not.
    """

    solo_text: int = 0  # generate_cached_text — one AI field, one note
    batched: int = 0  # generate_json — one chunk covering K notes
    detect: int = (
        0  # generate_text — the language detection a voice-less TTS rule makes
    )
    tts: int = 0  # synthesize — one audio segment
    errors: int = 0  # provider calls that raised
    rate_limited_calls: int = 0  # ...of which were rate limits the engine saw
    http_requests: int = 0  # logical HTTP requests (retry loop entered)
    http_attempts: int = 0  # actual round trips (retries included)
    http_429: int = 0  # attempts rejected with 429
    http_5xx: int = 0
    network_errors: int = 0
    # Tokens, from the usage dict the provider returns alongside the text. Only the two
    # tuple-returning LLM calls report it, which is every call that costs real money on this
    # workload; the language-detection call and TTS are not token-metered here.
    in_tokens: int = 0
    out_tokens: int = 0
    cached_tokens: int = (
        0  # ...of in_tokens, served from the provider's prompt cache (LAYER 2)
    )

    @property
    def provider_calls(self) -> int:
        """Every logical call the engine made to a provider."""
        return self.solo_text + self.batched + self.detect + self.tts

    @property
    def retries(self) -> int:
        """Round trips that were a second (or third) go at the same request."""
        return max(0, self.http_attempts - self.http_requests)

    @property
    def uninstrumented_calls(self) -> int:
        """Provider calls the HTTP columns above CANNOT see, and therefore cannot vouch for.

        ``edge_tts`` speaks a WebSocket, so it never enters ``UrllibHttpClient`` — roughly a
        third of this workload's traffic. Its throttling arrives as a socket timeout, which no
        429 classifier can recognise. Printed as its own column so "zero 429s" is read as a
        statement about the HTTP providers, which is all it is.
        """
        return max(0, self.provider_calls - self.http_requests)


class ProviderMeter:
    """Counts provider work by decorating the two wrappers every built provider goes through.

    Patched on the RECORDING wrappers rather than on the concrete providers because that is the
    single type every path gets back from ``ProviderHub`` — one seam catches gemini_vertex,
    google_cloud and edge_tts alike, and a provider added tomorrow is counted without this file
    knowing its name. The HTTP figures come from ``UrllibHttpClient`` instead, one layer below
    the real ``RetryPolicy``, so a throttled run's retries are observed rather than assumed.

    ``edge_tts`` speaks a WebSocket and therefore contributes to the logical counts but not to
    the HTTP ones. That gap is not a footnote: it is ~200 of every run's calls, a third of the
    heaviest batched arm's traffic, and it is the ONE path on which this study saw a provider
    error. The 429/retry columns describe the urllib providers and nothing else, so the gap is
    reported explicitly as ``uninstrumented_calls`` rather than left for a reader to derive.
    ``_is_rate_limit`` cannot help there either — throttling on a WebSocket arrives as a socket
    timeout, not as a status code.

    TOKENS are taken from the ``(text, usage)`` tuple the cached-text and JSON calls return.
    Batching changes the token bill in two directions at once — it re-sends the prompt prefix per
    chunk and enlarges each completion, while LAYER 2's implicit prompt caching discounts exactly
    that prefix — so a defaults change on a metered API that reported only call counts would be
    quoting a proxy for cost while the real number was one attribute away.
    """

    def __init__(self) -> None:
        self.counts = ProviderCounts()
        self._lock = __import__("threading").Lock()

    def _bump(self, name: str, by: int = 1) -> None:
        with self._lock:
            setattr(self.counts, name, getattr(self.counts, name) + by)

    def _record_usage(self, result: Any) -> None:
        """Add the token counts of a ``(text, usage)`` return, if it carried any."""
        if not (isinstance(result, tuple) and len(result) == 2):
            return
        usage = result[1]
        if not isinstance(usage, dict):
            return
        self._bump("in_tokens", int(usage.get("in", 0) or 0))
        self._bump("out_tokens", int(usage.get("out", 0) or 0))
        self._bump("cached_tokens", int(usage.get("cached", 0) or 0))

    def _wrap_call(self, original: Callable[..., Any], slot: str) -> Callable[..., Any]:
        def counted(inner_self: Any, *args: Any, **kwargs: Any) -> Any:
            self._bump(slot)
            try:
                result = original(inner_self, *args, **kwargs)
            except Exception as exc:
                self._bump("errors")
                if _is_rate_limit(exc):
                    self._bump("rate_limited_calls")
                raise
            self._record_usage(result)
            return result

        return counted

    def _wrap_open(self, original: Callable[..., Any]) -> Callable[..., Any]:
        def counted(inner_self: Any, req: Any) -> Any:
            self._bump("http_attempts")
            try:
                return original(inner_self, req)
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    self._bump("http_429")
                elif exc.code >= 500:
                    self._bump("http_5xx")
                raise
            except (urllib.error.URLError, TimeoutError, OSError):
                self._bump("network_errors")
                raise

        return counted

    def _wrap_request(self, original: Callable[..., Any]) -> Callable[..., Any]:
        def counted(inner_self: Any, req: Any) -> Any:
            self._bump("http_requests")
            return original(inner_self, req)

        return counted

    @contextmanager
    def installed(self) -> Iterator[ProviderMeter]:
        """Count everything that happens inside the block."""
        patches: list[tuple[type, str, Any]] = [
            (RecordingLLMProvider, "generate_cached_text", "solo_text"),
            (RecordingLLMProvider, "generate_json", "batched"),
            (RecordingLLMProvider, "generate_text", "detect"),
            (RecordingTTSProvider, "synthesize", "tts"),
        ]
        originals: list[tuple[type, str, Any]] = []
        for owner, name, slot in patches:
            original = getattr(owner, name)
            originals.append((owner, name, original))
            setattr(owner, name, self._wrap_call(original, slot))
        for owner, name, wrapper in (
            (UrllibHttpClient, "_open", self._wrap_open),
            (UrllibHttpClient, "_request", self._wrap_request),
        ):
            original = getattr(owner, name)
            originals.append((owner, name, original))
            setattr(owner, name, wrapper(original))
        try:
            yield self
        finally:
            for owner, name, original in originals:
                setattr(owner, name, original)


def _is_rate_limit(exc: BaseException) -> bool:
    """Whether an exception the engine saw was the provider refusing on quota.

    Status-code shaped, so it recognises throttling only where there is a status code. The
    WebSocket TTS path has none: a throttled ``edge_tts`` surfaces as a connect timeout, which
    this returns False for and correctly so — it is unclassifiable, not benign.
    """
    if getattr(exc, "status_code", None) == 429:
        return True
    return "429" in str(exc) or "rate limit" in str(exc).lower()


# ---------------------------------------------------------------------------------------
# Cross-contamination
# ---------------------------------------------------------------------------------------
class ContaminationScanner:
    """Counts generated fields that name a DIFFERENT note's headword.

    A blunt instrument, and it now says HOW blunt rather than merely admitting to it. Two
    weaknesses, in opposite directions.

    FALSE POSITIVES. A definition legitimately contains ordinary English, and two notes in a
    vocabulary deck can share a stem ("mental" / "mentality") or be each other's antonym
    ("mental" / "physical") entirely on purpose — this deck's ``Antonyms`` field will often name
    another note's headword because that is the correct answer. The ABSOLUTE number is therefore
    not a defect count.

    FALSE NEGATIVES, which matter more and were nearly missed. The scan fires only when the
    bleeding text happens to restate the OTHER note's headword — so it sees an answer that quotes
    its subject and is blind to one that merely explains it. Constructing the exact failure the
    column exists for (every note handed its neighbour's answer, in all ten scanned fields, on
    the real deck) it flagged 42% of them: ~100% on "Synonyms (explained)", "Word (family)",
    "Example 1" and "Phrasal Verb", but 12% on "Definition" and "Antonyms" and 0-2% on
    "Meaning (vi)", "part of speech" and "IPA". Fields where an answer restates its headword are
    covered; fields where it does not are near-invisible, and a batching bug that mis-attributes
    one to five text fields per hundred would land inside the run-to-run noise band.

    So the class measures its own recall and the report prints it beside the count
    (:meth:`recall_against_swap`). A sensitivity stated next to the number is the difference
    between "no bleed detected by an instrument that catches two in five" and "no bleed" — and
    the second sentence is how the previous round of this study went wrong.

    What the column IS good for is MOVEMENT, within its sensitivity. The scanned text, the
    scanned fields and the headword vocabulary are identical in every arm, so the only thing
    that can move the number between arms is the generation itself.
    """

    def __init__(self, headwords: Sequence[str]) -> None:
        self._patterns = [
            (word, re.compile(rf"\b{re.escape(word)}\b", re.IGNORECASE))
            for word in {w.lower() for w in headwords if len(w) >= _MIN_HEADWORD}
        ]

    def hits(self, own_headword: str, text: str) -> list[str]:
        """The OTHER notes' headwords named in ``text``."""
        own = own_headword.lower()
        plain = _plain(text)
        if not plain:
            return []
        return [
            word
            for word, pattern in self._patterns
            if word not in own and pattern.search(plain)
        ]

    def recall_against_swap(
        self, samples: Sequence[NoteSample], fields: Sequence[str]
    ) -> tuple[int, int]:
        """This scanner's RECALL on the exact failure it exists to catch, on this corpus.

        Builds the worst realistic bleed — every note handed the NEXT note's stored text, in
        every scanned field — and counts how many of those deliberate mis-attributions the scan
        actually flags. Costs no provider call and uses the collection's own existing values, so
        it can run before the first arm and label every bleed number the run prints.

        Args:
            samples: The notes of the run, in order, with their ORIGINAL field values.
            fields: The field names the run scans for bleed.

        Returns:
            ``(caught, planted)`` — how many planted mis-attributions were flagged, out of how
            many were plantable (a field empty in the donor plants nothing).
        """
        caught = planted = 0
        for index, note in enumerate(samples):
            donor = samples[(index + 1) % len(samples)]
            if donor.nid == note.nid:
                continue
            for name in fields:
                text = donor.fields.get(name, "")
                if not _plain(text):
                    continue
                planted += 1
                if self.hits(note.headword, text):
                    caught += 1
        return caught, planted


# ---------------------------------------------------------------------------------------
# One arm
# ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ArmSpec:
    """A ``<workers>x<K>[r<requests>]`` configuration."""

    workers: int
    batch: int
    request_limit: int = 0

    @classmethod
    def parse(cls, text: str) -> ArmSpec:
        match = re.fullmatch(r"(\d+)x(\d+)(?:r(\d+))?", text.strip())
        if not match:
            raise SystemExit(
                f"cannot read arm {text!r}; expected <workers>x<K>[r<requests>]"
            )
        return cls(int(match[1]), int(match[2]), int(match[3] or 0))

    @property
    def label(self) -> str:
        suffix = f"r{self.request_limit}" if self.request_limit else ""
        return f"{self.workers}x{self.batch}{suffix}"


@dataclass
class ArmResult:
    """What one (workers, K) configuration cost and produced."""

    label: str
    repeat: int
    seconds: float = 0.0
    counts: ProviderCounts = field(default_factory=ProviderCounts)
    limiter_acquired: int = 0
    limiter_peak: int = 0
    limiter_wait: float = 0.0
    fields_filled: int = 0
    fields_expected: int = 0
    notes_processed: int = 0
    blocked: int = 0
    field_failures: int = 0
    unfilled: int = 0
    contaminated_fields: int = 0
    contaminated_notes: int = 0
    scanned_fields: int = 0
    generated_chars: int = 0  # total plain-text length of the scanned AI fields
    examples: list[str] = field(default_factory=list)

    @property
    def fill_rate(self) -> float:
        """Share of expected fields that actually came back with content."""
        return (
            self.fields_filled / self.fields_expected if self.fields_expected else 0.0
        )

    @property
    def mean_answer_chars(self) -> float:
        """Mean length of a scanned AI answer — the CONTENT axis ``fields_filled`` cannot see.

        A field that came back at half its usual length still counts as filled, so a fill rate
        that is constant across arms says nothing about whether the answers got worse. Batched
        answers measured about 20% shorter than solo ones on this deck; terser rather than
        truncated, but not nothing, and not visible in any other column.
        """
        return (
            self.generated_chars / self.scanned_fields if self.scanned_fields else 0.0
        )


class ArmRunner:
    """Runs one arm end to end: config, env, generation, counting, scanning.

    Holds the things that must NOT be rebuilt per arm — the provider hub (a fresh one re-signs
    the Vertex JWT and refetches an OAuth token, which would charge the first arm for something
    no user pays per batch) and the note samples (the arms must run the same notes).
    """

    def __init__(
        self,
        *,
        service: Any,
        blob: dict[str, Any],
        note_type: str,
        samples: Sequence[NoteSample],
        scanner: ContaminationScanner,
        scanned_fields: Sequence[str],
        generatable: Sequence[str],
    ) -> None:
        self._service = service
        self._blob = blob
        self._note_type = note_type
        self._samples = list(samples)
        self._scanner = scanner
        self._scanned_fields = list(scanned_fields)
        self._generatable = list(generatable)

    def run(self, spec: ArmSpec, repeat: int) -> ArmResult:
        """Generate every sample under ``spec`` and return what it cost."""
        from omnia.plugins.smart_notes.config import SmartNotesSettings

        settings = SmartNotesSettings.parse_obj(
            _settings_for(self._blob, self._note_type, spec)
        )
        notes = {s.nid: BenchNote(s, self._note_type) for s in self._samples}
        result = ArmResult(label=spec.label, repeat=repeat)
        summaries: list[Any] = []

        PROVIDER_LIMITER.reset_stats()
        with (
            _environment(spec),
            InertCollection(notes).installed(),
            ProviderMeter().installed() as meter,
        ):
            started = time.perf_counter()
            try:
                batch_module.BatchGenerator(self._service, settings).run(
                    [s.nid for s in self._samples],
                    summaries.append,
                    show_progress=False,
                )
            finally:
                result.seconds = time.perf_counter() - started
        result.counts = meter.counts

        stats = PROVIDER_LIMITER.stats
        result.limiter_acquired = stats.acquired
        result.limiter_peak = stats.peak_in_flight
        result.limiter_wait = stats.total_wait_seconds

        summary = summaries[0]
        result.notes_processed = summary.processed
        result.blocked = summary.blocked
        result.field_failures = summary.field_failures
        result.unfilled = summary.unfilled
        self._score(notes, result)
        return result

    def _score(self, notes: dict[int, BenchNote], result: ArmResult) -> None:
        """Fill in the fill rate and the contamination counts from the finished notes."""
        result.fields_expected = len(notes) * len(self._generatable)
        by_nid = {s.nid: s for s in self._samples}
        for nid, note in notes.items():
            values = note.values
            result.fields_filled += sum(
                1 for name in self._generatable if values.get(name, "").strip()
            )
            dirty = False
            for name in self._scanned_fields:
                text = values.get(name, "")
                if not text.strip():
                    continue
                result.scanned_fields += 1
                result.generated_chars += len(_plain(text))
                hits = self._scanner.hits(by_nid[nid].headword, text)
                if hits:
                    dirty = True
                    result.contaminated_fields += 1
                    if len(result.examples) < 5:
                        result.examples.append(
                            f"{by_nid[nid].headword!r} · {name} names {hits[:3]}"
                        )
            if dirty:
                result.contaminated_notes += 1


def _settings_for(
    blob: dict[str, Any], note_type: str, spec: ArmSpec
) -> dict[str, Any]:
    """The user's stored settings, narrowed to this benchmark's workload and this arm.

    Three edits, and no others: keep only the measured note type, drop the field types this run
    skips, and set the two knobs the arm is about. Everything else — the prompts, the
    dependencies, the voices, the per-field providers — is the user's own, because a benchmark
    whose workload is invented cannot decide their default.
    """
    settings = dict(blob)
    settings["note_types"] = [
        {
            **nt,
            "fields": [f for f in nt["fields"] if f.get("type") not in SKIPPED_TYPES],
        }
        for nt in blob.get("note_types", [])
        if nt.get("note_type") == note_type
    ]
    settings["max_concurrent_generations"] = spec.workers
    settings["batch_notes_per_call"] = spec.batch
    return settings


@contextmanager
def _environment(spec: ArmSpec) -> Iterator[None]:
    """Pin the env knobs this arm is about, and put the machine's own values back.

    ``OMNIA_SMART_NOTES_BATCHING`` is the CEILING the stored ``batch_notes_per_call`` is clamped
    to, so an arm that inherited someone's environment would silently measure a different K —
    or, at ``-1``, the solo path wearing a batched arm's label.
    """
    wanted = {
        "OMNIA_SMART_NOTES_BATCHING": str(spec.batch if spec.batch > 1 else -1),
        "OMNIA_MAX_CONCURRENT_REQUESTS": str(spec.request_limit or 0),
    }
    previous = {name: os.environ.get(name) for name in wanted}
    os.environ.update(wanted)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


# ---------------------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------------------
class Report:
    """Renders the collected arm results as Markdown, per run and aggregated."""

    def __init__(self, results: Sequence[ArmResult], order: Sequence[str]) -> None:
        self._results = list(results)
        self._order = list(order)

    def _for(self, label: str) -> list[ArmResult]:
        return [r for r in self._results if r.label == label]

    def per_run(self) -> str:
        head = (
            "| arm | rep | wall clock (s) | provider calls | solo text | batched | detect "
            "| tts | not HTTP-metered | HTTP attempts | retries | 429s | limiter peak "
            "| limiter wait (s) | in tokens | out tokens | cached tokens "
            "| fields filled / expected | fill rate | mean answer chars | blocked "
            "| field errors | unfilled | contaminated fields / scanned "
            "| contaminated notes |\n" + "|---" * 25 + "|\n"
        )
        lines = []
        for label in self._order:
            for row in self._for(label):
                c = row.counts
                lines.append(
                    f"| {row.label} | {row.repeat} | {row.seconds:.1f} | {c.provider_calls} "
                    f"| {c.solo_text} | {c.batched} | {c.detect} | {c.tts} "
                    f"| {c.uninstrumented_calls} "
                    f"| {c.http_attempts} | {c.retries} | {c.http_429} "
                    f"| {row.limiter_peak} | {row.limiter_wait:.1f} "
                    f"| {c.in_tokens} | {c.out_tokens} | {c.cached_tokens} "
                    f"| {row.fields_filled} / {row.fields_expected} "
                    f"| {row.fill_rate * 100:.1f}% | {row.mean_answer_chars:.1f} "
                    f"| {row.blocked} | {row.field_failures} "
                    f"| {row.unfilled} "
                    f"| {row.contaminated_fields} / {row.scanned_fields} "
                    f"| {row.contaminated_notes} |"
                )
        return head + "\n".join(lines)

    def aggregated(self) -> str:
        """Mean and spread per arm. A single sample of a network-bound run is not a measurement.

        SPREAD IS THE COLUMN TO READ FIRST, and it is printed second for that reason. Two
        sessions of this benchmark have now disagreed about whether grouping is faster, with
        within-arm spread as wide as the between-arm gap; an arm whose spread covers its
        neighbour's mean has not been distinguished from it at n = 2, however confident the
        means look side by side.
        """
        head = (
            "| arm | wall clock mean (s) | spread (min–max) | spread % of mean "
            "| provider calls | not HTTP-metered | 429s | retries | out tokens "
            "| cached tokens | fill rate | mean answer chars "
            "| contaminated fields / scanned |\n" + "|---" * 13 + "|\n"
        )
        lines = []
        for label in self._order:
            rows = self._for(label)
            if not rows:
                continue
            seconds = [r.seconds for r in rows]
            avg = mean(seconds)
            spread = (max(seconds) - min(seconds)) / avg * 100 if avg else 0.0
            lines.append(
                f"| {label} | {avg:.1f} | {min(seconds):.1f}–{max(seconds):.1f} "
                f"| {spread:.1f}% "
                f"| {mean(r.counts.provider_calls for r in rows):.0f} "
                f"| {mean(r.counts.uninstrumented_calls for r in rows):.0f} "
                f"| {sum(r.counts.http_429 for r in rows)} "
                f"| {sum(r.counts.retries for r in rows)} "
                f"| {mean(r.counts.out_tokens for r in rows):.0f} "
                f"| {mean(r.counts.cached_tokens for r in rows):.0f} "
                f"| {mean(r.fill_rate for r in rows) * 100:.1f}% "
                f"| {mean(r.mean_answer_chars for r in rows):.1f} "
                f"| {mean(r.contaminated_fields for r in rows):.1f} "
                f"/ {mean(r.scanned_fields for r in rows):.0f} |"
            )
        return head + "\n".join(lines)

    def examples(self) -> str:
        seen: list[str] = []
        for row in self._results:
            for example in row.examples:
                if example not in seen:
                    seen.append(example)
        return "\n".join(f"  {line}" for line in seen[:12])


# ---------------------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------------------
def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notes", type=int, default=100)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--note-type", default="AnkiVocabulary")
    parser.add_argument(
        "--collection",
        default=str(
            Path.home() / "Library/Application Support/Anki2/Decks/collection.anki2"
        ),
    )
    parser.add_argument(
        "--arms",
        nargs="*",
        default=["4x1", "8x1", "4x10", "8x10", "8x20", "16x10"],
        help="<workers>x<K>[r<requests>]; K=1 is batching off",
    )
    parser.add_argument("--config", default=str(_REPO_ROOT / "user_files" / "config"))
    parser.add_argument("--tools", default=str(_REPO_ROOT / "user_files" / "tools"))
    parser.add_argument("--out", default="", help="write the raw results here as JSON")
    args = parser.parse_args(argv[1:])

    from omnia.core.config import ConfigLoader, ConfigRepository
    from omnia.core.providers import ProviderHub
    from omnia.plugins.smart_notes.config import SmartNotesSettings
    from omnia.plugins.smart_notes.engine import GenerationService
    from omnia.plugins.smart_notes.engine.tools import UserToolLoader, UserToolStore

    specs = [ArmSpec.parse(text) for text in args.arms]

    # --- the workload, from the user's own collection and their own settings -------------
    sample = CollectionSample(Path(args.collection), args.note_type)
    blob = sample.settings_blob()
    stored = SmartNotesSettings.parse_obj(_settings_for(blob, args.note_type, specs[0]))
    config = stored.note_type_config(args.note_type)
    if config is None:
        raise SystemExit(f"no smart-notes config for {args.note_type!r}")
    generatable = [rule.field for rule in config.generatable_fields()]
    # The fields a chunk can actually merge: pure-AI text on one provider/model/template. The
    # deterministic and media fields are generated identically in every arm, so scanning them
    # for bleed would only add a constant to every row.
    scanned = [
        f.field for f in config.fields if f.enabled and f.type == "text" and not f.tools
    ]
    notes = sample.notes(args.notes, base_field=config.base_field, blank=generatable)
    if len(notes) < args.notes:
        print(f"! only {len(notes)} notes have a non-empty {config.base_field!r}")
    # The SAME notes with their stored text intact, used only to calibrate the bleed scanner
    # (see ContaminationScanner.recall_against_swap). Read separately because the run's own
    # copies are blanked on purpose.
    written = sample.notes(args.notes, base_field=config.base_field, blank=())

    # --- the provider, built once and shared by every arm --------------------------------
    repo = ConfigRepository(ConfigLoader(Path(args.config)))
    hub = ProviderHub(config=repo)
    loader = UserToolLoader(UserToolStore(Path(args.tools)))
    loaded = loader.load_all()
    service = GenerationService(hub)

    print(f"collection : {args.collection} (copy, read-only)")
    print(
        f"note type  : {args.note_type} — {len(notes)} notes, {len(generatable)} fields each"
    )
    print(f"llm        : {repo.llm_settings().provider} / {hub.llm().name}")
    print(f"user tools : {len(loaded)} loaded from {args.tools}")
    print(
        f"skipped    : {sorted(SKIPPED_TYPES)} fields — constant per note in every arm, so"
    )
    print("             they cannot change the comparison; only the absolute seconds.")
    scanner = ContaminationScanner([n.headword for n in notes])
    caught, plantable = scanner.recall_against_swap(written, scanned)
    recall = 100 * caught / plantable if plantable else 0.0
    print(f"scanned for bleed: {len(scanned)} AI text fields per note")
    print(
        f"bleed scan recall: {recall:.0f}% ({caught}/{plantable}) on a constructed "
        "neighbour swap — every bleed number below is a count from an instrument that misses"
    )
    print(
        f"             {100 - recall:.0f}% of the failure it looks for. Read a flat bleed "
        "column as 'not detected', never as 'did not happen'."
    )
    print(f"arms       : {', '.join(s.label for s in specs)} x {args.repeats} repeats")
    print()

    # Every real Anki seam denied, and aqt/anki made unimportable, for the whole measured
    # session — the warm-up call included. Installed HERE rather than per arm so no gap exists
    # between arms either.
    guard = WriteGuard(batch_module.anki_compat)
    with guard.installed():
        # One throwaway call so the first arm is not charged for the OAuth token + JWT signature
        # every later arm inherits from the hub's cache.
        hub.llm().generate_text("Reply with the single word: ready.")

        runner = ArmRunner(
            service=service,
            blob=blob,
            note_type=args.note_type,
            samples=notes,
            scanner=scanner,
            scanned_fields=scanned,
            generatable=generatable,
        )

        results: list[ArmResult] = []
        for repeat in range(1, args.repeats + 1):
            # Reversed on alternate repeats. Anything that drifts monotonically through a
            # session — a warming prompt cache, a quota bucket refilling, the provider's own
            # load — would otherwise land entirely on whichever arm always goes first.
            ordered = specs if repeat % 2 else list(reversed(specs))
            for spec in ordered:
                print(f"[repeat {repeat}] {spec.label} …", flush=True)
                row = runner.run(spec, repeat)
                results.append(row)
                print(
                    f"    {row.seconds:.1f}s · {row.counts.provider_calls} calls · "
                    f"{row.counts.http_429} 429 · {row.counts.retries} retries · "
                    f"fill {row.fill_rate * 100:.1f}% · "
                    f"bleed {row.contaminated_fields}/{row.scanned_fields}",
                    flush=True,
                )
                if args.out:
                    _dump(Path(args.out), results)

    print()
    print(guard.check())

    report = Report(results, [s.label for s in specs])
    print("\n### Per run\n")
    print(report.per_run())
    print("\n### Aggregated\n")
    print(report.aggregated())
    print("\n### Contamination examples (a shared stem can false-positive)\n")
    print(report.examples())
    print(f"\n(image fields skipped in every arm: {sorted(SKIPPED_TYPES)})")
    print(
        f"(bleed scan recall on this deck: {recall:.0f}% — the column undercounts real bleed "
        "by construction; see ContaminationScanner)"
    )
    print(
        "(the 429/retry columns cover the urllib providers only; the 'not HTTP-metered' column "
        "is the WebSocket TTS traffic they cannot see)"
    )
    return 0


def _dump(path: Path, results: Sequence[ArmResult]) -> None:
    """Write the raw rows, so a run that dies late still leaves its measurements behind."""
    payload = [
        {
            **{k: v for k, v in vars(row).items() if k != "counts"},
            "counts": vars(row.counts),
        }
        for row in results
    ]
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
