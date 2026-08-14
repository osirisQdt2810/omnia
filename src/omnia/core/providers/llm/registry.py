"""The LLM binding of the generic provider registry.

Holds the one :class:`~omnia.core.providers.registry.ProviderRegistry` instance for LLM and the
thin public functions over it: each provider registers itself with :func:`register_llm` at
import time (so ``llm/__init__.py`` must import every provider module for the decorators to
run), and :func:`create_llm_provider` / the ``available_*`` queries read the registry.

Deliberately asymmetric with the TTS binding: that one also exposes ``registered_tts_providers``
next to ``available_tts_providers`` because both names are already imported elsewhere. There is
no ``registered_llm_providers`` twin here — nothing would call it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Optional

from omnia.core.providers.llm.base import LLMProvider
from omnia.core.providers.registry import ProviderRegistry

if TYPE_CHECKING:
    from omnia.core.network.http import HttpClient

# name -> provider class. One class may be bound under several names (the openai family shares
# OpenAICompatibleProvider), so this is NOT a 1:1 map. Reads like a read-only dict.
LLM_REGISTRY: ProviderRegistry[LLMProvider] = ProviderRegistry(
    "LLM", default="openai_compatible"
)


def register_llm(*names: str) -> Callable[[type[LLMProvider]], type[LLMProvider]]:
    """Register an :class:`LLMProvider` subclass under one or more config names.

    Args:
        *names: One or more unique, stable config keys for the provider.

    Returns:
        A class decorator that records the class under each name.

    Raises:
        ValueError: If ``names`` is empty, any name is empty, or a name is already bound to a
            DIFFERENT class. Re-registering the SAME class under a name is a no-op.
    """
    return LLM_REGISTRY.register(*names)


def get_llm(name: str) -> type[LLMProvider] | None:
    """Return the provider class registered under ``name`` (or None if unknown)."""
    return LLM_REGISTRY.get(name)


def create_llm_provider(
    config: dict[str, Any], http: Optional[HttpClient] = None
) -> LLMProvider:
    """Instantiate the LLM provider named by ``config['provider']``.

    Args:
        config: Provider config (``provider`` selects the implementation).
        http: Optional HTTP client to inject (defaults to the provider's own default).

    Returns:
        The configured :class:`LLMProvider`.

    Raises:
        ProviderError: If the provider name is unknown.
    """
    return LLM_REGISTRY.create(config, http)


def available_llm_providers() -> list[str]:
    """Return the registered LLM provider names (for the settings GUI)."""
    return LLM_REGISTRY.names()


def available_llm_providers_requiring_api() -> list[str]:
    """LLM providers that need an API key / credentials to call (skippable in real tests)."""
    return LLM_REGISTRY.requiring_api()


def available_keyless_llm_providers() -> list[str]:
    """LLM providers callable WITHOUT a key (free / offline / open-source — must always run)."""
    return LLM_REGISTRY.keyless()
