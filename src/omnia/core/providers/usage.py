"""Self-tracked provider usage: swappable stores + recorders + recording provider wrappers.

Pure module — no Anki imports at top level. The add-on tracks LLM/TTS usage itself (calls +
rough in/out character counts and exact token counts per provider+model) so the Account dialog
can show what is being used without depending on any provider's billing API.

The persisted aggregate is a ``{f"{kind}|{provider}|{model}": row}`` dict, where each row holds
``{kind, provider, model, calls, in_chars, out_chars, in_tokens, out_tokens, last_used_ts}``.
It is a small bounded map (one row per provider+model combination), so it lives behind a
:class:`UsageStore` as plain JSON-able data: the default :class:`CollectionUsageStore` keeps it in
the SYNCED collection config (``col.set_config`` under ``omnia:usage``), so usage rides along with
AnkiWeb sync and aggregates across a user's devices; :class:`JsonUsageStore` keeps it in a
device-local JSON file (the ``json`` backend). :func:`_fold_call` is the single row-default +
increment helper both recorders share.

Two recorders write the aggregate: :class:`JsonUsageRecorder` folds each call straight into its
:class:`UsageStore` under a lock (a synchronous read-modify-write, bg-thread-safe with the
file-backed store), while :class:`BufferedUsageRecorder` folds each call into a thread-safe
in-memory aggregate and flushes it to its store on the Qt main thread (coalesced), so a store
that isn't safe to touch off-thread — the collection (``col.set_config``) — is only written on
the main thread. The
:class:`RecordingLLMProvider` / :class:`RecordingTTSProvider` decorate a real provider so every
generation records a row; recording is best-effort and never raises into the call. A
process-wide default recorder is set once at bootstrap (a :class:`NullUsageRecorder` until then).
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

from omnia.core.providers.llm.base import LLMProvider
from omnia.core.providers.tts.base import TTSProvider


def _fold_call(
    rows: dict[str, dict],
    *,
    kind: str,
    provider: str,
    model: str,
    in_chars: int,
    out_chars: int,
    in_tokens: int,
    out_tokens: int,
    now: float,
) -> None:
    """Fold one call into ``rows`` in place (default the row, then increment its counters).

    The single aggregation helper shared by both recorders so their arithmetic can't drift.
    ``now`` is passed explicitly — :class:`JsonUsageRecorder` uses its injected ``time_fn`` and
    :class:`BufferedUsageRecorder` uses ``time.time()`` — so the helper stays clock-agnostic.
    """
    key = f"{kind}|{provider}|{model}"
    row = rows.get(
        key,
        {
            "kind": kind,
            "provider": provider,
            "model": model,
            "calls": 0,
            "in_chars": 0,
            "out_chars": 0,
            "in_tokens": 0,
            "out_tokens": 0,
            "last_used_ts": None,
        },
    )
    row["calls"] += 1
    row["in_chars"] += in_chars
    row["out_chars"] += out_chars
    row["in_tokens"] = row.get("in_tokens", 0) + in_tokens
    row["out_tokens"] = row.get("out_tokens", 0) + out_tokens
    row["last_used_ts"] = now
    rows[key] = row


class UsageRecorder(ABC):
    """Records one provider call (kind + provider + model + rough char/token counts)."""

    @abstractmethod
    def record(
        self,
        *,
        kind: str,
        provider: str,
        model: str,
        in_chars: int,
        out_chars: int,
        in_tokens: int = 0,
        out_tokens: int = 0,
    ) -> None:
        """Record one call. Must be cheap and must NOT raise into the caller.

        ``in_tokens``/``out_tokens`` are the EXACT token counts from the provider's response
        when it reports them (every LLM does); they stay 0 for providers/responses without
        usage (TTS, google_translate), where the char counts are the only signal.
        """

    @abstractmethod
    def snapshot(self) -> list[dict]:
        """Return the recorded rows (``[]`` when nothing is recorded/persisted)."""


class NullUsageRecorder(UsageRecorder):
    """The default recorder: records nothing (no file, no state)."""

    def record(
        self,
        *,
        kind: str,
        provider: str,
        model: str,
        in_chars: int,
        out_chars: int,
        in_tokens: int = 0,
        out_tokens: int = 0,
    ) -> None:
        return None

    def snapshot(self) -> list[dict]:
        return []


class UsageStore(ABC):
    """Persists the aggregated usage rows (``{key: row}``) — the store behind a recorder."""

    @abstractmethod
    def load(self) -> dict[str, dict]:
        """Return the persisted rows (``{}`` when absent/unreadable)."""

    @abstractmethod
    def save(self, data: dict[str, dict]) -> bool:
        """Persist the full rows dict.

        Returns:
            ``True`` if the data was actually persisted; ``False`` when the store silently
            skipped the write (e.g. the collection backend with no ``col`` loaded). The
            dispatcher relies on this to know whether a backend-switch copy succeeded.
        """


class JsonUsageStore(UsageStore):
    """A :class:`UsageStore` backed by an atomically-written JSON file.

    The SINGLE json-usage I/O implementation: a tolerant read (missing/corrupt → ``{}``) and an
    atomic write (temp file + ``os.replace``) so a mid-write failure can't truncate the store.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> dict[str, dict]:
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                parsed = json.load(handle)
        except (OSError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def save(self, data: dict[str, dict]) -> bool:
        # Write to a sibling temp file then atomically replace, so a mid-write failure (crash,
        # disk full) leaves the previous file intact instead of truncating it to nothing.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp, self._path)
        return True


_USAGE_ROW_FIELDS = (
    "kind",
    "provider",
    "model",
    "calls",
    "in_chars",
    "out_chars",
    "in_tokens",
    "out_tokens",
    "last_used_ts",
)


def _rows_from_raw(parsed: object) -> dict[str, dict]:
    """Rebuild the ``{key: row}`` aggregate from parsed config (tolerant of junk)."""
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, dict] = {}
    for key, row in parsed.items():
        if isinstance(key, str) and isinstance(row, dict):
            out[key] = row
    return out


