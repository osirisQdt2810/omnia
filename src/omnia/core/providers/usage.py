"""Self-tracked provider usage: a thread-safe JSON recorder + recording provider wrappers.

Pure module — no Anki imports. The add-on tracks LLM/TTS usage itself (calls + rough
in/out character counts per provider+model) so the Account dialog can show what is being
used without depending on any provider's billing API.

A :class:`UsageRecorder` is the small write surface; :class:`JsonUsageRecorder` persists to
a JSON file under a lock (it is called from background generation threads, so file I/O +
a lock is the safe choice — never the collection). The :class:`RecordingLLMProvider` /
:class:`RecordingTTSProvider` decorate a real provider so every generation records a row;
recording is best-effort and never raises into the call. A process-wide default recorder is
set once at bootstrap (a :class:`NullUsageRecorder` until then).
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Optional

from omnia.core.providers.llm.base import LLMProvider
from omnia.core.providers.tts.base import TTSProvider


class UsageRecorder(ABC):
    """Records one provider call (kind + provider + model + rough char counts)."""

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


class JsonUsageRecorder(UsageRecorder):
    """Thread-safe recorder aggregating usage rows into a JSON file.

    Each call is folded into a row keyed ``f"{kind}|{provider}|{model}"`` holding
    ``{kind, provider, model, calls, in_chars, out_chars, last_used_ts}``. :meth:`record`
    reads-modifies-writes under a lock so concurrent background generation threads can't
    corrupt the file or lose an increment. ``time_fn`` is injected for testability.
    """

    def __init__(self, path: Path, *, time_fn: Callable[[], float] = time.time) -> None:
        self._path = path
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
        key = f"{kind}|{provider}|{model}"
        with self._lock:
            data = self._load()
            row = data.get(
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
            row["last_used_ts"] = self._time_fn()
            data[key] = row
            self._dump(data)

    def snapshot(self) -> list[dict]:
        """Return the recorded rows (``[]`` if the file is absent or corrupt)."""
        with self._lock:
            return list(self._load().values())

    def _load(self) -> dict[str, dict]:
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                parsed = json.load(handle)
        except (OSError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _dump(self, data: dict[str, dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle)


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
        result = self._wrapped.generate_text(
            prompt, system=system, temperature=temperature, max_tokens=max_tokens
        )
        # Proxy the wrapped provider's exact-token usage so external readers see it too.
        self.last_usage = getattr(self._wrapped, "last_usage", None)
        self._record(
            kind="text",
            model=self._model,
            in_chars=len(prompt) + len(system or ""),
            out_chars=len(result),
        )
        return result

    def generate_image(self, prompt: str, *, size: str = "1024x1024") -> bytes:
        result = self._wrapped.generate_image(prompt, size=size)
        self.last_usage = getattr(self._wrapped, "last_usage", None)
        # Record under the IMAGE model (falling back to the text model only when none is
        # configured) so an image call never shows up as a text-model row.
        self._record(
            kind="image",
            model=self._image_model or self._model,
            in_chars=len(prompt),
            out_chars=len(result),
        )
        return result

    def _record(self, *, kind: str, model: str, in_chars: int, out_chars: int) -> None:
        # Best-effort: usage tracking must never break a generation. Swallow any recorder
        # failure (a corrupt/locked file, a disk error) rather than surface it to the user.
        usage = self.last_usage if isinstance(self.last_usage, dict) else {}
        with contextlib.suppress(Exception):
            self._recorder.record(
                kind=kind,
                provider=self._wrapped.name,
                model=model or "(default)",
                in_chars=in_chars,
                out_chars=out_chars,
                in_tokens=int(usage.get("in", 0)),
                out_tokens=int(usage.get("out", 0)),
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
