"""Env-driven persistence dispatch with sync-on-change (ADR-006).

Each persistence concern (config, usage, voice cache) has two independent backends behind one
ABC — a file backend and a collection/DB backend. :class:`PersistenceDispatcher` picks the
active backend per concern from the ``envs`` knobs (``OMNIA_CONFIG_STORAGE`` /
``OMNIA_USAGE_STORAGE`` / ``OMNIA_VOICE_CACHE_STORAGE``, default ``"database"``) and, when a
knob changed since the last startup, copies ALL of that concern's data from the previously-used
backend into the newly-selected one so switching never loses state.

The last-used value per concern is remembered in a small, device-local marker file
(``user_files/.storage.json``) — ``col`` config is *synced*, so it cannot hold a per-device
dispatch marker, whereas ``user_files/`` is local and preserved across add-on updates. On
startup the dispatcher compares the marker to the current ``envs`` value per concern:

* missing marker → first run, no sync (backends start fresh per ADR-006);
* equal → no-op;
* changed → build the old backend too, copy old→new, then update the marker.

The compare/build/(maybe)sync/record flow is a single private helper (:meth:`_dispatch`); each
resolver supplies its own explicit build + sync closures (the per-concern copy semantics differ:
config copies only ``omnia``/``features`` — never ``providers.toml``, which is the same file in
both config backends — while usage/voices are a same-shape ``new.save(old.load())``).

Pure module — no ``aqt``/``anki`` imports at top level (the collection/DB backends resolve
``mw`` lazily themselves), so this unit-tests headless.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from omnia import envs
from omnia.core.config.loader import BaseConfigLoader, build_config_loader
from omnia.core.providers.usage import (
    BufferedUsageRecorder,
    ColUsageStore,
    JsonUsageRecorder,
    JsonUsageStore,
    UsageRecorder,
    UsageStore,
)
from omnia.core.providers.voice_cache import (
    CollectionVoiceCache,
    JsonVoiceCache,
    VoiceCache,
)

_MARKER_FILE = ".storage.json"

T = TypeVar("T")


@dataclass(frozen=True)
class _ConcernSpec:
    """The env knob, allowed values, and default for one persistence concern."""

    env_key: str
    allowed: tuple[str, ...]
    default: str


# One spec per concern; the marker records the last-used value keyed by these concern names.
_CONCERNS: dict[str, _ConcernSpec] = {
    "config": _ConcernSpec("OMNIA_CONFIG_STORAGE", ("database", "toml"), "database"),
    "usage": _ConcernSpec("OMNIA_USAGE_STORAGE", ("database", "json"), "database"),
    "voices": _ConcernSpec(
        "OMNIA_VOICE_CACHE_STORAGE", ("database", "json"), "database"
    ),
}


class PersistenceDispatcher:
    """Resolves each concern's active backend from ``envs``, syncing data on a changed knob."""

    def __init__(self, user_files_dir: Path) -> None:
        """Initialise the dispatcher.

        Args:
            user_files_dir: The add-on's ``user_files`` directory — holds the device-local
                ``.storage.json`` marker and the file backends' data (``usage.json``,
                ``voices.json``).
        """
        self._user_files_dir = user_files_dir
        self._marker = self._load_marker()

    def config_loader(
        self, config_dir: Path, template_dir: Path | None = None
    ) -> BaseConfigLoader:
        """Return the active config loader, syncing ``omnia``/``features`` on a changed knob.

        ``config_dir`` is the LIVE dir; ``template_dir`` (default ``config_dir``) holds the
        ``*.example.toml`` templates the loader seeds missing live files from. ``providers.toml``
        is the same on-disk file in both config backends, so the sync copies only the ``omnia``
        and ``features`` domains (never ``providers``).
        """

        def build(value: str) -> BaseConfigLoader:
            return build_config_loader(
                config_dir, backend=value, template_dir=template_dir
            )

        def sync(old: BaseConfigLoader, new: BaseConfigLoader) -> None:
            new.ensure_live_files()
            for name in ("omnia.toml", "features.toml"):
                data = old.read_file(name)
                # Skip empty domains: the old backend having no data for a domain means there is
                # nothing to carry, and writing {} would clobber the template defaults the fresh
                # `ensure_live_files` just seeded (only matters for the toml backend).
                if data:
                    new.write_file(name, data)

        return self._dispatch("config", build=build, sync=sync)

    def usage_recorder(self) -> UsageRecorder:
        """Return the active usage recorder, syncing the usage aggregate on a changed knob.

        The store is synced (``new.save(old.load())``), then wrapped in its recorder: the file
        store in a synchronous :class:`JsonUsageRecorder`, the ``col.db`` store in a
        main-thread-flushing :class:`BufferedUsageRecorder`.
        """

        def build(value: str) -> UsageStore:
            if value == "json":
                return JsonUsageStore(self._user_files_dir / "usage.json")
            return ColUsageStore()

        def sync(old: UsageStore, new: UsageStore) -> None:
            new.save(old.load())

        store = self._dispatch("usage", build=build, sync=sync)
        # _dispatch just recorded the resolved value in the marker — reuse it (don't re-read the
        # env, which would log the invalid-value warning a second time).
        if self._marker["usage"] == "json":
            return JsonUsageRecorder(store)
        return BufferedUsageRecorder(store)

    def voice_cache(self) -> VoiceCache:
        """Return the active voice cache, syncing the cached voice map on a changed knob."""

        def build(value: str) -> VoiceCache:
            if value == "json":
                return JsonVoiceCache(self._user_files_dir)
            return CollectionVoiceCache()

        def sync(old: VoiceCache, new: VoiceCache) -> None:
            new.save(old.load())

        return self._dispatch("voices", build=build, sync=sync)

    def _dispatch(
        self,
        concern: str,
        *,
        build: Callable[[str], T],
        sync: Callable[[T, T], None],
    ) -> T:
        """Build the active backend for ``concern``, copying old→new when the knob changed.

        Reads the current (validated) env value, builds its backend, and — only when the marker
        holds a *different, valid* last-used value — builds the old backend and runs ``sync``
        before recording the current value in the marker. Returns the active backend.
        """
        current = self._value(concern)
        new = build(current)
        last = self._marker.get(concern)
        if (
            isinstance(last, str)
            and last in _CONCERNS[concern].allowed
            and last != current
        ):
            sync(build(last), new)
        self._record(concern, current)
        return new

    def _value(self, concern: str) -> str:
        """Return the current env value for ``concern`` (its default on an invalid value)."""
        spec = _CONCERNS[concern]
        value: str = getattr(envs, spec.env_key)  # envs.__getattr__ is typed Any
        if value in spec.allowed:
            return value
        from omnia.core.logging import get_logger

        get_logger("persistence").warning(
            "invalid %s=%r; falling back to %r", spec.env_key, value, spec.default
        )
        return spec.default

    def _record(self, concern: str, value: str) -> None:
        """Record ``value`` as the last-used backend for ``concern`` (persist only on change)."""
        if self._marker.get(concern) == value:
            return
        self._marker[concern] = value
        self._save_marker()

    def _load_marker(self) -> dict[str, str]:
        """Load the marker (``{}`` when the file is missing, unreadable, or not a JSON object)."""
        path = self._user_files_dir / _MARKER_FILE
        try:
            with path.open("r", encoding="utf-8") as handle:
                parsed = json.load(handle)
        except (OSError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _save_marker(self) -> None:
        """Persist the marker atomically (temp + ``os.replace``); a failure must not crash boot."""
        path = self._user_files_dir / _MARKER_FILE
        try:
            self._user_files_dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(self._marker, handle)
            os.replace(tmp, path)
        except OSError:
            from omnia.core.logging import get_logger

            get_logger("persistence").warning(
                "could not persist storage marker %s", path, exc_info=True
            )
