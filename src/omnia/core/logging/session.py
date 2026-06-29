"""Named, switchable run-capture session.

One switch enables/disables all capture for a flow. Open by name; the exact same code runs
whether or not it's enabled (every method no-ops when ``enable`` is False)::

    with get_logging_session("smart_notes", enable=capture_on, run_dir=…) as session:
        session.timing("generate", elapsed)
        session.io("generate", request=…, response=…)
        session.save_json("note.json", note)

``get_logging_session`` returns a :class:`LoggingSession` registered under ``name`` (created
on first use, reused by name), sets ``session.enable`` on entry, and flushes
``metrics.{json,md}`` + ``io/`` on exit. Feature code never references this — capture is
driven by whoever opens the session (e.g. a test); production stays clean.

Pure-Python and headless (no ``aqt``/``anki``); writes only to a caller-provided ``run_dir``.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import re
import shutil
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from .logger import get_logger

logger = get_logger(__name__)

# name → LoggingSession (created on first get_logging_session(name), reused by name).
_REGISTRY: dict[str, LoggingSession] = {}
_REGISTRY_LOCK = threading.Lock()


class LoggingSession:
    """A named run-capture sink. Every record/save no-ops while ``enable`` is False."""

    @staticmethod
    def _when_enabled(fn: Callable) -> Callable:
        """Make a method a no-op (returns None) while ``enable`` is False.

        Keeps the ``if not self.enable`` guard in ONE place so each method stays clean.

        Args:
            fn: The method to wrap.

        Returns:
            A wrapper that returns None when the session is disabled.
        """

        @functools.wraps(fn)
        def wrapper(self: LoggingSession, *args: Any, **kwargs: Any) -> Any:
            if not self.enable:
                return None
            return fn(self, *args, **kwargs)

        return wrapper

    def __init__(
        self, name: str, *, enable: bool = True, run_dir: Optional[str] = None
    ) -> None:
        """Initialise the session.

        Args:
            name: Registry key / report title.
            enable: When False, every record/save is a no-op.
            run_dir: Directory artifacts are written to; created eagerly when enabled.
        """
        self.name = name
        self.enable = bool(enable)
        self.run_dir = run_dir
        self._timings: list[tuple[str, float]] = []
        self._metrics: list[tuple[str, Any]] = []
        self._io: list[tuple[str, Any, Any]] = []
        self._lock = threading.Lock()
        if self.enable and self.run_dir:
            os.makedirs(self.run_dir, exist_ok=True)

    # ── recording (cheap; thread-safe; @_when_enabled → no-op when disabled) ──────
    @_when_enabled
    def timing(self, name: str, seconds: float) -> None:
        """Record a named timing in seconds."""
        with self._lock:
            self._timings.append((name, float(seconds)))

    @_when_enabled
    def metric(self, name: str, value: Any) -> None:
        """Record a named metric value and log it."""
        with self._lock:
            self._metrics.append((name, value))
        logger.info("[metric] %s = %s", name, value)

    @_when_enabled
    def io(self, stage: str, *, request: Any = None, response: Any = None) -> None:
        """Buffer a request/response pair for a stage (written to ``io/`` on flush)."""
        with self._lock:
            self._io.append((stage, request, response))

    # ── saving artifacts (write immediately; @_when_enabled → no-op when disabled) ─
    @_when_enabled
    def save_text(self, rel_path: str, text: str) -> Optional[str]:
        """Write ``text`` to ``run_dir/rel_path`` and return the absolute target path."""
        if not self.run_dir:
            return None
        target = os.path.join(self.run_dir, rel_path)
        os.makedirs(os.path.dirname(target) or self.run_dir, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(text)
        return target

    @_when_enabled
    def save_json(self, rel_path: str, obj: Any) -> Optional[str]:
        """Serialise ``obj`` as pretty JSON to ``run_dir/rel_path``."""
        return self.save_text(
            rel_path, json.dumps(obj, ensure_ascii=False, indent=2, default=str)
        )

    @_when_enabled
    def save_file(self, src_path: str, rel_path: str) -> Optional[str]:
        """Copy an existing file (audio, image, …) into the run dir."""
        if not self.run_dir or not src_path or not os.path.exists(src_path):
            return None
        target = os.path.join(self.run_dir, rel_path)
        os.makedirs(os.path.dirname(target) or self.run_dir, exist_ok=True)
        try:
            shutil.copy(src_path, target)
        except OSError as exc:  # best-effort
            logger.warning("[logsession] save_file failed %s: %s", src_path, exc)
            return None
        return target

    # Semantic alias — same as save_file, reads clearer at call sites.
    save_video = save_file

    # ── flush (write the report + buffered I/O) ───────────────────────────────────
    @_when_enabled
    def flush(self) -> None:
        """Write buffered I/O to ``io/`` and the run report (``metrics.{json,md}``)."""
        if not self.run_dir:
            return
        for idx, (stage, request, response) in enumerate(self._io, start=1):
            if request is not None:
                self.save_json(f"io/{idx:02d}_{_slug(stage)}.request.json", request)
            if response is not None:
                self.save_json(f"io/{idx:02d}_{_slug(stage)}.response.json", response)
        self._write_report()
        logger.info("[logsession] '%s' flushed → %s", self.name, self.run_dir)

    def _write_report(self) -> None:
        """Write ``metrics.json`` and ``metrics.md`` summarising timings and metrics."""
        total = sum(s for _, s in self._timings)
        self.save_json(
            "metrics.json",
            {
                "name": self.name,
                "total_seconds": round(total, 3),
                "timings": [
                    {"stage": n, "seconds": round(s, 3)} for n, s in self._timings
                ],
                "metrics": [{"name": n, "value": v} for n, v in self._metrics],
                "io_stages": [s for s, _, _ in self._io],
            },
        )
        lines = [
            f"# {self.name} — run report",
            "",
            f"**Total time: {total:.2f}s** over {len(self._timings)} timed stages",
            "",
            "| # | stage | seconds |",
            "|---|-------|---------|",
        ]
        for i, (name, secs) in enumerate(self._timings, start=1):
            lines.append(f"| {i} | {name} | {secs:.3f} |")
        if self._metrics:
            lines += ["", "## Metrics", "", "| name | value |", "|------|-------|"]
            for name, value in self._metrics:
                lines.append(f"| {name} | {value} |")
        self.save_text("metrics.md", "\n".join(lines) + "\n")

    # Tidy the descriptor (the methods above were decorated with the raw function);
    # subclasses reach it as ``LoggingSession._when_enabled`` (callable on all versions).
    _when_enabled = staticmethod(_when_enabled)


class AsyncLoggingSession(LoggingSession):
    """A :class:`LoggingSession` whose disk writes run on a background thread.

    A ``save_video`` of a big file (or a batch of ``save_json``/``io``) overlaps the next
    CPU-bound stage instead of blocking it.

    Only the **write** is offloaded — disk I/O releases the GIL, so it genuinely overlaps;
    serialization (``json.dumps``) stays on the caller's thread (CPU/GIL-bound anyway, and it
    snapshots the object before it can mutate). A single worker serializes writes FIFO, so
    order is preserved and there are no races. ``flush`` drains the queue (waits for all
    writes). Caveat: a ``save_file`` source must outlive the drain (its copy is deferred).
    """

    def __init__(
        self, name: str, *, enable: bool = True, run_dir: Optional[str] = None
    ) -> None:
        """Initialise the async session, spawning a single-worker pool when enabled."""
        super().__init__(name, enable=enable, run_dir=run_dir)
        self._pool: Optional[ThreadPoolExecutor] = (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"logsession-{name}")
            if self.enable
            else None
        )

    @LoggingSession._when_enabled
    def save_text(self, rel_path: str, text: str) -> Optional[str]:
        """Offload the text write to the worker; return the target path eagerly."""
        if not self.run_dir or self._pool is None:
            return None
        target = os.path.join(self.run_dir, rel_path)
        self._pool.submit(self._write_text, target, text)
        return target

    @LoggingSession._when_enabled
    def save_file(self, src_path: str, rel_path: str) -> Optional[str]:
        """Offload the file copy to the worker; return the target path eagerly."""
        if (
            not self.run_dir
            or self._pool is None
            or not src_path
            or not os.path.exists(src_path)
        ):
            return None
        target = os.path.join(self.run_dir, rel_path)
        self._pool.submit(self._copy, src_path, target)
        return target

    save_video = save_file

    @LoggingSession._when_enabled
    def flush(self) -> None:
        """Enqueue the report + I/O writes, then drain the worker queue."""
        super().flush()  # enqueues io/ + report writes via the overridden save_*
        if self._pool is not None:
            self._pool.shutdown(wait=True)  # drain: wait for every queued write
            self._pool = None
            logger.info("[logsession.async] '%s' I/O drained", self.name)

    @staticmethod
    def _write_text(target: str, text: str) -> None:
        """Write ``text`` to ``target`` on the worker thread (best-effort)."""
        try:
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError as exc:  # best-effort — capture must never break the flow
            logger.warning("[logsession.async] write failed %s: %s", target, exc)

    @staticmethod
    def _copy(src_path: str, target: str) -> None:
        """Copy ``src_path`` to ``target`` on the worker thread (best-effort)."""
        try:
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
            shutil.copy(src_path, target)
        except OSError as exc:
            logger.warning("[logsession.async] copy failed %s: %s", src_path, exc)


def get_logging_session(
    name: str,
    enable: bool = True,
    run_dir: Optional[str] = None,
    async_: bool = False,
) -> LoggingSession:
    """Get the session named ``name`` (created on first use, reused by name).

    Args:
        name: Registry key.
        enable: Whether capture is active; applied to the (re)used session.
        run_dir: Output directory; updated on the session when not None.
        async_: When True, create an :class:`AsyncLoggingSession` (writes offloaded to a
            background thread, drained on flush) on first use.

    Returns:
        The registered session. This is a plain getter — it does NOT flush or unregister;
        use :func:`logging_session` to own a session's lifecycle, or call ``flush()`` yourself.
    """
    with _REGISTRY_LOCK:
        session = _REGISTRY.get(name)
        if session is None:
            cls = AsyncLoggingSession if async_ else LoggingSession
            session = cls(name, enable=enable, run_dir=run_dir)
            _REGISTRY[name] = session
    session.enable = enable
    if run_dir is not None:
        session.run_dir = run_dir
    return session


@contextlib.contextmanager
def logging_session(
    name: str,
    enable: bool = True,
    run_dir: Optional[str] = None,
    async_: bool = False,
    flush_on_exit: bool = True,
) -> Any:
    """Own a session's lifecycle: yield :func:`get_logging_session`, then flush + unregister.

    On exit (when ``flush_on_exit``, the default) an enabled session is flushed and removed
    from the registry. Set ``flush_on_exit=False`` to keep it alive across several blocks and
    flush later.

    Args:
        name: Registry key.
        enable: Whether capture is active.
        run_dir: Output directory.
        async_: Create an :class:`AsyncLoggingSession` on first use.
        flush_on_exit: Flush + unregister the session when the block exits.

    Yields:
        The registered :class:`LoggingSession`.
    """
    session = get_logging_session(name, enable=enable, run_dir=run_dir, async_=async_)
    try:
        yield session
    finally:
        if flush_on_exit:
            if session.enable:
                session.flush()
            with _REGISTRY_LOCK:
                _REGISTRY.pop(name, None)


def _slug(text: str) -> str:
    """Slugify ``text`` for use in a filename (alnum runs → ``_``; fallback ``x``)."""
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower() or "x"
