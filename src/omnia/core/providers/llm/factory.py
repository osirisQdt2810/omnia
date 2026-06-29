"""Build an :class:`LLMProvider` from a config dict.

The ``provider`` key selects the implementation; the rest of the dict is provider-specific.
An optional :class:`HttpClient` is injected into the built provider (DIP). Adding a provider
= one entry in :data:`_BUILDERS`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Optional

from omnia.core.network.http import HttpClient
from omnia.core.providers.errors import ProviderError
from omnia.core.providers.llm.base import LLMProvider
from omnia.core.providers.llm.gemini import GeminiProvider
from omnia.core.providers.llm.gemini_vertex import GeminiVertexProvider
from omnia.core.providers.llm.openai_compatible import OpenAICompatibleProvider

# Sensible default base URLs for the OpenAI-compatible family.
_OPENAI_DEFAULTS = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "openai_compatible": "https://api.openai.com/v1",
}


def _build_openai(config: dict[str, Any], http: Optional[HttpClient]) -> LLMProvider:
    provider = config.get("provider", "openai_compatible")
    base_url = config.get("base_url") or _OPENAI_DEFAULTS.get(
        provider, _OPENAI_DEFAULTS["openai"]
    )
    return OpenAICompatibleProvider(
        api_key=config.get("api_key", ""),
        base_url=base_url,
        model=config.get("model", "gpt-4o-mini"),
        image_model=config.get("image_model"),
        temperature=float(config.get("temperature", 0.7)),
        http=http,
    )


def _build_gemini(config: dict[str, Any], http: Optional[HttpClient]) -> LLMProvider:
    return GeminiProvider(
        api_key=config.get("api_key", ""),
        model=config.get("model", "gemini-2.0-flash"),
        image_model=config.get("image_model", ""),
        temperature=float(config.get("temperature", 0.7)),
        http=http,
    )


def _build_gemini_vertex(
    config: dict[str, Any], http: Optional[HttpClient]
) -> LLMProvider:
    # The project is optional in config: the service-account JSON already carries `project_id`,
    # so fall back to it when no explicit project is set (an explicit one still wins).
    from omnia.core.providers.token_source import service_account_project

    return GeminiVertexProvider(
        project=config.get("project", "") or service_account_project(config),
        location=config.get("location", "global"),
        model=config.get("model", "gemini-2.5-flash"),
        image_model=config.get("image_model", ""),
        temperature=float(config.get("temperature", 0.7)),
        auth={
            "access_token": config.get("access_token"),
            "credentials_path": config.get("credentials_path"),
            "credentials_json": config.get("credentials_json"),
        },
        http=http,
    )


_BUILDERS: dict[str, Callable[[dict[str, Any], Optional[HttpClient]], LLMProvider]] = {
    "openai": _build_openai,
    "openrouter": _build_openai,
    "openai_compatible": _build_openai,
    "gemini": _build_gemini,
    "gemini_vertex": _build_gemini_vertex,
}

# name -> provider class, so callers can read each provider's `requires_api` WITHOUT building
# it (no creds needed). Kept in sync with _BUILDERS by test_provider_metadata.
_PROVIDER_CLASSES: dict[str, type[LLMProvider]] = {
    "openai": OpenAICompatibleProvider,
    "openrouter": OpenAICompatibleProvider,
    "openai_compatible": OpenAICompatibleProvider,
    "gemini": GeminiProvider,
    "gemini_vertex": GeminiVertexProvider,
}


def create_llm_provider(
    config: dict[str, Any], http: Optional[HttpClient] = None
) -> LLMProvider:
    """Instantiate the LLM provider named by ``config['provider']``.

    Args:
        config: Provider config (``provider`` selects the implementation).
        http: Optional HTTP client to inject (defaults to the provider's own default).

    Raises:
        ProviderError: If the provider name is unknown.
    """
    provider = config.get("provider", "openai_compatible")
    builder = _BUILDERS.get(provider)
    if builder is None:
        raise ProviderError(
            f"Unknown LLM provider {provider!r}; known: {sorted(_BUILDERS)}"
        )
    return builder(config, http)


def available_llm_providers() -> list[str]:
    """Return the registered LLM provider names (for the settings GUI)."""
    return sorted(_BUILDERS)


def available_llm_providers_requiring_api() -> list[str]:
    """LLM providers that need an API key / credentials to call (skippable in real tests)."""
    return sorted(n for n, c in _PROVIDER_CLASSES.items() if c.requires_api)


def available_keyless_llm_providers() -> list[str]:
    """LLM providers callable WITHOUT a key (free / offline / open-source — must always run)."""
    return sorted(n for n, c in _PROVIDER_CLASSES.items() if not c.requires_api)