class CollectionUsageStore(UsageStore):
    """A :class:`UsageStore` in the synced collection config (``col.set_config``).

    The bounded ``{key: row}`` aggregate is stored under ``omnia:usage`` as plain JSON-able data,
    so it syncs across a user's devices via AnkiWeb (mirroring :class:`CollectionVoiceCache`).
    ``col`` is resolved LAZILY (``mw.col`` isn't ready at add-on init); an optional
    ``col_provider`` lets tests inject a fake collection. Without a collection it degrades to
    ``{}`` on load and reports a non-persisted (``False``) save (headless-safe). Because a write
    touches the collection, saves must happen on the Qt main thread — hence the
    :class:`BufferedUsageRecorder` wrapper (see the module docstring).
    """

    KEY = "omnia:usage"

    def __init__(self, col_provider: Optional[Callable[[], Any]] = None) -> None:
        """Initialise the store.

        Args:
            col_provider: Returns the collection (``get_config``/``set_config``); defaults to the
                lazily-resolved ``mw.col``. Tests inject a fake collection.
        """
        self._col_provider = col_provider

    def load(self) -> dict[str, dict]:
        """Return the persisted rows as ``{key: row}`` (``{}`` when no collection is loaded)."""
        col = self._col()
        raw = col.get_config(self.KEY, None) if col is not None else None
        return _rows_from_raw(raw or {})

    def save(self, data: dict[str, dict]) -> bool:
        """Persist the full rows dict (no-op → ``False`` when no collection is loaded)."""
        col = self._col()
        if col is None:
            return False
        col.set_config(self.KEY, data)
        return True

    def _col(self) -> Any:
        if self._col_provider is not None:
            try:
                return self._col_provider()
            except Exception:
                return None
        from omnia.core import anki_compat

        try:
            return anki_compat.main_window().col
        except Exception:
            return None


def _default_schedule_main(callback: Callable[[], None]) -> None:
    """Schedule ``callback`` on the Qt main thread (lazy import keeps this module headless)."""
    from omnia.core import anki_compat

    anki_compat.run_on_main(callback)


