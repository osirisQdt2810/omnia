"""TTS provider self-registration registry.

Mirrors the add-on's feature-plugin registry (:mod:`omnia.core.registry`): each provider
registers itself with the :func:`register_tts` decorator at import time, and the public
factory functions (:func:`create_tts_provider`, the ``available_*`` queries) read
:data:`TTS_REGISTRY` instead of a hand-maintained builder table. Pure module — imports only
:class:`TTSProvider` from ``.base`` plus stdlib, so concrete providers depend on it without a
cycle (``registry`` ← ``base``; providers ← ``registry``; ``__init__`` ← providers).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Optional

from omnia.core.providers.errors import ProviderError
from omnia.core.providers.tts.base import TTSProvider

if TYPE_CHECKING:
    from omnia.core.network.http import HttpClient

# name -> provider class. One class may be bound under several names (the openai family
# shares OpenAICompatibleTTS), so this is NOT a 1:1 map.
TTS_REGISTRY: dict[str, type[TTSProvider]] = {}


def register_tts(
    *names: str,
) -> Callable[[type[TTSProvider]], type[TTSProvider]]:
    """Register a :class:`TTSProvider` subclass under one or more config names.

    Accepts multiple names so a single class can serve several config keys (the openai family
    binds ``"openai"``/``"openrouter"``/``"openai_compatible"`` to one class). Does NOT stamp a
    ``name`` attribute onto the class — a multi-name class keeps its own declared ``name`` (e.g.
    ``OpenAICompatibleTTS.name`` stays ``"openai_compatible"``).

    Args:
        *names: One or more unique, stable config keys for the provider.

    Returns:
        A class decorator that records the class under each name.

    Raises:
        ValueError: If ``names`` is empty, any name is empty, or a name is already bound to a
            DIFFERENT class. Re-registering the SAME class under a name is a no-op.
    """
    if not names:
        raise ValueError("register_tts requires at least one name")
    if any(not name for name in names):
        raise ValueError("TTS provider name must be a non-empty string")

    def decorator(cls: type[TTSProvider]) -> type[TTSProvider]:
        for name in names:
            existing = TTS_REGISTRY.get(name)
            if existing is not None and existing is not cls:
                raise ValueError(
                    f"TTS provider name {name!r} already registered to "
                    f"{existing.__name__}"
                )
            TTS_REGISTRY[name] = cls
        return cls

    return decorator


def get_tts(name: str) -> type[TTSProvider] | None:
    """Return the provider class registered under ``name`` (or None if unknown)."""
    return TTS_REGISTRY.get(name)


def registered_tts_providers() -> list[str]:
    """Return the registered TTS provider names, sorted."""
    return sorted(TTS_REGISTRY)


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
    provider = config.get("provider", "google_translate")
    cls = get_tts(provider)
    if cls is None:
        raise ProviderError(
            f"Unknown TTS provider {provider!r}; known: {registered_tts_providers()}"
        )
    return cls.from_config(config, http)


def available_tts_providers() -> list[str]:
    """Return the registered TTS provider names (for the settings GUI)."""
    return registered_tts_providers()


def available_tts_providers_requiring_api() -> list[str]:
    """TTS providers that need an API key / cloud credentials (skippable in real tests)."""
    return sorted(n for n, c in TTS_REGISTRY.items() if c.requires_api)


def available_keyless_tts_providers() -> list[str]:
    """TTS providers callable WITHOUT a key (google_translate, edge_tts, piper — must run)."""
    return sorted(n for n, c in TTS_REGISTRY.items() if not c.requires_api)
