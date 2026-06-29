"""On-disk cache of fetched TTS voices (the Refresh result).

The Smart Notes "Auto-detect voices" editor lets a user point each language at a concrete
``provider:voice``. Each provider's curated seed (its ``CURATED_VOICES``) covers the common
languages offline; a Refresh action fetches the FULL set from the fetch-capable providers (via
:func:`omnia.core.providers.tts.refresh_voices`) and caches it under ``user_files/`` so the
dropdowns can offer every voice across sessions. The cache is OPTIONAL: when the file is absent
(offline, never refreshed) callers fall back to the curated seed, so nothing requires the
network.

Pure module — no ``aqt``/``anki`` imports. It only persists/reads the aggregated
``{provider: [TTSVoice, ...]}`` map (serializing each :class:`TTSVoice` to a plain dict); the
fetch itself is the provider layer's concern, and ``user_files_dir`` is injected by the glue.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from omnia.core.providers.tts.base import TTSVoice

_CACHE_FILE = "voices.json"


def cache_path(user_files_dir: Path) -> Path:
    """Return the fetched-voice cache file path under ``user_files_dir``."""
    return user_files_dir / _CACHE_FILE


def load_cached_voices(user_files_dir: Path) -> dict[str, list[TTSVoice]]:
    """Return the cached ``{provider: [TTSVoice, ...]}`` map (``{}`` if absent or corrupt).

    Args:
        user_files_dir: The add-on's ``user_files`` directory.

    Returns:
        The cached voice map, or ``{}`` when the cache is missing/unreadable/malformed.
    """
    path = cache_path(user_files_dir)
    try:
        with path.open("r", encoding="utf-8") as handle:
            parsed = json.load(handle)
    except (OSError, ValueError):
        return {}
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


def save_cached_voices(user_files_dir: Path, voices: dict[str, list[TTSVoice]]) -> None:
    """Persist the aggregated ``{provider: [TTSVoice, ...]}`` map under user_files."""
    path = cache_path(user_files_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = {
        provider: [dataclasses.asdict(v) for v in entries]
        for provider, entries in voices.items()
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(serialized, handle)


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