class JsonUsageRecorder(UsageRecorder):
    """Thread-safe recorder that folds each call straight into its :class:`UsageStore`.

    :meth:`record` does a read-modify-write under a lock (via :func:`_fold_call`), so concurrent
    background generation threads can't lose an increment when the store is the file-backed
    :class:`JsonUsageStore` (whose write is atomic). ``time_fn`` is injected for testability.
    """

    def __init__(
        self, store: UsageStore, *, time_fn: Callable[[], float] = time.time
    ) -> None:
        self._store = store
        self._time_fn = time_fn
        self._lock = threading.Lock()

    def record(
        self,
        *,
        kind: str,
        provider: str,
        model: str,
        in_chars: int,
        out_chars: int,
        in_tokens: int = 0,
        out_tokens: int = 0,
    ) -> None:
        with self._lock:
            data = self._store.load()
            _fold_call(
                data,
                kind=kind,
                provider=provider,
                model=model,
                in_chars=in_chars,
                out_chars=out_chars,
                in_tokens=in_tokens,
                out_tokens=out_tokens,
                now=self._time_fn(),
            )
            self._store.save(data)

    def snapshot(self) -> list[dict]:
        """Return the persisted rows (``[]`` if the store is absent or corrupt)."""
        with self._lock:
            return list(self._store.load().values())


class BufferedUsageRecorder(UsageRecorder):
    """Recorder that aggregates in memory and flushes to its store on the main thread.

    :meth:`record` (called from background generation threads) folds each call into a
    thread-safe in-memory rows dict — same keying/fields as :class:`JsonUsageRecorder` — then
    schedules a COALESCED flush on the Qt main thread (only one flush pending at a time, so N
    rapid records cause one save, not N). This keeps a store that is unsafe to touch off-thread
    (the collection, ``col.set_config``) written only on the main thread. ``schedule_main`` is
    injected for tests (a
    synchronous stand-in runs the flush inline); the default defers to
    :func:`_default_schedule_main`.
    """

    def __init__(
        self,
        store: UsageStore,
        *,
        schedule_main: Callable[[Callable[[], None]], None] = _default_schedule_main,
    ) -> None:
        self._store = store
        self._schedule_main = schedule_main
        self._lock = threading.Lock()
        self._rows: dict[str, dict] = {
            key: dict(row) for key, row in store.load().items()
        }
        self._flush_pending = False

    def record(
        self,
        *,
        kind: str,
        provider: str,
        model: str,
        in_chars: int,
        out_chars: int,
        in_tokens: int = 0,
        out_tokens: int = 0,
    ) -> None:
        with self._lock:
            _fold_call(
                self._rows,
                kind=kind,
                provider=provider,
                model=model,
                in_chars=in_chars,
                out_chars=out_chars,
                in_tokens=in_tokens,
                out_tokens=out_tokens,
                now=time.time(),
            )
            if self._flush_pending:
                return  # a flush is already scheduled; coalesce into it
            self._flush_pending = True
        try:
            self._schedule_main(self._flush)
        except Exception:
            # Scheduling can fail (e.g. no main window during teardown). Clear the pending flag
            # so a later record() re-schedules instead of the recorder wedging into never
            # flushing again. Best-effort boundary: recording must not raise into the caller.
            with self._lock:
                self._flush_pending = False

    def snapshot(self) -> list[dict]:
        """Return the in-memory rows (matches :meth:`JsonUsageRecorder.snapshot`)."""
        with self._lock:
            return [dict(row) for row in self._rows.values()]

    def flush_now(self) -> None:
        """Flush the buffered aggregate to the store synchronously, on the CALLING thread.

        Called at teardown (profile close) on the Qt main thread. It writes the store inline, so
        with a ``CollectionUsageStore`` (``col.set_config``) it is ONLY safe on the main thread —
        off-thread it is safe only with the file-backed :class:`JsonUsageStore`. Never raises (the
        save is best-effort, suppressed in :meth:`_flush`).
        """
        self._flush()

    def _flush(self) -> None:
        # Best-effort: a failing store must never surface into a generation or teardown.
        with self._lock:
            self._flush_pending = False
            data = {key: dict(row) for key, row in self._rows.items()}
        with contextlib.suppress(Exception):
            self._store.save(data)


