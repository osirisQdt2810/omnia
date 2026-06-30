"""VietTTS — fully open-source, self-hosted Vietnamese TTS (``dangvansam/viet-tts``).

Unlike google_translate/edge_tts (whose engines are proprietary cloud services), viet-tts is
an OPEN model the user runs **locally** — so it is offline and private once started. It is a
heavy PyTorch model, so it runs as its own process (isolated from Anki) exposing an
OpenAI-compatible ``/v1/audio/speech`` API; this provider is therefore just
:class:`OpenAICompatibleTTS` pointed at the local server asking for WAV. Nothing to vendor —
the transport is the stdlib HTTP client, like every other HTTP provider.

Per ADR-005 the add-on **manages** viet-tts in a per-provider sidecar venv (it is too heavy
to vendor — PyTorch). Enabling it is an explicit, opt-in toggle in Smart Notes → Options →
Advanced; once installed, ``synthesize`` (with ``autostart``) asks the
:class:`NativeRuntimeManager` to start/reuse the local server and points the request at it.
If the runtime is not installed, the manager raises a clear, actionable error rather than
shelling out. With ``autostart`` off, point ``base_url`` at a server you started yourself.

``requires_api`` is False: the local server needs no cloud key (the OpenAI-compatible client
just wants a non-empty string, so a placeholder is sent).
"""

from __future__ import annotations

from typing import Any, ClassVar, Optional

from omnia.core.network.http import HttpClient
from omnia.core.providers.errors import ProviderError
from omnia.core.providers.native_runtime import (
    NativeRuntimeManager,
    NativeRuntimeSpec,
    default_manager,
    register_native_runtime,
)
from omnia.core.providers.tts.base import TTSVoice
from omnia.core.providers.tts.openai_compatible import OpenAICompatibleTTS
from omnia.core.providers.tts.registry import register_tts

_DEFAULT_BASE_URL = "http://localhost:8298/v1"
_DEFAULT_MODEL = "tts-1"
_DEFAULT_VOICE = "nu-nhe-nhang"  # a gentle female viet-tts voice

# The managed-venv spec (ADR-005): a persistent localhost server launched via the venv's own
# ``viettts`` console script, so the native PyTorch ABI matches the venv interpreter.
SPEC: NativeRuntimeSpec = register_native_runtime(
    NativeRuntimeSpec(
        name="viettts",
        section="tts",
        label="VietTTS (Vietnamese, local)",
        # Not on PyPI — installed from GitHub (needs `git` + a Python 3.10+ host). The VCS
        # install pulls PyTorch, hence the ~2 GB size hint.
        pip_packages=("git+https://github.com/dangvansam/viet-tts.git",),
        mode="server",
        size_hint="~2 GB",
        server_argv=(
            "{bin}/viettts",
            "server",
            "--host",
            "{host}",
            "--port",
            "{port}",
        ),
        port=8298,
    )
)


@register_tts("viettts")
class VietTTS(OpenAICompatibleTTS):
    """Open-source Vietnamese TTS via a managed-venv viet-tts server (WAV)."""

    name = "viettts"
    audio_ext = "wav"
    requires_api = False  # local open-source server, no cloud key
    # viet-tts ships many speaker voices that depend on the user's local model pack; this is a
    # sensible default seed (others can be set in config / typed in the picker). Overrides the
    # OpenAI seed inherited from the base so the picker shows Vietnamese speakers, not Alloy/etc.
    CURATED_VOICES: ClassVar[list[TTSVoice]] = [
        TTSVoice(
            "viettts", "nu-nhe-nhang", "Vietnamese", "Nu Nhe Nhang", "Female", "", "vi"
        ),
        TTSVoice("viettts", "cdteam", "Vietnamese", "CDTeam", "Male", "", "vi"),
    ]

    def __init__(
        self,
        base_url: str = _DEFAULT_BASE_URL,
        model: str = _DEFAULT_MODEL,
        voice: str = "",
        api_key: str = "",
        autostart: bool = True,
        http: Optional[HttpClient] = None,
        manager: Optional[NativeRuntimeManager] = None,
    ) -> None:
        # The local server needs no real key, but the OpenAI-compatible client requires a
        # non-empty string; allow an override for an auth-protected deployment.
        super().__init__(
            api_key=api_key or "viettts",
            base_url=base_url or _DEFAULT_BASE_URL,
            model=model or _DEFAULT_MODEL,
            voice=voice or _DEFAULT_VOICE,
            http=http,
            response_format="wav",
        )
        self._autostart = autostart
        # Inject for tests; default to the process-wide manager (lazy — no Anki import here).
        self._manager = manager

    @classmethod
    def from_config(
        cls, config: dict[str, Any], http: Optional[HttpClient] = None
    ) -> VietTTS:
        return cls(
            base_url=config.get("base_url", ""),
            model=config.get("model", ""),
            voice=config.get("voice", ""),
            api_key=config.get("api_key", ""),
            autostart=bool(config.get("autostart", True)),
            http=http,
        )

    def synthesize(
        self, text: str, *, lang: Optional[str] = None, voice: Optional[str] = None
    ) -> bytes:
        if self._autostart:
            # Start (and reuse) the managed-venv viet-tts server on demand and point this call
            # at it. Raises a clear "enable in Advanced" error if the runtime isn't installed.
            manager = self._manager or default_manager()
            host, port = manager.ensure_running(SPEC)
            self._base_url = f"http://{host}:{port}/v1"
        try:
            return super().synthesize(text, lang=lang, voice=voice)
        except ProviderError as exc:
            raise ProviderError(
                f"{exc}. Is the viet-tts server reachable at {self._base_url}?"
            ) from exc
