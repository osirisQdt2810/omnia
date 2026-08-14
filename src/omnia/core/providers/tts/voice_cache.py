"""Cache of fetched TTS voices (the Refresh result), behind a swappable backend.

The Smart Notes "Auto-detect voices" editor lets a user point each language at a concrete
``provider:voice``. Each provider's curated seed (its ``CURATED_VOICES``) covers the common
languages offline; a Refresh action fetches the FULL set from the fetch-capable providers (via
:func:`omnia.core.providers.tts.refresh_voices`) and caches it so the dropdowns can offer every
voice across sessions. The cache is OPTIONAL: when it is absent (offline, never refreshed)
callers fall back to the curated seed, so nothing requires the network.

Pure module — no ``aqt``/``anki`` imports at top level. A :class:`VoiceCache` persists/reads the
aggregated ``{provider: [TTSVoice, ...]}`` map (serializing each :class:`TTSVoice` to a plain
dict). :class:`JsonVoiceCache` stores it under ``user_files/``; :class:`CollectionVoiceCache`
stores it in the synced collection config (``col.set_config`` under ``omnia:voices``) — a
harmless re-fetchable cache that rides along with a device sync.
"""

from __future__ import annotations

import dataclasses
import json
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

from omnia.core.providers.tts.base import TTSVoice

_CACHE_FILE = "voices.json"


class VoiceCache(ABC):
    """Persists the aggregated ``{provider: [TTSVoice, ...]}`` fetched-voice map."""

    @abstractmethod
    def load(self) -> dict[str, list[TTSVoice]]:
        """Return the cached voice map (``{}`` when absent/unreadable/malformed)."""

    @abstractmethod
    def save(self, voices: dict[str, list[TTSVoice]]) -> bool:
        """Persist the aggregated voice map.

        Returns:
            ``True`` if actually persisted; ``False`` when the store silently skipped the write
            (the collection backend with no ``col`` loaded). The dispatcher uses this to decide
            whether a backend-switch copy succeeded.
        """


class JsonVoiceCache(VoiceCache):
    """A :class:`VoiceCache` backed by ``voices.json`` under the add-on's ``user_files``."""

    def __init__(self, user_files_dir: Path) -> None:
        self._dir = user_files_dir

    def load(self) -> dict[str, list[TTSVoice]]:
        path = self._dir / _CACHE_FILE
        try:
            with path.open("r", encoding="utf-8") as handle:
                parsed = json.load(handle)
        except (OSError, ValueError):
            return {}
        return _voices_from_raw(parsed)

    def save(self, voices: dict[str, list[TTSVoice]]) -> bool:
        path = self._dir / _CACHE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(_voices_to_raw(voices), handle)
        return True


class CollectionVoiceCache(VoiceCache):
    """A :class:`VoiceCache` in the synced collection config (``col.set_config``).

    The map is stored under ``omnia:voices`` as plain JSON-able dicts, so it syncs across
    devices (a harmless re-fetchable cache). ``col`` is resolved LAZILY (``mw.col`` isn't ready
    at add-on init); an optional ``col_provider`` lets tests inject a fake collection. Without a
    collection it degrades to ``{}`` on load and a no-op on save (headless-safe).
    """

    KEY = "omnia:voices"

    def __init__(self, col_provider: Optional[Callable[[], Any]] = None) -> None:
        self._col_provider = col_provider

    def load(self) -> dict[str, list[TTSVoice]]:
        col = self._col()
        raw = col.get_config(self.KEY, None) if col is not None else None
        return _voices_from_raw(raw or {})

    def save(self, voices: dict[str, list[TTSVoice]]) -> bool:
        col = self._col()
        if col is None:
            return False
        col.set_config(self.KEY, _voices_to_raw(voices))
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


def _voices_to_raw(voices: dict[str, list[TTSVoice]]) -> dict[str, list[dict]]:
    """Serialize the ``{provider: [TTSVoice, ...]}`` map to plain JSON-able dicts."""
    return {
        provider: [dataclasses.asdict(v) for v in entries]
        for provider, entries in voices.items()
    }


def _voices_from_raw(parsed: object) -> dict[str, list[TTSVoice]]:
    """Rebuild the ``{provider: [TTSVoice, ...]}`` map from a parsed cache (tolerant)."""
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, list[TTSVoice]] = {}
    for provider, entries in parsed.items():
        if not isinstance(entries, list):
            continue
        out[provider] = [
            _voice_from_dict(entry) for entry in entries if isinstance(entry, dict)
        ]
    return out


def _voice_from_dict(entry: dict[str, object]) -> TTSVoice:
    """Rebuild a :class:`TTSVoice` from a cached dict (tolerant of missing keys)."""
    return TTSVoice(
        provider=str(entry.get("provider", "")),
        voice=str(entry.get("voice", "")),
        language=str(entry.get("language", "")),
        name=str(entry.get("name", "")),
        gender=str(entry.get("gender", "")),
        model=str(entry.get("model", "")),
        lang_code=str(entry.get("lang_code", "")),
    )
