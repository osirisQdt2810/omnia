"""The TTS binding of the generic provider registry.

Holds the one :class:`~omnia.core.providers.registry.ProviderRegistry` instance for TTS and
the thin public functions over it: each provider registers itself with :func:`register_tts` at
import time, and :func:`create_tts_provider` / the ``available_*`` queries read the registry
instead of a hand-maintained builder table. The registration *mechanism* lives in
``core/providers/registry.py`` (shared with LLM); what stays here is what only TTS has —
:func:`tts_providers_with_ext`, which reads each class's ``audio_ext``. Pure module — imports
:class:`TTSProvider` from ``.base`` plus the generic registry, so concrete providers depend on
it without a cycle (``registry`` ← ``base``; providers ← ``registry``; ``__init__`` ← providers).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Optional

from omnia.core.providers.registry import ProviderRegistry
from omnia.core.providers.tts.base import TTSProvider

if TYPE_CHECKING:
    from omnia.core.network.http import HttpClient

# name -> provider class. One class may be bound under several names (the openai family
# shares OpenAICompatibleTTS), so this is NOT a 1:1 map. Reads like a read-only dict.
TTS_REGISTRY: ProviderRegistry[TTSProvider] = ProviderRegistry(
    "TTS", default="google_translate"
)


def register_tts(
    *names: str,
) -> Callable[[type[TTSProvider]], type[TTSProvider]]:
    """Register a :class:`TTSProvider` subclass under one or more config names.

    Args:
        *names: One or more unique, stable config keys for the provider.

    Returns:
        A class decorator that records the class under each name.

    Raises:
        ValueError: If ``names`` is empty, any name is empty, or a name is already bound to a
            DIFFERENT class. Re-registering the SAME class under a name is a no-op.
    """
    return TTS_REGISTRY.register(*names)


def tts_providers_with_ext(ext: str) -> list[str]:
    """Return the registered provider names whose audio is in ``ext`` format, sorted.

    The registry plus each class's :attr:`TTSProvider.audio_ext` is the ONE place that knows
    which provider returns what, so a caller that has to tell a user "this needs a WAV voice"
    derives the list instead of hard-coding it — a hard-coded list goes stale the moment a
    provider is added, renamed, or changes format, and it then LIES in the UI.

    Args:
        ext: The container/extension to match (``"wav"``, ``"mp3"``), compared case-insensitively.

    Returns:
        The matching config names, sorted (one class bound under several names contributes each
        of them, since a name is what the user actually configures).
    """
    wanted = ext.strip().lower()
    return sorted(
        name for name, cls in TTS_REGISTRY.items() if cls.audio_ext.lower() == wanted
    )


def create_tts_provider(
    config: dict[str, Any], http: Optional[HttpClient] = None
) -> TTSProvider:
    """Instantiate the TTS provider named by ``config['provider']`` (default free gTTS).

    Args:
        config: Provider config; ``config['provider']`` selects the class.
        http: Optional HTTP client injected into the built provider.

    Returns:
        The configured :class:`TTSProvider`.

    Raises:
        ProviderError: If the provider name is unknown.
    """
    return TTS_REGISTRY.create(config, http)


def available_tts_providers() -> list[str]:
    """Return the registered TTS provider names (for the settings GUI)."""
    return TTS_REGISTRY.names()


def available_tts_providers_requiring_api() -> list[str]:
    """TTS providers that need an API key / cloud credentials (skippable in real tests)."""
    return TTS_REGISTRY.requiring_api()


def available_keyless_tts_providers() -> list[str]:
    """TTS providers callable WITHOUT a key (google_translate, edge_tts, piper — must run)."""
    return TTS_REGISTRY.keyless()