class RecordingLLMProvider(LLMProvider):
    """Wraps an :class:`LLMProvider`, recording usage after each successful generation.

    ``name`` / ``requires_api`` proxy the wrapped provider so the wrapper is a transparent
    substitute. Recording is best-effort: a failing recorder never breaks generation.

    Text and image generation use DIFFERENT models on the same provider (e.g. Gemini Vertex's
    ``text_model`` vs ``image_model``), so each kind is recorded under its own model — otherwise
    an image call would be logged under the text model, polluting the Image usage table with a
    text-model row.
    """

    def __init__(
        self,
        wrapped: LLMProvider,
        recorder: UsageRecorder,
        *,
        model: str,
        image_model: str = "",
    ) -> None:
        self._wrapped = wrapped
        self._recorder = recorder
        self._model = model
        self._image_model = image_model

    @property
    def name(self) -> str:  # type: ignore[override]
        return self._wrapped.name

    @property
    def requires_api(self) -> bool:  # type: ignore[override]
        return self._wrapped.requires_api

    def generate_text(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> str:
        # Take the usage as the call's RETURN VALUE, not off the wrapped provider's shared
        # last_usage attribute: two overlapping generations on the same cached instance would
        # otherwise clobber that attribute and misattribute each other's token counts. We still
        # proxy it onto self.last_usage for external readers (that attribute alone stays racy for
        # them, but recording no longer depends on it).
        result, usage = self._wrapped.generate_text_with_usage(
            prompt, system=system, temperature=temperature, max_tokens=max_tokens
        )
        self.last_usage = usage
        self._record(
            kind="text",
            model=self._model,
            in_chars=len(prompt) + len(system or ""),
            out_chars=len(result),
            usage=usage,
        )
        return result

    def generate_image(self, prompt: str, *, size: str = "1024x1024") -> bytes:
        result, usage = self._wrapped.generate_image_with_usage(prompt, size=size)
        self.last_usage = usage
        # Record under the IMAGE model (falling back to the text model only when none is
        # configured) so an image call never shows up as a text-model row.
        self._record(
            kind="image",
            model=self._image_model or self._model,
            in_chars=len(prompt),
            out_chars=len(result),
            usage=usage,
        )
        return result

    def _record(
        self,
        *,
        kind: str,
        model: str,
        in_chars: int,
        out_chars: int,
        usage: Optional[dict] = None,
    ) -> None:
        # Best-effort: usage tracking must never break a generation. Swallow any recorder
        # failure (a corrupt/locked file, a disk error) rather than surface it to the user.
        counts = usage if isinstance(usage, dict) else {}
        with contextlib.suppress(Exception):
            self._recorder.record(
                kind=kind,
                provider=self._wrapped.name,
                model=model or "(default)",
                in_chars=in_chars,
                out_chars=out_chars,
                in_tokens=int(counts.get("in", 0)),
                out_tokens=int(counts.get("out", 0)),
            )


class RecordingTTSProvider(TTSProvider):
    """Wraps a :class:`TTSProvider`, recording usage after each synthesis.

    ``audio_ext`` / ``name`` / ``requires_api`` proxy the wrapped provider. The recorded
    ``model`` is the voice (or ``"(default)"``). Recording never raises into the call.
    """

    def __init__(self, wrapped: TTSProvider, recorder: UsageRecorder) -> None:
        self._wrapped = wrapped
        self._recorder = recorder

    @property
    def name(self) -> str:  # type: ignore[override]
        return self._wrapped.name

    @property
    def audio_ext(self) -> str:  # type: ignore[override]
        return self._wrapped.audio_ext

    @property
    def requires_api(self) -> bool:  # type: ignore[override]
        return self._wrapped.requires_api

    def synthesize(
        self, text: str, *, lang: Optional[str] = None, voice: Optional[str] = None
    ) -> bytes:
        audio = self._wrapped.synthesize(text, lang=lang, voice=voice)
        # Best-effort: recording must never break synthesis (see RecordingLLMProvider).
        with contextlib.suppress(Exception):
            self._recorder.record(
                kind="sound",
                provider=self._wrapped.name,
                model=voice or "(default)",
                in_chars=len(text),
                out_chars=len(audio),
            )
        return audio


_default: UsageRecorder = NullUsageRecorder()


def set_default_recorder(recorder: UsageRecorder) -> None:
    """Set the process-wide default usage recorder (called once at bootstrap)."""
    global _default
    _default = recorder


def default_recorder() -> UsageRecorder:
    """Return the process-wide default usage recorder."""
    return _default


def flush_default_recorder() -> None:
    """Flush the default recorder if it buffers (called at profile close before teardown)."""
    flush = getattr(_default, "flush_now", None)
    if callable(flush):
        flush()
